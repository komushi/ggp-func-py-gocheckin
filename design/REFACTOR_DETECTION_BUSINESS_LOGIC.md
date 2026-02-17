# Refactor Design: Separate Detection from Business Logic

**Prerequisite for**: `PROGRAM_DESIGN.md` Phase 1-2 (Inference Extraction + Business Logic Extraction)
**Scope**: Refactor + legacy lock elimination — simplify trigger model, then separate concerns
**Branch**: `refactor`
**Codebase**: `ggp-func-py-gocheckin`

---

## 0. Implementation Scope & Phasing

### Backend UC Requirements

| Backend | UC Scope | Reason |
|---------|----------|--------|
| **InsightFace** (CPU/ONNX) | **UC1 only** | InsightFace is the fallback/legacy backend. It only needs to support authorized member identification. No UC2-UC5, UC8. |
| **Hailo** (NPU) | **UC1 first**, then UC2-UC5+UC8 | Hailo is the primary backend. Start with UC1 parity, then add security use cases. |

### Implementation Phases

```
Phase 0A: Refactor (this document)
  ├── Legacy lock elimination
  ├── FaceRecognitionBase + MatchHandler separation
  ├── InsightFace subclass (process_frame → UC1)
  ├── Hailo subclass (process_frame → UC1)
  └── DefaultMatchHandler (UC1: snapshot + member payload + queue put)
      Both backends produce identical UC1 behavior.

Phase 0B: Verify UC1 on both backends
  ├── Deploy with INFERENCE_BACKEND=auto (Hailo) → test UC1
  ├── Deploy with INFERENCE_BACKEND=insightface → test UC1
  └── Confirm identical logs, payloads, snapshots

Phase 1: UC8 (Hailo only)
  ├── YOLOv8n gate in trigger_face_detection() — before feed_detecting()
  ├── Dual-signal extend check at timer expiry — in gstreamer_threading.py
  ├── ONVIF surveillance mode for P2 cameras — handle_notification() change
  └── InsightFace path unchanged (no UC8 gate, no extend)

Phase 2: UC2-UC5 (Hailo only)
  ├── Replace DefaultMatchHandler with SecurityHandlerChain
  ├── Add handler callbacks: on_no_match(), on_session_end()
  ├── Multi-category member matching (ACTIVE, INACTIVE, STAFF, BLOCKLIST)
  ├── Session state: unlocked, clicked_locks, known_members, unknown_clusters
  └── InsightFace path unchanged (still uses DefaultMatchHandler, UC1 only)
```

### Backend × UC Matrix

| UC | InsightFace | Hailo | Phase |
|----|:-----------:|:-----:|:-----:|
| UC1: Member ID | Yes | Yes | 0A |
| UC2: Tailgating | -- | Yes | 2 |
| UC3: Unknown Face | -- | Yes | 2 |
| UC4: Group Size | -- | Yes | 2 |
| UC5: Non-Active Member | -- | Yes | 2 |
| UC8: Human Body Detection | -- | Yes | 1 |

> **Design implication**: The `match_handler` passed to `FaceRecognition` can differ per backend. InsightFace always gets `DefaultMatchHandler`. Hailo gets `DefaultMatchHandler` in Phase 0, then `SecurityHandlerChain` in Phase 2. This is configured in `py_handler.py` at thread creation time — no code change needed in `FaceRecognitionBase` or subclasses.

---

## 1. Problem Statement

As identified in `PROGRAM_DESIGN.md` Section 1.2, two files exhibit the same anti-pattern:

```
face_recognition.py          (318 lines)
├── FaceRecognition(Thread)  ← Infrastructure + ML + Business logic in one run()
└── Shared methods           ← active_members, find_match, _build_member_embeddings

face_recognition_hailo.py    (864 lines)
├── HailoFace + HailoFaceApp ← Inference (SCRFD + ArcFace on Hailo-8)
└── FaceRecognition(Thread)  ← DUPLICATED from face_recognition.py (~300 lines)
```

The `FaceRecognition.run()` method in both files mixes three concerns:

| Concern | Lines (approx) | Shared? |
|---------|----------------|---------|
| **Infrastructure** — queue get, session tracking, frame age filter, SESSION_END | ~50 | Identical |
| **Core ML** — `face_app.get()`, threshold comparison, `find_match()` | ~20 | Different (threshold env var, pre_norm filter, log format) |
| **Business logic** — snapshot image, member payload, S3 key gen, queue put | ~80 | Identical |

The business logic (Phase 2, lines 130-207 in `face_recognition.py`, lines 669-747 in `face_recognition_hailo.py`) is **copy-pasted** between both files.

### What Differs Between the Two Backends

| Aspect | InsightFace | Hailo |
|--------|-------------|-------|
| Thread name prefix | `Thread-Detector` | `Thread-HailoDetector` |
| Threshold env var | `FACE_THRESHOLD_INSIGHTFACE` | `FACE_THRESHOLD_HAILO` |
| Pre-norm filter | None | `face.pre_norm < pre_norm_threshold` skip |
| Pre-norm in logs | No | Yes (`pre_norm: {face.pre_norm:.2f}`) |
| First frame log level | `logger.debug` (every frame) | `logger.info` (first frame only) |
| Embedding debug log | `InsightFace embedding: pre_norm=...` | None |
| `_build_member_embeddings` debug log | None | Per-member `Stored embedding [...]` log |

### Why Refactor Now

- Adding UC2-UC5 requires modifying business logic → must synchronize across both files
- `PROGRAM_DESIGN.md` Phase 1-2 requires this separation as foundation
- Risk is minimal: pure refactoring, no behavior change

---

## 2. Legacy Lock Elimination

### 2.1 Current Trigger Model (Complex)

The current code supports two trigger sources with different behavior:

| Trigger | Source | `lock_asset_id` | Timer behavior | Context tracking |
|---------|--------|-----------------|----------------|-----------------|
| **ONVIF motion** | Camera HTTP → `handle_notification()` → `trigger_face_detection(cam_ip, None)` | `None` | Does NOT extend timer | Sets `onvif_triggered=True` |
| **Occupancy sensor** | IoT → `function_handler` → `trigger_face_detection(cam_ip, lock_id)` | `"lock-xxx"` | Extends timer | Adds to `specific_locks` + `active_occupancy` sets |

This two-source model requires:
- `trigger_lock_context` dict per camera with `onvif_triggered`, `specific_locks`, `active_occupancy` fields
- `context_snapshots` keyed by `(cam_ip, detecting_txn)` to freeze context at detection start (Bug #5 fix)
- `has_legacy` checks: `any(not lock.get('withKeypad', False) for lock in camera_locks.values())`
- `handle_occupancy_false()` with conditional early-stop logic checking `active_occupancy`, `onvif_triggered`, and `has_legacy`
- `onvifTriggered` and `occupancyTriggeredLocks` fields in `member_detected` payload

### 2.2 Simplified Trigger Model (After Legacy Elimination)

**All locks now have occupancy sensors.** For P2 cameras (with locks), detection is always triggered by `occupancy=true` via IoT, never by ONVIF motion.

#### ONVIF — What Changes, What Stays

| ONVIF Role | Before (Master) | After (Refactor) | Reason |
|------------|-----------------|-------------------|--------|
| Video recording trigger | `handle_notification()` → `start_recording()` | **KEEP** | Still need recordings on motion |
| Face detection for legacy locks | `handle_notification()` → `trigger_face_detection(cam_ip, None)` | **REMOVE** | All locks have occupancy now |
| Face detection for P1 (no-lock cameras) | Same path, no locks to check | **KEEP** | P1 has no occupancy sensor — ONVIF is the only trigger |
| P2 surveillance mode start | Not in current code | **DEFER to UC2-UC5** | Future UCs need pre-click face accumulation; not needed for UC1-only refactor |
| UC8 gate signal | Not yet implemented | **DEFER to UC8** | YOLOv8n gate is future scope |
| UC8 extend — motion recency | Not yet implemented | **DEFER to UC8** | Motion recency check is future scope |

#### Trigger Matrix (This Refactor)

| Camera Pattern | Detection trigger | Timer extends on | Stops when |
|----------------|-------------------|------------------|------------|
| **P1 (no lock)** | ONVIF motion → `trigger_face_detection(cam_ip, None)` | ONVIF re-trigger (unchanged from master) | Timer expires |
| **P2 (with lock)** | Occupancy → `trigger_face_detection(cam_ip, lock_id)` | Occupancy re-trigger | All `active_occupancy` cleared |

> **P1 note**: P1 cameras have no locks, no occupancy sensors. ONVIF is the only way to know someone is there. The `lock_asset_id=None` path is kept for P1 but simplified — no `has_legacy` check, no `withKeypad` inspection. If the camera has no locks, ONVIF triggers detection directly.

> **P2 future**: When UC2-UC5 are implemented, P2 will gain ONVIF-triggered surveillance mode (face accumulation before any clicked event). That change belongs in `PROGRAM_DESIGN.md` Phase 3+, not this refactor.

#### Simplified `trigger_face_detection()` — P1 vs P2

```python
def trigger_face_detection(cam_ip, lock_asset_id=None):
    """Trigger face detection for a camera.

    Args:
        cam_ip: Camera IP address
        lock_asset_id: Lock assetId that triggered detection.
                       None = P1 camera (ONVIF, no lock)
                       string = P2 camera (occupancy sensor)
    """
    # ... validate camera, gstreamer (unchanged) ...

    camera_locks = camera_item.get('locks', {})

    if lock_asset_id is None:
        # P1: camera with no locks — ONVIF triggers detection
        if len(camera_locks) > 0:
            # P2 camera but no lock_asset_id → ignore (legacy path removed)
            logger.warning('trigger_face_detection - P2 camera without lock_asset_id, skipping: %s', cam_ip)
            return
        # P1: proceed with detection (no context tracking needed)
    else:
        # P2: occupancy-triggered detection with context tracking
        ...
```

### 2.3 What Gets Removed

| Code | Location | Action |
|------|----------|--------|
| `has_legacy` check (`any(not lock.get('withKeypad'...)`) | `py_handler.py:1501-1508` | **Remove** — replace with P1 vs P2 check (`len(camera_locks) > 0`) |
| `started_by_onvif` context field | `py_handler.py:1542` | **Remove** — no longer tracked |
| `onvif_triggered` context field | `py_handler.py:1544,1552` | **Remove** — always false for P2 |
| ONVIF re-trigger "no timer extend" branch | `py_handler.py:1577-1593` | **Remove** — P2 always extends; P1 keeps simple timer behavior |
| `has_legacy` check in `handle_occupancy_false()` | `py_handler.py:1664-1675` | **Remove** — always stops when `active_occupancy` empty |
| `onvifTriggered` in member payload | `py_handler.py:1165` | **Remove** field from payload |
| `trigger_face_detection(cam_ip, None)` in `handle_notification()` | `py_handler.py:1476` | **Remove** — ONVIF no longer triggers detection for P2 cameras |

### 2.3.1 What Stays (for P1 Cameras)

| Code | Reason |
|------|--------|
| `lock_asset_id=None` parameter on `trigger_face_detection()` | P1 cameras still use ONVIF as sole trigger |
| `force_detect` IoT topic calling `trigger_face_detection()` | Debug/testing needs a way to trigger detection; changed to call `trigger_face_detection()` directly (not through `handle_notification()`) |
| ONVIF HTTP notification handler + recording logic | Recording still triggered by ONVIF motion |

### 2.4 Simplified `trigger_face_detection()` (After)

```python
def trigger_face_detection(cam_ip, lock_asset_id=None):
    """Trigger face detection for a camera.

    Args:
        cam_ip: Camera IP address
        lock_asset_id: Lock assetId that triggered detection.
                       None = P1 camera (ONVIF trigger, no lock)
                       string = P2 camera (occupancy sensor trigger)
    """
    # Validate camera, gstreamer (unchanged)
    ...

    camera_locks = camera_item.get('locks', {})

    # --- P1 vs P2 gate ---
    if lock_asset_id is None:
        # P1: ONVIF trigger — camera must have NO locks
        if len(camera_locks) > 0:
            logger.warning('trigger_face_detection - P2 camera ONVIF trigger ignored: %s', cam_ip)
            return
        # P1: no context tracking, just start/extend detection
        if thread_gstreamer.is_feeding:
            return  # P1: ONVIF re-trigger ignored while already detecting
        fetch_members()
        if thread_detector is not None:
            thread_gstreamer.feed_detecting(int(os.environ['TIMER_DETECT']))
        return

    # --- P2: Occupancy-triggered detection ---

    # Clear stale context
    if cam_ip in trigger_lock_context and not thread_gstreamer.is_feeding:
        del trigger_lock_context[cam_ip]

    # Initialize context for new detection
    if cam_ip not in trigger_lock_context:
        trigger_lock_context[cam_ip] = {
            'specific_locks': set(),
            'active_occupancy': set()
        }

    context = trigger_lock_context[cam_ip]
    context['specific_locks'].add(lock_asset_id)
    context['active_occupancy'].add(lock_asset_id)

    # Extend if already detecting
    if thread_gstreamer.is_feeding:
        thread_gstreamer.extend_timer(int(os.environ['TIMER_DETECT']))
        # Update context snapshot
        ...
        return

    # Start new detection
    fetch_members()
    if thread_detector is not None:
        thread_gstreamer.feed_detecting(int(os.environ['TIMER_DETECT']))
        # Store context snapshot
        ...
```

### 2.5 Simplified `handle_occupancy_false()` (After)

```python
def handle_occupancy_false(cam_ip, lock_asset_id):
    """Handle occupancy:false — remove lock, stop if no active occupancy."""
    if cam_ip not in trigger_lock_context:
        return

    context = trigger_lock_context[cam_ip]
    context['specific_locks'].discard(lock_asset_id)
    context['active_occupancy'].discard(lock_asset_id)

    # Update context snapshot (unchanged)
    ...

    # Stop detection if no active occupancy
    if len(context['active_occupancy']) == 0:
        thread_gstreamer.stop_feeding()
        del trigger_lock_context[cam_ip]
```

### 2.6 Simplified `handle_notification()` (After)

```python
def handle_notification(cam_ip, utc_time, is_motion_value):
    # ... validation unchanged ...

    # Record (all cameras)
    if camera_item['isRecording']:
        if thread_gstreamer.start_recording(utc_time):
            set_recording_time(cam_ip, int(os.environ['TIMER_RECORD']), utc_time)

    # Detect — P1 only (no-lock cameras)
    # P2 cameras are triggered by occupancy via IoT, not ONVIF
    if camera_item['isDetecting']:
        trigger_face_detection(cam_ip, None)
        # trigger_face_detection() will reject if camera has locks (P2)
```

---

## 3. Scenario Flows (After Refactor)

The scenario flows focus on the **separation between FaceRecognition (detection) and MatchHandler (business logic)**. The key boundary is:

- **FaceRecognitionBase** owns the run loop, frame processing, and decides *what* was detected
- **MatchHandler** owns *what to do* with detections (snapshot, payload, IoT publish)
- **py_handler.py** owns trigger orchestration and result processing

### 3.1 Entry Points

```
IoT Topics (function_handler):
  gocheckin/trigger_detection    → { cam_ip, lock_asset_id }  → trigger_face_detection()   [P2: occupancy]
  gocheckin/stop_detection       → { cam_ip, lock_asset_id }  → handle_occupancy_false()   [P2: occupancy off]
  gocheckin/{thing}/force_detect → { cam_ip }                 → trigger_face_detection()   [P1+P2: debug]

ONVIF HTTP (start_http_server):
  POST /onvif_notifications      → handle_notification()       → recording (all cameras)
                                                                + detection (P1 only, no-lock cameras)
```

### 3.2 Scenario A: Match Found (UC1 — Current Behavior)

Guest approaches door, face is matched, handler produces snapshot + payload.

```
  py_handler           GStreamer        FaceRecognitionBase       Subclass            MatchHandler          py_handler
      │                    │                    │                    │                     │                    │
      │ feed_detecting()   │                    │                    │                     │                    │
      │───────────────────>│                    │                    │                     │                    │
      │                    │                    │                    │                     │                    │
      │                    │ frame via cam_queue│                    │                     │                    │
      │                    │───────────────────>│                    │                     │                    │
      │                    │                    │                    │                     │                    │
      │                    │              run() │                    │                     │                    │
      │                    │              ┌─────┴────────────────────┤                     │                    │
      │                    │              │ 1. queue get             │                     │                    │
      │                    │              │ 2. session tracking      │                     │                    │
      │                    │              │ 3. age filter            │                     │                    │
      │                    │              │                          │                     │                    │
      │                    │              │ 4. process_frame(img) ──>│                     │                    │
      │                    │              │                          │                     │                    │
      │                    │              │                          │ face_app.get(img)   │                    │
      │                    │              │                          │ find_match()→MATCH  │                    │
      │                    │              │                          │                     │                    │
      │                    │              │    matched_faces ◄───────│                     │                    │
      │                    │              │                          │                     │                    │
      │                    │              │ 5. identified = True     │                     │                    │
      │                    │              │                          │                     │                    │
      │                    │              │ 6. MatchEvent{           │                     │                    │
      │                    │              │      cam_info,           │                     │                    │
      │                    │              │      raw_img,            │                     │                    │
      │                    │              │      matched_faces,      │                     │                    │
      │                    │              │      detected,           │                     │                    │
      │                    │              │      first_frame_at      │                     │                    │
      │                    │              │    }                     │                     │                    │
      │                    │              │                          │                     │                    │
      │                    │              │ 7. on_match(event) ─────────────────────────>  │                    │
      │                    │              │                          │                     │                    │
      │                    │              │                          │              DefaultMatchHandler:         │
      │                    │              │                          │              ┌───────┴───────────┐        │
      │                    │              │                          │              │ a. draw bboxes    │        │
      │                    │              │                          │              │ b. cv2.imwrite    │        │
      │                    │              │                          │              │    snapshot       │        │
      │                    │              │                          │              │ c. build member   │        │
      │                    │              │                          │              │    payloads       │        │
      │                    │              │                          │              │ d. build snapshot │        │
      │                    │              │                          │              │    payload        │        │
      │                    │              │                          │              │ e. queue.put(     │        │
      │                    │              │                          │              │   member_detected)│        │
      │                    │              │                          │              └───────┬───────────┘        │
      │                    │              │                          │                     │                    │
      │                    │              └─────┬────────────────────┘                     │                    │
      │                    │                    │                                          │                    │
      │                    │              (continue run loop                               │                    │
      │                    │               for remaining frames                            │                    │
      │                    │               — identified=True,                              │                    │
      │                    │               so no more on_match calls)                      │                    │
      │                    │                    │                                          │                    │
      │                    │                    │                                          │                    │
      │ fetch_scanner_output_queue() ◄──────────────────────────── reads from queue ──────┘                    │
      │                    │                    │                                                               │
      │ ┌──────────────────┤                    │                                                               │
      │ │ lookup context   │                    │                                                               │
      │ │ upload snap → S3 │                    │                                                               │
      │ │ publish IoT      │                    │                                                               │
      │ │ clear context    │                    │                                                               │
      │ └──────────────────┤                    │                                                               │
```

**Future UC extension point**: Replace `DefaultMatchHandler` with `CompositeHandler` or `SecurityHandlerChain` to run UC1-UC5 handlers on every match. The FaceRecognitionBase run loop does NOT change.

### 3.3 Scenario B: No Match (All Frames)

No face matches any member. MatchHandler is never called.

```
  py_handler           GStreamer        FaceRecognitionBase       Subclass            MatchHandler
      │                    │                    │                    │                     │
      │ feed_detecting()   │                    │                    │                     │
      │───────────────────>│                    │                    │                     │
      │                    │ frames ───────────>│                    │                     │
      │                    │                    │                    │                     │
      │                    │              run() │                    │                     │
      │                    │              ┌─────┴────────────────────┤                     │
      │                    │              │ process_frame(img) ─────>│                     │
      │                    │              │                          │                     │
      │                    │              │                          │ face_app.get(img)   │
      │                    │              │                          │ find_match()→none   │
      │                    │              │                          │                     │
      │                    │              │    matched_faces=[] ◄────│                     │
      │                    │              │                          │                     │
      │                    │              │ (no match → skip handler)│     NOT CALLED      │
      │                    │              │                          │                     │
      │                    │              │ (repeat for each frame)  │                     │
      │                    │              └─────┬────────────────────┘                     │
      │                    │                    │                                          │
      │                    │ SESSION_END ──────>│                                          │
      │                    │                    │ log: identified=false                    │
```

**Future UC extension point**: When UC3 (Unknown Face Logging) is added, a new handler in the chain will fire on *unmatched* faces. This requires extending MatchEvent or adding an `on_no_match()` / `on_frame()` callback to the handler interface. Not in this refactor scope.

### 3.4 Scenario C: Overlapping Occupancy (P2, Two Locks)

Two locks trigger on the same camera. Shows that FaceRecognition/MatchHandler are **unaware** of lock context — that's py_handler's concern.

```
  TypeScript         py_handler            GStreamer       FaceRecognition    MatchHandler
      │                  │                      │                 │                │
      │ trigger(L1) ────>│                      │                 │                │
      │                  │ context:{L1}         │                 │                │
      │                  │ feed_detecting() ───>│                 │                │
      │                  │                      │ frames ────────>│                │
      │                  │                      │                 │                │
      │ trigger(L2) ────>│                      │                 │                │
      │                  │ context:{L1,L2}      │                 │                │
      │                  │ extend_timer() ─────>│                 │                │
      │                  │                      │                 │                │
      │                  │                      │           MATCH found            │
      │                  │                      │                 │                │
      │                  │                      │                 │ on_match() ───>│
      │                  │                      │                 │                │ queue.put()
      │                  │                      │                 │                │
      │                  │ fetch_scanner_output_queue()           │                │
      │                  │ payload.occupancyTriggeredLocks=[L1,L2]│                │
      │                  │ (lock context added by py_handler,     │                │
      │                  │  NOT by MatchHandler)                  │                │
      │                  │                      │                 │                │
      │ stop(L1) ───────>│ occupancy={L2}       │                 │                │
      │ stop(L2) ───────>│ occupancy={}         │                 │                │
      │                  │ stop_feeding() ─────>│                 │                │
      │                  │                      │ SESSION_END ───>│                │
```

**Key point**: Lock context (`specific_locks`, `occupancyTriggeredLocks`) is managed entirely by `py_handler.py` and `fetch_scanner_output_queue()`. MatchHandler only puts raw detection results on the queue. This separation means MatchHandler doesn't need to know about locks, occupancy, or P1/P2 patterns.

### 3.5 Scenario D: ONVIF Motion — P1 (No-Lock Camera)

P1 cameras have no locks and no occupancy sensors. ONVIF triggers both recording and detection.

```
  Camera (ONVIF)     py_handler            GStreamer       FaceRecognition    MatchHandler
      │                  │                      │                 │                │
      │ POST /onvif ────>│                      │                 │                │
      │                  │ start_recording() ──>│                 │                │
      │                  │                      │                 │                │
      │                  │ trigger_face_detection(cam_ip, None)   │                │
      │                  │   camera has 0 locks → P1              │                │
      │                  │ feed_detecting() ───>│                 │                │
      │                  │                      │ frames ────────>│                │
      │                  │                      │                 │                │
      │                  │                      │           (same run loop as A/B) │
      │                  │                      │           process_frame()        │
      │                  │                      │           match? → on_match() ──>│
      │                  │                      │                 │                │
      │                  │                      │ timer expires   │                │
      │                  │                      │ SESSION_END ───>│                │
```

### 3.5.1 Scenario D2: ONVIF Motion — P2 (Recording Only)

P2 cameras ignore ONVIF for detection. ONVIF only triggers recording. FaceRecognition/MatchHandler are not involved.

```
  Camera (ONVIF)     py_handler            GStreamer
      │                  │                      │
      │ POST /onvif ────>│                      │
      │                  │ start_recording() ──>│
      │                  │                      │
      │                  │ trigger_face_detection(cam_ip, None)
      │                  │   camera has locks → P2 → REJECTED
      │                  │                      │
      │                  │ (recording only)     │
```

### 3.6 Scenario E: Force Detect (Debug/Testing)

```
  IoT                py_handler            GStreamer       FaceRecognition    MatchHandler
      │                  │                      │                 │                │
      │ force_detect ───>│                      │                 │                │
      │ {cam_ip}         │                      │                 │                │
      │                  │ trigger_face_detection(cam_ip, 'force')│                │
      │                  │ feed_detecting() ───>│                 │                │
      │                  │                      │ frames ────────>│                │
      │                  │                      │           (same run loop as A/B) │
      │                  │                      │           process_frame()        │
      │                  │                      │ timer expires   │                │
      │                  │                      │ SESSION_END ───>│                │
```

### 3.7 Data Flow Between Components

Shows what each component produces and consumes — the contracts between them.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              DATA FLOW                                          │
│                                                                                  │
│  py_handler                                                                      │
│  ┌─────────┐     cam_queue      ┌────────────────────────────────────────────┐   │
│  │ trigger_ │    (frames)       │ FaceRecognitionBase                        │   │
│  │ face_    │──────────────────>│                                            │   │
│  │detection │                   │  run() loop                                │   │
│  └─────────┘                    │    │                                       │   │
│                                 │    ├─ queue get (cmd, raw_img, cam_info)   │   │
│                                 │    ├─ session tracking (detecting_txn)     │   │
│                                 │    ├─ age filter (AGE_DETECTING_SEC)       │   │
│                                 │    │                                       │   │
│                                 │    ├─ process_frame(img, cam_info, ...)    │   │
│                                 │    │  ┌───────────────────────────────┐    │   │
│                                 │    │  │ Subclass (InsightFace/Hailo)  │    │   │
│                                 │    │  │                               │    │   │
│                                 │    │  │ face_app.get(img) → faces     │    │   │
│                                 │    │  │ for face in faces:            │    │   │
│                                 │    │  │   find_match() → member, sim │    │   │
│                                 │    │  │                               │    │   │
│                                 │    │  │ return matched_faces          │    │   │
│                                 │    │  │   [(face, member, sim), ...]  │    │   │
│                                 │    │  └───────────────────────────────┘    │   │
│                                 │    │                                       │   │
│                                 │    ├─ if matched_faces and not identified: │   │
│                                 │    │    identified = True                  │   │
│                                 │    │    event = MatchEvent{                │   │
│                                 │    │      cam_info, raw_img,              │   │
│                                 │    │      matched_faces, detected,        │   │
│                                 │    │      first_frame_at                  │   │
│                                 │    │    }                                  │   │
│                                 │    │                                       │   │
│                                 │    │    ┌────── handler.on_match(event) ──────>│
│                                 │    │    │                                 │   ││
│                                 │    │    │  ┌─────────────────────────────┐│   ││
│                                 │    │    │  │ DefaultMatchHandler (UC1)   ││   ││
│                                 │    │    │  │                             ││   ││
│                                 │    │    │  │ • draw bboxes → snapshot    ││   ││
│                                 │    │    │  │ • build member payloads     ││   ││
│                                 │    │    │  │ • queue.put(member_detected)││   ││
│                                 │    │    │  └─────────────────────────────┘│   ││
│                                 │    │    │                                 │   ││
│                                 │    │    │  ┌─────────────────────────────┐│   ││
│                                 │    │    │  │ FUTURE: SecurityHandlerChain││   ││
│                                 │    │    │  │                             ││   ││
│                                 │    │    │  │ • UC1: member_detected      ││   ││
│                                 │    │    │  │ • UC2: tailgating_alert     ││   ││
│                                 │    │    │  │ • UC3: unknown_face_detected││   ││
│                                 │    │    │  │ • UC4: group_size_mismatch  ││   ││
│                                 │    │    │  │ • UC5: non_active_member    ││   ││
│                                 │    │    │  └─────────────────────────────┘│   ││
│                                 │    │    │                                 │   ││
│                                 │    │                                       │   │
│                                 └────┴───────────────────────────────────────┘   │
│                                                                                  │
│                              scanner_output_queue                                │
│                                      │                                           │
│                                      ▼                                           │
│  ┌──────────────────────────────────────────────────────────┐                    │
│  │ fetch_scanner_output_queue()                              │                    │
│  │                                                           │                    │
│  │ • lookup context_snapshot (lock context, timing)          │                    │
│  │ • add occupancyTriggeredLocks to payload                  │                    │
│  │ • upload snapshot → S3                                    │                    │
│  │ • publish member_detected → IoT                           │                    │
│  │ • clear context                                           │                    │
│  └──────────────────────────────────────────────────────────┘                    │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 3.8 Future UC Extension Points

This refactoring creates extension points for all future use cases. UCs operate at different layers:

#### Layer 1: Handler Chain (UC1-UC5) — extends MatchHandler

Per-frame business logic. Replace `DefaultMatchHandler` with `SecurityHandlerChain`.

| Extension Point | Where | How to Extend | Used By |
|-----------------|-------|---------------|---------|
| **MatchHandler** | `handler.on_match(event)` | Replace `DefaultMatchHandler` with `CompositeHandler` or `SecurityHandlerChain` | UC1-UC5 handlers |
| **MatchEvent** | Data passed to handler | Add fields: `session_state`, `known_members`, `unknown_clusters` | UC2 (needs `unlocked`), UC4 (needs accumulators) |
| **Handler callbacks** | New methods on `MatchHandler` | Add `on_no_match(event)`, `on_session_end(event)` | UC3 (unknown faces), UC4 (session-level count) |

```
CURRENT (this refactor):                    FUTURE (PROGRAM_DESIGN Phase 3+):

MatchHandler                                SecurityHandlerChain
  │                                           │
  └── DefaultMatchHandler                     ├── UC5-blocklist  (priority 5)  → block_further_unlocks
        • snapshot                             ├── UC1-member     (priority 10) → member_detected, unlock
        • member payload                       ├── UC5-inactive   (priority 20) → non_active_member_alert
        • queue.put()                          ├── UC2-tailgating (priority 30) → tailgating_alert
                                               ├── UC4-group      (priority 38) → group_size_mismatch
                                               └── UC3-unknown    (priority 40) → unknown_face_detected

on_match(event)                             on_match(event)      ← matched faces
                                            on_no_match(event)   ← unmatched faces (UC3, UC4 clustering)
                                            on_session_end(event)← session totals (UC4 count check)
```

#### Layer 2: Session Lifecycle (UC8) — extends py_handler / GStreamer

UC8 operates *outside* the FaceRecognition run loop — it gates session start and extends session duration.

| Extension Point | Where | How to Extend |
|-----------------|-------|---------------|
| **Session gate** | `py_handler.trigger_face_detection()`, before `feed_detecting()` | Insert YOLOv8n person check; no person → skip session + skip recording |
| **Session extend** | `gstreamer_threading.py`, at timer expiry | Dual-signal check: recent ONVIF event + YOLOv8n person → extend timer |
| **ONVIF → P2 surveillance** | `py_handler.handle_notification()` | Add ONVIF-triggered surveillance mode for P2 (deferred from this refactor) |

```
CURRENT (this refactor):                    FUTURE (UC8):

ONVIF/Occupancy                             ONVIF/Occupancy
    │                                           │
    ▼                                           ▼
trigger_face_detection()                    trigger_face_detection()
    │                                           │
    │                                      [UC8 GATE] YOLOv8n → person?
    │                                        NO → skip (no session, no recording)
    │                                        YES ↓
    ▼                                           ▼
feed_detecting(timer)                       feed_detecting(timer)
    │                                           │
    ▼                                           ▼
FaceRecognition run loop                    FaceRecognition run loop
    │                                           │
timer expires → SESSION_END                 timer expiry check:
                                              [UC8 EXTEND] motion recent + person?
                                                BOTH YES → extend timer
                                                EITHER NO → SESSION_END
```

#### Layer 3: Cross-Session Analysis (UC6) — cloud-side

UC6 (Loitering Detection) does NOT extend on-device code. Unknown face embeddings from UC3 are published to IoT / stored in S3. Cloud-side aggregation detects the same unknown face across sessions over days/weeks.

| Extension Point | Where | How to Extend |
|-----------------|-------|---------------|
| **UC3 output** | `unknown_face_detected` IoT payload | Add embedding data to payload for cloud-side matching |
| **Cloud pipeline** | Outside this codebase | Aggregate unknown face embeddings, detect repeats → `loitering_alert` |

```
CURRENT (this refactor):                    FUTURE (UC6):

(not applicable)                            UC3 handler publishes unknown_face_detected
                                              │
                                              ▼ (IoT → cloud)
                                            Cloud aggregation service
                                              │ compare embeddings across sessions
                                              ▼
                                            loitering_alert (cloud-originated)
```

#### Layer 4: Time-Based Policy (UC7) — extends handler chain

UC7 (After-Hours Access) is a simple time check added to the handler chain. No architectural changes needed.

| Extension Point | Where | How to Extend |
|-----------------|-------|---------------|
| **Handler chain** | `SecurityHandlerChain` | Add UC7 handler that checks current time against quiet hours config |

```
FUTURE (UC7):

SecurityHandlerChain
  ├── ... (UC1-UC5 handlers)
  └── UC7-after-hours (priority 50)
        if current_time in [QUIET_HOURS_START, QUIET_HOURS_END]:
          publish after_hours_access
          if QUIET_HOURS_UNLOCK=false: block_further_unlocks
```

#### Layer 5: Dedicated Camera Pipeline (UC9) — separate pipeline

UC9 (Anti-Spoofing) runs on dedicated close-range cameras near locks, NOT on surveillance cameras. It's a separate pipeline that doesn't use the same FaceRecognition run loop or MatchHandler chain.

| Extension Point | Where | How to Extend |
|-----------------|-------|---------------|
| **Separate FaceRecognition subclass** | New `face_recognition_uc9.py` | Dedicated `process_frame()` running multi-layer defense (face size, device detection, blink pattern) |
| **Separate handler** | New UC9 handler | `spoofing_alert` or unlock on all-pass |
| **Camera type flag** | `camera_item` config | `camera_type: 'verification'` routes to UC9 pipeline instead of standard pipeline |

```
CURRENT (this refactor):                    FUTURE (UC9):

Standard cameras:                           Standard cameras (unchanged):
  FaceRecognitionBase                         FaceRecognitionBase
    └── InsightFace/Hailo subclass              └── InsightFace/Hailo subclass
          └── SecurityHandlerChain                    └── SecurityHandlerChain

                                            Dedicated verification cameras (NEW):
                                              VerificationPipeline
                                                ├── ArcFace → member match?
                                                ├── SCRFD bbox → face size in range?
                                                ├── YOLOv8n → device (phone/tablet) detected?
                                                └── tddfa → blink pattern matches?
                                                      │
                                                  all pass → unlock
                                                  any fail → spoofing_alert
```

#### Summary: Where Each UC Extends

| UC | Layer | Extension Point | Modifies FaceRecognition? | Modifies MatchHandler? |
|----|-------|-----------------|---------------------------|------------------------|
| UC1 | Handler chain | `on_match()` | No | Yes (already DefaultMatchHandler) |
| UC2 | Handler chain | `on_match()` + session state (`unlocked`) | No | Yes (new handler) |
| UC3 | Handler chain | `on_no_match()` | No | Yes (new handler + callback) |
| UC4 | Handler chain | `on_session_end()` + accumulators | No | Yes (new handler + callback) |
| UC5 | Handler chain | `on_match()` + multi-category matching | Subclass change (match against all categories) | Yes (new handler) |
| UC6 | Cloud-side | UC3 output → cloud aggregation | No | No (cloud pipeline) |
| UC7 | Handler chain | `on_match()` + time check | No | Yes (new handler) |
| UC8 | Session lifecycle | `trigger_face_detection()` + timer expiry | No | No (py_handler + gstreamer) |
| UC9 | Separate pipeline | New pipeline for verification cameras | New subclass | New handler |

### 3.9 Module Responsibilities (After Refactor)

```
py_handler.py                          ← Orchestrator
  │
  ├── trigger_face_detection(cam_ip, lock_asset_id=None)
  │     │
  │     ├── lock_asset_id=None + no locks → P1: ONVIF detection (timer-based)
  │     ├── lock_asset_id=None + has locks → P2: REJECTED (log + return)
  │     └── lock_asset_id=string          → P2: occupancy detection (context-tracked)
  │           context: { specific_locks, active_occupancy }
  │
  ├── handle_occupancy_false()         ← P2: stop detection when occupancy clears
  │
  ├── handle_notification()            ← ONVIF → recording (all) + detection (P1 only)
  │
  ├── fetch_scanner_output_queue()     ← Process results: S3 upload, IoT publish
  │                                       Adds lock context to payload (py_handler concern)
  │
  ├── DefaultMatchHandler              ← Snapshot + payload + queue put
  │     (encapsulates scanner_output_queue)
  │     (does NOT know about locks, P1/P2, or occupancy)
  │
  └── FaceRecognition (via fdm)        ← Detection thread
        │
        ├── FaceRecognitionBase         ← Shared: run loop, session, matching
        │     face_recognition_base.py    Calls process_frame() and handler.on_match()
        │
        ├── InsightFace subclass        ← process_frame(): FACE_THRESHOLD_INSIGHTFACE
        │     face_recognition.py
        │
        └── Hailo subclass              ← process_frame(): pre_norm + FACE_THRESHOLD_HAILO
              face_recognition_hailo.py
              (also contains HailoFaceApp)
```

---

## 4. Design (Separate Detection from Business Logic)

### 4.1 Architecture

```
                          ┌─────────────────────┐
                          │ FaceRecognitionBase  │  face_recognition_base.py (NEW)
                          │ (threading.Thread)   │
                          │                      │
                          │ - run() loop         │
                          │ - session tracking   │
                          │ - frame age filter   │
                          │ - SESSION_END log    │
                          │ - calls subclass     │
                          │   process_frame()    │
                          │ - calls handler      │
                          │   on_match()         │
                          │                      │
                          │ Shared methods:      │
                          │ - active_members     │
                          │ - find_match()       │
                          │ - _build_member_     │
                          │   embeddings()       │
                          │ - compute_sim()      │
                          │ - stop_detection()   │
                          ├──────────┬───────────┤
                          │          │           │
              ┌───────────▼──┐  ┌───▼───────────┐
              │ InsightFace  │  │ Hailo         │
              │ FaceRecog    │  │ FaceRecog     │
              │              │  │               │
              │ process_     │  │ process_      │
              │  frame()     │  │  frame()      │
              │ ~30 lines    │  │ ~40 lines     │
              └──────────────┘  └───────────────┘
               face_recognition   face_recognition
               .py (MODIFIED)     _hailo.py (MODIFIED)

              ┌─────────────────────────────────┐
              │ MatchHandler (ABC)              │  match_handler.py (NEW)
              │ - on_match(event: MatchEvent)   │
              ├─────────────────────────────────┤
              │ DefaultMatchHandler             │
              │ - snapshot creation             │
              │ - member payload construction   │
              │ - scanner_output_queue.put()    │
              └─────────────────────────────────┘
```

### 4.2 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Base class (inheritance), not mixin | Both subclasses ARE threads — natural `is-a` relationship |
| `process_frame()` returns `list[(face, member, sim)]` | Keeps the base class in control of the identified flag and handler dispatch |
| `MatchHandler` is a separate class, not part of base | Enables future UC handlers without modifying the ML pipeline (aligns with `PROGRAM_DESIGN.md` `SecurityHandlerChain`) |
| `MatchEvent` is a plain data object, not a dict | Explicit contract between detection and business logic |
| `scanner_output_queue` encapsulated in handler | FaceRecognition threads no longer know about the output queue |
| `captured_members` moves to `DefaultMatchHandler` | It's business state, not detection state |
| HailoFaceApp stays in `face_recognition_hailo.py` | It's inference code that will move to `inference/hailo_face_backend.py` in PROGRAM_DESIGN Phase 1 — no point moving it twice |

### 4.3 Relationship to PROGRAM_DESIGN.md

This refactoring is Phase 0 — a prerequisite that makes Phase 1-2 of `PROGRAM_DESIGN.md` easier:

```
Phase 0 (this doc)     → Base class + MatchHandler
  ↓
Phase 1 (PROGRAM_DESIGN) → Extract inference into inference/ package
  ↓
Phase 2 (PROGRAM_DESIGN) → Split detection into Simple + Full paths
```

After Phase 0:
- `FaceRecognitionBase` becomes the ancestor of both `SimpleRecognition` (Phase 2) and `FaceProcessor` (Phase 3)
- `DefaultMatchHandler` evolves into `SecurityHandlerChain` (Phase 4)
- The `MatchEvent` data object evolves into the richer event passed to handlers

---

## 5. Files

### 5.1 Files to Create

| File | Lines (est) | Purpose |
|------|-------------|---------|
| `match_handler.py` | ~120 | `MatchEvent` data class + `MatchHandler` ABC + `DefaultMatchHandler` |
| `face_recognition_base.py` | ~190 | `FaceRecognitionBase(threading.Thread)` with shared run loop and utilities |

### 5.2 Files to Modify

| File | Before | After | Change |
|------|--------|-------|--------|
| `face_recognition.py` | 318 lines | ~55 lines | Keep only `process_frame()` + imports; extend `FaceRecognitionBase` |
| `face_recognition_hailo.py` | 864 lines | ~600 lines | `FaceRecognition` shrinks to `process_frame()` (~40 lines); `HailoFace` + `HailoFaceApp` unchanged |
| `py_handler.py` | 1766 lines | ~1770 lines | Import `DefaultMatchHandler`, create and pass to `FaceRecognition` |

### 5.3 Files Unchanged

- `gstreamer_threading.py`, `onvif_process.py`, `s3_uploader.py`, `web_image_process.py`
- `design/PROGRAM_DESIGN.md`, `design/SECURITY_USE_CASES.md`

---

## 6. Detailed Design

### 6.1 `match_handler.py`

```python
class MatchEvent:
    """Data object emitted when faces are matched in a frame."""
    cam_info: dict          # cam_ip, cam_uuid, cam_name, detecting_txn, frame_time
    raw_img: np.ndarray     # original frame (BGR)
    matched_faces: list     # [(face, active_member, sim), ...]
    detected: int           # frame counter
    first_frame_at: float   # T1 timestamp

class MatchHandler:
    """Base class for handling face match events."""
    def on_match(self, event: MatchEvent) -> None:
        raise NotImplementedError

class DefaultMatchHandler(MatchHandler):
    """Current Phase 2 behavior: snapshot + member payload + queue put."""
    def __init__(self, scanner_output_queue):
        self.scanner_output_queue = scanner_output_queue
        self.captured_members = {}

    def on_match(self, event: MatchEvent) -> None:
        # Exact copy of Phase 2 logic from face_recognition.py lines 130-207:
        # 1. Create date_folder, time_filename, local_file_path
        # 2. Draw bounding boxes → cv2.imwrite snapshot
        # 3. Build per-member payloads (reservationCode, S3 keys, etc.)
        # 4. Build snapshot_payload
        # 5. scanner_output_queue.put(member_detected message)
```

### 6.2 `face_recognition_base.py`

```python
class FaceRecognitionBase(threading.Thread):
    THREAD_NAME_PREFIX = "Thread-Detector"  # Subclasses override

    def __init__(self, face_app, active_members, match_handler, cam_queue):
        # Note: scanner_output_queue is NOT passed here — encapsulated in match_handler
        self.cam_queue = cam_queue
        self.match_handler = match_handler
        self.face_app = face_app
        self.active_members = active_members  # triggers property setter
        self.cam_detection_his = {}

    def run(self):
        # Shared loop from face_recognition.py lines 44-216:
        # - Queue get, SESSION_END handling
        # - Session init/reset (detecting_txn change detection)
        # - Age filter (AGE_DETECTING_SEC)
        # - Frame counter, first_frame_at tracking
        #
        # Then delegates to subclass:
        #   matched_faces = self.process_frame(raw_img, cam_info, detected, age)
        #
        # If matched_faces and not already identified:
        #   self.cam_detection_his[cam_ip]['identified'] = True
        #   event = MatchEvent(...)
        #   self.match_handler.on_match(event)

    def process_frame(self, raw_img, cam_info, detected, age) -> list:
        """Subclass implements: run face_app, apply filters, return matches."""
        raise NotImplementedError

    # Shared methods (identical in both current files):
    # - stop_detection()
    # - active_members property + setter with get_member_hash()
    # - _build_member_embeddings()
    # - find_match(face_embedding, threshold)
    # - compute_sim(feat1, feat2)
```

### 6.3 `face_recognition.py` (modified)

```python
from face_recognition_base import FaceRecognitionBase

class FaceRecognition(FaceRecognitionBase):
    THREAD_NAME_PREFIX = "Thread-Detector"

    def process_frame(self, raw_img, cam_info, detected, age):
        """InsightFace-specific: no pre_norm filter, FACE_THRESHOLD_INSIGHTFACE."""
        current_time = time.time()
        faces = self.face_app.get(raw_img)
        duration = time.time() - current_time
        logger.debug(f"... detection frame #{detected} ...")

        matched_faces = []
        for face in faces:
            emb = face.embedding
            emb_norm = np.linalg.norm(emb)
            logger.debug(f"InsightFace embedding: pre_norm={emb_norm:.4f} ...")

            threshold = float(os.environ['FACE_THRESHOLD_INSIGHTFACE'])
            active_member, sim, best_name = self.find_match(face.embedding, threshold)

            if active_member is None:
                logger.info(f"... best_sim: {sim:.4f} (no match)")
                continue
            logger.info(f"... sim: {sim:.4f} (MATCH)")
            matched_faces.append((face, active_member, sim))

        if not matched_faces:
            logger.debug(f"... matched: 0")
        return matched_faces
```

### 6.4 `face_recognition_hailo.py` (modified)

```python
# HailoFace class — UNCHANGED
# HailoFaceApp class — UNCHANGED (lines 52-556)

from face_recognition_base import FaceRecognitionBase

class FaceRecognition(FaceRecognitionBase):
    THREAD_NAME_PREFIX = "Thread-HailoDetector"

    def process_frame(self, raw_img, cam_info, detected, age):
        """Hailo-specific: pre_norm filter, FACE_THRESHOLD_HAILO, pre_norm in logs."""
        current_time = time.time()
        faces = self.face_app.get(raw_img)
        duration = time.time() - current_time
        if detected == 1:
            logger.info(f"... detection frame #{detected} ...")

        matched_faces = []
        rec_hef = os.environ.get('HAILO_REC_HEF', '')
        if 'mobilefacenet' in rec_hef or 'mbf' in rec_hef:
            pre_norm_threshold = float(os.environ.get('HAILO_PRE_NORM_THRESHOLD_MOBILEFACENET', '6.0'))
        else:
            pre_norm_threshold = float(os.environ.get('HAILO_PRE_NORM_THRESHOLD_R50', '10.0'))

        for face in faces:
            if pre_norm_threshold > 0 and face.pre_norm < pre_norm_threshold:
                logger.info(f"... pre_norm: {face.pre_norm:.2f} < {pre_norm_threshold:.1f} (skipped)")
                continue

            threshold = float(os.environ['FACE_THRESHOLD_HAILO'])
            active_member, sim, best_name = self.find_match(face.embedding, threshold)

            if active_member is None:
                logger.info(f"... pre_norm: {face.pre_norm:.2f} best_sim: {sim:.4f} (no match)")
                continue
            logger.info(f"... pre_norm: {face.pre_norm:.2f} sim: {sim:.4f} (MATCH)")
            matched_faces.append((face, active_member, sim))
        return matched_faces
```

Removed imports no longer needed by `FaceRecognition`: `threading`, `traceback`, `gstreamer_threading`, `datetime`/`timedelta`.

### 6.5 `py_handler.py` (modified)

Two changes:

**1. Add import + handler initialization:**
```python
from match_handler import DefaultMatchHandler

# After scanner_output_queue initialization:
match_handler = DefaultMatchHandler(scanner_output_queue)
```

**2. Replace `scanner_output_queue` with `match_handler` at both FaceRecognition creation sites:**

```python
# init_face_detector() line 300:
thread_detector = fdm.FaceRecognition(face_app, active_members, match_handler, cam_queue)

# monitor_detector() line 334:
thread_detector = fdm.FaceRecognition(face_app, active_members, match_handler, cam_queue)
```

**3. Legacy lock cleanup in `fetch_scanner_output_queue()`:**
```python
# REMOVE these lines (py_handler.py:1100-1101, 1114-1115, 1165-1166):
#   onvif_triggered = False                              ← remove
#   onvif_triggered = context.get('onvif_triggered', False)  ← remove
#   member_payload['onvifTriggered'] = onvif_triggered   ← remove

# KEEP (renamed for clarity):
occupancy_triggered_locks = list(context.get('specific_locks', set()))
member_payload['occupancyTriggeredLocks'] = occupancy_triggered_locks
```

**4. Rewrite `trigger_face_detection()` with P1/P2 gate** — see Section 2.4.

**5. Update `handle_notification()` — P1 detection + recording** — see Section 2.6.

**6. Change `force_detect` handler in `function_handler()`:**
```python
# BEFORE:
elif topic == f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/force_detect":
    if 'cam_ip' in event:
        handle_notification(event['cam_ip'], now, True)

# AFTER:
elif topic == f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/force_detect":
    if 'cam_ip' in event:
        trigger_face_detection(event['cam_ip'], 'force')
```

No other changes to `py_handler.py`. The `scanner_output_queue` is still used by:
- `fetch_scanner_output_queue()` — reads from queue (unchanged logic, minus `onvifTriggered`)
- `StreamCapture` constructor — video clipping (unchanged)

---

## 7. What Moves Where

| Code | From | To |
|------|------|----|
| `run()` loop (queue, session, age filter, SESSION_END) | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase.run()` |
| `active_members` property + setter + `get_member_hash()` | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase` |
| `_build_member_embeddings()` | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase` |
| `find_match()` | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase` |
| `compute_sim()` | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase` |
| `stop_detection()` | Both FR files | `face_recognition_base.py` → `FaceRecognitionBase` |
| Phase 2 (snapshot + payload + queue put) | Both FR files | `match_handler.py` → `DefaultMatchHandler.on_match()` |
| InsightFace detection + threshold + log format | `face_recognition.py` | Stays as `process_frame()` |
| Hailo detection + pre_norm + threshold + log format | `face_recognition_hailo.py` | Stays as `process_frame()` |
| `HailoFace` + `HailoFaceApp` (lines 52-556) | `face_recognition_hailo.py` | Stays unchanged |
| `captured_members` dict | Both FR `__init__` | `match_handler.py` → `DefaultMatchHandler.__init__` |

---

## 8. Differences to Preserve

The base class `_build_member_embeddings()` must include the per-member debug log that exists in the Hailo version but not the InsightFace version:

```python
# Hailo version has this extra debug log (face_recognition_hailo.py lines 817-819):
for i, member in enumerate(self.active_members):
    emb = self.member_embeddings[i]
    logger.debug(f"Stored embedding [{member.get('fullName', '?')}]: ...")
```

The InsightFace version does not have this. Since it's `logger.debug`, including it in the base class has no visible impact on InsightFace behavior (debug logs are suppressed in production).

The Hailo version uses `member.get('fullName', '?')` while InsightFace uses `member['fullName']` (no default). The base class should use the safer `.get()` form.

---

## 9. Constructor Signature Change

### Before (both files)
```python
FaceRecognition(face_app, active_members, scanner_output_queue, cam_queue)
```

### After (both files)
```python
FaceRecognition(face_app, active_members, match_handler, cam_queue)
```

The third argument changes from `scanner_output_queue` (a `Queue`) to `match_handler` (a `MatchHandler`). This is the only external API change, affecting two call sites in `py_handler.py`.

---

## 10. Verification

The refactoring must produce **identical runtime behavior**. Verification steps:

**P2 cameras (with locks):**
1. Deploy to pi_neoseed with `INFERENCE_BACKEND=auto` (Hailo backend)
2. Trigger detection via lock occupancy (`trigger_detection` IoT topic)
3. Verify:
   - SESSION_END log format and content unchanged
   - MATCH / no match log format and content unchanged
   - Snapshot uploaded to S3 with correct key
   - MQTT `member_detected` published with identical payload (minus `onvifTriggered`)
   - Timing metrics (trigger_to_first_frame, etc.) unchanged
4. Verify ONVIF motion on P2 camera → recording starts, NO detection triggered
5. Switch to `INFERENCE_BACKEND=insightface`, repeat steps 2-4

**P1 cameras (no locks):**
6. Configure a camera with no locks (P1 pattern)
7. Trigger ONVIF motion → verify detection starts AND recording starts
8. Verify timer-based session (no early stop via `handle_occupancy_false`)

**Force detect:**
9. Send `force_detect` IoT topic → verify detection starts on both P1 and P2 cameras

10. Compare logs before/after refactoring — should differ only in thread name timestamps and removed `onvifTriggered` field

### Specific Log Lines to Verify

```
# SESSION_END (both backends) — from base class:
{cam_ip} SESSION END - frames: {N}, identified: {bool}, duration: {ms}ms

# InsightFace no-match — from process_frame:
{cam_ip} detected: {N} age: {age} best_match: {name} best_sim: {sim} (no match)

# InsightFace match — from process_frame:
{cam_ip} detected: {N} age: {age} fullName: {name} sim: {sim} (MATCH)

# Hailo pre_norm skip — from process_frame:
{cam_ip} detected: {N} age: {age} pre_norm: {pn} < {threshold} (skipped - too far)

# Hailo no-match — from process_frame:
{cam_ip} detected: {N} age: {age} pre_norm: {pn} best_match: {name} best_sim: {sim} (no match)

# Hailo match — from process_frame:
{cam_ip} detected: {N} age: {age} pre_norm: {pn} fullName: {name} sim: {sim} (MATCH)
```

---

## 11. Future: How New Handlers Plug In

After this refactoring, adding a new use case handler is straightforward:

```python
class SecurityAlertHandler(MatchHandler):
    """UC2-UC5: Different logic, different payload."""
    def on_match(self, event):
        # Alert logic, no unlock, different payload
        ...

# Or compose multiple handlers:
class CompositeHandler(MatchHandler):
    def __init__(self, handlers):
        self.handlers = handlers
    def on_match(self, event):
        for h in self.handlers:
            h.on_match(event)
```

This aligns with `PROGRAM_DESIGN.md` Section 8 (`SecurityHandlerChain`), where `DefaultMatchHandler` will evolve into the chain of UC1-UC5 handlers.
