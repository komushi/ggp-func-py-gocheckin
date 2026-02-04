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
- Unlock door (on cameras with locks — see Camera-Lock Patterns)
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

---

## UC2: Tailgating Detection [NEW]

**Status**: To be implemented

**Scenario**: After an authorized guest unlocks the door, an unauthorized person follows them through before the door closes.

**Trigger**: Part of the continuous detection loop — fires when an unknown face appears in any frame after `unlocked=true` has been set in the current session

**Input**:
- Camera frames (continuous detection loop, same as all other UCs)
- ACTIVE member database
- Session state: `unlocked=true`

**Logic**:
- UC1 matched and set `unlocked=true` at some frame
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

**Output**:
- Publish `group_size_mismatch` alert to IoT
- Include: distinct_face_count, known_count, unknown_count, memberCount, matched_members, snapshot

**Models**:
- SCRFD + ArcFace only — already running for UC1/UC3. Face embeddings reused for clustering. Bbox positions reused for IoU. No extra model needed.

**Session Duration** (from codebase — `TIMER_DETECT` in `function.conf`, `extend_timer()` in `gstreamer_threading.py`):
- Base: `TIMER_DETECT` = 10 seconds (configurable)
- At 10 fps → 100 frames per session baseline
- Extended by occupancy triggers: each occupancy event adds up to `TIMER_DETECT` seconds (`elapsed + TIMER_DETECT`)
- Extended by ONVIF if session was started by ONVIF
- With extensions, sessions can reach 20-30+ seconds (200-300 frames)
- 10-20 seconds at a doorway is sufficient for all group members to show their faces at least once

**Extension rules** (from `py_handler.py:trigger_face_detection()`):

| Running session started by | New trigger | Extends? |
|---|---|---|
| ONVIF | ONVIF | Yes |
| ONVIF | Occupancy | Yes |
| Occupancy | Occupancy #2 | Yes |
| Occupancy | ONVIF | No |
| Any | **YOLOv8n person detected at timer expiry** | **Yes (up to BODY_EXTEND_MAX_SEC)** |

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

**Scenario**: YOLOv8n (COCO person class 0) provides **session lifecycle control** — deciding whether to start and whether to extend a detection session. UC8 is NOT an alerting mechanism. It is an operational control that filters false ONVIF triggers (wind, shadow, animal motion) before committing SCRFD+ArcFace resources.

**Three roles:**

### Role 1: Gate (session start + recording start)

After ONVIF fires, YOLOv8n runs on the grabbed frame. If a person (class 0) is detected with confidence ≥ `YOLO_DETECT_THRESHOLD`, the detection session starts (SCRFD+ArcFace loop) AND video recording starts (GStreamer RTSP pipeline). If no person is detected, the trigger is discarded as a false alarm — no session, no recording.

```
ONVIF camera detects motion
       │
       ▼
GStreamer grabs frame
       │
       ▼
Run YOLOv8n (person class 0) → person body detected?
  NO  → skip (false alarm: wind/shadow/animal — no session, no recording)
  YES → start recording + start detection session (SCRFD + ArcFace loop)
```

### Role 2: Extend (session timer expiry)

When the detection timer is about to expire, YOLOv8n runs on the current frame. If a person is still present, the timer is extended — recording continues alongside the session. This continues up to `BODY_EXTEND_MAX_SEC` total session duration (prevents infinite extension if someone stands at the camera indefinitely). When the session ends (person left or cap reached), recording ends with it.

```
Timer about to expire
       │
       ▼
Run YOLOv8n on current frame → person body still present?
  YES → extend timer (up to BODY_EXTEND_MAX_SEC total), recording continues
  NO  → end session normally, recording ends
```

### Role 3: Watch (post-cap monitoring + recording)

When a session ends because `BODY_EXTEND_MAX_SEC` is reached (person still present, session forced to end), the camera enters **watch mode** instead of returning to idle. Watch mode uses YOLOv8n only — no SCRFD+ArcFace — and only runs on ONVIF events (no polling, no timers).

Watch mode prevents infinite back-to-back full sessions (SCRFD+ArcFace) when a person remains at the camera after the session cap. Without watch mode, ONVIF would re-trigger and chain new full sessions indefinitely.

**Watch mode recording**: When YOLOv8n detects a person during watch mode (person still lingering after session cap), video recording starts using existing GStreamer recording parameters (`RECORD_AFTER_MOTION_SECOND`, pre-buffer). No SCRFD+ArcFace runs — recording only. This captures security-relevant footage of someone lingering at the camera after a full detection session.

**State machine:**

```
Session hits BODY_EXTEND_MAX_SEC cap
       │
       ▼
ENTER WATCH MODE (per-camera)
  State: person_was_seen = true
       │
       ▼
  ┌─── ONVIF fires during watch mode ───┐
  │                                       │
  │  Run YOLOv8n (person class 0)         │
  │                                       │
  │  Person detected?                     │
  │    YES + person_was_seen=true          │
  │      → same person still there         │
  │      → start recording (no SCRFD)      │
  │      → stay in watch mode              │
  │                                       │
  │    NO                                  │
  │      → person left                     │
  │      → set person_was_seen=false       │
  │      → stay in watch mode (armed)      │
  │                                       │
  │    YES + person_was_seen=false          │
  │      → person LEFT then RETURNED       │
  │      → EXIT watch mode                 │
  │      → start new full session          │
  │        (Gate already passed — skip     │
  │         redundant YOLOv8n gate)        │
  │        (recording starts via new       │
  │         session's Gate)                │
  └───────────────────────────────────────┘

  Occupancy trigger during watch mode:
    → EXIT watch mode immediately
    → start full session (occupancy = different signal)
    → recording starts via new session's Gate
```

**What triggers watch mode vs normal session end:**

| Session end reason | Next state |
|---|---|
| Timer expired + YOLOv8n extend says NO person | Normal end (no watch mode — person already left) |
| Timer expired + BODY_EXTEND_MAX_SEC cap reached | **Watch mode** (person still there, session forced to end) |
| Occupancy sensor goes inactive + no other triggers | Normal end |

**Key properties:**
- No SCRFD+ArcFace in watch mode — only YOLOv8n runs, on ONVIF events
- Requires leave-then-return transition — prevents re-processing the same person who never left
- Occupancy bypasses watch mode — occupancy is a lock-level signal, always starts a full session
- Zero resource cost when idle — watch mode is just a per-camera flag, no polling, no timers
- No additional config needed — watch mode is inherent behavior when BODY_EXTEND_MAX_SEC is reached

**Model**: YOLOv8n (COCO 80-class, person = class 0) — pre-compiled HEF available for Hailo-8 and Hailo-8L in [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo)

**Output**: No IoT alert. UC8 is a control decision, not a notification. It produces no `human_body_alert` topic — it gates session start and video recording, extends session duration (recording continues), and monitors post-cap presence (recording without SCRFD+ArcFace). No person detected = no session = no recording.

**Configuration**:
- `YOLO_DETECT_THRESHOLD=0.5` — YOLOv8n confidence threshold for person class 0
- `BODY_EXTEND_MAX_SEC=30` — Maximum total session duration with body-based extensions (prevents infinite loops)

**NPU impact**: Negligible per session. YOLOv8n runs gate-only: 1 inference at session start + 1 at timer expiry check + 1 per ONVIF event during watch mode. NOT every frame.

**Key Behaviors**:
- Applies to ALL 4 camera-lock patterns (P1-P4) as the first step before SCRFD+ArcFace
- Gates video recording alongside SCRFD+ArcFace — false ONVIF triggers (no person) produce neither face detection nor video recording, saving storage
- Enables P3 cameras to run surveillance UCs on ONVIF events (previously skipped)
- Session extension by body detection is capped at `BODY_EXTEND_MAX_SEC` to prevent infinite sessions
- When session ends due to cap (person still present), camera enters watch mode (YOLOv8n only on ONVIF events, no SCRFD+ArcFace) until person leaves then returns
- In watch mode, person detection triggers video recording without SCRFD+ArcFace (captures lingering presence using existing GStreamer recording parameters)
- Does not replace existing extension rules (ONVIF/occupancy extensions still apply)

---

## UC9: Face Anti-Spoofing (Liveness Detection) [FUTURE]

**Status**: Future implementation

**Scenario**: A person holds up a printed photo or phone/tablet displaying someone's face to trick the face recognition system into unlocking the door. A dedicated close-range camera near the keypad lock verifies the person is real through multiple defense layers.

**Camera Type**: This is NOT a general surveillance/recognition camera. It is a dedicated verification camera:
- Mounted close to the keypad lock (arm's length)
- Fixed close-range framing (face fills most of frame)
- Controlled angle and lighting
- Purpose: high-quality face capture for liveness verification only
- Does not participate in UC2 (tailgating), UC3 (unknown logging), UC4 (group counting), etc.

**Trigger**: Occupancy sensor on keypad lock → start detection on dedicated camera

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
Occupancy sensor triggers
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
- This camera is always associated with a keypad lock (P3 or P4)
- It is triggered by occupancy sensor (person at the lock)
- Other cameras on the same door still run normal UC1-UC5 pipeline
- The dedicated camera runs its own UC9 pipeline (not the standard continuous detection loop)

---

# Camera-Lock Patterns

From codebase (`py_handler.py`): cameras store locks in `camera_item['locks']` with `withKeypad` flag.

## Pattern Definitions

| Pattern | Locks | Detection Trigger | Unlock Action |
|---------|-------|-------------------|---------------|
| P1: No lock | None | ONVIF motion | None (surveillance / visual verification) |
| P2: Legacy lock | `withKeypad=false` | ONVIF motion | Unlock all legacy locks |
| P3: Keypad lock | `withKeypad=true` | Occupancy sensor | Unlock specific keypad lock |
| P4: Both locks | Legacy + Keypad | ONVIF or occupancy (merged) | Unlock appropriate locks |

## Session State

Every detection session maintains these (reset per session):

**Flags**:

| Flag | Default | Set by | Effect |
|------|---------|--------|--------|
| `unlocked` | `false` | UC1 (active member match) | Prevents re-triggering unlock for the same lock set; enables UC2 tailgating checks |
| `block_further_unlocks` | `false` | UC5-blocklist (when `BLOCKLIST_PREVENTS_UNLOCK=true`) | Prevents any new unlock actions for remainder of session (already-unlocked locks are NOT re-locked) |
| `active_member_matched` | `false` | UC1 | Enables UC4 group size comparison against `memberCount` |
| `unlocked_locks` | `{}` (empty set) | UC1 unlock action | Tracks which specific locks have been unlocked (P3/P4: per-lock granularity) |

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

**P4 note**: In P4 (both lock types), `unlocked` is set to `true` when legacy locks are unlocked, but a newly-arriving keypad lock (via occupancy trigger) is tracked separately in `unlocked_locks`. This allows: legacy unlocked at T=0, keypad lock_456 still lockable/unlockable independently at T=M.

## Detection Timelines

### P1: Camera with no lock (surveillance / visual verification)

Example: Common area camera, reception desk, parking lot.

```
Person approaches area
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frame
       │  [UC8 GATE] Run YOLOv8n (person class 0)
       │    → NO person detected? → SKIP (no session, no recording)
       │    → YES person detected? → start recording + continue ▼
       │
       │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Camera has NO locks → still proceed (UC1 always runs)
       │  Session state: unlocked=N/A, block_further_unlocks=N/A
       │  GStreamer starts feeding frames
       │
       ▼
T=0+   CONTINUOUS DETECTION LOOP (every frame, full timer)
       ┌─────────────────────────────────────────────────────────┐
       │ INFERENCE (every frame):                                 │
       │   SCRFD  → face bboxes + 5-point landmarks              │
       │   ArcFace → 512-d embedding per face                    │
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
       │  [UC8 EXTEND] Run YOLOv8n on current frame
       │    → Person still present AND total session < BODY_EXTEND_MAX_SEC?
       │      YES → extend timer, recording continues
       │      NO  → let session end normally, recording ends
       │
T=cap  BODY_EXTEND_MAX_SEC reached, person still present
       │  [UC8 WATCH] ENTER WATCH MODE (YOLOv8n only, no SCRFD+ArcFace)
       │  State: person_was_seen = true
       │
       │  ONVIF fires → run YOLOv8n (person class 0):
       │    Person YES + person_was_seen=true  → start recording (no SCRFD+ArcFace)
       │                                         stay in watch (same person)
       │    Person NO                          → set person_was_seen=false (armed)
       │    Person YES + person_was_seen=false  → EXIT watch → new full session
       │                                         (recording starts via new session's Gate)
       │  Occupancy trigger → EXIT watch mode → full session
       │
T=end  Session ends (normal end OR watch mode entered)
       │
       │  SESSION-LEVEL CHECKS:
       │  [UC4] If active_member_matched=true:
       │    distinct_faces = len(known_members) + len(unknown_face_clusters)
       │    If distinct_faces > memberCount → Publish group_size_mismatch
       │
       │  [UC6] Cross-session analysis (future):
       │  Same unknown face across sessions → loitering_alert
```

**P1 notes**: No unlock/lock state. No UC2 tailgating (no door to tailgate through). UC4 evaluated at session end using accumulated face data. Watch mode applies when BODY_EXTEND_MAX_SEC forces session end while person is still present.

### P2: Camera with legacy lock

Example: Front door with a legacy smart lock (no occupancy sensor).

```
Person approaches door
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frame
       │  [UC8 GATE] Run YOLOv8n (person class 0)
       │    → NO person detected? → SKIP (no session, no recording)
       │    → YES person detected? → start recording + continue ▼
       │
       │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Camera HAS legacy locks → start detection
       │  Context: started_by_onvif=True, onvif_triggered=True
       │  Session state: unlocked=false, block_further_unlocks=false
       │  GStreamer starts feeding frames
       │
       ▼
T=0+   CONTINUOUS DETECTION LOOP (every frame, full timer)
       ┌─────────────────────────────────────────────────────────┐
       │ INFERENCE (every frame):                                 │
       │   SCRFD  → face bboxes + 5-point landmarks              │
       │   ArcFace → 512-d embedding per face                    │
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
       │    │   If unlocked=false AND block_further_unlocks=false:│
       │    │     → UNLOCK ALL LEGACY LOCKS                       │
       │    │     → Set unlocked=true                             │
       │    │     → Publish member_detected                       │
       │    │     → Save snapshot to S3                           │
       │    │   If unlocked=true:                                 │
       │    │     → Skip (already unlocked this session)          │
       │    │   If block_further_unlocks=true:                    │
       │    │     → Publish member_detected (blocked=true)        │
       │    │     → Do NOT unlock                                 │
       │    │                                                     │
       │    ├─ INACTIVE → [UC5-inactive]                          │
       │    │   Add to known_members[id]={category:INACTIVE}      │
       │    │   Publish non_active_member_alert                   │
       │    │   Do NOT unlock                                     │
       │    │                                                     │
       │    └─ STAFF → Add to known_members, log only             │
       │                                                          │
       │   NO (below threshold for all):                          │
       │     [UC3] Cluster into unknown_face_clusters             │
       │       (by embedding similarity + bbox IoU)               │
       │     Publish unknown_face_detected                        │
       │     Save snapshot to S3                                  │
       │     If unlocked=true → also [UC2] tailgating_alert       │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues until timer expires / motion stops
       │
T=exp  Timer about to expire
       │  [UC8 EXTEND] Run YOLOv8n on current frame
       │    → Person still present AND total session < BODY_EXTEND_MAX_SEC?
       │      YES → extend timer, recording continues
       │      NO  → let session end normally, recording ends
       │
T=cap  BODY_EXTEND_MAX_SEC reached, person still present
       │  [UC8 WATCH] ENTER WATCH MODE (YOLOv8n only, no SCRFD+ArcFace)
       │  State: person_was_seen = true
       │
       │  ONVIF fires → run YOLOv8n (person class 0):
       │    Person YES + person_was_seen=true  → start recording (no SCRFD+ArcFace)
       │                                         stay in watch (same person)
       │    Person NO                          → set person_was_seen=false (armed)
       │    Person YES + person_was_seen=false  → EXIT watch → new full session
       │                                         (recording starts via new session's Gate)
       │  Occupancy trigger → EXIT watch mode → full session
       │
T=end  Session ends (normal end OR watch mode entered)
       │
       │  SESSION-LEVEL CHECKS:
       │  [UC4] If active_member_matched=true:
       │    distinct_faces = len(known_members) + len(unknown_face_clusters)
       │    If distinct_faces > memberCount → Publish group_size_mismatch
       │
       │  [UC6] Cross-session analysis (future):
       │  Same unknown face across sessions → loitering_alert
```

**P2 example scenario** — blocklist detected AFTER unlock:
1. Frame 3: Active member detected → `unlocked=true`, door opens
2. Frame 7: Blocklist person detected → `block_further_unlocks=true`, alert fires
3. Door is already open (no re-lock). Flag prevents further unlock extensions.
4. Frame 9: Another active member detected → `unlocked=true` already, skip. Even if it weren't, `block_further_unlocks` would prevent it.
5. Remaining frames: all UCs keep firing (UC3, UC4, UC5...)

### P3: Camera with keypad lock (occupancy sensor)

Example: Door with smart lock that has occupancy/proximity sensor.

```
Person approaches door
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frame
       │  [UC8 GATE] Run YOLOv8n (person class 0)
       │    → NO person detected? → SKIP (no session, no recording)
       │    → YES person detected? → start recording + continue ▼
       │
       │  START DETECTION IN SURVEILLANCE MODE (P1 behavior)
       │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Context: started_by_onvif=True, surveillance_mode=True
       │  Session state: unlocked=N/A (no unlock in surveillance mode)
       │  GStreamer starts feeding frames
       │
       ▼
T=0+   CONTINUOUS DETECTION LOOP (surveillance mode — P1 behavior)
       ┌─────────────────────────────────────────────────────────┐
       │ (Same inference + identification as P1)                  │
       │                                                          │
       │   ACTIVE → [UC1] Log only (no unlock — surveillance)     │
       │   BLOCKLIST → [UC5-blocklist] Alert only (no lock)       │
       │   INACTIVE → [UC5-inactive] Alert                        │
       │   Unknown → [UC3] Log + S3                               │
       │                                                          │
       │ No UC2 tailgating (no unlock has occurred)               │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues...
       │
T=N    │  Occupancy sensor on keypad lock triggers
       │  trigger_face_detection(cam_ip, lock_asset_id="lock_123")
       │  MERGE into running session — UPGRADE to unlock mode:
       │    specific_locks += {"lock_123"}
       │    active_occupancy += {"lock_123"}
       │    surveillance_mode=False
       │    Session state: unlocked=false, block_further_unlocks=false
       │    Extend detection timer
       │
       │  Now keypad lock_123 is eligible for unlock
       │
       ▼
T=N+   DETECTION LOOP CONTINUES (upgraded to unlock mode)
       ┌─────────────────────────────────────────────────────────┐
       │ (Same inference + identification as P2)                  │
       │                                                          │
       │   ACTIVE → [UC1]                                         │
       │     If unlocked=false AND block_further_unlocks=false:   │
       │       → UNLOCK SPECIFIC KEYPAD LOCK (lock_123) ONLY     │
       │       → payload: occupancyTriggeredLocks=["lock_123"]   │
       │       → Set unlocked=true                                │
       │     If block_further_unlocks=true:                       │
       │       → Do NOT unlock lock_123                           │
       │                                                          │
       │   BLOCKLIST → [UC5-blocklist]                            │
       │     Set block_further_unlocks=true                       │
       │     (If lock_123 already unlocked: no re-lock.           │
       │      If not yet unlocked: prevents future unlock.)       │
       │                                                          │
       │ (All other UCs — UC2, UC3, UC4, UC5-inactive             │
       │  — same as P2)                                           │
       └─────────────────────────────────────────────────────────┘
       │
       │  If NO occupancy trigger arrives, session runs in
       │  surveillance mode for full timer (P1 behavior)
       │
T=exp  Timer about to expire
       │  [UC8 EXTEND] Run YOLOv8n on current frame
       │    → Person still present AND total session < BODY_EXTEND_MAX_SEC?
       │      YES → extend timer, recording continues
       │      NO  → let session end normally, recording ends
       │
T=cap  BODY_EXTEND_MAX_SEC reached, person still present
       │  [UC8 WATCH] ENTER WATCH MODE (YOLOv8n only, no SCRFD+ArcFace)
       │  State: person_was_seen = true
       │
       │  ONVIF fires → run YOLOv8n (person class 0):
       │    Person YES + person_was_seen=true  → start recording (no SCRFD+ArcFace)
       │                                         stay in watch (same person)
       │    Person NO                          → set person_was_seen=false (armed)
       │    Person YES + person_was_seen=false  → EXIT watch → new full session
       │                                         (recording starts via new session's Gate)
       │  Occupancy trigger → EXIT watch mode → full session
       │
       ▼
T=occ_false  Occupancy sensor goes inactive (if it triggered)
       │  handle_occupancy_false(cam_ip, "lock_123")
       │  Remove lock_123 from active_occupancy
       │  If no more active triggers → stop detection early
```

**P3 key change**: ONVIF no longer skips detection. YOLOv8n gate confirms a person is present, then detection starts in surveillance mode (P1 behavior — log only, no unlock). If an occupancy trigger arrives during the session, the session upgrades to unlock mode for the specific keypad lock (same merge logic as P4). If no occupancy trigger arrives, the session completes as surveillance-only.

### P4: Camera with both lock types

Example: Door with legacy lock AND keypad lock with occupancy sensor.

```
Person approaches door
       │
T=0    │  ONVIF camera detects motion
       │  GStreamer grabs frame
       │  [UC8 GATE] Run YOLOv8n (person class 0)
       │    → NO person detected? → SKIP (no session, no recording)
       │    → YES person detected? → start recording + continue ▼
       │
       │  trigger_face_detection(cam_ip, lock_asset_id=None)
       │  Camera HAS legacy locks → start detection
       │  Context: started_by_onvif=True, onvif_triggered=True
       │  Session state: unlocked=false, block_further_unlocks=false
       │  GStreamer starts feeding frames
       │
       ▼
T=0+   CONTINUOUS DETECTION LOOP (same structure as P2)
       ┌─────────────────────────────────────────────────────────┐
       │ (Same inference + identification as P2)                  │
       │                                                          │
       │ At this point, only legacy locks are eligible for unlock │
       │ (no occupancy trigger yet for keypad)                    │
       │                                                          │
       │   ACTIVE → [UC1]                                         │
       │     If unlocked=false AND block_further_unlocks=false:   │
       │       → UNLOCK LEGACY LOCKS ONLY                         │
       │       → payload: onvifTriggered=true                     │
       │       → Set unlocked=true                                │
       │                                                          │
       │ (All other UCs same as P2)                               │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues...
       │
T=M    │  Occupancy sensor on keypad lock also triggers
       │  trigger_face_detection(cam_ip, lock_asset_id="lock_456")
       │  MERGE into running session:
       │    specific_locks += {"lock_456"}
       │    active_occupancy += {"lock_456"}
       │    Extend detection timer
       │
       │  Now keypad lock_456 is also eligible for unlock
       │
       ▼
T=M+   DETECTION LOOP CONTINUES (with expanded lock set)
       ┌─────────────────────────────────────────────────────────┐
       │ (Same inference + identification)                        │
       │                                                          │
       │   ACTIVE → [UC1]                                         │
       │     Legacy already unlocked (unlocked=true) → skip       │
       │     But lock_456 is NEW and not yet unlocked:            │
       │     If block_further_unlocks=false:                      │
       │       → UNLOCK KEYPAD LOCK lock_456                      │
       │       → payload: occupancyTriggeredLocks=["lock_456"]   │
       │     If block_further_unlocks=true:                       │
       │       → Do NOT unlock lock_456                           │
       │       → (Legacy already open — no re-lock)               │
       │                                                          │
       │   BLOCKLIST → [UC5-blocklist]                            │
       │     Set block_further_unlocks=true                       │
       │     (Prevents unlock of lock_456 if not yet unlocked.    │
       │      Legacy locks already open — no re-lock.)            │
       │                                                          │
       │ (All other UCs same as P2)                               │
       └─────────────────────────────────────────────────────────┘
       │
       │  Loop continues until all triggers expire
       │
T=exp  Timer about to expire
       │  [UC8 EXTEND] Run YOLOv8n on current frame
       │    → Person still present AND total session < BODY_EXTEND_MAX_SEC?
       │      YES → extend timer, recording continues
       │      NO  → let session end normally, recording ends
       │
T=cap  BODY_EXTEND_MAX_SEC reached, person still present
       │  [UC8 WATCH] ENTER WATCH MODE (YOLOv8n only, no SCRFD+ArcFace)
       │  State: person_was_seen = true
       │
       │  ONVIF fires → run YOLOv8n (person class 0):
       │    Person YES + person_was_seen=true  → start recording (no SCRFD+ArcFace)
       │                                         stay in watch (same person)
       │    Person NO                          → set person_was_seen=false (armed)
       │    Person YES + person_was_seen=false  → EXIT watch → new full session
       │                                         (recording starts via new session's Gate)
       │  Occupancy trigger → EXIT watch mode → full session
       │
       ▼
T=occ_false  Occupancy sensor goes inactive
       │  Remove lock_456 from active_occupancy
       │  Detection continues if ONVIF timer still active
```

**P4 example scenario** — blocklist detected between ONVIF and occupancy triggers:
1. T=0: ONVIF triggers, detection starts
2. Frame 3: Active member detected → legacy locks unlock (`unlocked=true`)
3. Frame 7: Blocklist person detected → `block_further_unlocks=true`, alert
4. T=M: Occupancy triggers for lock_456, merges into session
5. Frame at T=M+: Active member still in frame → but `block_further_unlocks=true`, lock_456 stays locked
6. Result: Legacy door open (can't undo), keypad lock blocked (protected by flag)

## UC Applicability Matrix

| UC | P1 (no lock) | P2 (legacy) | P3 (keypad) | P4 (both) | Scope |
|----|:---:|:---:|:---:|:---:|:---:|
| UC1: Member ID | Log only | Unlock all legacy | Unlock specific keypad | Unlock all | Current |
| UC2: Tailgating | N/A (no door) | Alert | Alert | Alert | New |
| UC3: Unknown Face | Log + S3 | Log + S3 | Log + S3 | Log + S3 | New |
| UC4: Group Size | Alert | Alert | Alert | Alert | New |
| UC5: Non-Active Member | Alert only | Alert + Block? | Alert + Block? | Alert + Block? | New |
| UC6: Loitering | Alert | Alert | Alert | Alert | Future |
| UC7: After-Hours | Alert | Alert + Policy | Alert + Policy | Alert + Policy | Future |
| UC8: Human Body Detection | Gate + Extend + Watch | Gate + Extend + Watch | Gate + Extend + Watch | Gate + Extend + Watch | New |
| UC9: Anti-Spoofing | N/A | N/A | Gate unlock | Gate unlock | Future |

- "Block?" = BLOCKLIST sub-type only, configurable via `BLOCKLIST_PREVENTS_UNLOCK`
- "Policy" = QUIET_HOURS_UNLOCK config decides whether to still unlock
- "Gate unlock" = UC9 runs only on dedicated verification cameras (P3/P4 with keypad lock); blocks unlock if spoof detected
- "Gate + Extend + Watch" = YOLOv8n gate runs as first step before SCRFD+ArcFace on all patterns; extend-check runs at timer expiry; watch mode activates when BODY_EXTEND_MAX_SEC cap is reached with person still present

---

# Implementation Scope

## Initial Version (SCRFD + ArcFace + YOLOv8n)
- UC1: Authorized Member Identification (refactor existing)
- UC2: Tailgating Detection
- UC3: Unknown Face Logging
- UC4: Group Size Validation
- UC5: Non-Active Member Alert
- UC8: Human Body Detection (session lifecycle control + recording gate — gate + extend + watch)

Three models: SCRFD (face detection) + ArcFace (face recognition) + YOLOv8n (human body detection). UC1-UC5 run within the continuous detection loop. UC8 operates at session lifecycle level: YOLOv8n gate runs before the detection loop starts (filters false ONVIF triggers and gates video recording — no person = no session = no recording), YOLOv8n extend-check runs when the timer is about to expire (recording continues with session), and YOLOv8n watch mode monitors post-cap presence (no SCRFD+ArcFace, but triggers recording to capture lingering presence) to prevent infinite back-to-back sessions.

## Future Version
- UC6: Loitering Detection — requires cloud-side data aggregation to persist unknown face embeddings across sessions over days/weeks
- UC7: After-Hours Access Attempt — trivial time check, deferred to reduce initial scope
- UC9: Face Anti-Spoofing — dedicated close-range verification camera near keypad lock. Multi-layer defense: face size validation, device detection (YOLOv8n), blink pattern challenge (tddfa_mobilenet_v1). Blink pattern per-reservation, delivered via booking channel. All models have pre-compiled Hailo HEFs

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

**Note:** UC8 (Human Body Detection) has no IoT topic. It is an operational control (session gate + extend + watch), not an alerting mechanism.

---

# Handler Priority

| Priority | Handler | Action on Match | Scope |
|----------|---------|-----------------|-------|
| 5 | Non-Active Member (BLOCKLIST) | Alert (HIGH), set `block_further_unlocks` | New (UC5) |
| 10 | Member Identification | Unlock (once), log, set `unlocked` | Current (UC1) |
| 20 | Non-Active Member (INACTIVE) | Alert | New (UC5) |
| 30 | Tailgating | Alert (requires `unlocked=true`) | New (UC2) |
| 38 | Group Size | Alert at session end (requires `active_member_matched`) | New (UC4) |
| 40 | Unknown Face | Log | New (UC3) |
| 45 | Loitering | Alert (cross-session) | Future (UC6) |
| 8 | Anti-Spoofing (dedicated camera) | Multi-layer gate: face size + device detection + blink pattern → block if any fail | Future (UC9) |

All handlers run on every frame within the continuous detection loop. "Priority" determines evaluation order within a single frame, not mutual exclusion — multiple handlers can fire on the same frame.

**UC8 (Human Body Detection) is NOT in this table.** UC8 operates at session lifecycle level, not per-frame:
- **Gate**: YOLOv8n runs once before the detection loop starts (after ONVIF, before SCRFD+ArcFace)
- **Extend**: YOLOv8n runs once when the timer is about to expire (decides whether to extend)
- **Watch**: YOLOv8n runs on ONVIF events after session cap (monitors leave-then-return transition, no SCRFD+ArcFace)

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

# UC8: Human Body Detection (session lifecycle control)
YOLO_DETECT_THRESHOLD=0.5             # YOLOv8n confidence threshold for person class 0
BODY_EXTEND_MAX_SEC=30                # Max total session duration with body-based extensions

# Feature flags
ENABLE_TAILGATING_DETECTION=true      # UC2
ENABLE_UNKNOWN_FACE_LOGGING=true      # UC3
ENABLE_GROUP_VALIDATION=true          # UC4
ENABLE_NON_ACTIVE_MEMBER_ALERT=true   # UC5
```

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
14. **Detection sequence is pattern-specific**: Four camera-lock patterns (P1-P4) define when detection starts, what triggers it, and what unlock actions result. Documented in Camera-Lock Patterns section with detailed timelines
15. **Continuous detection loop**: Detection does NOT stop after UC1 match/unlock. The loop runs every frame for the full timer. Unlock is a one-time side effect; all UCs continue to fire on every subsequent frame. All handlers run on every frame (priority = evaluation order, not mutual exclusion)
16. **Post-unlock blocklist policy**: If a blocklist person is detected AFTER the door has already been unlocked, the system does NOT re-lock (can't undo). Instead it sets `block_further_unlocks` to prevent any additional locks from being unlocked for the remainder of the session (e.g., a keypad lock that triggers later via occupancy sensor)
17. **Behavioral change from current code**: Current code stops detection immediately on UC1 match (`stop_feeding()`). New design continues the detection loop for the full `TIMER_DETECT` duration after match — unlock is a one-time side effect, not a session terminator. This is required for UC2-UC5 to function
18. **Two-threshold approach for masked faces**: `FACE_DETECT_THRESHOLD` (SCRFD, lower, e.g. 0.3) and `FACE_RECOG_THRESHOLD` (ArcFace, higher, e.g. 0.45). Masked faces pass detection but fail recognition → classified as unknown → still counted in UC4 via embedding + bbox IoU clustering. Per-camera threshold tuning possible since each camera has fixed angle/lighting/distance
19. **UC6 requires cloud**: Loitering detection needs unknown face embeddings persisted across sessions over days/weeks — cloud-side data aggregation
20. **UC7 deferred**: Trivial time check but deferred to reduce initial scope
21. **UC8 redefined and moved to initial scope**: UC8 changed from "Human Without Face Detection" (alerting) to "Human Body Detection" (session lifecycle control). Three roles: (1) Gate — YOLOv8n filters false ONVIF triggers (wind/shadow/animal) before starting SCRFD+ArcFace, (2) Extend — YOLOv8n checks if person still present at timer expiry, (3) Watch — YOLOv8n-only post-cap monitoring prevents infinite back-to-back sessions. Runs gate-only (1-2 inferences per session + 1 per ONVIF event in watch mode), not every frame — minimal NPU impact
22. **YOLOv8n gate on all patterns**: All 4 camera-lock patterns (P1-P4) run YOLOv8n as first step after ONVIF motion, before SCRFD+ArcFace. Filters non-human ONVIF triggers
23. **P3 no longer skips on ONVIF**: P3 cameras start detection in surveillance mode (P1 behavior) when ONVIF fires and YOLOv8n confirms person. Upgrades to unlock mode if occupancy triggers during session (same merge logic as P4)
24. **Session extension by body detection**: YOLOv8n person detection at timer expiry extends the session, capped at `BODY_EXTEND_MAX_SEC` to prevent infinite loops
25. **UC9 dedicated camera with multi-layer defense**: Face anti-spoofing runs only on dedicated close-range verification cameras near keypad locks. Four defense layers: (1) face recognition, (2) face size validation against expected range at known distance, (3) device detection via YOLOv8n COCO classes 62/63/67 — detects phone/tablet/screen containing the face, (4) blink pattern challenge via tddfa_mobilenet_v1 68-point landmarks + Eye Aspect Ratio. Blink pattern is per-reservation, delivered via booking app/SMS — no on-site instruction device needed. All four models (SCRFD, ArcFace, tddfa_mobilenet_v1, YOLOv8n) have pre-compiled Hailo HEFs. YOLOv8n shared with UC8
26. **Watch mode after session cap**: When `BODY_EXTEND_MAX_SEC` forces session end while person is still present, camera enters YOLOv8n-only watch mode. Full session restarts only on leave-then-return transition (YOLOv8n no-detect → detect) or occupancy trigger. Prevents infinite back-to-back SCRFD+ArcFace sessions. No new config variable — watch mode is inherent behavior when the cap is reached
27. **UC8 gates video recording**: YOLOv8n person detection gates the existing GStreamer RTSP recording pipeline alongside SCRFD+ArcFace. No person = no recording = storage savings on false ONVIF triggers. In watch mode, person detection triggers recording without SCRFD+ArcFace (capture lingering presence). No new config variables — uses existing GStreamer recording parameters (`RECORD_AFTER_MOTION_SECOND`, pre-buffer)
