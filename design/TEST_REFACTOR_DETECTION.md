# Test: Refactor Detection Business Logic

**Date**: 2026-02-22
**Commits**: 8b64df2..4fab235
**Setup**: Dahua camera (P2), MAG001 (LOCK + ENTRY/EXIT buttons), DC010 (KEYPAD_LOCK)

---

## Backend Behavior Summary

The P2 ONVIF behavior differs between backends:

| Behavior | InsightFace | Hailo |
|---|---|---|
| ONVIF motion → recording | Yes (both) | Yes (both) |
| ONVIF motion → detection (P2) | **Rejected** (CPU too expensive for surveillance-only) | **Starts in surveillance mode** (NPU headroom allows continuous detection) |
| Clicked event → detection | Starts new detection session | Upgrades existing surveillance session to unlock mode |
| Clicked + face already matched | N/A (no prior session) | Immediate unlock (Decision 30) |

**InsightFace**: P2 gate rejects ONVIF-triggered detection entirely. Detection only starts when a clicked event arrives (occupancy/button). Simple trigger → detect → match → unlock flow.

**Hailo**: ONVIF starts detection in surveillance mode (face recognition runs but cannot unlock). A clicked event adds the lock to `clicked_locks`, enabling unlock. If a face was already matched during surveillance, unlock happens immediately (Decision 30). Enables UC2 (tailgating), UC3 (blocklist screening), UC5 (stranger alerting).

---

## Shared Tests (both backends)

These tests are backend-independent — they test TS handler logic, recording pipeline, and the shared `FaceRecognitionBase` / `DefaultMatchHandler` / `clickedLocks` flow.

| # | Scenario | Status | Notes |
|---|---|---|---|
| 1 | Occupancy trigger + match | | |
| 2 | Entry button + match | PASS | Fixed `clickedLocks` rename, match + unlock verified (2x) |
| 3 | Exit button → direct unlock | PASS | MAG001 toggled OFF/ON, no detection triggered (3x) |
| 5 | Occupancy off → stop detection | | |
| 6a | No match → timeout (occupancy) | | |
| 6b | No match → timeout (entry button) | PASS | 99 frames, 10846ms, identified: False |
| 8 | ONVIF motion → video recording | PASS | Video clip recorded, uploaded to S3, IoT published |

## InsightFace-Specific Tests

| # | Scenario | Status | Notes |
|---|---|---|---|
| 4a | ONVIF rejected (P2, InsightFace) | PASS | P2 gate rejects detection, recording proceeds independently |
| 7a | Force detect (InsightFace) | PASS | P2 gate bypassed, 92 frames, 13699ms, no match |

## Hailo-Specific Tests

| # | Scenario | Status | Notes |
|---|---|---|---|
| 4b | ONVIF → surveillance mode (P2, Hailo) | | Detection starts but cannot unlock until clicked |
| 7b | Force detect (Hailo) | | P2 gate bypassed, detection starts — verify Hailo inference runs |
| 9 | Clicked upgrades surveillance → unlock | | Face matched during surveillance, click arrives → immediate unlock (Decision 30) |
| 10 | Blocklist blocks unlock (UC3) | | Blocklist detected before click → click arrives → unlock denied |

---

## Test 1: Occupancy Trigger + Match

**Applies to**: Both backends
**Trigger**: Touch DC010 keypad (occupancy sensor)
**Expected**: Detection starts → face match → `member_detected` → unlock DC010

**Steps**:
1. Touch DC010 keypad → `zigbee2mqtt/DC010` with `occupancy: true`
2. TS handler: `handleLockTouchEvent()` → publish `gocheckin/trigger_detection { cam_ip, lock_asset_id }`
3. PY handler: `trigger_face_detection()` → starts detection thread
4. Face detected → `FaceRecognition.process_frame()` → `find_match()`
5. Match found → `DefaultMatchHandler.on_match()` → snapshot + `member_detected` on queue
6. PY handler: `fetch_scanner_output_queue()` → adds `clickedLocks` → publishes `gocheckin/member_detected`
7. TS handler: `unlockByMemberDetected()` → unlock DC010 via TOGGLE

**Verify**:
- [ ] PY log: `trigger_face_detection` with `lock_asset_id`
- [ ] PY log: `MATCH` with member name and similarity
- [ ] Snapshot file created
- [ ] `member_detected` payload has `clickedLocks: [DC010's assetId]`
- [ ] DC010 lock toggles
- [ ] No `onvifTriggered` field in payload (removed)

**Result**:

---

## Test 2: Entry Button + Match

**Applies to**: Both backends
**Trigger**: Press MAG001_ENTRY button
**Expected**: Detection starts → face match → `member_detected` → unlock MAG001

**Steps**:
1. Press MAG001_ENTRY → `zigbee2mqtt/MAG001_ENTRY` with `action: "clicked"`
2. TS handler: `handleButtonClickEvent()` → `companionOf` → MAG001 → `lock.cameras` → publish `gocheckin/trigger_detection { cam_ip, lock_asset_id: MAG001 }`
3. PY handler: `trigger_face_detection()` → starts detection
4. Face detected → match → `DefaultMatchHandler.on_match()` → `member_detected`
5. TS handler: `unlockByMemberDetected()` → unlock MAG001

**Verify**:
- [x] TS log: `handleButtonClickEvent ENTRY button -> triggering detection`
- [x] PY log: `trigger_face_detection` with `lock_asset_id` = MAG001's assetId (`0xe4b323fffeb4b614`)
- [x] PY log: `MATCH` with member name
- [x] `member_detected` payload has `clickedLocks: [MAG001's assetId]`
- [x] MAG001 lock toggles

**Result**: PASS (2026-02-22 20:24–20:25 JST, InsightFace) — after `clickedLocks` fix

**Bug found and fixed** (19:37–19:39 JST): PY handler sent `occupancyTriggeredLocks` but TS reads `clickedLocks`. Fixed by renaming in `py_handler.py` `fetch_scanner_output_queue()`.

**Re-test after fix** (20:24:30): Entry button → detection → match → unlock — full flow verified:
- 20:24:30: Xu, sim=0.4116, 6 frames, 896ms → `clickedLocks: ["0xe4b323fffeb4b614"]` → MAG001 TOGGLE
- 20:25:47: Xu, sim=0.3606, 3 frames, 660ms → `clickedLocks: ["0xe4b323fffeb4b614"]` → MAG001 TOGGLE

**Log trace** (20:24:30 press):
```
TS: zigbee2mqtt/MAG001_ENTRY → action: "clicked"
TS: handleButtonClickEvent → companionOf → MAG001 → cameras → 192.168.11.62
TS: publish gocheckin/trigger_detection { cam_ip: "192.168.11.62", lock_asset_id: "0xe4b323fffeb4b614" }

PY: trigger_face_detection - started for camera: 192.168.11.62
PY: MATCH Xu sim=0.4116, 6 frames, duration: 886ms
PY: snapshot uploaded to S3
PY: member_detected published with clickedLocks: ["0xe4b323fffeb4b614"]

TS: unlockByMemberDetected → clickedLocks: ["0xe4b323fffeb4b614"]
TS: unlocking specific lock: 0xe4b323fffeb4b614
TS: unlockZbLock → TOGGLE → MAG001
```

---

## Test 3: Exit Button → Direct Unlock

**Applies to**: Both backends (TS handler only, no PY involvement)
**Trigger**: Press MAG001_EXIT button
**Expected**: MAG001 lock toggles immediately, no `trigger_detection` published, no face detection

**Steps**:
1. Press MAG001_EXIT (GreenPower_2 button)
2. Verify TS handler log: `handleButtonClickEvent EXIT button -> unlocking lock`
3. Verify no `trigger_detection` MQTT message
4. Verify MAG001 lock toggles

**Result**: PASS (2026-02-22 19:26:09, 20:23:15, 20:23:53 JST) — verified 3 times

- 19:26:09: MAG001 → ON
- 20:23:15: MAG001 → OFF
- 20:23:53: MAG001 → ON

**Log trace** (20:23:15):
```
zigbee2mqtt/MAG001_EXIT → action: "clicked"
→ getZbLockByName → buttonType: EXIT, companionOf: 0xe4b323fffeb4b614
→ handleButtonClickEvent EXIT button -> unlocking lock 0xe4b323fffeb4b614
→ unlockZbLock → publish zigbee2mqtt/MAG001/set {"state":"TOGGLE"}
→ MAG001 confirmed state: OFF
```

No `trigger_detection` or `stop_detection` published. Pure TS handler path — py_handler not involved.

---

## Test 4a: ONVIF Detection Rejected (P2, InsightFace)

**Applies to**: InsightFace backend only
**Trigger**: Motion in front of Dahua camera (ONVIF notification)
**Expected**: Detection rejected — InsightFace on CPU is too expensive for surveillance-only sessions. Recording proceeds independently.

**Why InsightFace-specific**: On Hailo backend, ONVIF would start detection in surveillance mode (face recognition runs but cannot unlock until a clicked event). InsightFace skips detection entirely for P2 cameras on ONVIF triggers because CPU-based inference lacks the headroom for surveillance-only sessions.

**Steps**:
1. Wave hand / walk in front of Dahua camera to trigger ONVIF motion
2. PY handler receives ONVIF notification via HTTP server
3. `handle_notification()` → recording starts (if `isRecording`)
4. `trigger_face_detection(cam_ip, lock_asset_id=None)` — no lock context
5. P2 gate: camera has locks → ONVIF trigger rejected

**Verify**:
- [x] PY log: `trigger_face_detection` called with `lock_asset_id=None`
- [x] PY log: `trigger_face_detection - P2 camera, ONVIF trigger rejected: 192.168.11.62`
- [x] No detection thread started
- [x] Recording started independently (verified with Test 8)

**Result**: PASS (2026-02-22 20:27:20 JST)

**Log trace**:
```
PY: handle_notification → is_motion_value: True, cam_ip: 192.168.11.62
PY: start_recording (recording proceeds)
PY: trigger_face_detection - P2 camera, ONVIF trigger rejected: 192.168.11.62
```

---

## Test 4b: ONVIF → Surveillance Mode (P2, Hailo)

**Applies to**: Hailo backend only
**Trigger**: Motion in front of camera (ONVIF notification)
**Expected**: Detection starts in surveillance mode — SCRFD + ArcFace run on every frame, faces are recognized and logged, but no unlock is possible until a clicked event arrives.

**Why Hailo-specific**: Hailo-8 NPU has headroom for continuous detection (~6.4% for YOLOv8n, ~35% for SCRFD+ArcFace). Surveillance mode enables UC2 (tailgating), UC3 (blocklist screening before unlock), UC5 (stranger alerting), and Decision 30 (immediate unlock when click arrives if face already matched).

**Steps**:
1. Walk in front of camera → ONVIF motion event
2. UC8 gate: YOLOv8n checks 10 frames for person presence
3. Person confirmed → `trigger_face_detection(cam_ip, lock_asset_id=None)`
4. Detection starts in surveillance mode: `clicked_locks={}`, `unlocked=false`
5. Faces detected and matched → `active_member_matched=true` but no unlock (no clicked locks)

**Verify**:
- [ ] PY log: detection starts in surveillance mode
- [ ] PY log: face matches logged but no `member_detected` unlock published
- [ ] Session state: `clicked_locks` empty, `unlocked=false`
- [ ] Recording starts alongside detection

**Result**:

---

## Test 5: Occupancy Off → Stop Detection

**Applies to**: Both backends
**Trigger**: Touch DC010 keypad, then release (occupancy true → false)
**Expected**: Detection starts on occupancy=true, stops on occupancy=false

**Steps**:
1. Touch DC010 → `occupancy: true` → TS publishes `trigger_detection`
2. PY handler starts detection
3. DC010 releases → `occupancy: false` → TS publishes `stop_detection`
4. PY handler: `handle_occupancy_false()` → `active_occupancy` empty → stops detection

**Verify**:
- [ ] PY log: `trigger_face_detection` on occupancy=true
- [ ] PY log: `handle_occupancy_false` on occupancy=false
- [ ] PY log: detection stopped early (no `has_legacy` check — simplified logic)
- [ ] SESSION_END log with `identified: False` (if no match before release)

**Result**:

---

## Test 6a: No Match → Timeout (Occupancy)

**Applies to**: Both backends
**Trigger**: Touch DC010 keypad, present unknown face or no face
**Expected**: Detection runs for TIMER_DETECT duration, then expires with no match

**Steps**:
1. Touch DC010 → `occupancy: true` → TS publishes `trigger_detection { lock_asset_id: DC010 }`
2. PY handler starts detection
3. Do not present a known face (look away or use unknown face)
4. Wait for TIMER_DETECT to expire

**Verify**:
- [ ] PY log: `trigger_face_detection` with `lock_asset_id` = DC010's assetId
- [ ] PY log: detection starts, frames processed
- [ ] PY log: no MATCH lines
- [ ] PY log: SESSION_END with `identified: False`
- [ ] No `member_detected` published
- [ ] No lock unlock

**Result**:

---

## Test 6b: No Match → Timeout (Entry Button)

**Applies to**: Both backends
**Trigger**: Press MAG001_ENTRY button, present unknown face or no face
**Expected**: Detection runs for TIMER_DETECT duration, then expires with no match. Same timeout behavior as occupancy trigger — the trigger source (occupancy vs button) does not affect timeout.

**Steps**:
1. Press MAG001_ENTRY → TS publishes `trigger_detection { lock_asset_id: MAG001 }`
2. PY handler starts detection
3. Do not present a known face (look away or use unknown face)
4. Wait for TIMER_DETECT to expire

**Verify**:
- [x] TS log: `handleButtonClickEvent ENTRY button -> triggering detection`
- [x] PY log: `trigger_face_detection` with `lock_asset_id` = MAG001's assetId (`0xe4b323fffeb4b614`)
- [x] PY log: detection starts, 99 frames processed
- [x] PY log: no MATCH lines
- [x] PY log: SESSION END with `identified: False`, duration: 10846ms
- [x] No `member_detected` published
- [x] No lock unlock

**Result**: PASS (2026-02-22 20:40:56 JST, InsightFace)

**Log trace**:
```
TS: zigbee2mqtt/MAG001_ENTRY → action: "clicked"
TS: handleButtonClickEvent ENTRY button -> triggering detection for camera: 192.168.11.62
TS: publish gocheckin/trigger_detection { cam_ip: "192.168.11.62", lock_asset_id: "0xe4b323fffeb4b614" }

PY: trigger_face_detection - started for camera: 192.168.11.62
PY: (no MATCH lines — no known face presented)
PY: SESSION END - frames: 99, identified: False, duration: 10846ms
(no member_detected published, no lock unlock)
```

---

## Test 7a: Force Detect (InsightFace)

**Applies to**: InsightFace backend only
**Trigger**: Publish `force_detect` IoT topic
**Expected**: Detection starts regardless of P2 camera status, bypasses P1/P2 gate. `'force'` sentinel is not added to `specific_locks` or `active_occupancy`. Timer-based session only.

**Steps**:
1. Publish `gocheckin/{coreName}/force_detect` with `{ "cam_ip": "192.168.11.62" }`
2. PY handler: `trigger_face_detection(cam_ip, 'force')` — `'force'` sentinel bypasses gate
3. Detection starts with InsightFace, runs for TIMER_DETECT duration

**Verify**:
- [x] PY log: `function_handler force_detect` received
- [x] PY log: `trigger_face_detection - started for camera: 192.168.11.62` — P2 gate bypassed
- [x] Detection starts on P2 camera (normally rejected for ONVIF — no "P2 camera, ONVIF trigger rejected" message)
- [x] Timer-based session: ran for TIMER_DETECT duration (13699ms), 92 frames, no match
- [x] No `member_detected` published, no lock unlock

**Result**: PASS (2026-02-23 12:19:28 JST, InsightFace)

**Log trace**:
```
PY: function_handler force_detect
PY: trigger_face_detection - started for camera: 192.168.11.62
PY: detected faces on frames 5-92, all below threshold (best_sim ~0.22-0.25)
PY: SESSION END - frames: 92, identified: False, duration: 13699ms
(no member_detected published, no lock unlock)
```

---

## Test 7b: Force Detect (Hailo)

**Applies to**: Hailo backend only
**Trigger**: Publish `force_detect` IoT topic
**Expected**: Same gate bypass as 7a — `'force'` sentinel bypasses P2 gate. Detection starts with Hailo NPU (SCRFD+ArcFace). `specific_locks` empty, so detection runs in surveillance mode (no unlock possible). Timer-based session.

**Why Hailo-specific**: Hailo runs SCRFD+ArcFace on NPU instead of InsightFace on CPU. Verifies that `force_detect` works with Hailo inference pipeline and that surveillance mode behaves correctly when there are no clicked locks.

**Steps**:
1. Publish `gocheckin/{coreName}/force_detect` with `{ "cam_ip": "..." }`
2. PY handler: `trigger_face_detection(cam_ip, 'force')` — bypasses P2 gate
3. Detection starts with Hailo backend in surveillance mode (`specific_locks` empty)
4. Faces detected and matched → logged but no unlock (no clicked locks)

**Verify**:
- [ ] PY log: `function_handler force_detect` received
- [ ] PY log: `trigger_face_detection - started` — P2 gate bypassed
- [ ] Hailo inference runs (SCRFD+ArcFace on NPU)
- [ ] Faces detected, matches logged but no `member_detected` unlock published
- [ ] Session ends after TIMER_DETECT with `identified: False` or surveillance-only matches
- [ ] No lock unlock

**Result**:

---

## Test 8: ONVIF Motion → Video Recording

**Applies to**: Both backends (recording pipeline is backend-independent)
**Trigger**: Motion in front of Dahua camera (ONVIF notification)
**Expected**: Recording starts → video clip created → uploaded to S3 → `video_clipped` published
**Note**: This was working at commit 8b64df2. Verify not broken by the refactor.

**Steps**:
1. Walk in front of Dahua camera → ONVIF motion event sent to PY HTTP server
2. PY handler: `/onvif_notifications` → `handle_notification(cam_ip, utc_time, is_motion_value=True)`
3. `camera_item['isRecording']` → `thread_gstreamer.start_recording(utc_time)`
4. `set_recording_time()` schedules `stop_recording` after `TIMER_RECORD` seconds
5. Motion continues → extends recording timer
6. Motion stops → timer expires → `stop_recording()` → video clip file created
7. `fetch_scanner_output_queue()` picks up `video_clipped` → uploads to S3 → publishes IoT

**Verify**:
- [x] PY log: `handle_notification` with `is_motion_value: True`
- [x] PY log: `start_recording` for camera IP
- [x] PY log: `stop_recording` after motion ends
- [x] Video clip file created locally
- [x] S3 upload: `put_object` → `.../192.168.11.62/2026-02-22/11:27:20.mp4`
- [x] `video_clipped` IoT message published
- [x] Recording is independent of face detection (detection rejected by P2 gate, recording still worked)

**Result**: PASS (2026-02-22 20:27:20 JST)

**Log trace**:
```
PY: ONVIF notification → handle_notification(192.168.11.62, is_motion_value=True)
PY: start_recording
PY: trigger_face_detection - P2 camera, ONVIF trigger rejected (detection blocked, recording independent)
PY: stop_recording → video clip created
PY: put_object → .../192.168.11.62/2026-02-22/11:27:20.mp4 uploaded to S3
PY: video_clipped IoT published
```

---

## Test 9: Clicked Upgrades Surveillance → Immediate Unlock (Hailo)

**Applies to**: Hailo backend only
**Trigger**: ONVIF motion starts surveillance → face matched → then clicked event arrives
**Expected**: Unlock happens immediately without re-presenting face (Decision 30)

**Steps**:
1. ONVIF motion → detection starts in surveillance mode (Test 4b)
2. Active member face recognized → `active_member_matched=true`, logged but no unlock
3. Press ENTRY button or touch keypad → clicked event arrives
4. `clicked_locks` updated with lock ID
5. Immediate unlock check: `active_member_matched=true` + lock in `clicked_locks` → UNLOCK

**Verify**:
- [ ] Detection running in surveillance mode before click
- [ ] Face matched during surveillance (logged, no unlock)
- [ ] Click arrives → lock added to `clicked_locks`
- [ ] Immediate unlock without requiring face re-presentation
- [ ] Lock toggles

**Result**:

---

## Test 10: Blocklist Blocks Unlock (Hailo, UC3)

**Applies to**: Hailo backend only
**Trigger**: ONVIF motion → blocklist person detected → then clicked event arrives
**Expected**: Unlock denied despite active member being present (UC3 override)

**Steps**:
1. ONVIF motion → detection starts in surveillance mode
2. Blocklist person detected → `block_further_unlocks=true`, alert fires
3. Active member also detected → `active_member_matched=true`
4. Click arrives → `clicked_locks` updated
5. Unlock check: `active_member_matched=true` BUT `block_further_unlocks=true` → NO unlock

**Verify**:
- [ ] Blocklist match logged with alert
- [ ] `block_further_unlocks=true` set
- [ ] Click arrives but unlock denied
- [ ] Lock does NOT toggle

**Result**:
