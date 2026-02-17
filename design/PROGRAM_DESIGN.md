# Program Design: Security Use-Cases (UC1-UC5, UC8)

**Based on**: `SECURITY_USE_CASES.md`
**Scope**: Initial version — UC1 (refactor), UC2-UC5 (new), UC8 (new)
**Codebase**: `uc/ggp-func-py-gocheckin`

---

## 1. Overview

This document translates the security use-case requirements into concrete implementation: module structure, classes, interfaces, data structures, session state, detection pipeline, and implementation order.

### 1.1 Key Constraints

| Constraint | Detail |
|-----------|--------|
| **Hailo-only UCs** | UC2-UC5 and UC8 only run on the Hailo inference backend. InsightFace does not support these use cases. |
| **InsightFace = UC1 only** | The InsightFace backend retains existing UC1 behavior: detect → match → stop on first match. No continuous loop, no security handler chain. |
| **Decoupling required** | Current code mixes inference and business logic in single files. Must separate cleanly. |

### 1.2 Current Coupling Problem

Two files in `uc/ggp-func-py-gocheckin` exhibit the same anti-pattern — inference logic and business logic mixed in a single file:

```
face_recognition.py
├── FaceRecognition(Thread)         ← Business logic (queue, matching, snapshots, IoT)
└── uses InsightFace FaceAnalysis   ← Inference (passed in, but tightly coupled)

face_recognition_hailo.py
├── HailoFace + HailoFaceApp        ← Inference (SCRFD + ArcFace on Hailo-8)
└── FaceRecognition(Thread)          ← Business logic (DUPLICATED from face_recognition.py)
```

**Problems**:
- `FaceRecognition` thread is duplicated nearly line-for-line (~240 lines) across both files
- Adding UC2-UC5 to the Hailo path would require modifying the duplicated business logic
- Adding a new backend (e.g., NVIDIA Jetson) would require yet another copy of the same business logic

### 1.3 Design Principles

1. **Inference modules are inference-only** — no queue processing, no IoT, no snapshots
2. **Business logic exists in one place** — backend-agnostic for shared logic, backend-specific where needed
3. **Two processing paths** — InsightFace (simple, UC1) and Hailo (full, UC1-UC5 + UC8)
4. **Extensible backend interface** — adding a new backend (e.g., NVIDIA Jetson) requires only: (a) a new inference module implementing the abstract base class, and (b) a backend selection entry in `py_handler.py`. No changes to business logic, session state, or security handlers

### 1.4 Models

| Model | Backend | Purpose | UCs |
|-------|---------|---------|-----|
| InsightFace FaceAnalysis | CPU/ONNX | Face detection + recognition | UC1 only |
| SCRFD | Hailo HEF | Face detection (bboxes + landmarks) | UC1-UC5 |
| ArcFace | Hailo HEF | Face recognition (512-d embeddings) | UC1-UC5 |
| YOLOv8n | Hailo HEF | Person detection (COCO class 0) | UC8 only |

---

## 2. Module Structure (After Refactor)

### 2.1 Target Structure

```
uc/ggp-func-py-gocheckin/
├── py_handler.py                      # Orchestrator (unchanged role, modified internals)
│
├── inference/                         # Inference-only modules
│   ├── __init__.py
│   ├── base.py                        # Abstract base classes: FaceBackend, PersonBackend
│   ├── face_result.py                 # Common FaceResult, PersonResult dataclasses
│   ├── insightface_backend.py         # FaceBackend impl → InsightFace CPU/ONNX
│   ├── hailo_face_backend.py          # FaceBackend impl → Hailo SCRFD+ArcFace
│   ├── hailo_yolo_backend.py          # PersonBackend impl → Hailo YOLOv8n
│   └── jetson_backend.py              # (future) FaceBackend impl → NVIDIA Jetson TensorRT
│
├── detection/                         # Business logic
│   ├── __init__.py
│   ├── face_recognition_simple.py     # SimpleRecognition(Thread) — InsightFace path, UC1 only
│   ├── face_processor.py              # FaceProcessor(Thread) — Hailo path, UC1-UC5 continuous loop
│   ├── session_state.py               # DetectionSession dataclass (flags + accumulators)
│   ├── member_store.py                # MemberStore — multi-category member database
│   ├── security_handlers.py           # SecurityHandlerChain (UC1-UC5 handler chain)
│   └── unknown_face_cluster.py        # Session-level unknown face clustering (UC3/UC4)
│
├── gstreamer_threading.py             # Video pipeline (modified for UC8 extend)
├── onvif_process.py                   # ONVIF integration (unchanged)
├── s3_uploader.py                     # S3 upload (unchanged)
└── web_image_process.py               # Image utilities (unchanged)

REMOVED:
├── face_recognition.py                # → split into inference/insightface_backend.py
│                                      #   + detection/face_recognition_simple.py
└── face_recognition_hailo.py          # → split into inference/hailo_face_backend.py
                                       #   + detection/face_processor.py
```

### 2.2 Processing Paths

The `use_full_path` flag determines which processing path is used. Future backends (e.g., Jetson) choose one of the two paths based on their capability level.

```
                      py_handler.py
                      detect_face_backend()
                           │
                    ┌──────┴──────┐
                    │             │
               Simple path   Full path
              (UC1 only)    (UC1-UC5 + UC8)
                    │             │
             ┌──────┘      ┌──────┘
             │             │
        Any FaceBackend    Any FaceBackend
        impl that only     impl that supports
        needs UC1:         full UCs:
             │             │
        • InsightFace      • Hailo
        • (future:         • (future: Jetson
           Jetson-lite)       w/ YOLOv8n)
             │             │
        detection/         detection/
        face_recognition   face_processor.py
        _simple.py         ├── session_state.py
             │             ├── member_store.py
          UC1 only         ├── security_handlers.py
        Stop on match      └── unknown_face_cluster.py
                                │
                           UC1-UC5 + UC8
                           Continuous loop
```

### 2.3 Dependency Graph

```
py_handler.py
    │
    ├── inference/base.py                          (abstract base classes)
    │
    ├── [Simple path — UC1 only]
    │   ├── inference/insightface_backend.py       (implements FaceBackend)
    │   └── detection/face_recognition_simple.py
    │
    ├── [Full path — UC1-UC5 + UC8]
    │   ├── inference/hailo_face_backend.py        (implements FaceBackend)
    │   ├── inference/hailo_yolo_backend.py        (implements PersonBackend, UC8)
    │   └── detection/face_processor.py
    │           ├── detection/session_state.py
    │           ├── detection/member_store.py
    │           ├── detection/security_handlers.py
    │           └── detection/unknown_face_cluster.py
    │
    ├── [Future: Jetson path — choose simple or full based on capability]
    │   └── inference/jetson_backend.py            (implements FaceBackend + optionally PersonBackend)
    │
    ├── gstreamer_threading.py
    ├── onvif_process.py
    └── s3_uploader.py
```

---

## 3. Inference Layer (`inference/`)

Inference modules are **pure inference** — no threading, no queues, no IoT, no business decisions. They take an image and return structured results.

All face backends implement `FaceBackend` (ABC). All person detection backends implement `PersonBackend` (ABC). This ensures new backends (e.g., Jetson) are drop-in replacements with a verified interface.

### 3.1 Common Data Types (`inference/face_result.py`)

```python
from dataclasses import dataclass
import numpy as np

@dataclass
class FaceResult:
    """Common face result returned by all face inference backends."""
    bbox: np.ndarray        # shape (4,): x1, y1, x2, y2
    embedding: np.ndarray   # shape (512,): L2-normalized
    det_score: float        # Detection confidence
    kps: np.ndarray = None  # shape (5, 2): 5 facial landmarks (optional)

@dataclass
class PersonResult:
    """Person detection result (e.g., YOLOv8n class 0)."""
    bbox: np.ndarray        # shape (4,): x1, y1, x2, y2
    confidence: float       # Detection confidence for class 0 (person)
```

### 3.2 Abstract Base Classes (`inference/base.py`)

```python
from abc import ABC, abstractmethod
import numpy as np
from inference.face_result import FaceResult, PersonResult

class FaceBackend(ABC):
    """Abstract base class for face detection + recognition backends.

    All backends must implement get() returning List[FaceResult].
    Each FaceResult must have:
      - bbox: np.ndarray shape (4,) — x1, y1, x2, y2
      - embedding: np.ndarray shape (512,) — L2-normalized
      - det_score: float

    To add a new backend (e.g., NVIDIA Jetson):
      1. Create inference/jetson_backend.py
      2. Implement JetsonFaceBackend(FaceBackend)
      3. Add selection logic in py_handler.py detect_face_backend()
    """

    @abstractmethod
    def get(self, img: np.ndarray, max_num: int = 0,
            det_size: tuple = (640, 640)) -> list[FaceResult]:
        """Detect faces and extract embeddings.

        Args:
            img: BGR image (np.ndarray, HxWx3, uint8)
            max_num: Max faces to return (0 = unlimited)
            det_size: Detection input resolution hint

        Returns:
            List of FaceResult with bbox, embedding, det_score
        """
        ...


class PersonBackend(ABC):
    """Abstract base class for person/body detection backends.

    Used by UC8 for session gate and extend.
    Currently only Hailo YOLOv8n implements this.
    Future backends (e.g., Jetson TensorRT YOLOv8n) implement the same interface.
    """

    @abstractmethod
    def detect_persons(self, img: np.ndarray) -> list[PersonResult]:
        """Detect person class in image.

        Args:
            img: BGR image (np.ndarray, HxWx3, uint8)

        Returns:
            List of PersonResult for detected persons above threshold.
        """
        ...
```

### 3.3 InsightFace Backend (`inference/insightface_backend.py`)

Extracted from current `face_recognition.py`. Wraps `FaceAnalysis`. Implements `FaceBackend`.

```python
from inference.base import FaceBackend

class InsightFaceBackend(FaceBackend):
    """InsightFace CPU/ONNX face detection + recognition.

    Supports UC1 only (simple path).
    """

    def __init__(self, model_name='buffalo_sc', root=None, det_size=(640, 640)):
        self.det_size = det_size
        app = FaceAnalysis(
            name=model_name,
            allowed_modules=['detection', 'recognition'],
            providers=['CPUExecutionProvider'],
            root=root or os.environ.get('INSIGHTFACE_LOCATION', '/etc/insightface')
        )
        app.prepare(ctx_id=0, det_size=det_size)
        self._app = app

    def get(self, img: np.ndarray, max_num: int = 0,
            det_size: tuple = (640, 640)) -> list[FaceResult]:
        """Detect faces and extract embeddings."""
        faces = self._app.get(img, max_num=max_num)
        return [
            FaceResult(
                bbox=f.bbox,
                embedding=f.embedding,
                det_score=f.det_score,
                kps=getattr(f, 'kps', None),
            )
            for f in faces
        ]
```

### 3.5 Hailo Face Backend (`inference/hailo_face_backend.py`)

Extracted from current `face_recognition_hailo.py`. Contains all SCRFD + ArcFace inference logic. Implements `FaceBackend`.

**No changes to inference logic** — only remove the duplicated `FaceRecognition` thread class and convert return type to `FaceResult`.

```python
from inference.base import FaceBackend

class HailoFaceBackend(FaceBackend):
    """Hailo-8 accelerated SCRFD + ArcFace face detection + recognition.

    Extracted from face_recognition_hailo.py.
    All inference logic preserved as-is.
    """

    def __init__(self, det_hef_path=None, rec_hef_path=None,
                 score_threshold=0.5, nms_threshold=0.4):
        # ... (existing HailoFaceApp.__init__ logic)
        ...

    def get(self, img: np.ndarray, max_num: int = 0,
            det_size: tuple = (640, 640)) -> list[FaceResult]:
        """Detect faces and extract embeddings.

        Internally: BGR→RGB, preprocess, SCRFD detection, ArcFace recognition.
        Returns FaceResult objects (not HailoFace).
        """
        # ... (existing HailoFaceApp.get logic)
        # Convert HailoFace → FaceResult at return
        ...

    # All existing private methods preserved:
    # _init_device, _build_output_layer_map, _generate_anchors,
    # _preprocess_detection, _run_detection, _postprocess_detection,
    # _nms, _extract_embedding, _align_face, _preprocess_recognition
```

### 3.6 Hailo YOLOv8n Backend (`inference/hailo_yolo_backend.py`)

New module for UC8 session lifecycle control. Implements `PersonBackend`.

```python
from inference.base import PersonBackend

class HailoYOLOv8nBackend(PersonBackend):
    """YOLOv8n person detection on Hailo-8.

    Uses pre-compiled HEF from Hailo Model Zoo.
    Filters for COCO class 0 (person) only.
    """

    def __init__(self, hef_path: str = None,
                 confidence_threshold: float = 0.5):
        """Initialize YOLOv8n on Hailo VDevice.

        Args:
            hef_path: Path to yolov8n.hef. Defaults to /etc/hailo/models/yolov8n.hef.
            confidence_threshold: Min confidence for person class 0.
        """
        ...

    def detect_persons(self, img: np.ndarray) -> list[PersonResult]:
        """Detect person class (COCO class 0) in image.

        Returns only person detections above confidence threshold.
        """
        ...
```

### 3.7 Future: Jetson Backend (`inference/jetson_backend.py`)

Placeholder for NVIDIA Jetson TensorRT backend. Demonstrates the extension pattern.

```python
from inference.base import FaceBackend, PersonBackend

class JetsonFaceBackend(FaceBackend):
    """NVIDIA Jetson TensorRT face detection + recognition.

    Uses TensorRT-optimized SCRFD + ArcFace (or equivalent).
    Drop-in replacement — implements FaceBackend ABC.

    To add:
      1. Implement __init__() to load TensorRT engines
      2. Implement get() to run inference and return List[FaceResult]
      3. Add 'jetson' case in py_handler.py detect_face_backend()
    """

    def __init__(self, det_engine_path=None, rec_engine_path=None, **kwargs):
        ...

    def get(self, img: np.ndarray, max_num: int = 0,
            det_size: tuple = (640, 640)) -> list[FaceResult]:
        ...


class JetsonYOLOv8nBackend(PersonBackend):
    """NVIDIA Jetson TensorRT YOLOv8n person detection.

    Optional — only needed if Jetson path supports UC8.
    """

    def __init__(self, engine_path=None, confidence_threshold=0.5):
        ...

    def detect_persons(self, img: np.ndarray) -> list[PersonResult]:
        ...
```

### 3.8 Backend Selection (`py_handler.py`)

```python
def detect_face_backend():
    """Detect available backends.

    Returns:
        face_backend: FaceBackend implementation
        person_backend: PersonBackend implementation or None
        use_full_path: bool — True = FaceProcessor (UC1-UC5+UC8), False = SimpleRecognition (UC1)
    """
    backend_pref = os.environ.get('INFERENCE_BACKEND', 'auto')
    use_full_path = False
    person_backend = None

    if backend_pref in ('hailo', 'auto'):
        try:
            from inference.hailo_face_backend import HailoFaceBackend
            face_backend = HailoFaceBackend(...)
            use_full_path = True
        except Exception:
            if backend_pref == 'hailo':
                raise
            from inference.insightface_backend import InsightFaceBackend
            face_backend = InsightFaceBackend(...)

    elif backend_pref == 'jetson':
        # Future: Jetson TensorRT backend
        from inference.jetson_backend import JetsonFaceBackend
        face_backend = JetsonFaceBackend(...)
        use_full_path = True  # Jetson supports full UC path

    else:  # 'insightface'
        from inference.insightface_backend import InsightFaceBackend
        face_backend = InsightFaceBackend(...)

    # PersonBackend (UC8) — only on full path
    if use_full_path:
        try:
            if backend_pref in ('hailo', 'auto'):
                from inference.hailo_yolo_backend import HailoYOLOv8nBackend
                person_backend = HailoYOLOv8nBackend(
                    confidence_threshold=float(os.environ.get('YOLO_DETECT_THRESHOLD', '0.5'))
                )
            elif backend_pref == 'jetson':
                from inference.jetson_backend import JetsonYOLOv8nBackend
                person_backend = JetsonYOLOv8nBackend(...)
        except Exception as e:
            logger.warning(f"PersonBackend not available, UC8 disabled: {e}")

    return face_backend, person_backend, use_full_path
```

---

## 4. Detection Layer — Two Paths (`detection/`)

### 4.1 Path A: InsightFace — SimpleRecognition (`detection/face_recognition_simple.py`)

This is the **existing** `FaceRecognition` thread from `face_recognition.py`, cleaned up to use `FaceResult` but otherwise **unchanged in behavior**:

- UC1 only
- Stop on first match (`identified = True` → skip remaining frames)
- Match against ACTIVE members only
- Output: `member_detected` to `scanner_output_queue`

```python
class SimpleRecognition(threading.Thread):
    """InsightFace detection path — UC1 only.

    Behavior identical to current face_recognition.py:
    - Processes frames from cam_queue
    - Matches against active members (ACTIVE category only)
    - Stops processing on first match (stop-on-match)
    - Outputs member_detected to scanner_output_queue

    No security handler chain. No continuous loop.
    No UC2-UC5, UC8 support.
    """

    def __init__(self, face_backend, active_members, scanner_output_queue, cam_queue):
        super().__init__(daemon=True, name="Thread-SimpleRecognition-...")
        self.face_backend = face_backend  # InsightFaceBackend
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()

        self._active_members = None
        self.active_members = active_members  # Triggers _build_member_embeddings

        self.captured_members = {}
        self.cam_detection_his = {}

    # Existing logic from face_recognition.py:
    # run(), find_match(), _build_member_embeddings(), compute_sim()
    # All preserved as-is, only change: face_app → face_backend
    ...
```

**Key**: This class is a direct extraction of the existing `FaceRecognition` from `face_recognition.py`. No new features, no behavior changes.

### 4.2 Path B: Hailo — FaceProcessor (`detection/face_processor.py`)

New processing thread for the Hailo path. Runs the **continuous detection loop** with full security handler chain (UC1-UC5). Session state tracked per camera.

```python
class FaceProcessor(threading.Thread):
    """Hailo detection path — UC1-UC5 continuous loop.

    Key differences from SimpleRecognition:
    - Does NOT stop on first match — runs every frame for full timer
    - Matches against ALL member categories (ACTIVE, INACTIVE, STAFF, BLOCKLIST)
    - Runs SecurityHandlerChain on every face, every frame
    - Maintains DetectionSession state per camera (flags + accumulators)
    - Supports UC2 tailgating, UC3 unknown face logging, UC4 group size, UC5 alerts
    - UC4 group size check runs at session end
    """

    def __init__(self, face_backend, member_store: MemberStore,
                 cam_queue: Queue, output_queue: Queue,
                 handler_chain: SecurityHandlerChain,
                 stop_event: threading.Event, config: dict):
        super().__init__(daemon=True, name="Thread-FaceProcessor-...")
        self.face_backend = face_backend  # HailoFaceBackend
        self.member_store = member_store
        self.cam_queue = cam_queue
        self.output_queue = output_queue
        self.handler_chain = handler_chain
        self.stop_event = stop_event
        self.config = config

        # Per-camera active sessions
        self._sessions: dict[str, DetectionSession] = {}

    def run(self):
        """Main loop: dequeue frames, detect faces, run all handlers."""
        while not self.stop_event.is_set():
            try:
                cmd, payload, cam_info = self.cam_queue.get(timeout=0.5)
            except Empty:
                continue

            cam_ip = cam_info.get('cam_ip', '')
            detecting_txn = cam_info.get('detecting_txn', '')

            if cmd == StreamCommands.FRAME:
                self._process_frame(payload, cam_info, cam_ip, detecting_txn)
            elif cmd == StreamCommands.SESSION_END:
                self._end_session(cam_ip, detecting_txn, cam_info)

    def _process_frame(self, img, cam_info, cam_ip, detecting_txn):
        """Process a single frame through the security handler chain."""
        session = self._get_or_create_session(cam_ip, detecting_txn, cam_info)
        session.frame_count += 1

        # Face detection + recognition via Hailo backend
        faces = self.face_backend.get(img)
        if not faces:
            return

        # Run ALL security handlers on EVERY face
        all_results = []
        for face in faces:
            results = self.handler_chain.process_face(face, session, cam_info)
            all_results.extend(results)

        # Emit results to output queue (if any)
        if all_results:
            snapshot_path = self._save_snapshot(img, faces, cam_info)
            self.output_queue.put({
                'type': 'handler_results',
                'results': all_results,
                'cam_ip': cam_ip,
                'detecting_txn': detecting_txn,
                'local_file_path': snapshot_path,
                'cam_info': cam_info,
                'session_snapshot': {
                    'active_member_matched': session.active_member_matched,
                    'unlocked': session.unlocked,
                    'unlocked_locks': list(session.unlocked_locks),
                    'clicked_locks': list(session.clicked_locks),
                    'block_further_unlocks': session.block_further_unlocks,
                }
            })

    def _end_session(self, cam_ip, detecting_txn, cam_info):
        """Session end: run UC4 group size check, clean up."""
        session = self._sessions.get(cam_ip)
        if session is None or session.detecting_txn != detecting_txn:
            return

        # UC4: Group size validation at session end (P2 only)
        has_lock = bool(cam_info.get('locks'))
        if has_lock:
            uc4_result = self.handler_chain.check_group_size(session, cam_info)
            if uc4_result:
                self.output_queue.put({
                    'type': 'handler_results',
                    'results': [uc4_result],
                    'cam_ip': cam_ip,
                    'detecting_txn': detecting_txn,
                    'cam_info': cam_info,
                })

        del self._sessions[cam_ip]

    def update_session_lock(self, cam_ip: str, lock_id: str):
        """Called when a clicked event arrives — add lock to session.

        Also performs immediate unlock check (Decision 30):
        if active_member already matched, unlock immediately.
        """
        session = self._sessions.get(cam_ip)
        if session is None:
            return

        session.add_clicked_lock(lock_id)

        # Decision 30: Immediate unlock if member already matched
        if (session.active_member_matched
                and not session.block_further_unlocks
                and lock_id not in session.unlocked_locks):
            last_member = self._last_active_member_id(session)
            if session.try_unlock(lock_id, last_member):
                self.output_queue.put({
                    'type': 'immediate_unlock',
                    'cam_ip': cam_ip,
                    'detecting_txn': session.detecting_txn,
                    'lock_id': lock_id,
                    'member_id': session.unlock_authorized_member,
                })

    def _get_or_create_session(self, cam_ip, detecting_txn, cam_info):
        """Get existing session or create new one."""
        session = self._sessions.get(cam_ip)
        if session is None or session.detecting_txn != detecting_txn:
            session = DetectionSession(
                detecting_txn=detecting_txn,
                cam_ip=cam_ip,
                started_by_onvif=cam_info.get('started_by_onvif', False),
            )
            self._sessions[cam_ip] = session
        return session

    def _last_active_member_id(self, session) -> str | None:
        """Get the last active member that matched in this session."""
        for member_id, info in session.known_members.items():
            if info['category'] == MemberCategory.ACTIVE:
                return member_id
        return None

    def _save_snapshot(self, img, faces, cam_info) -> str | None:
        """Save annotated snapshot to disk. Returns local file path."""
        ...
```

### 4.3 Side-by-Side Comparison

| Aspect | SimpleRecognition (InsightFace) | FaceProcessor (Hailo) |
|--------|-------------------------------|----------------------|
| Backend | InsightFaceBackend | HailoFaceBackend |
| Use cases | UC1 only | UC1-UC5 + UC8 |
| Loop behavior | Stop on first match | Continuous for full timer |
| Member categories | ACTIVE only | ACTIVE, INACTIVE, STAFF, BLOCKLIST |
| Handler chain | None (inline matching) | SecurityHandlerChain |
| Session state | Simple `cam_detection_his` dict | `DetectionSession` dataclass |
| Unknown face tracking | None | `UnknownFaceClusterStore` |
| Clicked/lock handling | Existing `trigger_lock_context` | `DetectionSession.clicked_locks` |
| Output format | `member_detected` (existing) | `handler_results` (new) |
| UC8 gate/extend | No | Yes (via py_handler + gstreamer) |

---

## 5. Member Store (`detection/member_store.py`)

### 5.1 Purpose

Multi-category member database with precomputed embedding matrices. **Used by Hailo path only.** InsightFace path continues to use the flat `active_members` list.

### 5.2 Member Categories

```python
from enum import Enum

class MemberCategory(Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    STAFF = "STAFF"
    BLOCKLIST = "BLOCKLIST"
```

### 5.3 Class Design

```python
class MemberStore:
    """Multi-category member database with vectorized matching.

    Each category maintains its own precomputed embedding matrix
    for fast vectorized cosine similarity.
    """

    def __init__(self):
        self._categories: dict[MemberCategory, list[dict]] = {
            cat: [] for cat in MemberCategory
        }
        self._embedding_matrices: dict[MemberCategory, np.ndarray | None] = {
            cat: None for cat in MemberCategory
        }
        self._embedding_norms: dict[MemberCategory, np.ndarray | None] = {
            cat: None for cat in MemberCategory
        }

    def update_category(self, category: MemberCategory, members: list[dict]):
        """Replace all members in a category and rebuild embedding matrix."""
        self._categories[category] = members
        self._rebuild_matrix(category)

    def _rebuild_matrix(self, category: MemberCategory):
        """Precompute embedding matrix + L2 norms for vectorized similarity."""
        members = self._categories[category]
        embeddings = [m['faceEmbedding'] for m in members if 'faceEmbedding' in m]
        if embeddings:
            matrix = np.array(embeddings, dtype=np.float32)
            self._embedding_matrices[category] = matrix
            self._embedding_norms[category] = np.linalg.norm(matrix, axis=1)
        else:
            self._embedding_matrices[category] = None
            self._embedding_norms[category] = None

    def find_best_match(self, embedding: np.ndarray,
                        threshold: float = 0.45
                        ) -> tuple[MemberCategory | None, dict | None, float]:
        """Find the best matching member across all categories.

        Priority order: BLOCKLIST > ACTIVE > INACTIVE > STAFF.
        Returns the highest-similarity match above threshold,
        with BLOCKLIST taking priority if it ties.
        """
        priority_order = [
            MemberCategory.BLOCKLIST,
            MemberCategory.ACTIVE,
            MemberCategory.INACTIVE,
            MemberCategory.STAFF,
        ]

        best_category = None
        best_member = None
        best_sim = 0.0

        for category in priority_order:
            matrix = self._embedding_matrices.get(category)
            if matrix is None:
                continue
            norms = self._embedding_norms[category]

            sims = np.dot(matrix, embedding) / (norms * np.linalg.norm(embedding))
            max_idx = int(np.argmax(sims))
            max_sim = float(sims[max_idx])

            if max_sim >= threshold and max_sim > best_sim:
                best_category = category
                best_member = self._categories[category][max_idx]
                best_sim = max_sim

        if best_sim >= threshold:
            return best_category, best_member, best_sim
        return None, None, 0.0

    def get_member_count_for_reservation(self, reservation_code: str) -> int | None:
        """Get expected memberCount for a reservation (UC4)."""
        for member in self._categories[MemberCategory.ACTIVE]:
            res = member.get('_reservation', {})
            if res.get('reservationCode') == reservation_code:
                return res.get('memberCount')
        return None
```

### 5.4 Loading Members from DynamoDB (`py_handler.py`)

```python
def fetch_all_members():
    """Load all member categories from DynamoDB into MemberStore.

    Called at session start (full path only — Hailo, future Jetson).
    Simple path (InsightFace) continues using existing fetch_members() → active_members list.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    cutoff = (datetime.now() - timedelta(
        days=int(os.environ.get('INACTIVE_MEMBER_DAYS_BACK', '30'))
    )).strftime('%Y-%m-%d')

    all_reservations = scan_table(TBL_RESERVATION)

    categorized = {cat: [] for cat in MemberCategory}

    for res in all_reservations:
        check_in = res.get('checkInDate', '')
        check_out = res.get('checkOutDate', '')

        if res.get('blocklist', False):
            categorized[MemberCategory.BLOCKLIST].append(res)
        elif res.get('staff', False):
            categorized[MemberCategory.STAFF].append(res)
        elif check_in <= today <= check_out:
            categorized[MemberCategory.ACTIVE].append(res)
        elif cutoff <= check_out < today:
            categorized[MemberCategory.INACTIVE].append(res)

    for category, reservations in categorized.items():
        members = []
        for res in reservations:
            res_members = query_members(res['reservationCode'])
            for m in res_members:
                if 'faceEmbedding' in m:
                    m['_reservation'] = res  # Attach reservation context
                    members.append(m)
        member_store.update_category(category, members)
```

---

## 6. Session State (`detection/session_state.py`)

Per-session mutable state. **Hailo path only.**

```python
@dataclass
class DetectionSession:
    """Per-session state for a detection session (Hailo path).

    Created when a detection session starts.
    Destroyed when the session ends.
    """

    # Identity
    detecting_txn: str
    cam_ip: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    started_by_onvif: bool = False

    # --- Flags ---
    active_member_matched: bool = False       # UC1: ACTIVE member seen
    unlocked: bool = False                    # UC1: at least one lock unlocked → enables UC2
    block_further_unlocks: bool = False       # UC5-BLOCKLIST: block all future unlocks

    # --- Lock tracking ---
    clicked_locks: set = field(default_factory=set)     # Lock IDs that received clicked
    unlocked_locks: set = field(default_factory=set)    # Lock IDs already unlocked

    # --- Face accumulators ---
    known_members: dict = field(default_factory=dict)
    # key: member_id → { category, member, similarity, first_frame, last_frame, reservation_code }

    unknown_face_clusters: UnknownFaceClusterStore = field(
        default_factory=UnknownFaceClusterStore
    )

    # --- Frame counter ---
    frame_count: int = 0

    # --- UC2 context ---
    unlock_authorized_member: str | None = None
    unlock_timestamp: datetime | None = None

    # --- UC8 motion tracking ---
    last_motion_timestamp: datetime | None = None

    # --- UC4 context ---
    matched_active_member_reservation: str | None = None

    def add_clicked_lock(self, lock_id: str):
        self.clicked_locks.add(lock_id)

    def try_unlock(self, lock_id: str, member_id: str) -> bool:
        """Attempt to unlock a specific lock. Returns True if unlock proceeds."""
        if lock_id not in self.clicked_locks:
            return False
        if lock_id in self.unlocked_locks:
            return False
        if self.block_further_unlocks:
            return False
        self.unlocked_locks.add(lock_id)
        self.unlocked = True
        self.unlock_authorized_member = member_id
        self.unlock_timestamp = datetime.utcnow()
        return True

    def record_known_member(self, member_id, category, member, similarity, reservation_code):
        """Record a recognized face (deduplicate by member_id across frames)."""
        if member_id in self.known_members:
            self.known_members[member_id]['last_frame'] = self.frame_count
        else:
            self.known_members[member_id] = {
                'category': category,
                'member': member,
                'similarity': similarity,
                'first_frame': self.frame_count,
                'last_frame': self.frame_count,
                'reservation_code': reservation_code,
            }

    @property
    def distinct_face_count(self) -> int:
        """Total distinct persons in session (UC4)."""
        return len(self.known_members) + len(self.unknown_face_clusters)
```

---

## 7. Unknown Face Clustering (`detection/unknown_face_cluster.py`)

Session-level clustering for UC3 (logging) and UC4 (distinct person counting). **Hailo path only.**

```python
class UnknownFaceCluster:
    """A single cluster representing one distinct unknown person."""

    def __init__(self, embedding: np.ndarray, bbox: np.ndarray):
        self.centroid = embedding.copy()
        self.last_bbox = bbox.copy()
        self.count = 1
        self.embeddings = [embedding]

    def merge(self, embedding: np.ndarray, bbox: np.ndarray):
        self.count += 1
        self.embeddings.append(embedding)
        self.centroid = np.mean(self.embeddings, axis=0)
        self.centroid /= np.linalg.norm(self.centroid)
        self.last_bbox = bbox.copy()


class UnknownFaceClusterStore:
    """Dual-signal unknown face clustering (embedding similarity + bbox IoU)."""

    def __init__(self,
                 embedding_threshold: float = 0.45,
                 iou_threshold: float = 0.5):
        self.clusters: list[UnknownFaceCluster] = []
        self.embedding_threshold = embedding_threshold
        self.iou_threshold = iou_threshold

    def add(self, embedding: np.ndarray, bbox: np.ndarray) -> int:
        """Add unknown face. Returns cluster index.

        Dual-signal:
        1. bbox IoU >= threshold → merge (spatial continuity)
        2. embedding similarity >= threshold → merge (same person)
        3. Neither → new cluster (new distinct person)
        """
        if not self.clusters:
            self.clusters.append(UnknownFaceCluster(embedding, bbox))
            return 0

        best_iou_idx, best_iou = -1, 0.0
        best_sim_idx, best_sim = -1, 0.0

        for i, cluster in enumerate(self.clusters):
            iou = self._compute_iou(bbox, cluster.last_bbox)
            if iou > best_iou:
                best_iou, best_iou_idx = iou, i

            sim = float(np.dot(embedding, cluster.centroid))
            if sim > best_sim:
                best_sim, best_sim_idx = sim, i

        if best_iou >= self.iou_threshold:
            self.clusters[best_iou_idx].merge(embedding, bbox)
            return best_iou_idx
        elif best_sim >= self.embedding_threshold:
            self.clusters[best_sim_idx].merge(embedding, bbox)
            return best_sim_idx
        else:
            self.clusters.append(UnknownFaceCluster(embedding, bbox))
            return len(self.clusters) - 1

    def __len__(self):
        return len(self.clusters)

    @staticmethod
    def _compute_iou(box1, box2) -> float:
        x1 = max(box1[0], box2[0]); y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2]); y2 = min(box1[3], box2[3])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        union = area1 + area2 - inter
        return inter / union if union > 0 else 0.0
```

---

## 8. Security Handler Chain (`detection/security_handlers.py`)

Per-frame handler chain. **Hailo path only.** All handlers run on every frame in priority order.

```python
@dataclass
class HandlerResult:
    handler_name: str       # "UC1", "UC2", "UC5-BLOCKLIST", etc.
    action: str             # "unlock", "alert", "log"
    payload: dict           # IoT message payload
    topic_suffix: str       # "member_detected", "tailgating_alert", etc.
    priority_level: str = "normal"  # "normal" or "HIGH"


class SecurityHandlerChain:
    """Evaluates all security use cases for each face on each frame.

    Priority order (all run on every frame):
      5  — UC5-BLOCKLIST
     10  — UC1: Active member identification + unlock
     20  — UC5-INACTIVE
     30  — UC2: Tailgating (requires unlocked=true, unknown face)
     40  — UC3: Unknown face logging

    UC4 (group size) runs at session end via check_group_size().
    UC8 operates at session lifecycle level, not per-frame.
    """

    def __init__(self, member_store: MemberStore, config: dict):
        self.member_store = member_store
        self.config = config

    def process_face(self, face: FaceResult, session: DetectionSession,
                     cam_info: dict) -> list[HandlerResult]:
        """Evaluate all handlers for a single detected face."""
        results = []

        category, member, similarity = self.member_store.find_best_match(
            face.embedding,
            threshold=self.config.get('FACE_RECOG_THRESHOLD', 0.45)
        )

        if category == MemberCategory.BLOCKLIST:
            results.extend(self._handle_blocklist(face, member, similarity, session, cam_info))

        elif category == MemberCategory.ACTIVE:
            results.extend(self._handle_active_member(face, member, similarity, session, cam_info))

        elif category == MemberCategory.INACTIVE:
            results.extend(self._handle_inactive_member(face, member, similarity, session, cam_info))

        elif category == MemberCategory.STAFF:
            self._handle_staff(face, member, similarity, session)

        else:  # No match in any category
            results.extend(self._handle_unknown_face(face, session, cam_info))

        return results

    # --- Priority 5: UC5-BLOCKLIST ---
    def _handle_blocklist(self, face, member, similarity, session, cam_info):
        member_id = self._member_id(member)
        session.record_known_member(
            member_id, MemberCategory.BLOCKLIST, member, similarity,
            member.get('reservationCode', ''))

        if self.config.get('BLOCKLIST_PREVENTS_UNLOCK', True):
            session.block_further_unlocks = True

        return [HandlerResult(
            handler_name="UC5-BLOCKLIST", action="alert",
            topic_suffix="non_active_member_alert", priority_level="HIGH",
            payload={
                "sub_type": "BLOCKLIST",
                "cameraIp": cam_info.get('cam_ip'),
                "memberInfo": {"fullName": member.get('fullName', ''),
                               "reservationCode": member.get('reservationCode', '')},
                "blocklist_reason": member.get('_reservation', {}).get('blocklistReason', ''),
                "similarity": similarity,
            }
        )]

    # --- Priority 10: UC1 ---
    def _handle_active_member(self, face, member, similarity, session, cam_info):
        member_id = self._member_id(member)
        reservation_code = member.get('reservationCode', '')

        session.record_known_member(
            member_id, MemberCategory.ACTIVE, member, similarity, reservation_code)
        session.active_member_matched = True
        session.matched_active_member_reservation = reservation_code

        # Try to unlock each clicked lock
        locks_unlocked = []
        for lock_id in list(session.clicked_locks):
            if session.try_unlock(lock_id, member_id):
                locks_unlocked.append(lock_id)

        return [HandlerResult(
            handler_name="UC1",
            action="unlock" if locks_unlocked else "log",
            topic_suffix="member_detected",
            payload={
                "cameraIp": cam_info.get('cam_ip'),
                "fullName": member.get('fullName', ''),
                "similarity": similarity,
                "reservationCode": reservation_code,
                "memberNo": member.get('memberNo'),
                "clickedLocks": locks_unlocked,
                "blocked": session.block_further_unlocks,
            }
        )]

    # --- Priority 20: UC5-INACTIVE ---
    def _handle_inactive_member(self, face, member, similarity, session, cam_info):
        member_id = self._member_id(member)
        session.record_known_member(
            member_id, MemberCategory.INACTIVE, member, similarity,
            member.get('reservationCode', ''))

        return [HandlerResult(
            handler_name="UC5-INACTIVE", action="alert",
            topic_suffix="non_active_member_alert", priority_level="normal",
            payload={
                "sub_type": "INACTIVE",
                "cameraIp": cam_info.get('cam_ip'),
                "memberInfo": {"fullName": member.get('fullName', ''),
                               "reservationCode": member.get('reservationCode', '')},
                "checkoutDate": member.get('_reservation', {}).get('checkOutDate', ''),
                "similarity": similarity,
            }
        )]

    # --- Priority 30+40: UC2 + UC3 (unknown face) ---
    def _handle_unknown_face(self, face, session, cam_info):
        results = []

        # UC3: Log unknown face + cluster
        if self.config.get('ENABLE_UNKNOWN_FACE_LOGGING', True):
            cluster_idx = session.unknown_face_clusters.add(face.embedding, face.bbox)
            results.append(HandlerResult(
                handler_name="UC3", action="log",
                topic_suffix="unknown_face_detected", priority_level="low",
                payload={
                    "cameraIp": cam_info.get('cam_ip'),
                    "cluster_id": cluster_idx,
                    "total_clusters": len(session.unknown_face_clusters),
                }
            ))

        # UC2: Tailgating (unknown face + door already unlocked)
        if session.unlocked and self.config.get('ENABLE_TAILGATING_DETECTION', True):
            results.append(HandlerResult(
                handler_name="UC2", action="alert",
                topic_suffix="tailgating_alert",
                payload={
                    "cameraIp": cam_info.get('cam_ip'),
                    "authorized_member": session.unlock_authorized_member,
                    "unlock_timestamp": session.unlock_timestamp.isoformat() + 'Z'
                        if session.unlock_timestamp else None,
                }
            ))

        return results

    def _handle_staff(self, face, member, similarity, session):
        member_id = self._member_id(member)
        session.record_known_member(
            member_id, MemberCategory.STAFF, member, similarity,
            member.get('reservationCode', ''))

    # --- Session-end: UC4 ---
    def check_group_size(self, session, cam_info) -> HandlerResult | None:
        """UC4: Group size validation at session end."""
        if not session.active_member_matched:
            return None
        if not self.config.get('ENABLE_GROUP_VALIDATION', True):
            return None

        expected = self.member_store.get_member_count_for_reservation(
            session.matched_active_member_reservation)
        if expected is None:
            return None

        actual = session.distinct_face_count
        if actual > expected:
            return HandlerResult(
                handler_name="UC4", action="alert",
                topic_suffix="group_size_mismatch",
                payload={
                    "cameraIp": cam_info.get('cam_ip'),
                    "distinct_face_count": actual,
                    "known_count": len(session.known_members),
                    "unknown_count": len(session.unknown_face_clusters),
                    "memberCount": expected,
                    "matched_members": list(session.known_members.keys()),
                    "reservationCode": session.matched_active_member_reservation,
                }
            )
        return None

    @staticmethod
    def _member_id(member: dict) -> str:
        return f"{member.get('reservationCode', '')}-{member.get('memberNo', 0)}"
```

---

## 9. UC8: YOLOv8n Session Lifecycle (Hailo Only)

UC8 operates **outside** the per-frame detection loop at the session lifecycle level. Only available when Hailo backend is active (YOLOv8n requires Hailo).

### 9.1 Gate (Session Start)

**Location**: `py_handler.py` → `handle_notification()`

```python
def handle_notification(cam_ip, utc_time, is_motion_value):
    """ONVIF motion handler — UC8 gate added for Hailo path."""
    if not is_motion_value:
        return

    # ... existing camera/gstreamer lookup ...

    # UC8 GATE: YOLOv8n person detection (Hailo only)
    if person_backend is not None:
        frame = thread_gstreamer.grab_detecting_frame()
        if frame is not None:
            persons = person_backend.detect_persons(frame)
            if not persons:
                logger.info(f"UC8 GATE: {cam_ip} — no person, skipping")
                return  # No session, no recording

    # Proceed with recording + detection (existing flow)
    ...
```

### 9.2 Extend (Timer Expiry)

**Location**: `gstreamer_threading.py` — timer callback (Hailo path only)

```python
def _on_timer_expiry(self):
    """Timer expiry — UC8 dual-signal extend check."""
    if self._person_backend is None:
        # InsightFace path — end session normally
        self.stop_feeding()
        return

    # Signal 1: Motion recency
    motion_recent = False
    if self._session and self._session.last_motion_timestamp:
        elapsed = (datetime.utcnow() - self._session.last_motion_timestamp).total_seconds()
        motion_recent = elapsed < float(os.environ.get('MOTION_RECENCY_SEC', '5'))

    if not motion_recent:
        self.stop_feeding()
        return

    # Signal 2: YOLOv8n person detection
    frame = self.grab_detecting_frame()
    if frame is None:
        self.stop_feeding()
        return

    persons = self._person_backend.detect_persons(frame)
    if not persons:
        self.stop_feeding()
        return

    # Both signals present — extend
    self.extend_timer(float(os.environ.get('TIMER_DETECT', '10')))
```

### 9.3 New GStreamer Method

```python
def grab_detecting_frame(self) -> np.ndarray | None:
    """Grab a single decoded frame from the detecting buffer.

    Used by UC8 gate (before session start) and extend (at timer expiry).
    """
    if self.detecting_buffer:
        return self.detecting_buffer[-1]
    return None
```

---

## 10. py_handler.py Orchestration

### 10.1 Initialization (Backend-Dependent Path)

```python
# Global state
face_backend = None
person_backend = None
use_full_path = False
member_store = None      # Full path only (Hailo, future Jetson)
thread_detector = None   # SimpleRecognition or FaceProcessor

def init_face_detector():
    """Initialize the appropriate detection path based on backend."""
    global face_backend, person_backend, use_full_path, member_store, thread_detector

    face_backend, person_backend, use_full_path = detect_face_backend()

    if use_full_path:
        # Full path (Hailo/Jetson): FaceProcessor + MemberStore + SecurityHandlerChain
        member_store = MemberStore()
        fetch_all_members()  # Load all 4 categories

        config = {
            'FACE_RECOG_THRESHOLD': float(os.environ.get('FACE_RECOG_THRESHOLD', '0.45')),
            'BLOCKLIST_PREVENTS_UNLOCK': os.environ.get('BLOCKLIST_PREVENTS_UNLOCK', 'true') == 'true',
            'ENABLE_TAILGATING_DETECTION': os.environ.get('ENABLE_TAILGATING_DETECTION', 'true') == 'true',
            'ENABLE_UNKNOWN_FACE_LOGGING': os.environ.get('ENABLE_UNKNOWN_FACE_LOGGING', 'true') == 'true',
            'ENABLE_GROUP_VALIDATION': os.environ.get('ENABLE_GROUP_VALIDATION', 'true') == 'true',
        }

        handler_chain = SecurityHandlerChain(member_store, config)
        thread_detector = FaceProcessor(
            face_backend=face_backend,
            member_store=member_store,
            cam_queue=cam_queue,
            output_queue=scanner_output_queue,
            handler_chain=handler_chain,
            stop_event=threading.Event(),
            config=config,
        )
    else:
        # InsightFace path: SimpleRecognition (UC1 only)
        active_members = fetch_members()  # Existing function, ACTIVE only
        thread_detector = SimpleRecognition(
            face_backend=face_backend,
            active_members=active_members,
            scanner_output_queue=scanner_output_queue,
            cam_queue=cam_queue,
        )

    thread_detector.start()
```

### 10.2 Output Processing (Backend-Dependent)

```python
def fetch_scanner_output_queue():
    """Process detection results — dispatches based on message type."""
    while True:
        message = scanner_output_queue.get()
        msg_type = message.get('type')

        if msg_type == 'member_detected':
            # InsightFace path — existing processing
            _process_member_detected_legacy(message)

        elif msg_type == 'handler_results':
            # Hailo path — new handler results
            _process_handler_results(message)

        elif msg_type == 'immediate_unlock':
            # Hailo path — Decision 30 immediate unlock
            _process_immediate_unlock(message)

        elif msg_type == 'video_clipped':
            _process_video_clipped(message)
```

### 10.3 Clicked Event Handling (Backend-Dependent)

```python
def trigger_face_detection(cam_ip, lock_asset_id=None):
    """Start or extend detection session."""
    thread_gstreamer = thread_gstreamers.get(cam_ip)
    if not thread_gstreamer:
        return

    if thread_gstreamer.is_feeding:
        # Session already running
        if lock_asset_id:
            thread_gstreamer.extend_timer(float(os.environ.get('TIMER_DETECT', '10')))
            if use_full_path:
                # Hailo path: update session lock + Decision 30
                thread_detector.update_session_lock(cam_ip, lock_asset_id)
            # InsightFace path: existing trigger_lock_context logic
        else:
            # ONVIF re-trigger — existing extension logic
            ...
    else:
        # Start new session
        if use_full_path:
            fetch_all_members()
        else:
            active_members = fetch_members()
            thread_detector.active_members = active_members

        thread_gstreamer.feed_detecting(float(os.environ.get('TIMER_DETECT', '10')))

        if lock_asset_id and use_full_path:
            _pending_clicked_locks.setdefault(cam_ip, set()).add(lock_asset_id)
```

---

## 11. IoT Topics

### 11.1 Topic Summary

| Topic | UC | Backend | Priority |
|-------|------|---------|----------|
| `gocheckin/{thing}/member_detected` | UC1 | Both | Normal |
| `gocheckin/{thing}/tailgating_alert` | UC2 | Hailo | Normal |
| `gocheckin/{thing}/unknown_face_detected` | UC3 | Hailo | Low |
| `gocheckin/{thing}/group_size_mismatch` | UC4 | Hailo | Normal |
| `gocheckin/{thing}/non_active_member_alert` | UC5 | Hailo | Normal/HIGH |

UC8 has no IoT topic (session lifecycle control only).

### 11.2 Payload Changes

**`member_detected` (Hailo path)**:
- `onvifTriggered` — **removed**
- `occupancyTriggeredLocks` — **renamed** to `clickedLocks`
- `blocked` — **new** (true if BLOCKLIST prevented unlock)

**`member_detected` (InsightFace path)**: Unchanged from current.

---

## 12. Configuration

### 12.1 New Environment Variables (Hailo Path Only)

```bash
# UC2: Tailgating
ENABLE_TAILGATING_DETECTION=true
TAILGATE_WINDOW_SEC=10

# UC3: Unknown Face Logging
ENABLE_UNKNOWN_FACE_LOGGING=true

# UC4: Group Size Validation
ENABLE_GROUP_VALIDATION=true
UNKNOWN_FACE_CLUSTER_THRESHOLD=0.45
FACE_IOU_THRESHOLD=0.5

# UC5: Non-Active Member
ENABLE_NON_ACTIVE_MEMBER_ALERT=true
INACTIVE_MEMBER_DAYS_BACK=30
BLOCKLIST_PREVENTS_UNLOCK=true

# UC8: Human Body Detection
YOLO_DETECT_THRESHOLD=0.5
MOTION_RECENCY_SEC=5
```

### 12.2 Existing Variables (Both Paths)

```bash
FACE_DETECT_THRESHOLD=0.3        # SCRFD detection confidence
FACE_RECOG_THRESHOLD=0.45        # ArcFace recognition threshold
TIMER_DETECT=10                  # Session duration (seconds)
INFERENCE_BACKEND=auto           # hailo | insightface | auto
```

---

## 13. Threading Model

```
┌────────────────────────────────────────────────────────────────┐
│ Main Thread (py_handler)                                        │
│   HTTP server, IoT dispatch, timer scheduling                   │
│   UC8 gate check (Hailo only)                                  │
│   Backend selection → use_full_path flag                             │
└────────────────────────────────────────────────────────────────┘
        │
        ├── GStreamer threads (1 per camera)
        │     StreamCapture: RTSP → buffers
        │     UC8 extend (Hailo only): timer callback
        │
        ├── Detection thread (1 total, backend-dependent)
        │     ├── [InsightFace] SimpleRecognition: UC1, stop-on-match
        │     └── [Hailo] FaceProcessor: UC1-UC5, continuous loop
        │
        ├── Output processor thread
        │     fetch_scanner_output_queue: IoT publish, S3 upload
        │
        └── ONVIF handler threads (daemon, per-notification)
```

### 13.1 Thread Safety

| Shared State | Writers | Readers | Protection |
|-------------|---------|---------|------------|
| `cam_queue` | GStreamer threads | Detection thread | `Queue` |
| `scanner_output_queue` | Detection thread | Output processor | `Queue` |
| `_sessions` (full path) | FaceProcessor only | FaceProcessor only | Single-thread |
| `member_store` (full path) | Main thread (fetch) | FaceProcessor (read) | `threading.Lock` on update |

---

## 14. Data Flow (Full Path)

```
ONVIF Motion
    │
    ▼
[UC8 GATE] grab frame → YOLOv8n → person?
    │                              │
    NO → skip                     YES
                                   │
                                   ▼
                        Start recording + detection session
                        fetch_all_members() → MemberStore (4 categories)
                        StreamCapture.feed_detecting()
                                   │
                                   ▼
                        ┌───────────────────────────────────┐
                        │ cam_queue (Frame, img, cam_info)  │
                        └───────────────┬───────────────────┘
                                        │
                                        ▼
                        ┌───────────────────────────────────────┐
                        │ FaceProcessor Thread (continuous)      │
                        │                                       │
                        │ HailoFaceBackend.get(img) → faces     │
                        │                                       │
                        │ For EACH face, EVERY frame:           │
                        │   MemberStore.find_best_match()       │
                        │   ↓                                   │
                        │   SecurityHandlerChain.process_face() │
                        │     BLOCKLIST → UC5 alert (HIGH)      │
                        │                 block_further_unlocks  │
                        │     ACTIVE   → UC1 log/unlock         │
                        │     INACTIVE → UC5 alert              │
                        │     STAFF    → log only               │
                        │     NO MATCH → UC3 log + cluster      │
                        │                UC2 if unlocked=true   │
                        │                                       │
                        │ Session state updated per frame       │
                        └───────────────┬───────────────────────┘
                                        │
         CLICKED EVENT ─────────────────┤
         update_session_lock()          │
         Decision 30: immediate unlock  │
                                        │
                                        ▼
                        ┌───────────────────────────────────┐
                        │ scanner_output_queue               │
                        └───────────────┬───────────────────┘
                                        │
                                        ▼
                        ┌───────────────────────────────────┐
                        │ Output Processor                   │
                        │   Snapshot → S3                    │
                        │   Publish → IoT topics             │
                        └───────────────────────────────────┘

TIMER EXPIRY
    │
    ▼
[UC8 EXTEND] dual-signal: motion recent + person present?
    ├── BOTH YES → extend_timer()
    └── EITHER NO → stop_feeding() → SESSION_END
                        │
                        ▼
                    FaceProcessor._end_session()
                        ├── [UC4] group_size_mismatch check
                        └── Clean up session
```

---

## 15. Implementation Order

### Phase 1: Inference Extraction (NFR2)

Extract inference logic from mixed files. No behavior changes.

| Step | Task | From | To |
|------|------|------|-----|
| 1.1 | Create `inference/` package + `FaceResult` + `PersonResult` | — | `inference/__init__.py`, `inference/face_result.py` |
| 1.2 | Create abstract base classes (`FaceBackend`, `PersonBackend`) | — | `inference/base.py` |
| 1.3 | Extract InsightFace inference, implement `FaceBackend` | `face_recognition.py` | `inference/insightface_backend.py` |
| 1.4 | Extract Hailo inference (HailoFaceApp), implement `FaceBackend` | `face_recognition_hailo.py` | `inference/hailo_face_backend.py` |
| 1.5 | **Verify**: Both backends work via common `FaceBackend.get()` interface | — | — |

### Phase 2: Business Logic Extraction

Extract and split business logic into two paths.

| Step | Task | From | To |
|------|------|------|-----|
| 2.1 | Create `detection/` package | — | `detection/__init__.py` |
| 2.2 | Move InsightFace `FaceRecognition` | `face_recognition.py` | `detection/face_recognition_simple.py` |
| 2.3 | Update `py_handler.py` to use new module paths | `py_handler.py` | `py_handler.py` |
| 2.4 | Remove `face_recognition.py` and `face_recognition_hailo.py` | — | — |
| 2.5 | **Verify**: UC1 works on both backends (existing behavior) | — | — |

### Phase 3: Hailo Continuous Loop + MemberStore

Foundation for UC2-UC5.

| Step | Task | Files |
|------|------|-------|
| 3.1 | Create `MemberStore` with multi-category support | `detection/member_store.py` |
| 3.2 | Create `DetectionSession` dataclass | `detection/session_state.py` |
| 3.3 | Create `FaceProcessor` with continuous loop (UC1 only initially) | `detection/face_processor.py` |
| 3.4 | Add `fetch_all_members()` to `py_handler.py` | `py_handler.py` |
| 3.5 | Wire Hailo path to `FaceProcessor` in `py_handler.py` | `py_handler.py` |
| 3.6 | **Verify**: Hailo UC1 works with continuous loop (no stop-on-match) | — |

### Phase 4: UC5 — Non-Active Member Alert

| Step | Task | Files |
|------|------|-------|
| 4.1 | Create `SecurityHandlerChain` with UC1 + UC5 handlers | `detection/security_handlers.py` |
| 4.2 | Wire into `FaceProcessor` | `detection/face_processor.py` |
| 4.3 | Add `non_active_member_alert` IoT publishing | `py_handler.py` |
| 4.4 | **Verify**: BLOCKLIST blocks unlock, INACTIVE alerts | — |

### Phase 5: UC3 — Unknown Face Logging

| Step | Task | Files |
|------|------|-------|
| 5.1 | Create `UnknownFaceClusterStore` | `detection/unknown_face_cluster.py` |
| 5.2 | Add UC3 handler to chain | `detection/security_handlers.py` |
| 5.3 | Add `unknown_face_detected` IoT publishing | `py_handler.py` |
| 5.4 | **Verify**: Unknown faces logged with cluster IDs | — |

### Phase 6: UC2 — Tailgating Detection

| Step | Task | Files |
|------|------|-------|
| 6.1 | Add UC2 handler (unknown face + `unlocked=true`) | `detection/security_handlers.py` |
| 6.2 | Add `tailgating_alert` IoT publishing | `py_handler.py` |
| 6.3 | **Verify**: UC2 fires only after unlock + unknown face | — |

### Phase 7: UC4 — Group Size Validation

| Step | Task | Files |
|------|------|-------|
| 7.1 | Add `check_group_size()` to handler chain | `detection/security_handlers.py` |
| 7.2 | Wire session-end processing | `detection/face_processor.py` |
| 7.3 | Add `group_size_mismatch` IoT publishing | `py_handler.py` |
| 7.4 | **Verify**: Correct counting with known + unknown clusters | — |

### Phase 8: Clicked Event Refactor (Full Path)

| Step | Task | Files |
|------|------|-------|
| 8.1 | Implement `update_session_lock()` with Decision 30 | `detection/face_processor.py` |
| 8.2 | Refactor `trigger_face_detection()` for full path | `py_handler.py` |
| 8.3 | Update `member_detected` payload (`clickedLocks`) | `py_handler.py` |
| 8.4 | **Verify**: Immediate unlock on clicked if already matched | — |

### Phase 9: UC8 — Human Body Detection (Full Path Only)

| Step | Task | Files |
|------|------|-------|
| 9.1 | Create `HailoYOLOv8nBackend` (implements `PersonBackend` ABC) | `inference/hailo_yolo_backend.py` |
| 9.2 | Add UC8 gate in `handle_notification()` | `py_handler.py` |
| 9.3 | Add `grab_detecting_frame()` to `StreamCapture` | `gstreamer_threading.py` |
| 9.4 | Add UC8 dual-signal extend at timer expiry | `gstreamer_threading.py` |
| 9.5 | **Verify**: False ONVIF triggers skipped, sessions extend correctly | — |

### Phase 10: Integration Testing

| Test | Scope |
|------|-------|
| 10.1 | InsightFace: UC1 unchanged behavior (stop-on-match) |
| 10.2 | Hailo P1 (no lock): UC1 log, UC3, UC5 |
| 10.3 | Hailo P2 (with lock): full UC1-UC5 flow |
| 10.4 | Hailo UC8 gate filters false ONVIF |
| 10.5 | Hailo UC8 extend with dual-signal |
| 10.6 | Hailo Decision 30 (immediate unlock on clicked) |
| 10.7 | Hailo BLOCKLIST blocks unlock |
| 10.8 | Hailo UC4 group size with masked faces |

---

## 16. Files Changed Summary

| File | Change | Description |
|------|--------|-------------|
| `inference/__init__.py` | NEW | Package init |
| `inference/base.py` | NEW | Abstract base classes: `FaceBackend`, `PersonBackend` |
| `inference/face_result.py` | NEW | `FaceResult`, `PersonResult` dataclasses |
| `inference/insightface_backend.py` | NEW | `InsightFaceBackend(FaceBackend)` — extracted from `face_recognition.py` |
| `inference/hailo_face_backend.py` | NEW | `HailoFaceBackend(FaceBackend)` — extracted from `face_recognition_hailo.py` |
| `inference/hailo_yolo_backend.py` | NEW | `HailoYOLOv8nBackend(PersonBackend)` — UC8 |
| `inference/jetson_backend.py` | FUTURE | `JetsonFaceBackend(FaceBackend)` + `JetsonYOLOv8nBackend(PersonBackend)` — placeholder |
| `detection/__init__.py` | NEW | Package init |
| `detection/face_recognition_simple.py` | NEW | `SimpleRecognition` — extracted from `face_recognition.py`, UC1 only |
| `detection/face_processor.py` | NEW | `FaceProcessor` — full path, UC1-UC5 continuous loop |
| `detection/session_state.py` | NEW | `DetectionSession` dataclass |
| `detection/member_store.py` | NEW | `MemberStore` multi-category |
| `detection/security_handlers.py` | NEW | `SecurityHandlerChain` |
| `detection/unknown_face_cluster.py` | NEW | `UnknownFaceClusterStore` |
| `py_handler.py` | MODIFIED | Backend selection, path branching, UC8 gate, output processing |
| `gstreamer_threading.py` | MODIFIED | `grab_detecting_frame()`, UC8 extend callback |
| `face_recognition.py` | REMOVED | Replaced by `inference/insightface_backend.py` + `detection/face_recognition_simple.py` |
| `face_recognition_hailo.py` | REMOVED | Replaced by `inference/hailo_face_backend.py` + `detection/face_processor.py` |
| `function.conf` | MODIFIED | New config variables |

---

## 17. Risk Mitigations

| Risk | Mitigation |
|------|------------|
| Simple path regression | Phase 2 ends with UC1 verification on InsightFace before touching full path |
| Full path UC1 regression | Phase 3 verifies Hailo UC1 with continuous loop before adding UC2-UC5 |
| Multi-category DDB scan too slow | Single scan, split in memory |
| PersonBackend not available | UC8 gracefully disabled (`person_backend = None`) |
| Simple path accidentally gets UC2-UC5 | `use_full_path` flag gates all new UC code paths |
| New backend breaks interface | Abstract base classes (`FaceBackend`, `PersonBackend`) enforce the contract at instantiation |
| Thread contention on MemberStore | Lock-guarded updates; read path is lock-free |
