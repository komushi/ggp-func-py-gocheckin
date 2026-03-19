# face_recognition_hailo.py
#
# Drop-in Hailo-8 accelerated replacement for face_recognition.py
# Uses SCRFD (detection) + ArcFace (recognition) HEF models via HailoRT.
#
# The HailoFaceApp class exposes the same .get(img) interface as InsightFace
# FaceAnalysis, returning face objects with .embedding and .bbox attributes.
#
# Reference: Seeed-Solution/face-recognition-api (MIT License)
#   - SCRFD postprocessing (anchor decode, NMS)
#   - ArcFace alignment (5-point SimilarityTransform)
#   - Embedding dequantization and L2 normalization
#
# UC8: Human Body Detection with YOLOv8n
#   - Gate check: YOLOv8n on 10 frames before SCRFD+ArcFace session
#   - Continuous: YOLOv8n on every frame during session
#   - Extend: Dual-signal (motion + person) at timer expiry

import logging
import time
from datetime import datetime, timezone, timedelta
import sys
import os
import threading
import traceback
from collections import deque
import numpy as np
import cv2

import gstreamer_threading as gst

from skimage.transform import SimilarityTransform

if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HailoRT imports
# ---------------------------------------------------------------------------
try:
    from hailo_platform import (
        VDevice,
        HailoSchedulingAlgorithm,
        FormatType,
    )
    HAILO_AVAILABLE = True
except ImportError:
    HAILO_AVAILABLE = False
    logger.warning("hailo_platform not available — HailoFaceApp will not work")


# ---------------------------------------------------------------------------
# UC Toggle Cache — updated from py_handler.py per camera
# ---------------------------------------------------------------------------
# Structure: { cam_ip: {'uc8_enabled': bool, 'uc1_enabled': bool, ...} }
_uc_toggle_cache = {}


def set_uc_toggle(cam_ip, uc_toggles):
    """Set UC toggle configuration for a camera.

    Called from py_handler.py when detection session starts.

    Args:
        cam_ip: Camera IP address
        uc_toggles: Dict with toggle states from get_uc_toggles()
    """
    _uc_toggle_cache[cam_ip] = uc_toggles
    logger.debug(f"UC toggle set for {cam_ip}: {uc_toggles}")


def clear_uc_toggle(cam_ip):
    """Clear UC toggle configuration for a camera (called at session end)."""
    if cam_ip in _uc_toggle_cache:
        del _uc_toggle_cache[cam_ip]
        logger.debug(f"UC toggle cleared for {cam_ip}")


def is_uc8_enabled(cam_ip):
    """Check if UC8 is enabled for a camera.

    Args:
        cam_ip: Camera IP address

    Returns:
        True if UC8 continuous person detection should run
    """
    # Check environment variable override for testing (bypasses toggle system)
    if os.environ.get('UC8_ALWAYS_ENABLED', '').lower() == 'true':
        return True

    toggles = _uc_toggle_cache.get(cam_ip, {})
    # P1: uc8_standalone_enabled, P2: uc4_uc8_enabled
    return toggles.get('uc8_standalone_enabled', True) or toggles.get('uc4_uc8_enabled', True)


# ---------------------------------------------------------------------------
# HailoYoloApp — YOLOv8n person detection (UC8)
# ---------------------------------------------------------------------------
class HailoYoloApp:
    """
    YOLOv8n person detection using Hailo-8.

    Detects person class (COCO class 0) and returns bounding boxes.
    Used for UC8: Gate check, continuous person detection, and extend check.
    """

    PERSON_CLASS_ID = 0

    def __init__(self, vdevice, hef_path, score_threshold=0.5):
        """
        Initialize YOLOv8n person detector.

        Args:
            vdevice: Shared Hailo VDevice
            hef_path: Path to YOLOv8n HEF model
            score_threshold: Minimum confidence for person detection
        """
        self.hef_path = hef_path
        self.score_threshold = score_threshold
        self.num_classes = 80

        self.infer_model = vdevice.create_infer_model(hef_path)
        for output_info in self.infer_model.hef.get_output_vstream_infos():
            self.infer_model.output(output_info.name).set_format_type(FormatType.FLOAT32)
        self.configured = self.infer_model.configure()

        inp = self.infer_model.input()
        self.input_h = int(inp.shape[0])
        self.input_w = int(inp.shape[1])

        output_infos = self.infer_model.hef.get_output_vstream_infos()
        self.output_names = [info.name for info in output_infos]

        logger.info(f"YOLOv8n initialized: {self.input_h}x{self.input_w}, "
                    f"outputs={len(self.output_names)}")

    def detect_persons(self, img, threshold=None):
        """
        Detect persons in BGR image.

        Args:
            img: BGR numpy array (H, W, 3) uint8
            threshold: Optional override for score_threshold

        Returns:
            List of person detections with bbox, confidence
        """
        thresh = threshold if threshold is not None else self.score_threshold
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if not rgb.flags['C_CONTIGUOUS']:
            rgb = np.ascontiguousarray(rgb)
        preprocessed, scale, pad_left, pad_top = self._preprocess(rgb)
        outputs = self._run_inference(preprocessed)
        detections = self._decode_hailo_nms(outputs, scale, pad_left, pad_top, img.shape)
        return [d for d in detections if d['class_id'] == self.PERSON_CLASS_ID
                and d['confidence'] >= thresh]

    def count_persons(self, img, threshold=None):
        """
        Count persons in image (for UC8 continuous detection).

        Args:
            img: BGR numpy array
            threshold: Optional confidence threshold

        Returns:
            Integer count of persons detected
        """
        persons = self.detect_persons(img, threshold)
        return len(persons)

    def _preprocess(self, image):
        """Resize with aspect ratio, pad to model input size (letterbox)."""
        h, w = image.shape[:2]
        scale = min(self.input_w / w, self.input_h / h)
        new_w, new_h = int(w * scale), int(h * scale)

        resized = cv2.resize(image, (new_w, new_h))
        padded = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
        top = (self.input_h - new_h) // 2
        left = (self.input_w - new_w) // 2
        padded[top:top + new_h, left:left + new_w] = resized

        return padded, scale, left, top

    def _run_inference(self, preprocessed):
        """Run YOLOv8n inference on Hailo-8."""
        output_buffers = {
            info.name: np.empty(info.shape, dtype=np.float32)
            for info in self.infer_model.outputs
        }
        bindings = self.configured.create_bindings(output_buffers=output_buffers)
        bindings.input().set_buffer(preprocessed)
        job = self.configured.run_async([bindings], lambda *args, **kwargs: None)
        job.wait(10000)
        return output_buffers

    def _decode_hailo_nms(self, outputs, scale, pad_left, pad_top, orig_shape):
        """
        Decode YOLOv8n NMS output format.

        YOLOv8n with on-chip NMS outputs:
        [num_dets, [y1, x1, y2, x2, score] * num_dets] per class
        """
        output_name = self.output_names[0]
        raw = outputs[output_name].flatten().astype(np.float32)
        detections = []
        offset = 0

        for class_id in range(self.num_classes):
            if offset >= len(raw):
                break
            num_dets = int(raw[offset])
            offset += 1

            for _ in range(num_dets):
                if offset + 5 > len(raw):
                    break
                y_min, x_min, y_max, x_max, score = raw[offset:offset + 5]
                offset += 5

                if score < self.score_threshold:
                    continue

                # Map back to original image coordinates
                x1 = (x_min * self.input_w - pad_left) / scale
                y1 = (y_min * self.input_h - pad_top) / scale
                x2 = (x_max * self.input_w - pad_left) / scale
                y2 = (y_max * self.input_h - pad_top) / scale

                h_orig, w_orig = orig_shape[:2]
                x1 = max(0, min(x1, w_orig - 1))
                y1 = max(0, min(y1, h_orig - 1))
                x2 = max(0, min(x2, w_orig - 1))
                y2 = max(0, min(y2, h_orig - 1))

                detections.append({
                    'class_id': class_id,
                    'confidence': float(score),
                    'bbox': [float(x1), float(y1), float(x2), float(y2)],
                })

        return detections


# ---------------------------------------------------------------------------
# Lightweight face result object (matches InsightFace Face interface)
# ---------------------------------------------------------------------------
class HailoFace:
    """Minimal face object compatible with InsightFace Face attributes."""
    def __init__(self, bbox: np.ndarray, embedding: np.ndarray,
                 kps: np.ndarray = None, det_score: float = 0.0, pre_norm: float = 0.0):
        self.bbox = bbox            # np.ndarray shape (4,) — x1,y1,x2,y2
        self.embedding = embedding  # np.ndarray shape (512,) — L2-normalized
        self.kps = kps              # np.ndarray shape (5,2) or None
        self.det_score = det_score
        self.pre_norm = pre_norm    # Embedding magnitude before L2 norm (proxy for face quality/distance)


# ---------------------------------------------------------------------------
# HailoUC8App — Combined UC8 (YOLOv8n) + UC1/3/4/5 (SCRFD+ArcFace)
# ---------------------------------------------------------------------------
class HailoUC8App:
    """
    Combined application for all use cases using shared Hailo VDevice.

    Manages:
    - YOLOv8n: Person detection (UC8 gate, continuous, extend)
    - SCRFD: Face detection (UC1/3/4/5)
    - ArcFace: Face recognition (UC1/3/4/5)

    Provides:
    - gate_check(): Run YOLOv8n on N frames, return True if person detected
    - get(): Detect faces and extract embeddings (same as HailoFaceApp)
    - count_persons(): Count persons in frame (UC8 continuous)
    - Session state: max_simultaneous_persons tracking
    """

    def __init__(self,
                 yolo_hef_path: str = None,
                 det_hef_path: str = None,
                 rec_hef_path: str = None,
                 yolo_threshold: float = 0.5,
                 face_threshold: float = 0.5,
                 nms_threshold: float = 0.4):
        """
        Initialize combined UC8 + face recognition app.

        Args:
            yolo_hef_path: Path to YOLOv8n HEF
            det_hef_path: Path to SCRFD HEF
            rec_hef_path: Path to ArcFace HEF
            yolo_threshold: YOLOv8n confidence threshold
            face_threshold: SCRFD confidence threshold
            nms_threshold: NMS IoU threshold
        """
        if not HAILO_AVAILABLE:
            raise RuntimeError("hailo_platform not installed")

        # Default HEF paths
        default_model_dir = '/etc/hailo/models' if sys.platform == 'linux' else os.path.join(os.path.dirname(__file__), 'models')
        self.yolo_hef_path = yolo_hef_path or os.environ.get('HAILO_YOLO_HEF') or os.path.join(default_model_dir, 'yolov8n.hef')
        self.det_hef_path = det_hef_path or os.environ.get('HAILO_DET_HEF') or os.path.join(default_model_dir, 'scrfd_2.5g.hef')
        self.rec_hef_path = rec_hef_path or os.environ.get('HAILO_REC_HEF') or os.path.join(default_model_dir, 'arcface_r50.hef')

        logger.info(f"Loading Hailo models: yolo={self.yolo_hef_path}, det={self.det_hef_path}, rec={self.rec_hef_path}")
        self._init_device()

        # Initialize YOLOv8n
        self.yolo_app = HailoYoloApp(self.vdevice, self.yolo_hef_path, score_threshold=yolo_threshold)

        # Initialize SCRFD + ArcFace (sharing VDevice)
        self.face_app = HailoFaceApp(
            det_hef_path=det_hef_path,
            rec_hef_path=rec_hef_path,
            score_threshold=face_threshold,
            nms_threshold=nms_threshold,
            vdevice=self.vdevice
        )

        # Session state for UC8
        self.session_state = {}  # cam_ip -> session state

        logger.info(f"HailoUC8App initialized: yolo={os.path.basename(self.yolo_hef_path)}, "
                    f"det={os.path.basename(self.det_hef_path)}, rec={os.path.basename(self.rec_hef_path)}")

    def _init_device(self):
        """Create shared Hailo VDevice."""
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        logger.info("Created shared VDevice for UC8 + face recognition")

    def _get_session_state(self, cam_ip):
        """Get or create session state for a camera."""
        if cam_ip not in self.session_state:
            self.session_state[cam_ip] = {
                'max_simultaneous_persons': 0,
                'person_count_history': deque(maxlen=100),  # Last 100 frames
                'frame_count': 0,
            }
        return self.session_state[cam_ip]

    def gate_check(self, frames, min_detections=3, gate_frames=10):
        """
        UC8 Role 1: Gate check - run YOLOv8n on N frames to verify person presence.

        Args:
            frames: List of BGR numpy arrays (frames to check)
            min_detections: Minimum number of frames with person detected to pass
            gate_frames: Number of frames to check (default: 10)

        Returns:
            True if person detected in >= min_detections frames, False otherwise
        """
        # Limit to gate_frames
        frames_to_check = frames[:gate_frames]
        person_detections = 0

        for frame in frames_to_check:
            persons = self.yolo_app.detect_persons(frame)
            if len(persons) > 0:
                person_detections += 1

        passed = person_detections >= min_detections
        logger.info(f"UC8 Gate check: {person_detections}/{len(frames_to_check)} frames with person, "
                    f"min_detections={min_detections} → {'PASSED' if passed else 'FAILED'}")
        return passed

    def count_persons(self, img, cam_ip=None):
        """
        UC8 Role 2: Count persons in frame and update session state.

        Args:
            img: BGR numpy array
            cam_ip: Camera IP for session state tracking

        Returns:
            Tuple of (person_count, max_simultaneous_persons)
        """
        persons = self.yolo_app.detect_persons(img)
        person_count = len(persons)

        # Update session state
        if cam_ip:
            state = self._get_session_state(cam_ip)
            state['frame_count'] += 1
            state['person_count_history'].append(person_count)
            if person_count > state['max_simultaneous_persons']:
                state['max_simultaneous_persons'] = person_count

        return person_count, state['max_simultaneous_persons'] if cam_ip else person_count

    def get(self, img, cam_ip=None, max_num=0, det_size=(640, 640)):
        """
        UC1/3/4/5: Detect faces and extract embeddings.

        Also updates UC8 session state with person count.

        Args:
            img: BGR numpy array
            cam_ip: Camera IP for session state tracking
            max_num: Maximum faces to return (0 = all)
            det_size: Detection input size (ignored, uses HEF model size)

        Returns:
            Tuple of (faces, person_count, max_simultaneous_persons)
        """
        # Count persons first (UC8 Role 2)
        person_count, max_simultaneous = self.count_persons(img, cam_ip)

        # Detect faces (UC1/3/4/5)
        faces = self.face_app.get(img, max_num=max_num, det_size=det_size)

        return faces, person_count, max_simultaneous

    def get_extend_check(self, cam_ip, min_detections=3, lookback_frames=10):
        """
        UC8 Role 3: Extend check - query person detection history.

        Args:
            cam_ip: Camera IP
            min_detections: Minimum frames with person to pass extend
            lookback_frames: Number of recent frames to check

        Returns:
            True if person detected in >= min_detections of last lookback_frames
        """
        if cam_ip not in self.session_state:
            return False

        state = self.session_state[cam_ip]
        history = list(state['person_count_history'])[-lookback_frames:]

        if len(history) < lookback_frames:
            # Not enough history yet
            return True  # Allow extend during early session

        detections_with_person = sum(1 for count in history if count > 0)
        passed = detections_with_person >= min_detections

        logger.debug(f"UC8 Extend check for {cam_ip}: {detections_with_person}/{len(history)} "
                     f"frames with person → {'PASSED' if passed else 'FAILED'}")
        return passed

    def get_session_stats(self, cam_ip):
        """Get session statistics for a camera."""
        if cam_ip not in self.session_state:
            return {'max_simultaneous_persons': 0, 'frame_count': 0}

        state = self.session_state[cam_ip]
        return {
            'max_simultaneous_persons': state['max_simultaneous_persons'],
            'frame_count': state['frame_count'],
            'avg_person_count': np.mean(state['person_count_history']) if state['person_count_history'] else 0,
        }

    def reset_session(self, cam_ip):
        """Reset session state for a camera (called at session end)."""
        if cam_ip in self.session_state:
            state = self.session_state[cam_ip]
            logger.info(f"UC8 Session end for {cam_ip}: max_simultaneous={state['max_simultaneous_persons']}, "
                        f"frames={state['frame_count']}, avg_persons={np.mean(state['person_count_history']) if state['person_count_history'] else 0:.2f}")
            del self.session_state[cam_ip]

    def cleanup(self):
        """Clean up VDevice."""
        if hasattr(self, 'vdevice'):
            del self.vdevice


# ---------------------------------------------------------------------------
# HailoFaceApp — SCRFD + ArcFace on Hailo-8
# ---------------------------------------------------------------------------
class HailoFaceApp:
    """
    Face analysis using Hailo-8 accelerator.

    Compatible with InsightFace FaceAnalysis.get() interface:
        faces = app.get(img)
        faces = app.get(img, max_num=0, det_size=(640, 640))

    Each returned face has .bbox (ndarray) and .embedding (ndarray 512-dim).
    """

    # ArcFace canonical destination landmarks (112x112 coordinate space)
    DEST_LANDMARKS = np.array([
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ], dtype=np.float32)

    def __init__(self,
                 det_hef_path: str = None,
                 rec_hef_path: str = None,
                 score_threshold: float = 0.5,
                 nms_threshold: float = 0.4,
                 vdevice=None):
        """
        Args:
            det_hef_path: Path to SCRFD HEF (default: env HAILO_DET_HEF or models/scrfd_10g.hef)
            rec_hef_path: Path to ArcFace HEF (default: env HAILO_REC_HEF or models/arcface_mobilefacenet.hef)
            score_threshold: Minimum detection confidence
            nms_threshold: NMS IoU threshold
            vdevice: Optional shared VDevice (if None, creates new one)
        """
        if not HAILO_AVAILABLE:
            raise RuntimeError("hailo_platform not installed")

        # Default HEF paths - use /etc/hailo/models/ on Linux, local ./models/ otherwise
        # Available detection models: scrfd_10g.hef, scrfd_2.5g.hef
        # Available recognition models: arcface_r50.hef, arcface_mobilefacenet.hef
        default_model_dir = '/etc/hailo/models' if sys.platform == 'linux' else os.path.join(os.path.dirname(__file__), 'models')
        self.det_hef_path = det_hef_path or os.environ.get(
            'HAILO_DET_HEF') or os.path.join(default_model_dir, 'scrfd_2.5g.hef')
        self.rec_hef_path = rec_hef_path or os.environ.get(
            'HAILO_REC_HEF') or os.path.join(default_model_dir, 'arcface_r50.hef')

        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.strides = [8, 16, 32]
        self.num_anchors = 2

        logger.info(f"Loading Hailo models: det={self.det_hef_path}, rec={self.rec_hef_path}")
        self._init_device(vdevice)

    def _init_device(self, vdevice=None):
        """Initialize Hailo VDevice and load both models.

        Args:
            vdevice: Optional shared VDevice. If None, creates a new one.
        """
        if vdevice is not None:
            # Use shared VDevice
            self.vdevice = vdevice
            logger.info("Using shared VDevice")
        else:
            # Create new VDevice
            params = VDevice.create_params()
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            self.vdevice = VDevice(params)
            logger.info("Created new VDevice")

        # Detection model — FLOAT32 output so HailoRT auto-dequantizes (scrfd HEF uses UINT8 input)
        self.det_infer_model = self.vdevice.create_infer_model(self.det_hef_path)
        for output_info in self.det_infer_model.hef.get_output_vstream_infos():
            self.det_infer_model.output(output_info.name).set_format_type(FormatType.FLOAT32)
        self.det_configured = self.det_infer_model.configure()

        # Recognition model — UINT8 input (matches HEF compiled type), FLOAT32 output (auto-dequantize)
        self.rec_infer_model = self.vdevice.create_infer_model(self.rec_hef_path)
        self.rec_infer_model.input().set_format_type(FormatType.UINT8)
        self.rec_infer_model.output().set_format_type(FormatType.FLOAT32)
        self.rec_configured = self.rec_infer_model.configure()

        # Cache detection input shape
        det_input = self.det_infer_model.input()
        self.det_input_shape = (int(det_input.shape[0]), int(det_input.shape[1]))  # (H, W)
        logger.info(f"Hailo detection model input: {self.det_input_shape}")

        # Cache recognition input shape
        rec_input = self.rec_infer_model.input()
        self.rec_input_shape = (int(rec_input.shape[0]), int(rec_input.shape[1]), int(rec_input.shape[2]))  # (H, W, C)
        logger.info(f"Hailo recognition model input: {self.rec_input_shape}")

        # Extract quantization parameters for detection outputs
        det_vstream_infos = self.det_infer_model.hef.get_output_vstream_infos()
        self.det_quant_infos = {
            info.name: (info.quant_info.qp_scale, info.quant_info.qp_zp)
            for info in det_vstream_infos
        }

        # Extract quantization parameters for recognition outputs
        rec_vstream_infos = self.rec_infer_model.hef.get_output_vstream_infos()
        self.rec_quant_infos = {
            info.name: (info.quant_info.qp_scale, info.quant_info.qp_zp)
            for info in rec_vstream_infos
        }

        # Build output name → stride mapping for SCRFD
        self._build_output_layer_map()

        # Pre-generate anchors
        self.anchors = self._generate_anchors(self.det_input_shape, self.strides, self.num_anchors)

        logger.info(f"HailoFaceApp initialized: det={os.path.basename(self.det_hef_path)}, "
                     f"rec={os.path.basename(self.rec_hef_path)}")

    def _build_output_layer_map(self):
        """Map SCRFD output layer names to (stride, type) based on naming convention.

        SCRFD-10G layers:
          stride 8:  conv41 (score), conv42 (bbox), conv43 (kps)
          stride 16: conv49 (score), conv50 (bbox), conv51 (kps)
          stride 32: conv56 (score), conv57 (bbox), conv58 (kps)

        For other SCRFD variants (2.5g, 500m), the conv indices differ.
        We detect the model variant from the layer names and build the map dynamically.
        """
        output_names = [info.name for info in self.det_infer_model.hef.get_output_vstream_infos()]
        logger.info(f"SCRFD output layers: {output_names}")

        # Try to detect known patterns
        self.stride_outputs = {}  # stride -> {'score': name, 'bbox': name, 'kps': name}

        # Pattern: scrfd_10g/convNN or scrfd_2.5g/convNN or scrfd_500m/convNN
        # Group outputs by stride based on output tensor shapes
        # Scores: (H, W, num_anchors*1), Bbox: (H, W, num_anchors*4), Kps: (H, W, num_anchors*10)
        output_infos = self.det_infer_model.hef.get_output_vstream_infos()

        # Group by feature map size (which determines stride)
        by_fmap = {}
        for info in output_infos:
            shape = info.shape
            # Shape is typically (H, W, C) for Hailo outputs
            h, w = int(shape[0]), int(shape[1])
            c = int(shape[2]) if len(shape) > 2 else int(shape[-1])
            fmap_key = (h, w)
            if fmap_key not in by_fmap:
                by_fmap[fmap_key] = []
            by_fmap[fmap_key].append((info.name, c))

        # Map feature map sizes to strides
        model_h, model_w = self.det_input_shape
        for (fh, fw), layers in sorted(by_fmap.items(), key=lambda x: -x[0][0]):
            # Larger feature map = smaller stride
            stride_h = model_h // fh
            stride_w = model_w // fw
            stride = stride_h  # should equal stride_w for square input

            if stride not in [8, 16, 32]:
                logger.warning(f"Unexpected stride {stride} for fmap ({fh},{fw}), skipping")
                continue

            self.stride_outputs[stride] = {}
            for name, channels in layers:
                if channels == self.num_anchors:         # scores
                    self.stride_outputs[stride]['score'] = name
                elif channels == self.num_anchors * 4:   # bbox
                    self.stride_outputs[stride]['bbox'] = name
                elif channels == self.num_anchors * 10:  # landmarks
                    self.stride_outputs[stride]['kps'] = name
                else:
                    logger.warning(f"Unknown output {name} with {channels} channels at stride {stride}")

        for stride in self.strides:
            if stride in self.stride_outputs:
                logger.info(f"  stride {stride}: {self.stride_outputs[stride]}")
            else:
                logger.warning(f"  stride {stride}: NOT FOUND in outputs")

    # ------------------------------------------------------------------
    # Anchor generation
    # ------------------------------------------------------------------
    def _generate_anchors(self, model_input_shape, strides, num_anchors):
        """Generate anchor centers for all strides."""
        all_anchors = {}
        for stride in strides:
            fmap_h = model_input_shape[0] // stride
            fmap_w = model_input_shape[1] // stride

            x_centers = (np.arange(fmap_w) + 0.5) * stride
            y_centers = (np.arange(fmap_h) + 0.5) * stride

            xv, yv = np.meshgrid(x_centers, y_centers)
            anchor_centers = np.stack([xv, yv], axis=-1).reshape(-1, 2)

            total = anchor_centers.shape[0] * num_anchors
            repeated = np.repeat(anchor_centers, num_anchors, axis=0)
            stride_col = np.full((total, 1), stride)
            all_anchors[stride] = np.concatenate([repeated, stride_col], axis=1)

        return all_anchors

    # ------------------------------------------------------------------
    # Public interface: .get(img, max_num=0, det_size=(640,640))
    # ------------------------------------------------------------------
    def get(self, img, max_num=0, det_size=(640, 640)):
        """
        Detect faces and extract embeddings.

        Args:
            img: BGR numpy array (H, W, 3) uint8 — OpenCV format, converted to RGB internally
            max_num: Maximum faces to return (0 = all)
            det_size: Detection input size (ignored, uses HEF model size)

        Returns:
            List of HailoFace objects with .bbox, .embedding, .kps, .det_score
        """
        # --- Detection ---
        t0 = time.time()

        # Convert BGR to RGB — Hailo HEF models (SCRFD, ArcFace) expect RGB input
        # This matches InsightFace behavior which also converts BGR→RGB internally
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        # Ensure input is contiguous (Hailo may require this)
        if not img.flags['C_CONTIGUOUS']:
            img = np.ascontiguousarray(img)
            logger.debug("HailoFaceApp.get: made input array contiguous")
        preprocessed, scale, (pad_left, pad_top) = self._preprocess_detection(img)
        t1 = time.time()
        det_results = self._run_detection(preprocessed)
        t2 = time.time()
        boxes, scores, landmarks = self._postprocess_detection(det_results, scale, pad_left, pad_top, img.shape)
        t3 = time.time()

        logger.debug(f"HailoFaceApp.get timing: preprocess={1000*(t1-t0):.1f}ms, inference={1000*(t2-t1):.1f}ms, postprocess={1000*(t3-t2):.1f}ms, boxes={len(boxes)}")

        if len(boxes) == 0:
            return []

        # Sort by score descending
        order = scores.argsort()[::-1]
        boxes = boxes[order]
        scores = scores[order]
        landmarks = landmarks[order]

        if max_num > 0:
            boxes = boxes[:max_num]
            scores = scores[:max_num]
            landmarks = landmarks[:max_num]

        # --- Recognition ---
        faces = []
        for i in range(len(boxes)):
            kps = landmarks[i].reshape(5, 2) if landmarks[i] is not None else None
            embedding, pre_norm = self._extract_embedding(img, kps)
            faces.append(HailoFace(
                bbox=boxes[i],
                embedding=embedding,
                kps=kps,
                det_score=float(scores[i]),
                pre_norm=pre_norm,
            ))

        return faces

    # ------------------------------------------------------------------
    # Detection: preprocess → infer → postprocess
    # ------------------------------------------------------------------
    def _preprocess_detection(self, image):
        """Resize with aspect ratio, pad to model input size."""
        model_h, model_w = self.det_input_shape
        h, w = image.shape[:2]
        scale = min(model_w / w, model_h / h)
        new_w, new_h = int(w * scale), int(h * scale)

        resized = cv2.resize(image, (new_w, new_h))
        padded = np.zeros((model_h, model_w, 3), dtype=np.uint8)
        top = (model_h - new_h) // 2
        left = (model_w - new_w) // 2
        padded[top:top + new_h, left:left + new_w] = resized

        return padded, scale, (left, top)

    def _run_detection(self, preprocessed):
        """Run SCRFD inference on Hailo-8."""
        try:
            output_buffers = {
                info.name: np.empty(info.shape, dtype=np.float32)
                for info in self.det_infer_model.outputs
            }
            bindings = self.det_configured.create_bindings(output_buffers=output_buffers)
            bindings.input().set_buffer(preprocessed)
            job = self.det_configured.run_async([bindings], lambda *args, **kwargs: None)
            status = job.wait(10000)
            logger.debug(f"Hailo detection job completed with status: {status}")
            return output_buffers
        except Exception as e:
            logger.error(f"Hailo _run_detection error: {e}")
            # Return empty buffers on error
            return {
                info.name: np.zeros(info.shape, dtype=np.float32)
                for info in self.det_infer_model.outputs
            }

    def _postprocess_detection(self, outputs, scale, pad_left, pad_top, orig_shape):
        """Decode SCRFD outputs into boxes, scores, landmarks in original image coords."""
        all_boxes = []
        all_scores = []
        all_kps = []

        for stride in self.strides:
            if stride not in self.stride_outputs:
                continue

            layer_names = self.stride_outputs[stride]
            current_anchors = self.anchors[stride]

            # Scores — already dequantized by HailoRT (FormatType.FLOAT32)
            score_name = layer_names['score']
            raw_scores = outputs[score_name].flatten().astype(np.float32)

            # Filter by threshold
            mask = raw_scores > self.score_threshold
            if not np.any(mask):
                continue

            filtered_scores = raw_scores[mask]
            filtered_anchors = current_anchors[mask]

            # Bbox — already dequantized by HailoRT
            bbox_name = layer_names['bbox']
            raw_bbox = outputs[bbox_name].reshape(-1, 4).astype(np.float32)
            filtered_bbox = raw_bbox[mask]

            # Decode boxes
            anchor_cx = filtered_anchors[:, 0]
            anchor_cy = filtered_anchors[:, 1]
            s = float(stride)

            x1 = anchor_cx - filtered_bbox[:, 0] * s
            y1 = anchor_cy - filtered_bbox[:, 1] * s
            x2 = anchor_cx + filtered_bbox[:, 2] * s
            y2 = anchor_cy + filtered_bbox[:, 3] * s
            decoded_boxes = np.stack([x1, y1, x2, y2], axis=-1)

            # Landmarks — already dequantized by HailoRT
            kps_name = layer_names.get('kps')
            if kps_name is not None:
                raw_kps = outputs[kps_name].reshape(-1, 10).astype(np.float32)
                filtered_kps = raw_kps[mask]

                decoded_kps = np.zeros_like(filtered_kps)
                for k in range(5):
                    decoded_kps[:, k * 2] = anchor_cx + filtered_kps[:, k * 2] * s
                    decoded_kps[:, k * 2 + 1] = anchor_cy + filtered_kps[:, k * 2 + 1] * s
            else:
                decoded_kps = np.zeros((len(filtered_scores), 10))

            all_boxes.append(decoded_boxes)
            all_scores.append(filtered_scores)
            all_kps.append(decoded_kps)

        if len(all_boxes) == 0:
            return np.empty((0, 4)), np.empty(0), np.empty((0, 10))

        all_boxes = np.concatenate(all_boxes, axis=0)
        all_scores = np.concatenate(all_scores, axis=0)
        all_kps = np.concatenate(all_kps, axis=0)

        # NMS
        keep = self._nms(all_boxes, all_scores, self.nms_threshold)
        boxes = all_boxes[keep]
        scores = all_scores[keep]
        kps = all_kps[keep]

        # Map coordinates back to original image space
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_left) / scale
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_top) / scale
        kps[:, 0::2] = (kps[:, 0::2] - pad_left) / scale
        kps[:, 1::2] = (kps[:, 1::2] - pad_top) / scale

        # Clamp to image bounds
        h, w = orig_shape[:2]
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, h)

        return boxes, scores, kps

    def _nms(self, boxes, scores, iou_threshold):
        """Non-maximum suppression."""
        if boxes.shape[0] == 0:
            return []

        idxs = scores.argsort()[::-1]
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        area = (x2 - x1) * (y2 - y1)
        keep = []

        while idxs.size > 0:
            i = idxs[0]
            keep.append(i)
            if idxs.size == 1:
                break

            xx1 = np.maximum(x1[i], x1[idxs[1:]])
            yy1 = np.maximum(y1[i], y1[idxs[1:]])
            xx2 = np.minimum(x2[i], x2[idxs[1:]])
            yy2 = np.minimum(y2[i], y2[idxs[1:]])
            w = np.maximum(0.0, xx2 - xx1)
            h = np.maximum(0.0, yy2 - yy1)
            intersection = w * h
            union = area[i] + area[idxs[1:]] - intersection
            iou = intersection / union
            remaining = np.where(iou <= iou_threshold)[0]
            idxs = idxs[remaining + 1]

        return keep

    # ------------------------------------------------------------------
    # Recognition: align → preprocess → infer → dequantize → normalize
    # ------------------------------------------------------------------
    def _extract_embedding(self, image, kps):
        """Align face and extract 512-dim L2-normalized embedding."""
        if kps is not None:
            logger.debug(f"Landmarks for alignment: {kps.tolist()}")

        # Align face using 5-point landmarks
        aligned = self._align_face(image, kps)

        # Preprocess for recognition model
        preprocessed = self._preprocess_recognition(aligned)

        # Run ArcFace inference — output is auto-dequantized FLOAT32 by HailoRT
        output_buffers = {
            info.name: np.empty(info.shape, dtype=np.float32)
            for info in self.rec_infer_model.outputs
        }
        bindings = self.rec_configured.create_bindings(output_buffers=output_buffers)
        bindings.input().set_buffer(np.ascontiguousarray(preprocessed.astype(np.uint8)))
        job = self.rec_configured.run_async([bindings], lambda *args, **kwargs: None)
        job.wait(10000)

        # Output is already dequantized by HailoRT (FormatType.FLOAT32)
        output_name = list(output_buffers.keys())[0]
        raw = output_buffers[output_name]

        logger.debug(f"ArcFace output: {output_name}, shape={raw.shape}, dtype={raw.dtype}, mean={raw.mean():.4f}, std={raw.std():.4f}")

        embedding = raw.astype(np.float32).flatten()

        # Pad or truncate to 512
        if len(embedding) != 512:
            logger.warning(f"ArcFace embedding size {len(embedding)} != 512, adjusting")
            if len(embedding) > 512:
                embedding = embedding[:512]
            else:
                embedding = np.concatenate([embedding, np.zeros(512 - len(embedding))])

        # L2 normalize
        pre_norm = np.linalg.norm(embedding)
        if pre_norm > 0:
            embedding = embedding / pre_norm

        logger.debug(f"Live embedding: pre_norm={pre_norm:.4f}, mean={embedding.mean():.4f}, std={embedding.std():.4f}")

        return embedding.astype(np.float32), pre_norm

    def _align_face(self, image, kps):
        """Align face using 5-point landmarks with SimilarityTransform."""
        if kps is None or kps.shape != (5, 2):
            # Fallback: just crop center
            h, w = image.shape[:2]
            size = min(h, w)
            y0 = (h - size) // 2
            x0 = (w - size) // 2
            crop = image[y0:y0+size, x0:x0+size]
            return cv2.resize(crop, (self.rec_input_shape[1], self.rec_input_shape[0]))

        tform = SimilarityTransform()
        tform.estimate(kps, self.DEST_LANDMARKS)
        M = tform.params[0:2, :]

        model_h, model_w = self.rec_input_shape[0], self.rec_input_shape[1]
        aligned = cv2.warpAffine(image, M, (model_w, model_h), borderValue=0.0)
        return aligned

    def _preprocess_recognition(self, face_image):
        """Resize/pad face to recognition model input size."""
        model_h, model_w, _ = self.rec_input_shape
        h, w = face_image.shape[:2]

        if h == model_h and w == model_w:
            return face_image

        scale = min(model_w / w, model_h / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(face_image, (new_w, new_h))

        padded = np.zeros((model_h, model_w, 3), dtype=np.uint8)
        top = (model_h - new_h) // 2
        left = (model_w - new_w) // 2
        padded[top:top + new_h, left:left + new_w] = resized
        return padded


# ---------------------------------------------------------------------------
# FaceRecognition thread — Hailo-specific subclass
# ---------------------------------------------------------------------------
from face_recognition_base import FaceRecognitionBase


class FaceRecognition(FaceRecognitionBase):
    THREAD_NAME_PREFIX = "Thread-HailoDetector"

    def __init__(self, face_app, active_members, match_handler, cam_queue):
        """Initialize FaceRecognition with UC8 support.

        Args:
            face_app: HailoFaceApp or HailoUC8App instance
            active_members: List of active member dicts
            match_handler: MatchEvent handler
            cam_queue: Queue for camera frames
        """
        super().__init__(face_app, active_members, match_handler, cam_queue)
        # UC8 session state is managed by HailoUC8App if available
        self.yolo_app = getattr(face_app, 'yolo_app', None)
        self.uc8_app = face_app if isinstance(face_app, HailoUC8App) else None

    def process_frame(self, raw_img, cam_info, detected, age):
        """Run UC8 person detection + UC1/3/4/5 face recognition on a frame.

        UC8 Role 2 (Continuous): Count persons on every frame (if enabled)
        UC1: Active member identification
        """
        cam_ip = cam_info.get('cam_ip')

        # UC8 Role 2: Count persons (continuous detection) - only if UC8 is enabled
        person_count, max_simultaneous = 0, 0

        # UC1/3/4/5: Face detection and recognition
        current_time = time.time()
        # HailoUC8App.get() returns (faces, person_count, max_simultaneous)
        # HailoFaceApp.get() returns just faces
        if self.uc8_app:
            faces, person_count, max_simultaneous = self.uc8_app.get(raw_img, cam_ip=cam_ip)
        else:
            faces = self.face_app.get(raw_img)
        duration = time.time() - current_time

        if detected == 1:
            logger.info(f"{cam_ip} detection frame #{detected} - age: {age:.3f} duration: {duration:.3f} "
                        f"face(s): {len(faces)}, person(s): {person_count}")
        else:
            logger.debug(f"{cam_ip} frame #{detected} - {len(faces)} faces, {person_count} persons, "
                         f"max_simultaneous: {max_simultaneous}")

        # Skip face recognition only if ALL category databases are empty (UC8 continues running)
        if not self.active_members and not self.has_any_members():
            if detected == 1:
                logger.debug(f"{cam_ip} No members in any category - skipping face recognition (UC1/3/4/5)")
            return {
                'matched': [],
                'unmatched': [],
                'person_count': person_count,
                'max_simultaneous_persons': max_simultaneous,
            }

        matched_faces = []
        unmatched_faces = []  # UC3: Track unknown faces

        # Pre-norm threshold: skip low-quality embeddings (face too far/small)
        # Auto-select based on model: 10.0 for arcface_r50, 6.0 for arcface_mobilefacenet
        rec_hef = os.environ.get('HAILO_REC_HEF', '')
        if 'mobilefacenet' in rec_hef:
            pre_norm_threshold = float(os.environ.get('HAILO_PRE_NORM_THRESHOLD_MOBILEFACENET', '6.0'))
        else:
            pre_norm_threshold = float(os.environ.get('HAILO_PRE_NORM_THRESHOLD_R50', '10.0'))

        for face in faces:
            # Skip faces with low pre_norm (too far from camera)
            if pre_norm_threshold > 0 and face.pre_norm < pre_norm_threshold:
                logger.info(f"{cam_ip} detected: {detected} age: {age:.3f} pre_norm: {face.pre_norm:.2f} "
                            f"< {pre_norm_threshold:.1f} (skipped - too far)")
                unmatched_faces.append((face, 'skipped_low_pre_norm', 0.0))
                continue

            threshold = float(os.environ['FACE_THRESHOLD_HAILO'], 0.25)

            if self.has_any_members():
                # Multi-category priority matching (BLOCKLIST > ACTIVE > INACTIVE > STAFF)
                member, sim, best_name, category = self.find_match_with_category(face.embedding, threshold)
            else:
                # Fallback: ACTIVE-only matching via base find_match
                member, sim, best_name = self.find_match(face.embedding, threshold)
                category = 'ACTIVE' if member is not None else None

            if member is None:
                logger.info(f"{cam_ip} detected: {detected} age: {age:.3f} pre_norm: {face.pre_norm:.2f} "
                            f"best_match: {best_name} best_sim: {sim:.4f} (no match)")
                # UC3: Track unmatched faces for unknown face logging
                unmatched_faces.append((face, best_name, sim))
                continue

            if 'category' not in member:
                member = dict(member)
                member['category'] = category or 'ACTIVE'

            logger.info(f"{cam_ip} detected: {detected} age: {age:.3f} pre_norm: {face.pre_norm:.2f} "
                        f"fullName: {member['fullName']} category: {member['category']} sim: {sim:.4f} (MATCH)")
            matched_faces.append((face, member, sim))

        # Return extended result with unmatched faces and person data
        return {
            'matched': matched_faces,
            'unmatched': unmatched_faces,
            'person_count': person_count,
            'max_simultaneous_persons': max_simultaneous,
        }
