# Face Detection Security Use-Cases Document

## Purpose

Document all security use-cases for face/human detection system in `ggp-func-py-gocheckin`.

## Facility Types Supported

- Vacation rentals (Airbnb-style short-term)
- Hotels / Serviced apartments
- Office buildings

## Focus Area

Security use-cases prioritized over hospitality/analytics use-cases.

---

# Use-Case Catalog

## UC1: Authorized Member Identification [CURRENT]

**Status**: Implemented (current behavior)

**Scenario**: A guest with a valid reservation approaches the door. The system recognizes their face and unlocks the door automatically.

**Trigger**: Motion detected at camera → face detection starts

**Input**:
- Camera frame with face
- ACTIVE member database (guests with current reservations)

**Logic**:
- Detect faces in frame
- Compare each face against ACTIVE member embeddings
- If similarity > threshold → MATCH

**Output**:
- Unlock door
- Publish `member_detected` to IoT
- Save annotated snapshot to S3
- Log check-in event

**Data Source**:
- TBL_RESERVATION (active reservations)
- TBL_MEMBER (face embeddings)

**Key Behaviors**:
- Once identified, stop detection for this session
- Only one member needs to match for unlock

---

## UC2: Tailgating Detection [NEW]

**Status**: To be implemented

**Scenario**: After an authorized guest unlocks the door, an unauthorized person follows them through before the door closes.

**Trigger**: Continue monitoring for N seconds after door unlock

**Input**:
- Camera frames during tailgate window
- ACTIVE member database
- Knowledge of recent unlock event

**Logic**:
- Door unlocked for Member A at T=0
- Continue detecting faces for TAILGATE_WINDOW_SEC seconds
- If another face detected that doesn't match ANY active member → TAILGATING

**Output**:
- Publish `tailgating_alert` to IoT
- Include: cam_ip, authorized_member, timestamp, snapshot of unknown face
- Do NOT prevent access (door already unlocked)

**Configuration**:
- `TAILGATE_WINDOW_SEC`: Duration to monitor after unlock (default: 10s)

**Key Behaviors**:
- Does not prevent access (too late)
- Alert is informational for security review
- Multiple unauthorized faces = multiple alerts

---

## UC3: Unknown Face Logging [NEW]

**Status**: To be implemented

**Scenario**: A completely unknown person is detected - not matching any database (active, inactive, staff, or blocklist).

**Trigger**: Face detected that doesn't match ANY database

**Input**:
- Camera frame with face
- All member databases: ACTIVE, INACTIVE, STAFF, BLOCKLIST (no match anywhere)

**Logic**:
- Detect face and extract embedding
- Compare against ALL categories: ACTIVE, INACTIVE, STAFF, BLOCKLIST
- All categories are loaded from TBL_RESERVATION with different filters:
  - ACTIVE: reservations with current date in [checkIn, checkOut] range
  - INACTIVE: reservations with checkOut in past N days
  - STAFF: reservations with staff flag
  - BLOCKLIST: reservations with blocklist flag
- If no match in any category → unknown face

**Output**:
- Save snapshot to S3 (temporary local → upload → remove, same pattern as `member_detected`)
- Publish `unknown_face_detected` to IoT (low priority)
- Include: cam_ip, timestamp, snapshot S3 key

**Note**: Embedding storage is not needed for the initial version. Can be added later for UC7 (Loitering Detection) to enable same-face matching over time.

**Purpose**:
- Build database of unknown visitors
- Enable pattern analysis over time
- Support future loitering detection (UC7)

---

## UC4: Group Size & Person Count Validation [NEW]

**Status**: To be implemented

**Scenario**: After an authorized member is identified (UC1), the system counts human bodies in the frame and compares against the reservation's expected guest count. This covers both "too many people" and "people hiding their faces" scenarios in a single check.

**Trigger**: After UC1 identifies at least one authorized face

**Input**:
- Camera frame
- YOLOv8n detection output (COCO person class)
- SCRFD face detection output (face count from recognition pipeline)
- Reservation `memberCount` field from TBL_RESERVATION

**Logic**:
- UC1 matches at least one authorized member → get reservation's `memberCount`
- YOLOv8n detects person bodies (COCO class 0) → `human_body_count`
- SCRFD face count from recognition pipeline → `face_count`
- Compare `human_body_count` vs `memberCount`
- If `human_body_count > memberCount` → group size mismatch (extra people)
- If `human_body_count > face_count` → some people hiding faces

**Output**:
- Publish single `group_size_mismatch` alert to IoT
- Include: human_count, face_count, memberCount, matched_members, snapshot

**Models**:
- `yolov8n` (COCO 80-class, filter to person class 0) — pre-compiled HEF available for both Hailo-8 and Hailo-8L. 202 FPS on Hailo-8L at 640x640. Used for person body counting only.
- SCRFD + ArcFace — used separately for face detection/recognition (UC1/UC3). SCRFD face count is reused here for `face_count`. Both have pre-compiled Hailo-8L HEFs.

**Purpose**:
- Detect unauthorized additional guests (capacity enforcement)
- Detect group where some members are deliberately avoiding face detection
- Single combined alert covers both scenarios (replaces old UC4 + UC5)

---

## UC5: Inactive Member Alert [FUTURE]

**Status**: Future implementation

**Scenario**: A guest who previously stayed (reservation ended) returns and tries to access the property. They should not have access anymore.

**Trigger**: Face detected that doesn't match active members

**Input**:
- Camera frame with face
- ACTIVE member database (no match)
- INACTIVE member database (checked-out guests)

**Logic**:
- Face does NOT match any ACTIVE member
- Face DOES match an INACTIVE member (past guest)
- This is a former guest trying to access

**Output**:
- Publish `inactive_member_alert` to IoT
- Include: cam_ip, member_info (name, original reservation), checkout_date, similarity
- Do NOT unlock door

**Data Source**:
- TBL_RESERVATION (checkOut < today, last N days)
- TBL_MEMBER (face embeddings)

**Configuration**:
- `INACTIVE_MEMBER_DAYS_BACK`: How far back to check (default: 30 days)

**Key Behaviors**:
- Does not unlock door
- Alert includes original stay information
- Useful for property owners to know who is trying to return

---

## UC6: Blocklist Detection [FUTURE]

**Status**: Future implementation

**Scenario**: A banned individual (previous problem guest, known troublemaker) attempts to access the property.

**Trigger**: Any face detection

**Input**:
- Camera frame with face
- BLOCKLIST member database

**Logic**:
- Face matches a BLOCKLIST member
- Blocklist takes priority over all other checks

**Output**:
- Publish `blocklist_alert` to IoT (HIGH priority)
- Include: cam_ip, member_info, blocklist_reason, snapshot
- Do NOT unlock door

**Data Source**:
- TBL_RESERVATION (type=blocklist) - pseudo-reservations for blocklist entries
- TBL_MEMBER (face embeddings with blocklist reason)

**Key Behaviors**:
- Highest priority - checked before member identification
- Door NEVER unlocks for blocklisted individuals
- Alert should be treated as urgent by property owner

---

## UC7: Loitering Detection [FUTURE]

**Status**: Future implementation

**Scenario**: The same unknown person is seen multiple times over a period - potentially casing the property.

**Trigger**: Same unknown face detected multiple times

**Input**:
- Current detection
- Unknown face history (from UC3)

**Logic**:
- Unknown face detected at T=0
- Same face (high similarity) detected again at T+N minutes
- Pattern indicates potential loitering/casing

**Output**:
- Publish `loitering_alert` to IoT
- Include: first_seen, last_seen, detection_count, all locations

**Requires**: UC3 (Unknown Face Logging) for history tracking. Requires embedding storage in UC3 (not included in initial version).

---

## UC8: After-Hours Access Attempt [FUTURE]

**Status**: Future implementation

**Scenario**: Access attempt during quiet hours (e.g., 11 PM - 6 AM).

**Trigger**: Face detection during configured quiet hours

**Input**:
- Camera frame with face
- Current time
- Quiet hours configuration

**Logic**:
- Face detected between QUIET_HOURS_START and QUIET_HOURS_END
- May or may not unlock depending on policy

**Output**:
- Publish `after_hours_access` to IoT
- Include: cam_ip, member_info (if identified), timestamp

**Configuration**:
- `QUIET_HOURS_START`: e.g., 23:00
- `QUIET_HOURS_END`: e.g., 06:00
- `QUIET_HOURS_UNLOCK`: true/false (policy decision)

---

## UC9: Human Without Face Detection [FUTURE]

**Status**: Future implementation

**Scenario**: A person is detected but their face is not visible - possibly hiding face intentionally.

**Trigger**: Human body detected but no face detected

**Input**:
- Camera frame
- Human detection model output (body bboxes)
- Face detection model output (face bboxes)

**Logic**:
- YOLOv8n detects human body (COCO person class 0)
- SCRFD face detection returns no faces
- Person is hiding face, turned away, or wearing mask

**Output**:
- Publish `faceless_human_alert` to IoT
- Include: cam_ip, timestamp, body_bbox, snapshot
- Do NOT unlock door

**Model Required**: YOLOv8n (COCO person class) — same model as UC4, pre-compiled HEF available for Hailo-8 and Hailo-8L

**Key Behaviors**:
- Suspicious behavior indicator
- Does not unlock without face verification
- Alert for security review

---

# Implementation Scope

## Initial Version
- UC1: Authorized Member Identification (refactor existing)
- UC2: Tailgating Detection
- UC3: Unknown Face Logging
- UC4: Group Size & Person Count Validation

## Future Version
- UC5: Inactive Member Alert
- UC6: Blocklist Detection
- UC7: Loitering Detection
- UC8: After-Hours Access
- UC9: Human Without Face Detection

---

# Data Sources Summary

| Category | Source | Use-Cases |
|----------|--------|-----------|
| ACTIVE | TBL_RESERVATION (active dates) + TBL_MEMBER | UC1 (unlock), UC2, UC3, UC4 |
| INACTIVE | TBL_RESERVATION (past N days) + TBL_MEMBER | UC3, UC5 (alert) |
| STAFF | TBL_RESERVATION (staff flag) + TBL_MEMBER | UC3 |
| BLOCKLIST | TBL_RESERVATION (blocklist flag) + TBL_MEMBER | UC3, UC6 (block) |
| UNKNOWN | Runtime storage (no pre-existing data) | UC3, UC7 |

---

# IoT Topics Summary

| Topic | Use-Case | Priority | Scope |
|-------|----------|----------|-------|
| `gocheckin/{thing}/member_detected` | UC1 | Normal | Current |
| `gocheckin/{thing}/tailgating_alert` | UC2 | Normal | New |
| `gocheckin/{thing}/unknown_face_detected` | UC3 | Low | New |
| `gocheckin/{thing}/group_size_mismatch` | UC4 | Normal | New |
| `gocheckin/{thing}/inactive_member_alert` | UC5 | Normal | Future |
| `gocheckin/{thing}/blocklist_alert` | UC6 | HIGH | Future |
| `gocheckin/{thing}/loitering_alert` | UC7 | Normal | Future |
| `gocheckin/{thing}/after_hours_access` | UC8 | Normal | Future |
| `gocheckin/{thing}/faceless_human_alert` | UC9 | Normal | Future |

---

# Handler Priority

| Priority | Handler | Action on Match | Scope |
|----------|---------|-----------------|-------|
| 5 | Blocklist | Stop, alert, NO unlock | Future (UC6) |
| 10 | Member Identification | Stop, unlock, log | Current (UC1) |
| 20 | Inactive Member | Alert (no stop) | Future (UC5) |
| 30 | Tailgating | Alert (no stop) | New (UC2) |
| 35 | Faceless Human | Alert (no stop) | Future (UC9) |
| 38 | Group Size & Person Count | Alert (no stop) | New (UC4) |
| 40 | Unknown Face | Log (no stop) | New (UC3) |
| 45 | Loitering | Alert (no stop) | Future (UC7) |

---

# Configuration Variables

```bash
# Core
FACE_THRESHOLD=0.45                   # Face similarity threshold

# UC2: Tailgating
TAILGATE_WINDOW_SEC=10                # Seconds to monitor after unlock

# UC5: Inactive Members (future)
INACTIVE_MEMBER_DAYS_BACK=30          # Days to look back for past guests

# UC8: After-Hours (future)
QUIET_HOURS_START=23:00
QUIET_HOURS_END=06:00
QUIET_HOURS_UNLOCK=true

# Feature flags
ENABLE_TAILGATING_DETECTION=true      # UC2
ENABLE_UNKNOWN_FACE_LOGGING=true      # UC3
ENABLE_GROUP_VALIDATION=true          # UC4
ENABLE_BLOCKLIST_DETECTION=true       # UC6 (future)
ENABLE_INACTIVE_MEMBER_ALERT=true     # UC5 (future)
```

---

# Non-Functional Requirements

## NFR1: Backend Compatibility

Only UC1 (Authorized Member Identification) is required to work with all inference backends.
UC2-UC9 only need to work with the primary backend in use at runtime.

**Supported Backends**:
- InsightFace (CPU, current default)
- Hailo-8 (HEF models, current alternative)
- NVIDIA Jetson (future)

## NFR2: Separation of Business Logic and Inference Logic

**Problem**: The current `face_recognition.py` and `face_recognition_hailo.py` each contain **both** inference logic and business logic. The `FaceRecognition` class (business logic) is duplicated nearly identically (~240 lines) across both files. The only difference is the thread name prefix and which inference app (`FaceAnalysis` vs `HailoFaceApp`) is passed in.

**Current Structure (problematic)**:

```
face_recognition.py
├── FaceRecognition(Thread)          # Business logic (queue, matching, snapshots, IoT output)
│   ├── run()                        # Main loop: queue → detect → match → output
│   ├── find_match()                 # Cosine similarity matching
│   ├── _build_member_embeddings()   # Embedding matrix precomputation
│   └── ... (snapshot, S3 keys, output queue)
└── (uses insightface FaceAnalysis as face_app)

face_recognition_hailo.py
├── HailoFace                        # Face result object (InsightFace-compatible)
├── HailoFaceApp                     # Inference-only: SCRFD detection + ArcFace recognition
│   ├── get(img) → List[HailoFace]   # Same interface as FaceAnalysis.get()
│   ├── _preprocess_detection()
│   ├── _run_detection()
│   ├── _postprocess_detection()
│   ├── _extract_embedding()
│   └── _align_face()
└── FaceRecognition(Thread)          # DUPLICATED business logic (copy of face_recognition.py)
```

**Required Structure**:

Inference backends must be inference-only modules that expose a common interface.
Business logic (queue processing, member matching, security handlers, snapshot saving, IoT output) must exist in a single place and be backend-agnostic.

```
inference_backend_insightface.py     # Inference only
├── wraps FaceAnalysis
└── get(img) → List[FaceResult]      # Common interface

inference_backend_hailo.py           # Inference only
├── HailoFaceApp
└── get(img) → List[FaceResult]      # Common interface

inference_backend_nvidia.py          # Future, inference only
└── get(img) → List[FaceResult]      # Common interface

face_recognition.py                  # Business logic only (single copy)
├── FaceRecognition(Thread)
│   ├── run()                        # Queue processing loop
│   ├── find_match()                 # Cosine similarity
│   ├── _build_member_embeddings()   # Embedding matrix
│   ├── Security handler integration (UC1-UC4)
│   └── Snapshot / IoT output
└── Uses whichever inference backend is active via common interface
```

**Common Inference Interface**:

Each backend module must expose a class with at minimum:
- `get(img: np.ndarray) -> List[FaceResult]`

Each `FaceResult` object must have at minimum:
- `bbox: np.ndarray` — shape (4,) — x1, y1, x2, y2
- `embedding: np.ndarray` — shape (512,) — L2-normalized
- `det_score: float`

**Key Constraint**: Adding a new backend (e.g., NVIDIA Jetson) should only require creating a new `inference_backend_nvidia.py` file and adding backend selection logic in `py_handler.py`. No changes to business logic or security handlers should be needed.

---

# Decisions Made

1. **Scope**: Initial version implements UC1-UC4
2. **Facility types**: All (vacation rentals, hotels, offices)
3. **UC3 databases**: All categories (ACTIVE, INACTIVE, STAFF, BLOCKLIST) loaded from TBL_RESERVATION with different filters
4. **UC3 storage**: Snapshot to S3 only (no embedding storage in initial version)
5. **UC4+UC5 merged**: Old UC4 (Multi-Face Group Validation) and old UC5 (Person Count Mismatch) merged into single UC4 (Group Size & Person Count Validation) with one combined `group_size_mismatch` alert
6. **UC4 model**: `yolov8n` (COCO 80-class, filter to person class 0, pre-compiled HEF for Hailo-8 and Hailo-8L). Used for person body counting only; SCRFD face count reused for face counting
7. **Hailo-8L compatibility**: All three models confirmed available — SCRFD (face detection + landmarks), ArcFace (face recognition), YOLOv8n (person detection)
8. **Inactive members**: Future (UC5) — query from TBL_RESERVATION/TBL_MEMBER (past checkouts)
9. **Blocklist source**: Future (UC6) — TBL_RESERVATION/TBL_MEMBER with blocklist flag (pseudo-reservations)
10. **Alert action**: IoT publish only (cloud handles notifications)
11. **Focus**: Security use-cases prioritized
12. **Architecture**: Separate inference backends from business logic (NFR2)
