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

**Scenario**: A guest with a valid reservation approaches the door. The system recognizes their face and, when a clicked event is received for a specific lock, unlocks that lock.

**Trigger**: Motion detected at camera → face detection starts

**Input**:
- Camera frame with face
- ACTIVE member database (guests with current reservations)

**Logic**:
- Detect faces in frame
- Compare each face against ACTIVE member embeddings
- If similarity > threshold → MATCH

**Output**:
- Unlock door (only when clicked event received for a specific lock — see Camera-Lock Patterns)
- Publish `member_detected` to IoT
- Save annotated snapshot to S3
- Log check-in event

**Data Source**:
- TBL_RESERVATION (active reservations)
- TBL_MEMBER (face embeddings)

**Key Behaviors**:
- Unlock is a **one-time side effect**: once an active member is identified and the door unlocks, the unlock action is not repeated, but the detection loop **continues running** for the full timer duration
- All other UCs continue to fire on every subsequent frame after unlock
- Only one member needs to match for unlock
- On cameras without locks (Pattern P1), UC1 still runs but publishes `member_detected` without unlock action (log only)
- On cameras with locks (Pattern P2), ONVIF starts the session in surveillance mode (no unlock possible). A clicked event upgrades the session to unlock mode for the specific lock
- If `active_member_matched=true` already when clicked arrives, unlock immediately (no need to re-present face — Decision 30)

---

## UC2: Tailgating Detection [NEW]

**Status**: To be implemented

**Scenario**: After an authorized guest unlocks the door, an unauthorized person follows them through before the door closes.

**Trigger**: Part of the continuous detection loop — fires when an unknown face appears in any frame after `unlocked=true` has been set in the current session (only possible after a clicked event triggers unlock)

**Input**:
- Camera frames (continuous detection loop, same as all other UCs)
- ACTIVE member database
- Session state: `unlocked=true`

**Logic**:
- UC1 matched and clicked event triggered unlock, setting `unlocked=true` at some frame
- Subsequent frames detect a face that doesn't match ANY active member → TAILGATING
- This is not a separate detection phase — it is the normal per-frame identification (UC3) combined with the knowledge that the door is already open

**Output**:
- Publish `tailgating_alert` to IoT
- Include: cam_ip, authorized_member, timestamp, snapshot of unknown face
- Do NOT prevent access (door already unlocked)

**Configuration**:
- `TAILGATE_WINDOW_SEC`: Duration to monitor after unlock (default: 10s)

**Key Behaviors**:
- Does not prevent access (too late — door already open)
- Alert is informational for security review
- Multiple unauthorized faces = multiple alerts
- UC2 fires in addition to UC3 (the unknown face is both logged AND flagged as tailgating)

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

**Note**: Unknown face embeddings are clustered within the session (for UC4 distinct counting) but NOT persisted to disk. Cloud-side embedding storage is needed for UC6 (Loitering Detection) to enable same-face matching across sessions over days/weeks.

**Purpose**:
- Build database of unknown visitors (snapshots to S3)
- Enable pattern analysis over time
- Support future loitering detection (UC6, cloud-side)

---

## UC4: Group Size Validation [NEW]

**Status**: To be implemented

**Scenario**: Over the course of a detection session (10-15 seconds, 100-150 frames at 10 fps), the system accumulates distinct faces seen and compares the total against the reservation's expected guest count. This catches "too many people" using face-level deduplication across the entire session — not a single-frame snapshot.

**Trigger**: Session-level check, evaluated after `active_member_matched=true`

**Input**:
- Session-level face accumulators (see Session State):
  - `known_members`: dict of distinct recognized member IDs
  - `unknown_face_clusters`: list of distinct unknown face embedding clusters
- Reservation `memberCount` field from TBL_RESERVATION

**Two-threshold approach** (handles masked faces):

| Threshold | Variable | Purpose | Value |
|-----------|----------|---------|-------|
| Face detection | `FACE_DETECT_THRESHOLD` | SCRFD confidence — accept face bbox (lower to catch masked faces) | e.g. 0.3 |
| Face recognition | `FACE_RECOG_THRESHOLD` | ArcFace cosine similarity — match to known member | e.g. 0.45 |

A masked face passes detection (low bar) but fails recognition (high bar) → classified as unknown → embedding still available for session-level clustering. Per-camera threshold tuning is possible since each camera has fixed angle, lighting, and distance.

**Logic**:
- Each frame: SCRFD detects faces (using `FACE_DETECT_THRESHOLD`), ArcFace extracts 512-d embeddings
- Known faces: match against member database (using `FACE_RECOG_THRESHOLD`), deduplicate by member ID (same member in 80 frames = 1 person)
- Unknown faces (including masked): cluster using dual-signal approach:
  1. **ArcFace embedding similarity** — even degraded embeddings from same masked person cluster together within a session (same person, same mask, same angle)
  2. **Face bbox IoU across consecutive frames** — spatial continuity catches cases where embedding is too degraded
  3. If best bbox IoU > `FACE_IOU_THRESHOLD` OR best embedding similarity > `UNKNOWN_FACE_CLUSTER_THRESHOLD` → merge into existing cluster, else → new cluster
- `distinct_face_count` = `len(known_members)` + `len(unknown_face_clusters)`
- Compare `distinct_face_count` vs `memberCount`
- If `distinct_face_count > memberCount` → group size mismatch
- **UC8 cross-check**: At session end, compare `max_simultaneous_persons` (from UC8 continuous YOLOv8n) with `distinct_face_count`. If `max_simultaneous_persons > distinct_face_count`, some people's faces were never captured (back turned, masked, outside camera angle). This is logged as supplementary evidence in the alert — it does not independently trigger a group size mismatch, but strengthens the alert when faces alone already exceed `memberCount`

**Output**:
- Publish `group_size_mismatch` alert to IoT
- Include: distinct_face_count, known_count, unknown_count, memberCount, matched_members, max_simultaneous_persons (from UC8), snapshot

**Models**:
- SCRFD + ArcFace only — already running for UC1/UC3. Face embeddings reused for clustering. Bbox positions reused for IoU. No extra model needed.

**Session Duration** (from codebase — `TIMER_DETECT` in `function.conf`, `extend_timer()` in `gstreamer_threading.py`):
- Base: `TIMER_DETECT` = 10 seconds (configurable)
- At 10 fps → 100 frames per session baseline
- Extended by clicked triggers: each clicked event adds up to `TIMER_DETECT` seconds (`elapsed + TIMER_DETECT`)
- Extended by ONVIF if session was started by ONVIF
- With extensions, sessions can reach 20-30+ seconds (200-300 frames)
- 10-20 seconds at a doorway is sufficient for all group members to show their faces at least once

**Extension rules** (from `py_handler.py:trigger_face_detection()`):

| Running session started by | New trigger | Extends? |
|---|---|---|
| ONVIF | ONVIF | Yes |
| ONVIF | Clicked | Yes |
| Clicked | Clicked #2 | Yes |
| Clicked | ONVIF | No |
| Any | **Dual-signal at timer expiry (recent motion + YOLOv8n person)** | **Yes (both signals required)** |

**Purpose**:
- Detect unauthorized additional guests (capacity enforcement)
- Session-level counting is more accurate than any single frame (people arrive at different times, turn away momentarily, etc.)

---

## UC5: Non-Active Member Alert [NEW]

**Status**: To be implemented

**Scenario**: A person who is in the system but should NOT have access is detected. This covers two sub-types:

- **INACTIVE**: A guest who previously stayed (reservation ended) returns and tries to access the property.
- **BLOCKLIST**: A banned individual (previous problem guest, known troublemaker) attempts to access the property.

**Trigger**: Face detected that matches INACTIVE or BLOCKLIST member database

**Input**:
- Camera frame with face
- ACTIVE member database (no match)
- INACTIVE member database (checked-out guests)
- BLOCKLIST member database (banned individuals)

**Logic**:
- Face does NOT match any ACTIVE member
- Face DOES match an INACTIVE member → sub-type INACTIVE
- Face DOES match a BLOCKLIST member → sub-type BLOCKLIST
- BLOCKLIST takes priority if face matches both

**Sub-type: INACTIVE** (normal priority):
- Former guest trying to access
- Publish `non_active_member_alert` with `sub_type=INACTIVE`
- Include: cam_ip, member_info (name, original reservation), checkout_date, similarity
- Do NOT unlock door

**Sub-type: BLOCKLIST** (HIGH priority):
- Banned individual attempting access
- Publish `non_active_member_alert` with `sub_type=BLOCKLIST`
- Include: cam_ip, member_info, blocklist_reason, snapshot
- Do NOT unlock door
- If `BLOCKLIST_PREVENTS_UNLOCK=true`: actively block unlock even if an active member is also detected in the same session

**Data Source**:
- TBL_RESERVATION (checkOut < today, last N days) + TBL_MEMBER — for INACTIVE
- TBL_RESERVATION (blocklist flag) + TBL_MEMBER — for BLOCKLIST (pseudo-reservations)

**Configuration**:
- `INACTIVE_MEMBER_DAYS_BACK`: How far back to check (default: 30 days)
- `BLOCKLIST_PREVENTS_UNLOCK`: Whether blocklist detection blocks unlock for the entire session (default: true)

**Key Behaviors**:
- BLOCKLIST sub-type is highest priority — checked before member identification
- INACTIVE sub-type does not unlock door
- Alert includes original stay information (INACTIVE) or blocklist reason (BLOCKLIST)
- Useful for property owners to know who is trying to access
- `block_further_unlocks` prevents unlock of any lock in `clicked_locks` that hasn't been unlocked yet (already-unlocked locks are NOT re-locked)

---

## UC6: Loitering Detection [FUTURE]

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

**Requires**: UC3 (Unknown Face Logging) for history tracking. Requires cloud-side data aggregation to persist unknown face embeddings across sessions over days/weeks. Not feasible on-device alone.

---

## UC7: After-Hours Access Attempt [FUTURE]

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

## UC8: Human Body Detection [NEW]

**Status**: To be implemented (initial scope)

**Scenario**: YOLOv8n (COCO person class 0) provides **session lifecycle control and continuous person presence monitoring**. UC8 is NOT an alerting mechanism. It is an operational control that (1) filters false ONVIF triggers (wind, shadow, animal motion) before committing SCRFD+ArcFace resources, and (2) provides real-time person count throughout the detection session.

**Three roles:**

### Role 1: Gate (session start + recording start)

After ONVIF fires, YOLOv8n runs on `YOLO_GATE_FRAMES` consecutive frames (default: 10 frames = 1 second at 10fps). If a person (class 0) is detected with confidence ≥ `YOLO_DETECT_THRESHOLD` in at least `YOLO_GATE_MIN_DETECTIONS` of those frames (default: 3), the detection session starts (SCRFD+ArcFace loop) AND video recording starts (GStreamer RTSP pipeline). If the minimum detection count is not reached, the trigger is discarded as a false alarm — no session, no recording.

**Why multi-frame gate (not single frame):** A single frame is too fragile for the gate decision. The gate is a high-stakes check — a false negative skips the entire SCRFD+ArcFace session and no face recognition happens at all. A person may be missed in any single frame due to motion blur, partial occlusion, or awkward body angle. Running YOLOv8n over 10 frames (1 second) provides robust detection while adding acceptable latency — the person is still approaching the door after ONVIF fired.

**"Any K of N" threshold:** Requiring at least `YOLO_GATE_MIN_DETECTIONS` (default: 3) out of `YOLO_GATE_FRAMES` (default: 10) balances false positives and false negatives. A single-frame shadow/reflection won't trigger a session (requires 3+ frames), while a real person only needs to be visible in 3 of 10 frames to pass — tolerating occlusion, blur, and turning in the remaining frames.

**NPU impact:** 10 YOLOv8n inferences at gate (vs 1 previously). At ~6-10ms per inference = ~60-100ms of NPU time. Still negligible vs the subsequent SCRFD+ArcFace loop running 100+ inferences per session. Adds ~1-2% NPU utilization during the 1-second gate window.

```
ONVIF camera detects motion
       │
       ▼
GStreamer grabs 10 frames (1 second at 10fps)
       │
       ▼
Run YOLOv8n on each frame (person class 0)
       │
       ▼
Person detected in ≥ 3 of 10 frames?
  NO  → skip (false alarm: wind/shadow/animal — no session, no recording)
  YES → start recording + start detection session (SCRFD + ArcFace loop)
```

### Role 2: Continuous person detection (during session)

Once the gate passes and the detection session starts, YOLOv8n runs on **every frame** alongside SCRFD+ArcFace for the entire session duration. This provides real-time person count data at no additional implementation complexity — the same inference pipeline processes every frame.

**Why continuous (not burst-only):** Feasibility testing on Hailo-8 measured YOLOv8n at only **6.4% NPU utilization** at 10fps continuous. This is low enough to run alongside SCRFD+ArcFace (~35%) with comfortable headroom (~41.4% total per camera, ~82.8% for 2 cameras). At this cost, the complexity of burst-only gate/extend windows is not justified. Continuous detection provides richer data for extend decisions and enables UC4 enhancement (max simultaneous person count).

**What continuous detection provides:**
- **Per-frame person count** — how many people are visible in each frame
- **Max simultaneous persons** — the peak person count observed in any single frame during the session (lower bound on total individuals present)
- **Person presence history** — a continuous buffer of detection results, available for extend checks without any additional NPU cost
- **Session state**: `max_simultaneous_persons` tracked as session-level accumulator (updated each frame)

**NPU impact:** 6.4% continuous during session (measured on Hailo-8 at 10fps). Combined with SCRFD+ArcFace (~35%): **~41.4% per camera**. Two cameras: ~82.8%. This is higher than the previous burst-only design (~36.3%) but still well within Hailo-8 capacity for 2 cameras.

### Role 3: Extend (session timer expiry)

When the detection timer is about to expire, two signals are checked: (1) whether a motion event was received within the last `MOTION_RECENCY_SEC` seconds, and (2) whether a person was detected in at least `YOLO_EXTEND_MIN_DETECTIONS` of the last `YOLO_EXTEND_LOOKBACK` frames from the continuous detection buffer. Both signals must be true to extend the session — either signal alone is insufficient. This dual-signal requirement reduces false extensions: person detection alone may trigger on static figures (poster, mannequin), while motion alone may be non-human (wind, animal). Together they confirm something is moving AND it's a person. No explicit cap is needed — the motion recency requirement is a natural session limiter. ONVIF is single-fire (one event per motion onset), so unless new motion events keep arriving, `MOTION_RECENCY_SEC` expires and the session ends. When the session ends (person left or no recent motion), recording ends with it.

**Simplified extend (no burst needed):** Because YOLOv8n runs continuously during the session (Role 2), the person detection data is already available at timer expiry. The extend check simply queries the last N frames from the continuous buffer — no separate YOLOv8n burst is needed. This adds **zero additional NPU cost** at extend time.

Motion detection is designed as an event-based abstraction: motion sources emit timestamped events per camera, and the extend check queries whether a motion event was received within the recency window. Currently ONVIF events serve as the motion source (single-fire — one event per motion onset, recency based on last event timestamp). Future H265 HW frame-level decoding will emit equivalent events via the same interface, making the motion source pluggable.

**Session duration governance**: Two parameters interact to control how long sessions extend. The motion source's **notification gap** (camera-side, tunable per camera/location) controls how frequently motion events are emitted — busy locations set a 20-30s gap to naturally space out sessions, while quiet locations can set 0s for maximum responsiveness. **`MOTION_RECENCY_SEC`** (software-side, global default) defines how recent a motion event must be to count at timer expiry. Operators tune the notification gap per camera to match their location's activity level; `MOTION_RECENCY_SEC` remains a global default. This applies to both ONVIF and future H265 HW motion sources — the notification gap is a property of the motion source abstraction.

```
Timer about to expire
       │
       ▼
Check dual signal:
  1. Motion recent? (motion event within MOTION_RECENCY_SEC)
  2. Person in ≥ 3 of last 10 frames? (from continuous detection buffer — no extra inference)

  BOTH YES → extend timer, recording continues
  EITHER NO → end session normally, recording ends
```

**Model**: YOLOv8n (COCO 80-class, person = class 0) — pre-compiled HEF available for Hailo-8 and Hailo-8L in [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo)

**Output**: No IoT alert. UC8 is a control decision, not a notification. It produces no `human_body_alert` topic — it gates session start and video recording, extends session duration (recording continues), and provides `max_simultaneous_persons` as a session-level accumulator for UC4 enhancement. No person detected at gate = no session = no recording.

**Configuration**:
- `YOLO_DETECT_THRESHOLD=0.5` — YOLOv8n confidence threshold for person class 0
- `YOLO_GATE_FRAMES=10` — Number of consecutive frames to run YOLOv8n at gate check (at 10fps = 1 second)
- `YOLO_GATE_MIN_DETECTIONS=3` — Minimum number of frames with person detected to pass gate (any 3 of 10)
- `YOLO_EXTEND_LOOKBACK=10` — Number of recent frames to check from continuous buffer at extend time
- `YOLO_EXTEND_MIN_DETECTIONS=3` — Minimum number of frames with person detected to pass extend check
- `MOTION_RECENCY_SEC=5` — Time window (in seconds) to consider a motion event as "recent" at timer expiry for dual-signal extension check

**NPU impact**: YOLOv8n runs on every frame during the session (continuous) at **6.4% NPU utilization** (measured on Hailo-8 at 10fps). Combined with SCRFD+ArcFace (~35%): **~41.4% per camera**. Two cameras: ~82.8%. Gate adds a 1-second pre-session burst (same 6.4% NPU). Extend check uses the continuous buffer — zero additional NPU cost at timer expiry.

**Key Behaviors**:
- Applies to both camera-lock patterns (P1-P2) as the first step before SCRFD+ArcFace
- Gates video recording alongside SCRFD+ArcFace — false ONVIF triggers (no person) produce neither face detection nor video recording, saving storage
- Runs continuously on every frame during the session — provides real-time person count and `max_simultaneous_persons` accumulator
- Session extension requires both recent motion (event within `MOTION_RECENCY_SEC`) AND person presence in recent frames (from continuous buffer — no extra inference needed) — no explicit cap needed, motion recency is a natural session limiter
- Does not replace existing extension rules (ONVIF/clicked extensions still apply)
- `max_simultaneous_persons` enhances UC4 group size validation — catches people whose faces were never captured (back turned, hooded) by comparing body count vs face count

---

## UC9: Face Anti-Spoofing (Liveness Detection) [FUTURE]

**Status**: Future implementation

**Scenario**: A person holds up a printed photo or phone/tablet displaying someone's face to trick the face recognition system into unlocking the door. A dedicated close-range camera near the lock verifies the person is real through multiple defense layers.

**Camera Type**: This is NOT a general surveillance/recognition camera. It is a dedicated verification camera:
- Mounted close to the lock (arm's length)
- Fixed close-range framing (face fills most of frame)
- Controlled angle and lighting
- Purpose: high-quality face capture for liveness verification only
- Does not participate in UC2 (tailgating), UC3 (unknown logging), UC4 (group counting), etc.

**Trigger**: Clicked event on lock → start detection on dedicated camera

**Input**:
- Camera frame from dedicated close-range camera
- Guest's pre-assigned blink pattern (delivered via booking app/SMS before arrival)

**Defense Layers** (multi-signal, no single point of failure):

| Layer | Signal | Model / Method | What it defeats |
|-------|--------|---------------|-----------------|
| 1 | Face recognition | ArcFace (Hailo) | Random strangers |
| 2 | Face size validation | SCRFD bbox vs expected range (CPU math) | Phone/tablet screens (face on screen is smaller than real head at arm's length) |
| 3 | Device detection | YOLOv8n COCO classes 62/63/67 (Hailo) | Phone/tablet held up to camera — detect device object containing/surrounding face |
| 4 | Blink pattern challenge | tddfa_mobilenet_v1 landmarks + Eye Aspect Ratio (Hailo + CPU) | Printed photos (can't blink), pre-recorded video (wrong sequence) |

**Layer 1: Face Recognition** (ArcFace):
- SCRFD detects face, ArcFace matches to active member
- If no match → stop (not an authorized guest)

**Layer 2: Face Size Validation** (SCRFD bbox, CPU math):
- Dedicated camera is at known fixed distance from where person stands
- Expected face bbox width at arm's length: ~200-400px (camera-dependent, calibrated once at install)
- Phone screen face: ~50-100px, tablet screen face: ~80-150px
- If `bbox_width` outside expected range → suspicious
- Configuration: `FACE_SIZE_MIN_PX`, `FACE_SIZE_MAX_PX` per camera

**Layer 3: Device Detection** (YOLOv8n):
- Run YOLOv8n on the same frame
- Check for COCO classes: tv/monitor (62), laptop (63), cell phone (67)
- If device bbox detected AND face bbox is contained within device bbox → face is displayed on a screen → spoofing
- Same YOLOv8n model as UC8 (person class 0 for UC8, device classes 62/63/67 for UC9)

**Layer 4: Blink Pattern Challenge** (tddfa_mobilenet_v1 + CPU):
- Each reservation is assigned a random blink sequence (e.g., LEFT, RIGHT, LEFT)
- Delivered to guest via booking channel (app notification, SMS, email) before arrival
- Guest performs the blink pattern at the camera
- tddfa_mobilenet_v1 outputs 68 3D facial landmarks per frame
- Eye Aspect Ratio (EAR) computed from eye landmarks (points 36-47):
  ```
  EAR = (||p2-p6|| + ||p3-p5||) / (2 * ||p1-p4||)
  EAR > 0.2 → eye open
  EAR < 0.15 → eye closed
  ```
- Left and right eyes computed independently
- System detects blink sequence over N frames → compare against expected pattern
- Correct pattern → pass. Wrong pattern or no blinks → fail.
- No on-site instruction device needed — guest already knows the pattern

**Pipeline**:
```
Clicked event triggers
       │
       ▼
1. SCRFD       → face bbox                          (Hailo)
2. ArcFace     → match to active member              (Hailo)
   If no match → stop
       │
       ▼
3. Face size   → bbox within expected range?          (CPU)
   If out of range → spoofing_alert
       │
       ▼
4. YOLOv8n     → device (tv/phone/laptop) detected?  (Hailo)
   If face inside device bbox → spoofing_alert
       │
       ▼
5. tddfa       → 68 landmarks → EAR per eye           (Hailo + CPU)
   Monitor blink sequence over frames
   If correct pattern → UNLOCK
   If wrong/no pattern → spoofing_alert
```

**Output**:
- If any layer fails:
  - Publish `spoofing_alert` to IoT (HIGH priority)
  - Include: cam_ip, failed_layer, member_info, snapshot
  - Do NOT unlock door
  - Set `block_further_unlocks` for session
- If all layers pass: unlock door

**Models Required** (all have pre-compiled Hailo HEFs):
- SCRFD — face detection (already used in UC1-UC5)
- ArcFace — face recognition (already used in UC1-UC5)
- tddfa_mobilenet_v1 — 68 facial landmarks + head pose (3.26M params, 11,321 FPS on Hailo-8, [pre-compiled HEF in Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo/blob/master/docs/public_models/HAILO8/HAILO8_facial_landmark_detection.rst))
- YOLOv8n — device detection, COCO classes 62/63/67 (shared with UC8, [pre-compiled HEF available](https://github.com/hailo-ai/hailo_model_zoo))

**Key Behaviors**:
- Only runs on dedicated verification cameras (not surveillance cameras)
- Blink pattern is per-reservation, delivered via booking channel, no on-site display needed
- Layers are sequential — early failure short-circuits (no point checking blink if device detected)
- A printed photo fails layer 4 (can't blink)
- A phone video fails layers 2 (too small), 3 (device detected), and 4 (wrong blink sequence)
- The only attack that passes all 4 layers: a live person who looks like the guest AND knows the blink code

**Relationship to Camera-Lock Patterns**:
- This camera is always associated with a lock (P2)
- It is triggered by clicked event (person at the lock)
- Other cameras on the same door still run normal UC1-UC5 pipeline
- The dedicated camera runs its own UC9 pipeline (not the standard continuous detection loop)

---

# Camera-Lock Patterns

All locks require a "clicked" event to unlock. "Clicked" unifies two physical signals:
1. **Occupancy sensor activation** (keypad locks like MTR001DC/MTR001AC — current `occupancy: true`)
2. **LOCK_BUTTON press** (GreenPower_2 kinetic switch — `action: 'clicked'`)

No lock unlocks from ONVIF alone. ONVIF starts sessions in surveillance mode; clicked events upgrade to unlock mode.

## Pattern Definitions

| Pattern | Locks | Session Start | Unlock Trigger | Unlock Action |
|---------|-------|---------------|----------------|---------------|
| P1: No lock | None | ONVIF motion | N/A | None (surveillance / visual verification) |
| P2: Camera with lock(s) | Any lock type | ONVIF motion | Clicked event for specific lock | Unlock specific lock |

## Session State

Every detection session maintains these (reset per session):

**Flags**:

| Flag | Default | Set by | Effect |
|------|---------|--------|--------|
| `unlocked` | `false` | UC1 (active member match + clicked event) | Prevents re-triggering unlock for the same lock set; enables UC2 tailgating checks |
| `block_further_unlocks` | `false` | UC5-blocklist (when `BLOCKLIST_PREVENTS_UNLOCK=true`) | Prevents any new unlock actions for remainder of session (already-unlocked locks are NOT re-locked) |
| `active_member_matched` | `false` | UC1 | Enables UC4 group size comparison against `memberCount` |
| `unlocked_locks` | `{}` (empty set) | UC1 unlock action | Tracks which specific locks have been unlocked (per-lock granularity) |
| `clicked_locks` | `{}` (empty set) | Clicked event (occupancy or LOCK_BUTTON) | Set of lock IDs that received a clicked signal during this session. Determines which locks are eligible for unlock |

**Face accumulators** (for session-level distinct person counting):

| Accumulator | Type | Updated by | Purpose |
|-------------|------|-----------|---------|
| `known_members` | `dict[member_id → {category, first_frame, last_frame}]` | UC1, UC5 | Deduplicate recognized faces by member ID across frames |
| `unknown_face_clusters` | `list[embedding_cluster]` | UC3 | Group unknown face embeddings by cosine similarity. Each cluster = 1 distinct unknown person |

Clustering logic for `unknown_face_clusters` (dual-signal, incremental):
1. New unknown face arrives with embedding + bbox
2. Compute bbox IoU against all existing clusters' `last_bbox`
3. Compute embedding cosine similarity against all cluster centroids
4. If best IoU ≥ `FACE_IOU_THRESHOLD` → merge (spatial continuity, handles degraded masked embeddings)
5. Elif best similarity ≥ `UNKNOWN_FACE_CLUSTER_THRESHOLD` → merge (embedding match)
6. Else → create new cluster (new distinct person)

This handles masked faces: even when ArcFace embeddings are degraded, bbox IoU catches the same person standing at the door across consecutive frames.

**Clicked locks note**: Each clicked event adds a specific lock ID to `clicked_locks`. A lock can only be unlocked if its ID is in `clicked_locks`. Multiple clicked events during a session expand the set of eligible locks.

## Detection Timelines

### P1: Camera with no lock (surveillance / visual verification)

Example: Common area camera, reception desk, parking lot.

```
Person approaches area
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frames
       │  [UC8 GATE] Run YOLOv8n on 10 frames (1 sec at 10fps)
       │    → Person in ≥ 3 of 10 frames?
       │    → NO  → SKIP (no session, no recording)
       │    → YES → start recording + continue ▼
       │
T=1    │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Camera has NO locks → still proceed (UC1 always runs)
       │  Session state: unlocked=N/A, block_further_unlocks=N/A
       │  GStreamer starts feeding frames
       │
       ▼
T=1+   CONTINUOUS DETECTION LOOP (every frame, full timer)
       ┌─────────────────────────────────────────────────────────┐
       │ INFERENCE (every frame):                                 │
       │   SCRFD  → face bboxes + 5-point landmarks              │
       │   ArcFace → 512-d embedding per face                    │
       │   [UC8] YOLOv8n → person count (continuous)             │
       │     Update max_simultaneous_persons                      │
       │     Append to person_detection_buffer                    │
       │                                                          │
       │ PER-FACE IDENTIFICATION (every frame, every face):       │
       │   Compute cosine similarity against ALL categories       │
       │                                                          │
       │   Best match ≥ threshold?                                │
       │                                                          │
       │   YES → Which category?                                  │
       │    ├─ BLOCKLIST → [UC5-blocklist]                        │
       │    │   Add to known_members[id]={category:BLOCKLIST}     │
       │    │   Publish non_active_member_alert (HIGH priority)   │
       │    │   (No lock to block — alert only)                   │
       │    │   Save snapshot to S3                               │
       │    │                                                     │
       │    ├─ ACTIVE → [UC1]                                     │
       │    │   Add to known_members[id]={category:ACTIVE}        │
       │    │   Publish member_detected (LOG ONLY, no unlock)     │
       │    │   Set active_member_matched=true                    │
       │    │   Save snapshot to S3                               │
       │    │                                                     │
       │    ├─ INACTIVE → [UC5-inactive]                          │
       │    │   Add to known_members[id]={category:INACTIVE}      │
       │    │   Publish non_active_member_alert                   │
       │    │                                                     │
       │    └─ STAFF → Add to known_members, log only             │
       │                                                          │
       │   NO (below threshold for all) → [UC3]                   │
       │     Cluster into unknown_face_clusters                   │
       │       (by embedding similarity + bbox IoU)               │
       │     Publish unknown_face_detected                        │
       │     Save snapshot to S3                                  │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues until timer expires / motion stops
       │
T=exp  Timer about to expire
       │  [UC8 EXTEND] Dual-signal check:
       │    1. Motion recent? (motion event within MOTION_RECENCY_SEC)
       │    2. Person in ≥ 3 of last 10 frames? (from continuous buffer)
       │    → BOTH YES? → extend timer, recording continues
       │    → EITHER NO? → let session end normally, recording ends
       │
T=end  Session ends
       │
       │  SESSION-LEVEL CHECKS:
       │  [UC6] Cross-session analysis (future):
       │  Same unknown face across sessions → loitering_alert
```

**P1 notes**: No unlock/lock state. No UC2 tailgating (no door to tailgate through). No UC4 group size validation (no door/reservation context to compare against).

### P2: Camera with lock(s)

Example: Front door with any type of smart lock (occupancy sensor lock, kinetic switch lock, or both).

```
Person approaches door
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frames
       │  [UC8 GATE] Run YOLOv8n on 10 frames (1 sec at 10fps)
       │    → Person in ≥ 3 of 10 frames?
       │    → NO  → SKIP (no session, no recording)
       │    → YES → start recording + continue ▼
       │
T=1    │  START DETECTION IN SURVEILLANCE MODE
       │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Context: started_by_onvif=True, surveillance_mode=True
       │  Session state: unlocked=false, block_further_unlocks=false
       │  Session state: clicked_locks={} (empty — no unlock possible yet)
       │  GStreamer starts feeding frames
       │
       ▼
T=1+   CONTINUOUS DETECTION LOOP (surveillance mode — no unlock possible)
       ┌─────────────────────────────────────────────────────────┐
       │ INFERENCE (every frame):                                 │
       │   SCRFD  → face bboxes + 5-point landmarks              │
       │   ArcFace → 512-d embedding per face                    │
       │   [UC8] YOLOv8n → person count (continuous)             │
       │     Update max_simultaneous_persons                      │
       │     Append to person_detection_buffer                    │
       │                                                          │
       │ PER-FACE IDENTIFICATION (every frame, every face):       │
       │   Compute cosine similarity against ALL categories       │
       │                                                          │
       │   Best match ≥ threshold?                                │
       │                                                          │
       │   YES → Which category?                                  │
       │    ├─ BLOCKLIST → [UC5-blocklist]                        │
       │    │   Add to known_members[id]={category:BLOCKLIST}     │
       │    │   Publish non_active_member_alert (HIGH priority)   │
       │    │   Set block_further_unlocks=true                    │
       │    │   Save snapshot to S3                               │
       │    │                                                     │
       │    ├─ ACTIVE → [UC1]                                     │
       │    │   Add to known_members[id]={category:ACTIVE}        │
       │    │   Set active_member_matched=true                    │
       │    │   No unlock yet (clicked_locks is empty)            │
       │    │   Publish member_detected (LOG ONLY — surveillance) │
       │    │   Save snapshot to S3                               │
       │    │                                                     │
       │    ├─ INACTIVE → [UC5-inactive]                          │
       │    │   Add to known_members[id]={category:INACTIVE}      │
       │    │   Publish non_active_member_alert                   │
       │    │                                                     │
       │    └─ STAFF → Add to known_members, log only             │
       │                                                          │
       │   NO (below threshold for all) → [UC3]                   │
       │     Cluster into unknown_face_clusters                   │
       │       (by embedding similarity + bbox IoU)               │
       │     Publish unknown_face_detected                        │
       │     Save snapshot to S3                                  │
       │                                                          │
       │ No UC2 tailgating (no unlock has occurred)               │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues...
       │
T=N    │  CLICKED EVENT arrives for lock "lock_123"
       │  (occupancy sensor activation OR LOCK_BUTTON press)
       │  trigger_face_detection(cam_ip, lock_asset_id="lock_123")
       │  MERGE into running session — UPGRADE to unlock mode:
       │    clicked_locks += {"lock_123"}
       │    Extend detection timer
       │
       │  Now lock_123 is eligible for unlock
       │
       │  ┌─────────────────────────────────────────────────────┐
       │  │ IMMEDIATE UNLOCK CHECK (Decision 30):                │
       │  │   If active_member_matched=true                      │
       │  │   AND block_further_unlocks=false                    │
       │  │   AND "lock_123" not in unlocked_locks:              │
       │  │     → UNLOCK lock_123 IMMEDIATELY                    │
       │  │     → Add "lock_123" to unlocked_locks               │
       │  │     → Set unlocked=true                              │
       │  │     → Publish member_detected                        │
       │  │       payload: clickedLocks=["lock_123"]             │
       │  │     → Save snapshot to S3                            │
       │  │   (No need to re-present face — already matched)     │
       │  └─────────────────────────────────────────────────────┘
       │
       ▼
T=N+   DETECTION LOOP CONTINUES (unlock mode active for lock_123)
       ┌─────────────────────────────────────────────────────────┐
       │ (Same inference + identification as above, incl. YOLOv8n)│
       │                                                          │
       │   ACTIVE → [UC1]                                         │
       │     If lock_123 not in unlocked_locks                    │
       │     AND block_further_unlocks=false:                     │
       │       → UNLOCK lock_123                                  │
       │       → Add to unlocked_locks                            │
       │       → Set unlocked=true                                │
       │       → payload: clickedLocks=["lock_123"]              │
       │     If lock_123 already in unlocked_locks:               │
       │       → Skip (already unlocked)                          │
       │     If block_further_unlocks=true:                       │
       │       → Publish member_detected (blocked=true)           │
       │       → Do NOT unlock                                    │
       │                                                          │
       │   BLOCKLIST → [UC5-blocklist]                            │
       │     Set block_further_unlocks=true                       │
       │     (If lock_123 already unlocked: no re-lock.           │
       │      If not yet unlocked: prevents future unlock.)       │
       │                                                          │
       │   Unknown face + unlocked=true → [UC2] tailgating_alert │
       │                                                          │
       │ (All other UCs — UC3, UC4, UC5-inactive — same as above) │
       └─────────────────────────────────────────────────────────┘
       │
       │  If NO clicked event arrives, session runs in
       │  surveillance mode for full timer (P1 behavior)
       │
T=exp  Timer about to expire
       │  [UC8 EXTEND] Dual-signal check:
       │    1. Motion recent? (motion event within MOTION_RECENCY_SEC)
       │    2. Person in ≥ 3 of last 10 frames? (from continuous buffer)
       │    → BOTH YES? → extend timer, recording continues
       │    → EITHER NO? → let session end normally, recording ends
       │
T=end  Session ends
       │
       │  SESSION-LEVEL CHECKS:
       │  [UC4] If active_member_matched=true:
       │    distinct_faces = len(known_members) + len(unknown_face_clusters)
       │    If distinct_faces > memberCount → Publish group_size_mismatch
       │    [UC4+UC8] If max_simultaneous_persons > distinct_faces:
       │      → Additional signal: persons present but face never captured
       │      → Include max_simultaneous_persons in payload
       │
       │  [UC6] Cross-session analysis (future):
       │  Same unknown face across sessions → loitering_alert
```

**P2 example scenario 1** — blocklist detected BEFORE clicked:
1. T=0: ONVIF triggers, detection starts in surveillance mode
2. Frame 3: Active member detected → `active_member_matched=true`, but no clicked yet → log only
3. Frame 7: Blocklist person detected → `block_further_unlocks=true`, alert fires
4. T=N: Clicked event for lock_123 → `clicked_locks={"lock_123"}`
5. Immediate unlock check: `active_member_matched=true` BUT `block_further_unlocks=true` → NO unlock
6. Result: Blocklist prevented unlock despite active member being present

**P2 example scenario 2** — clicked before face match:
1. T=0: ONVIF triggers, detection starts in surveillance mode
2. T=N: Clicked event for lock_123 → `clicked_locks={"lock_123"}`, no member matched yet
3. Frame at T=N+: Active member detected → `active_member_matched=true`, lock_123 in clicked_locks, `block_further_unlocks=false` → UNLOCK lock_123
4. Result: Normal unlock flow, clicked arrived before face match

**P2 example scenario 3** — multiple locks:
1. T=0: ONVIF triggers, surveillance mode
2. T=N: Clicked event for lock_123 → `clicked_locks={"lock_123"}`
3. Frame: Active member → unlock lock_123, `unlocked_locks={"lock_123"}`
4. T=M: Clicked event for lock_456 → `clicked_locks={"lock_123", "lock_456"}`
5. Immediate unlock check: `active_member_matched=true`, lock_456 not in unlocked_locks → UNLOCK lock_456 immediately
6. Result: Each lock unlocked independently as its clicked event arrived

## UC Applicability Matrix

| UC | P1 (no lock) | P2 (with lock) | Scope |
|----|:---:|:---:|:---:|
| UC1: Member ID | Log only | Unlock specific lock (via clicked) | Current |
| UC2: Tailgating | N/A (no door) | Alert (after clicked-triggered unlock) | New |
| UC3: Unknown Face | Log + S3 | Log + S3 | New |
| UC4: Group Size | N/A (no door context) | Alert | New |
| UC5: Non-Active Member | Alert only | Alert + Block? | New |
| UC6: Loitering | Alert | Alert | Future |
| UC7: After-Hours | Alert | Alert + Policy | Future |
| UC8: Human Body Detection | Gate + Continuous + Extend | Gate + Continuous + Extend | New |
| UC9: Anti-Spoofing | N/A | Gate unlock (dedicated camera) | Future |

- "Block?" = BLOCKLIST sub-type only, configurable via `BLOCKLIST_PREVENTS_UNLOCK`
- "Policy" = QUIET_HOURS_UNLOCK config decides whether to still unlock
- "Gate unlock" = UC9 runs only on dedicated verification cameras (P2 with lock); blocks unlock if spoof detected
- "Gate + Continuous + Extend" = YOLOv8n gate runs as first step before SCRFD+ArcFace; during session YOLOv8n runs continuously on every frame (6.4% NPU) providing person count and max_simultaneous_persons; extend check uses continuous buffer (no extra inference)

---

# Implementation Scope

## Initial Version (SCRFD + ArcFace + YOLOv8n)
- UC1: Authorized Member Identification (refactor existing)
- UC2: Tailgating Detection
- UC3: Unknown Face Logging
- UC4: Group Size Validation
- UC5: Non-Active Member Alert
- UC8: Human Body Detection (session lifecycle control + continuous person count + recording gate)

Three models: SCRFD (face detection) + ArcFace (face recognition) + YOLOv8n (human body detection). UC1-UC5 run within the continuous detection loop. UC8 operates at three levels: (1) **Gate** — YOLOv8n runs on 10 frames (1 second at 10fps) before the detection loop starts, person must be detected in at least 3 of 10 frames to pass (filters false ONVIF triggers, gates video recording — no person = no session = no recording). (2) **Continuous** — during the session, YOLOv8n runs on every frame alongside SCRFD+ArcFace at 6.4% NPU, providing real-time person count and `max_simultaneous_persons` for UC4 enhancement. (3) **Extend** — at timer expiry, checks the continuous person detection buffer (person in ≥3 of last 10 frames) plus motion recency — no extra inference needed. Motion recency naturally limits session duration — no explicit cap needed.

Two camera-lock patterns: P1 (no lock — surveillance only) and P2 (camera with lock(s) — clicked event required for unlock). All locks require a "clicked" event (occupancy sensor or LOCK_BUTTON press) to unlock.

## Future Version
- UC6: Loitering Detection — requires cloud-side data aggregation to persist unknown face embeddings across sessions over days/weeks
- UC7: After-Hours Access Attempt — trivial time check, deferred to reduce initial scope
- UC9: Face Anti-Spoofing — dedicated close-range verification camera near lock. Multi-layer defense: face size validation, device detection (YOLOv8n), blink pattern challenge (tddfa_mobilenet_v1). Blink pattern per-reservation, delivered via booking channel. All models have pre-compiled Hailo HEFs

## Simplified Pipelines (Initial Scope)

The following diagrams show only the initial scope use cases (UC1-UC5, UC8). Future use cases (UC6, UC7, UC9) are omitted.

### Simplified P1: Camera with No Lock (Surveillance Only)

```
ONVIF motion
    │
    ▼
[UC8 GATE] YOLOv8n on 10 frames → person in ≥3? ─NO─→ SKIP (no session, no recording)
    │
   YES (1 sec later)
    ▼
Start recording + detection session
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ CONTINUOUS DETECTION LOOP (every frame)             │
│                                                     │
│   SCRFD → face bbox                                 │
│   ArcFace → 512-d embedding                         │
│   [UC8] YOLOv8n → person count (continuous, 6.4%)  │
│     Update max_simultaneous_persons                 │
│                                                     │
│   Match against ALL categories:                     │
│                                                     │
│   BLOCKLIST → [UC5] Alert (HIGH), log              │
│   ACTIVE    → [UC1] Log only (no unlock)           │
│   INACTIVE  → [UC5] Alert, log                     │
│   STAFF     → Log only                              │
│   NO MATCH  → [UC3] Log unknown, save snapshot     │
└─────────────────────────────────────────────────────┘
    │
    ▼
Timer expiry → [UC8 EXTEND] motion recent + person in ≥3 of last 10? (buffer)
    │
  BOTH YES → extend timer
  EITHER NO → end session
    │
    ▼
Session ends (no UC4 — no door context)
```

### Simplified P2: Camera with Lock(s)

```
ONVIF motion
    │
    ▼
[UC8 GATE] YOLOv8n on 10 frames → person in ≥3? ─NO─→ SKIP (no session, no recording)
    │
   YES (1 sec later)
    ▼
Start recording + detection session (SURVEILLANCE MODE)
Session state: clicked_locks={}, unlocked_locks={}, unlocked=false
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ CONTINUOUS DETECTION LOOP (every frame)             │
│                                                     │
│   SCRFD → face bbox                                 │
│   ArcFace → 512-d embedding                         │
│   [UC8] YOLOv8n → person count (continuous, 6.4%)  │
│     Update max_simultaneous_persons                 │
│                                                     │
│   BLOCKLIST → [UC5] Alert (HIGH)                   │
│               Set block_further_unlocks=true        │
│                                                     │
│   ACTIVE → [UC1]                                    │
│     Set active_member_matched=true                  │
│     If lock in clicked_locks                        │
│        AND not in unlocked_locks                    │
│        AND block_further_unlocks=false:             │
│          → UNLOCK, set unlocked=true                │
│                                                     │
│   INACTIVE → [UC5] Alert                           │
│                                                     │
│   NO MATCH → [UC3] Log unknown                      │
│              If unlocked=true → [UC2] Tailgating   │
│                                                     │
│   Accumulate: known_members, unknown_face_clusters  │
└─────────────────────────────────────────────────────┘
    │
    ├──── CLICKED EVENT for lock_X ────┐
    │     clicked_locks += {lock_X}    │
    │     If active_member_matched     │
    │        AND !block_further_unlocks│
    │        → UNLOCK IMMEDIATELY      │
    │     (Decision 30)                │
    │◄─────────────────────────────────┘
    │
    ▼
Timer expiry → [UC8 EXTEND] motion recent + person in ≥3 of last 10? (buffer)
    │
  BOTH YES → extend timer
  EITHER NO → end session
    │
    ▼
Session ends
    │
    ▼
[UC4] If active_member_matched:
      distinct_faces = len(known_members) + len(unknown_face_clusters)
      If distinct_faces > memberCount → group_size_mismatch alert
      [UC4+UC8] If max_simultaneous_persons > distinct_faces:
        → Additional signal in payload (persons not face-captured)
```

### UC Applicability (Initial Scope Only)

| UC | P1 (no lock) | P2 (with lock) |
|----|:---:|:---:|
| UC1: Member ID | Log only | Unlock via clicked |
| UC2: Tailgating | N/A | Alert (after unlock) |
| UC3: Unknown Face | Log + S3 | Log + S3 |
| UC4: Group Size | N/A | Alert at session end |
| UC5: Non-Active | Alert | Alert + Block |
| UC8: Body Detection | Gate + Continuous + Extend | Gate + Continuous + Extend |

---

# Data Sources Summary

| Category | Source | Use-Cases |
|----------|--------|-----------|
| ACTIVE | TBL_RESERVATION (active dates) + TBL_MEMBER | UC1 (unlock), UC2, UC3, UC4 |
| INACTIVE | TBL_RESERVATION (past N days) + TBL_MEMBER | UC3, UC5-inactive (alert) |
| STAFF | TBL_RESERVATION (staff flag) + TBL_MEMBER | UC3 |
| BLOCKLIST | TBL_RESERVATION (blocklist flag) + TBL_MEMBER | UC3, UC5-blocklist (block) |
| UNKNOWN | Runtime storage (no pre-existing data) | UC3, UC6 |

---

# IoT Topics Summary

| Topic | Use-Case | Priority | Scope |
|-------|----------|----------|-------|
| `gocheckin/{thing}/member_detected` | UC1 | Normal | Current |
| `gocheckin/{thing}/tailgating_alert` | UC2 | Normal | New |
| `gocheckin/{thing}/unknown_face_detected` | UC3 | Low | New |
| `gocheckin/{thing}/group_size_mismatch` | UC4 | Normal | New |
| `gocheckin/{thing}/non_active_member_alert` | UC5 | Normal / HIGH | New |
| `gocheckin/{thing}/loitering_alert` | UC6 | Normal | Future |
| `gocheckin/{thing}/after_hours_access` | UC7 | Normal | Future |
| `gocheckin/{thing}/spoofing_alert` | UC9 | HIGH | Future |

**Note:** UC8 (Human Body Detection) has no IoT topic. It is an operational control (session gate + extend), not an alerting mechanism.

**`member_detected` payload changes**: `onvifTriggered` field removed — ONVIF never triggers unlock directly. `occupancyTriggeredLocks` renamed to `clickedLocks` — reflects unified clicked signal (occupancy sensor + LOCK_BUTTON). When `clickedLocks` is present and non-empty, unlock was triggered by a clicked event for those specific locks.

---

# Handler Priority

| Priority | Handler | Action on Match | Scope |
|----------|---------|-----------------|-------|
| 5 | Non-Active Member (BLOCKLIST) | Alert (HIGH), set `block_further_unlocks` | New (UC5) |
| 10 | Member Identification | Unlock (once, via clicked), log, set `unlocked` | Current (UC1) |
| 20 | Non-Active Member (INACTIVE) | Alert | New (UC5) |
| 30 | Tailgating | Alert (requires `unlocked=true`) | New (UC2) |
| 38 | Group Size | Alert at session end (requires `active_member_matched`) | New (UC4) |
| 40 | Unknown Face | Log | New (UC3) |
| 45 | Loitering | Alert (cross-session) | Future (UC6) |
| 8 | Anti-Spoofing (dedicated camera) | Multi-layer gate: face size + device detection + blink pattern → block if any fail | Future (UC9) |

All handlers run on every frame within the continuous detection loop. "Priority" determines evaluation order within a single frame, not mutual exclusion — multiple handlers can fire on the same frame.

**UC8 (Human Body Detection) is NOT in this table.** UC8 operates at session lifecycle level, not per-frame:
- **Gate**: YOLOv8n runs once before the detection loop starts (after ONVIF, before SCRFD+ArcFace)
- **Extend**: Dual-signal check (recent motion + YOLOv8n person present) runs when the timer is about to expire (decides whether to extend)

---

# Configuration Variables

```bash
# Core — two-threshold approach
FACE_DETECT_THRESHOLD=0.3             # SCRFD detection confidence (lower to catch masked faces)
FACE_RECOG_THRESHOLD=0.45             # ArcFace cosine similarity for known member match
TIMER_DETECT=10                       # Detection session duration in seconds (existing, function.conf)

# UC2: Tailgating
TAILGATE_WINDOW_SEC=10                # Seconds to monitor after unlock

# UC4: Group Size — unknown face clustering
UNKNOWN_FACE_CLUSTER_THRESHOLD=0.45   # Cosine similarity threshold for same-person clustering
FACE_IOU_THRESHOLD=0.5                # Bbox IoU threshold for spatial continuity (consecutive frames)

# UC5: Non-Active Member
INACTIVE_MEMBER_DAYS_BACK=30          # Days to look back for past guests
BLOCKLIST_PREVENTS_UNLOCK=true        # Block unlock for entire session if blocklist match

# UC8: Human Body Detection (gate + continuous + extend)
YOLO_DETECT_THRESHOLD=0.5             # YOLOv8n confidence threshold for person class 0
YOLO_GATE_FRAMES=10                   # Number of frames for gate check (at 10fps = 1 second)
YOLO_GATE_MIN_DETECTIONS=3            # Min frames with person detected to pass gate (any 3 of 10)
YOLO_EXTEND_LOOKBACK=10              # Number of recent frames from continuous buffer to check at extend
YOLO_EXTEND_MIN_DETECTIONS=3         # Min frames with person detected to pass extend check
MOTION_RECENCY_SEC=5                  # Seconds to consider a motion event "recent" for dual-signal extension

# Feature flags
ENABLE_TAILGATING_DETECTION=true      # UC2
ENABLE_UNKNOWN_FACE_LOGGING=true      # UC3
ENABLE_GROUP_VALIDATION=true          # UC4
ENABLE_NON_ACTIVE_MEMBER_ALERT=true   # UC5
```

**Note:** `withKeypad` is no longer used for unlock decisions. All locks behave the same — a clicked event (from any source) is required to unlock. Lock type metadata may still exist in the data model for hardware configuration but does not affect unlock logic.

---

# Non-Functional Requirements

## NFR1: Backend Compatibility

Only UC1 (Authorized Member Identification) is required to work with all inference backends.
UC2-UC5, UC8 only need to work with the primary backend in use at runtime.
UC6, UC7, UC9 are future scope.

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
│   ├── Security handler integration (UC1-UC5)
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

1. **Scope**: Initial version implements UC1-UC5 + UC8 with three models (SCRFD + ArcFace + YOLOv8n). UC6, UC7, UC9 are future
2. **Facility types**: All (vacation rentals, hotels, offices)
3. **UC3 databases**: All categories (ACTIVE, INACTIVE, STAFF, BLOCKLIST) loaded from TBL_RESERVATION with different filters
4. **UC3 storage**: Snapshot to S3 only (no embedding storage in initial version)
5. **UC4+UC5 merged**: Old UC4 (Multi-Face Group Validation) and old UC5 (Person Count Mismatch) merged into single UC4 (Group Size Validation) with one combined `group_size_mismatch` alert
6. **UC4 distinct counting is face-only**: Session-level distinct person count uses ArcFace face embeddings + bbox IoU (dual-signal clustering). Known faces deduplicated by member ID, unknown faces (including masked) clustered by embedding similarity and spatial continuity. No body tracking needed
7. **Initial scope needs three models**: SCRFD (face detection) + ArcFace (face recognition) + YOLOv8n (human body detection for UC8 session lifecycle control). All three have pre-compiled Hailo-8 and Hailo-8L HEFs
8. **UC5+UC6 merged**: Old UC5 (Inactive Member Alert) and old UC6 (Blocklist Detection) merged into single UC5 (Non-Active Member Alert) with sub-types INACTIVE and BLOCKLIST. Single `non_active_member_alert` IoT topic with `sub_type` field
9. **Blocklist block configurable**: `BLOCKLIST_PREVENTS_UNLOCK` controls whether blocklist detection blocks unlock for the entire session (default: true)
10. **Alert action**: IoT publish only (cloud handles notifications)
11. **Focus**: Security use-cases prioritized
12. **Architecture**: Separate inference backends from business logic (NFR2)
13. **UC1 always runs**: On all camera-lock patterns including no-lock cameras (P1). On P1, UC1 publishes `member_detected` without unlock action (log only)
14. **Detection sequence is pattern-specific**: Two camera-lock patterns (P1-P2) define when detection starts, what triggers it, and what unlock actions result. Documented in Camera-Lock Patterns section with detailed timelines
15. **Continuous detection loop**: Detection does NOT stop after UC1 match/unlock. The loop runs every frame for the full timer. Unlock is a one-time side effect; all UCs continue to fire on every subsequent frame. All handlers run on every frame (priority = evaluation order, not mutual exclusion)
16. **Post-unlock blocklist policy**: If a blocklist person is detected AFTER the door has already been unlocked, the system does NOT re-lock (can't undo). Instead it sets `block_further_unlocks` to prevent any additional locks from being unlocked for the remainder of the session (e.g., a lock whose clicked event arrives later)
17. **Behavioral change from current code**: Current code stops detection immediately on UC1 match (`stop_feeding()`). New design continues the detection loop for the full `TIMER_DETECT` duration after match — unlock is a one-time side effect, not a session terminator. This is required for UC2-UC5 to function
18. **Two-threshold approach for masked faces**: `FACE_DETECT_THRESHOLD` (SCRFD, lower, e.g. 0.3) and `FACE_RECOG_THRESHOLD` (ArcFace, higher, e.g. 0.45). Masked faces pass detection but fail recognition → classified as unknown → still counted in UC4 via embedding + bbox IoU clustering. Per-camera threshold tuning possible since each camera has fixed angle/lighting/distance
19. **UC6 requires cloud**: Loitering detection needs unknown face embeddings persisted across sessions over days/weeks — cloud-side data aggregation
20. **UC7 deferred**: Trivial time check but deferred to reduce initial scope
21. **UC8 redefined and moved to initial scope**: UC8 changed from "Human Without Face Detection" (alerting) to "Human Body Detection" (session lifecycle control + continuous person presence). Three roles: (1) Gate — YOLOv8n runs on `YOLO_GATE_FRAMES` (10) frames before session, person must be detected in ≥ `YOLO_GATE_MIN_DETECTIONS` (3) frames to pass, filters false ONVIF triggers, (2) Continuous — YOLOv8n runs on every frame during session alongside SCRFD+ArcFace at 6.4% NPU (measured on Hailo-8 at 10fps), provides real-time person count and `max_simultaneous_persons` for UC4, (3) Extend — at timer expiry, checks continuous buffer (person in ≥3 of last 10 frames) + motion recency — zero additional NPU cost. Total per camera: ~41.4% (35% face + 6.4% YOLO). Two cameras: ~82.8%
22. **YOLOv8n gate on both patterns**: Both camera-lock patterns (P1-P2) run YOLOv8n as first step after ONVIF motion, before SCRFD+ArcFace. Filters non-human ONVIF triggers
23. *(Removed — surveillance mode is now the default P2 behavior, not a separate pattern)*
24. **Session extension by dual-signal detection**: Session extension at timer expiry requires both recent motion (event within `MOTION_RECENCY_SEC`) AND person presence in recent frames from the continuous detection buffer (person in ≥ `YOLO_EXTEND_MIN_DETECTIONS` of last `YOLO_EXTEND_LOOKBACK` frames) — either signal alone is insufficient. No separate YOLOv8n burst needed — the continuous buffer already has the data. No explicit cap needed — motion recency is a natural session limiter (ONVIF is single-fire, so motion signal goes stale unless new events arrive)
25. **UC9 dedicated camera with multi-layer defense**: Face anti-spoofing runs only on dedicated close-range verification cameras near locks. Four defense layers: (1) face recognition, (2) face size validation against expected range at known distance, (3) device detection via YOLOv8n COCO classes 62/63/67 — detects phone/tablet/screen containing the face, (4) blink pattern challenge via tddfa_mobilenet_v1 68-point landmarks + Eye Aspect Ratio. Blink pattern is per-reservation, delivered via booking app/SMS — no on-site instruction device needed. All four models (SCRFD, ArcFace, tddfa_mobilenet_v1, YOLOv8n) have pre-compiled Hailo HEFs. YOLOv8n shared with UC8
26. *(Removed — watch mode and `BODY_EXTEND_MAX_SEC` cap eliminated. Dual-signal extension (motion recency + person detection) naturally limits sessions without an explicit cap or post-cap state machine)*
27. **UC8 gates video recording**: YOLOv8n person detection gates the existing GStreamer RTSP recording pipeline alongside SCRFD+ArcFace. No person = no recording = storage savings on false ONVIF triggers. No new config variables — uses existing GStreamer recording parameters (`RECORD_AFTER_MOTION_SECOND`, pre-buffer)
28. **Lock simplification**: All locks require a "clicked" signal to unlock. The `withKeypad` flag is no longer used for unlock decisions. The `onvifTriggered` field is removed from `member_detected` payload — ONVIF never triggers unlock directly. Four camera-lock patterns (P1-P4) consolidated to two (P1-P2)
29. **"Clicked" unifies occupancy and LOCK_BUTTON signals**: Occupancy sensor activation (keypad locks like MTR001DC/MTR001AC) and LOCK_BUTTON press (GreenPower_2 kinetic switch) are treated as the same trigger type — a "clicked" event that makes a specific lock eligible for unlock
30. **Immediate unlock on clicked if member already matched**: When a clicked event arrives and `active_member_matched=true` already (face was recognized during surveillance mode), unlock happens immediately without requiring the person to re-present their face. This avoids poor UX where the system "forgets" a match
31. **`member_detected` payload simplification**: `onvifTriggered` field removed (ONVIF never triggers unlock). `occupancyTriggeredLocks` renamed to `clickedLocks` (reflects unified clicked signal from occupancy sensor + LOCK_BUTTON). Payload now uses `clickedLocks` to indicate which locks were unlocked via clicked events
32. **Session extension dual-signal**: Both recent motion event (within `MOTION_RECENCY_SEC`) AND person presence in recent frames from the continuous YOLOv8n detection buffer (person in ≥ `YOLO_EXTEND_MIN_DETECTIONS` of last `YOLO_EXTEND_LOOKBACK` frames) required to extend session at timer expiry. No separate YOLOv8n burst needed — the continuous buffer already has the data, adding zero NPU cost at extend time. Person detection alone may false-positive on static figures; motion alone may be non-human. Together they confirm something is moving AND it's a person. The dual-signal requirement eliminates the need for `BODY_EXTEND_MAX_SEC` cap and watch mode — motion recency is a natural session limiter (ONVIF is single-fire, so without new motion events the signal goes stale and the session ends). Motion source is event-based and pluggable — currently ONVIF events (single-fire, recency based on last event timestamp), future H265 HW frame-level decoding will emit equivalent timestamped events per camera via the same interface. The motion source's **notification gap** (camera-side, tunable per camera/location) works together with `MOTION_RECENCY_SEC` to govern session duration. New config: `MOTION_RECENCY_SEC`, `YOLO_EXTEND_LOOKBACK`, `YOLO_EXTEND_MIN_DETECTIONS`
34. **UC8 multi-frame gate (10 frames, any 3 of 10)**: Single-frame YOLOv8n detection is too fragile for the gate decision. The gate is high-stakes — a false negative skips the entire SCRFD+ArcFace session (no face recognition at all). A person may be missed in any single frame due to motion blur, partial occlusion, or awkward body angle. Running YOLOv8n over `YOLO_GATE_FRAMES` (default: 10, = 1 second at 10fps) frames and requiring person detection in ≥ `YOLO_GATE_MIN_DETECTIONS` (default: 3) frames provides robust detection. The "any 3 of 10" threshold balances: single-frame shadows/reflections won't trigger false sessions (requires 3+ frames), while a real person only needs to be visible in 3 of 10 frames (tolerates occlusion/blur in 7 frames). Gate adds 1 second latency before session starts — acceptable since person is still approaching after ONVIF fired. Extend no longer uses a separate burst — it reads from the continuous detection buffer (see Decision 35). New config: `YOLO_GATE_FRAMES=10`, `YOLO_GATE_MIN_DETECTIONS=3`
35. **UC8 continuous YOLOv8n during session**: Feasibility testing on Hailo-8 measured YOLOv8n at only **6.4% NPU utilization** at 10fps continuous (via `HAILO_MONITOR`). This is low enough to run alongside SCRFD+ArcFace (~35%) with comfortable headroom (~41.4% total per camera, ~82.8% for 2 cameras). At this cost, burst-only gate/extend windows are not justified — YOLOv8n runs on every frame during the detection session. Benefits: (1) extend decisions read from the continuous buffer instead of running a separate inference burst — zero additional NPU cost at extend time, (2) per-frame person count enables `max_simultaneous_persons` accumulator — the peak person count seen in any single frame during the session, (3) `max_simultaneous_persons` enhances UC4 by catching people whose faces were never captured (back turned, masked, outside camera angle) — if max_simultaneous_persons > distinct_face_count, the system knows faces were missed. Note: this is a lower bound on total individuals (not total distinct — that would require Person Re-ID which is not needed for current scope)
33. **Risk assessment for unbounded session extension**: Six risks were evaluated after removing the explicit session cap (`BODY_EXTEND_MAX_SEC`) and watch mode. Resolutions: (1) **Unbounded sessions on RPi** — mitigated by the ONVIF notification gap, which is tunable per camera/location; busy locations set 20-30s gap to naturally space out sessions, quiet locations set 0s for maximum responsiveness. (2) **ONVIF not truly single-fire** — the ONVIF notification gap is camera-side and reliably throttles ALL motion events, confirming single-fire behavior. (3) **Future H265 HW motion defeats limiter** — H265 HW motion source will also have a notification gap setting, same as ONVIF; the notification gap is a property of the motion source abstraction. (4) **Recording gaps for lingering persons** — acceptable; a person standing still is not a threat, and recording resumes on the next motion event. (5) **Session state reset on re-trigger** — acceptable; each session is independent, and cloud-side can deduplicate across sessions. (6) **`MOTION_RECENCY_SEC` hard to tune** — `MOTION_RECENCY_SEC` is a global default; operators tune the ONVIF notification gap per camera to match their location's activity level. Conclusion: no explicit session cap needed — the combination of motion source notification gap (camera-side, per-location) and `MOTION_RECENCY_SEC` (software-side, global) provides sufficient session duration control
