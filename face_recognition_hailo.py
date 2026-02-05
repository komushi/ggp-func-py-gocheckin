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

import logging
import time
from datetime import datetime, timezone, timedelta
import sys
import os
import threading
import traceback
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
# Lightweight face result object (matches InsightFace Face interface)
# ---------------------------------------------------------------------------
class HailoFace:
    """Minimal face object compatible with InsightFace Face attributes."""
    def __init__(self, bbox: np.ndarray, embedding: np.ndarray,
                 kps: np.ndarray = None, det_score: float = 0.0):
        self.bbox = bbox            # np.ndarray shape (4,) — x1,y1,x2,y2
        self.embedding = embedding  # np.ndarray shape (512,) — L2-normalized
        self.kps = kps              # np.ndarray shape (5,2) or None
        self.det_score = det_score


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
                 nms_threshold: float = 0.4):
        """
        Args:
            det_hef_path: Path to SCRFD HEF (default: env HAILO_DET_HEF or models/scrfd_10g.hef)
            rec_hef_path: Path to ArcFace HEF (default: env HAILO_REC_HEF or models/arcface_mobilefacenet.hef)
            score_threshold: Minimum detection confidence
            nms_threshold: NMS IoU threshold
        """
        if not HAILO_AVAILABLE:
            raise RuntimeError("hailo_platform not installed")

        # Default HEF paths - use /etc/hailo/models/ on Linux, local ./models/ otherwise
        default_model_dir = '/etc/hailo/models' if sys.platform == 'linux' else os.path.join(os.path.dirname(__file__), 'models')
        self.det_hef_path = det_hef_path or os.environ.get(
            'HAILO_DET_HEF') or os.path.join(default_model_dir, 'scrfd_10g.hef')
        self.rec_hef_path = rec_hef_path or os.environ.get(
            'HAILO_REC_HEF') or os.path.join(default_model_dir, 'arcface_mobilefacenet.hef')

        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.strides = [8, 16, 32]
        self.num_anchors = 2

        logger.info(f"Loading Hailo models: det={self.det_hef_path}, rec={self.rec_hef_path}")
        self._init_device()

    def _init_device(self):
        """Initialize Hailo VDevice and load both models."""
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)

        # Detection model — request FLOAT32 output so HailoRT auto-dequantizes
        self.det_infer_model = self.vdevice.create_infer_model(self.det_hef_path)
        for output_info in self.det_infer_model.hef.get_output_vstream_infos():
            self.det_infer_model.output(output_info.name).set_format_type(FormatType.FLOAT32)
        self.det_configured = self.det_infer_model.configure()

        # Recognition model — request FLOAT32 output so HailoRT auto-dequantizes
        self.rec_infer_model = self.vdevice.create_infer_model(self.rec_hef_path)
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
            embedding = self._extract_embedding(img, kps)
            faces.append(HailoFace(
                bbox=boxes[i],
                embedding=embedding,
                kps=kps,
                det_score=float(scores[i]),
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
    def _extract_embedding(self, image, kps, debug_save=False):
        """Align face and extract 512-dim L2-normalized embedding."""
        if kps is not None:
            logger.debug(f"Landmarks for alignment: {kps.tolist()}")

        # Align face using 5-point landmarks
        aligned = self._align_face(image, kps)

        # Debug: save aligned face and original with landmarks for inspection
        if debug_save or os.environ.get('HAILO_DEBUG_SAVE_ALIGNED'):
            ts = time.time()
            # Save aligned face (convert RGB to BGR for cv2.imwrite)
            debug_aligned_path = f"/tmp/hailo_aligned_{ts:.3f}.jpg"
            cv2.imwrite(debug_aligned_path, cv2.cvtColor(aligned, cv2.COLOR_RGB2BGR))
            logger.info(f"Debug: saved aligned face to {debug_aligned_path}")

            # Save original with landmarks drawn
            if kps is not None:
                debug_orig_path = f"/tmp/hailo_landmarks_{ts:.3f}.jpg"
                img_with_landmarks = cv2.cvtColor(image.copy(), cv2.COLOR_RGB2BGR)
                for i, (x, y) in enumerate(kps):
                    cv2.circle(img_with_landmarks, (int(x), int(y)), 3, (0, 255, 0), -1)
                    cv2.putText(img_with_landmarks, str(i), (int(x)+5, int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                cv2.imwrite(debug_orig_path, img_with_landmarks)
                logger.info(f"Debug: saved landmarks image to {debug_orig_path}")

        # Preprocess for recognition model
        preprocessed = self._preprocess_recognition(aligned)

        # Run ArcFace inference — output is auto-dequantized FLOAT32 by HailoRT
        output_buffers = {
            info.name: np.empty(info.shape, dtype=np.float32)
            for info in self.rec_infer_model.outputs
        }
        bindings = self.rec_configured.create_bindings(output_buffers=output_buffers)
        bindings.input().set_buffer(preprocessed)
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
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        logger.debug(f"Live embedding: pre_norm={norm:.4f}, mean={embedding.mean():.4f}, std={embedding.std():.4f}")

        return embedding.astype(np.float32)

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
# FaceRecognition thread — identical logic to face_recognition.py
# ---------------------------------------------------------------------------
class FaceRecognition(threading.Thread):
    def __init__(self, face_app, active_members, scanner_output_queue, cam_queue):
        super().__init__(name=f"Thread-HailoDetector-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}")

        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()

        self.face_app = face_app
        self._active_members = None

        # Pre-compute embeddings matrix for vectorized comparison
        # (triggered automatically by the property setter)
        self.active_members = active_members

        self.captured_members = {}
        self.cam_detection_his = {}

    def run(self):
        logger.info(f"{self.name} started")
        time.sleep(1)

        while not self.stop_event.is_set():
            try:
                if not self.cam_queue.empty():
                    cmd, raw_img, cam_info = self.cam_queue.get(False)

                    # Handle session end signal
                    if cmd == gst.StreamCommands.SESSION_END:
                        if cam_info['cam_ip'] in self.cam_detection_his:
                            prev = self.cam_detection_his[cam_info['cam_ip']]
                            if prev['detecting_txn'] == cam_info['detecting_txn']:
                                detected = prev.get('detected', 0)
                                face_detected_at = prev.get('face_detected_at', 0)
                                face_detected_frames = prev.get('face_detected_frames', 0)
                                identified_at = prev.get('identified_at', 0)
                                logger.info(f"{cam_info['cam_ip']} session ended - detected: {detected}, face_detected_at: {face_detected_at}, face_detected_frames: {face_detected_frames}, identified_at: {identified_at}")
                        continue

                    if cam_info['cam_ip'] not in self.cam_detection_his:
                        self.cam_detection_his[cam_info['cam_ip']] = {}
                        self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                        self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                        self.cam_detection_his[cam_info['cam_ip']]['face_detected_at'] = 0
                        self.cam_detection_his[cam_info['cam_ip']]['face_detected_frames'] = 0
                        self.cam_detection_his[cam_info['cam_ip']]['identified_at'] = 0
                    else:
                        if self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] != cam_info['detecting_txn']:
                            self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                            self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                            self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                            self.cam_detection_his[cam_info['cam_ip']]['face_detected_at'] = 0
                            self.cam_detection_his[cam_info['cam_ip']]['face_detected_frames'] = 0
                            self.cam_detection_his[cam_info['cam_ip']]['identified_at'] = 0

                    # TODO: Temporarily disabled for testing — allow detection to continue after match
                    # if self.cam_detection_his[cam_info['cam_ip']]['identified']:
                    #     continue

                    current_time = time.time()
                    age = current_time - float(cam_info['frame_time'])

                    if age > float(os.environ['AGE_DETECTING_SEC']):
                        logger.debug(f"{cam_info['cam_ip']} age: {age}")
                        continue
                    else:
                        faces = self.face_app.get(raw_img)
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] += 1
                        detected = self.cam_detection_his[cam_info['cam_ip']]['detected']
                        duration = time.time() - current_time
                        # TODO: Temporarily log every frame for debugging, revert to "if detected == 1:" later
                        logger.info(f"{cam_info['cam_ip']} detection frame #{detected} - age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)}")

                        # Track frames where face is detected
                        if len(faces) > 0:
                            self.cam_detection_his[cam_info['cam_ip']]['face_detected_frames'] += 1
                            if self.cam_detection_his[cam_info['cam_ip']]['face_detected_at'] == 0:
                                self.cam_detection_his[cam_info['cam_ip']]['face_detected_at'] = detected

                    # Phase 1: Match all faces, collect results
                    matched_faces = []
                    for face in faces:
                        threshold = float(os.environ['FACE_THRESHOLD_HAILO'])
                        active_member, sim = self.find_match(face.embedding, threshold)

                        if active_member is None:
                            logger.info(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} best_sim: {sim:.4f} (no match)")
                            continue

                        logger.info(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} fullName: {active_member['fullName']} sim: {sim:.4f} (MATCH)")
                        matched_faces.append((face, active_member, sim))

                    if not matched_faces:
                        continue  # back to outer while loop — no matches this frame

                    # TODO: Temporarily skip snapshot/upload for continuous testing
                    self.cam_detection_his[cam_info['cam_ip']]['identified_at'] = detected
                    continue

                    # Phase 2: Build composite snapshot + single queue entry
                    self.cam_detection_his[cam_info['cam_ip']]['identified'] = True
                    self.cam_detection_his[cam_info['cam_ip']]['identified_at'] = detected
                    logger.info(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)} matched: {len(matched_faces)}")

                    date_folder = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
                    time_filename = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
                    ext = ".jpg"

                    local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder, time_filename + ext)
                    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

                    # Draw ALL bounding boxes on one image
                    img = raw_img.astype(np.uint8)
                    for face, active_member, sim in matched_faces:
                        bbox = face.bbox.astype(int)
                        cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                        cv2.putText(img, f"{active_member['fullName']}:{str(round(sim, 2))}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36,255,12), 2)
                    cv2.imwrite(local_file_path, img)
                    logger.info(f"Snapshot taken at {local_file_path} with {len(matched_faces)} face(s)")

                    # Build per-member payloads
                    members_data = []
                    for face, active_member, sim in matched_faces:
                        memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                        keyNotified = active_member.get('keyNotified', False)
                        checkin_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/listings/{active_member['listingId']}/{active_member['reservationCode']}/checkIn/{str(active_member['memberNo'])}{ext}"""
                        property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""

                        member_payload = {
                            "hostId": os.environ['HOST_ID'],
                            "propertyCode": os.environ['PROPERTY_CODE'],
                            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                            "coreName": os.environ['AWS_IOT_THING_NAME'],
                            "assetId": cam_info['cam_uuid'],
                            "assetName": cam_info['cam_name'],
                            "cameraIp": cam_info['cam_ip'],
                            "reservationCode": active_member['reservationCode'],
                            "listingId": active_member['listingId'],
                            "memberNo": int(str(active_member['memberNo'])),
                            "fullName": active_member['fullName'],
                            "similarity": sim,
                            "recordTime": datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                            "checkInImgKey": checkin_object_key,
                            "propertyImgKey": property_object_key,
                            "keyNotified": keyNotified,
                        }

                        self.captured_members[memberKey] = member_payload
                        members_data.append({"memberKey": memberKey, "payload": member_payload, "keyNotified": keyNotified})

                    # Single queue entry with all members
                    snapshot_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""
                    snapshot_payload = {
                        "hostId": os.environ['HOST_ID'],
                        "propertyCode": os.environ['PROPERTY_CODE'],
                        "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                        "coreName": os.environ['AWS_IOT_THING_NAME'],
                        "assetId": cam_info['cam_uuid'],
                        "assetName": cam_info['cam_name'],
                        "cameraIp": cam_info['cam_ip'],
                        "recordStart": members_data[0]['payload']['recordTime'],
                        "recordEnd": members_data[0]['payload']['recordTime'],
                        "identityId": os.environ['IDENTITY_ID'],
                        "s3level": 'private',
                        "videoKey": '',
                        "snapshotKey": snapshot_key
                    }

                    if not self.scanner_output_queue.full():
                        self.scanner_output_queue.put({
                            "type": "member_detected",
                            "members": members_data,
                            "cam_ip": cam_info['cam_ip'],
                            "detecting_txn": cam_info['detecting_txn'],
                            "local_file_path": local_file_path,
                            "property_object_key": members_data[0]['payload']['propertyImgKey'],
                            "snapshot_payload": snapshot_payload,
                        }, block=False)

                else:
                    time.sleep(float(os.environ['DETECTING_SLEEP_SEC']))

            except Exception as e:
                logger.error(f"Caught {self.name} runtime exception!")
                logger.error(e)
                traceback.print_exc()
                self.stop_event.set()

        while not self.cam_queue.empty():
            _, _, _ = self.cam_queue.get(False)

    def stop_detection(self):
        logger.info(f"Stop face detector {self.name}")
        self.stop_event.set()
        if self.face_app is not None:
            del self.face_app

    @property
    def active_members(self):
        return self._active_members

    @active_members.setter
    def active_members(self, value):
        self._active_members = value
        self._build_member_embeddings()

    def _build_member_embeddings(self):
        """Pre-compute embeddings matrix and norms for vectorized comparison."""
        if not self.active_members:
            self.member_embeddings = np.empty((0, 512), dtype=np.float32)
            self.member_norms = np.empty(0, dtype=np.float32)
            logger.info("No active members - embeddings matrix empty")
            return

        # Stack all embeddings into a matrix (N x 512)
        embeddings_list = []
        for member in self.active_members:
            emb = np.array(member['faceEmbedding'], dtype=np.float32).ravel()
            embeddings_list.append(emb)

        self.member_embeddings = np.array(embeddings_list, dtype=np.float32)
        self.member_norms = np.linalg.norm(self.member_embeddings, axis=1)

        logger.info(f"Built embeddings matrix: {self.member_embeddings.shape[0]} members, {self.member_embeddings.shape[1]} dimensions")

        # Debug: log stored embedding statistics for each member
        for i, member in enumerate(self.active_members):
            emb = self.member_embeddings[i]
            logger.info(f"Stored embedding [{member.get('fullName', '?')}]: norm={self.member_norms[i]:.4f}, mean={emb.mean():.4f}, std={emb.std():.4f}, min={emb.min():.4f}, max={emb.max():.4f}")

    def find_match(self, face_embedding, threshold):
        """
        Vectorized face matching - find best matching member above threshold.

        Args:
            face_embedding: Face embedding vector (512-dim)
            threshold: Minimum similarity threshold

        Returns:
            Tuple of (matched_member, similarity) or (None, 0.0) if no match
        """
        if self.member_embeddings.shape[0] == 0:
            return None, 0.0

        # Normalize face embedding
        face_emb = np.array(face_embedding, dtype=np.float32).ravel()
        face_norm = np.linalg.norm(face_emb)

        if face_norm == 0:
            return None, 0.0

        # Vectorized cosine similarity: dot(embeddings, face) / (norms * face_norm)
        similarities = np.dot(self.member_embeddings, face_emb) / (self.member_norms * face_norm)

        # Find best match
        max_idx = np.argmax(similarities)
        max_sim = similarities[max_idx]

        if max_sim >= threshold:
            return self.active_members[max_idx], float(max_sim)

        return None, float(max_sim)

    def compute_sim(self, feat1, feat2):
        """Legacy method for single pairwise comparison (kept for compatibility)."""
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()
        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))
        return sim
