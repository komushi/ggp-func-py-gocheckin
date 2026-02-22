# Test: Refactor Detection Business Logic (InsightFace)

**Date**: 2026-02-22
**Backend**: InsightFace (`INFERENCE_BACKEND=insightface`)
**Commits**: 8b64df2..4fab235
**Setup**: Dahua camera (P2), MAG001 (LOCK + ENTRY/EXIT buttons), DC010 (KEYPAD_LOCK)

---

## Test Results

| # | Scenario | Status | Notes |
|---|---|---|---|
| 1 | Occupancy trigger + match | | |
| 2 | Entry button + match | BUG FOUND | Match OK, unlock failed — `occupancyTriggeredLocks` vs `clickedLocks` mismatch (fixed) |
| 3 | Exit button → direct unlock | PASS | MAG001 toggled ON, no detection triggered |
| 4 | ONVIF rejected (P2) | | |
| 5 | Occupancy off → stop detection | | |
| 6 | No match → timeout | | |
| 7 | Force detect | | |
| 8 | ONVIF motion → video recording | | Working at 8b64df2, verify not broken |

---

## Test 1: Occupancy Trigger + Match

**Trigger**: Touch DC010 keypad (occupancy sensor)
**Expected**: Detection starts → face match → `member_detected` → unlock DC010

**Steps**:
1. Touch DC010 keypad → `zigbee2mqtt/DC010` with `occupancy: true`
2. TS handler: `handleLockTouchEvent()` → publish `gocheckin/trigger_detection { cam_ip, lock_asset_id }`
3. PY handler: `trigger_face_detection()` → starts InsightFace detection thread
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
- [x] PY log: `MATCH` with member name (Xu, sim=0.57/0.42/0.45)
- [x] `member_detected` payload published with lock ID
- [ ] MAG001 lock toggles — FAILED (field name mismatch)

**Result**: BUG FOUND (2026-02-22 19:37–19:39 JST)

**Attempt 1** (19:31:51): PARTIAL — trigger + detection OK, no match (sim=0.34, below threshold 0.45)

**Attempt 2** (19:37:27–19:39:04): Three button presses, all matched:
- 19:37:28: Xu, similarity=0.5674 → `member_detected` published
- 19:37:59: Xu, similarity=0.4246 → `member_detected` published
- 19:39:09: Xu, similarity=0.4510 → `member_detected` published

**Bug**: PY handler sent `occupancyTriggeredLocks: ["0xe4b323fffeb4b614"]` but TS handler reads `clickedLocks`. TS logged "no occupancy locks to unlock (ONVIF-only triggers do not unlock)" and skipped unlock.

**Fix**: Renamed `occupancyTriggeredLocks` → `clickedLocks` in `py_handler.py` `fetch_scanner_output_queue()`. Needs re-test after deploy.

**Log trace** (19:37:27 press):
```
TS: zigbee2mqtt/MAG001_ENTRY → action: "clicked"
TS: handleButtonClickEvent → companionOf → MAG001 → cameras → 192.168.11.62
TS: publish gocheckin/trigger_detection { cam_ip: "192.168.11.62", lock_asset_id: "0xe4b323fffeb4b614" }

PY: function_handler trigger_detection event: { cam_ip: "192.168.11.62", lock_asset_id: "0xe4b323fffeb4b614" }
PY: trigger_face_detection - started for camera: 192.168.11.62
PY: MATCH Xu sim=0.5674 → member_detected published
PY: payload: { ..., "occupancyTriggeredLocks": ["0xe4b323fffeb4b614"] }

TS: unlockByMemberDetected — clickedLocks=undefined
TS: "no occupancy locks to unlock (ONVIF-only triggers do not unlock)"
→ Lock NOT toggled
```

---

## Test 3: Exit Button → Direct Unlock

**Trigger**: Press MAG001_EXIT button
**Expected**: MAG001 lock toggles immediately, no `trigger_detection` published, no face detection

**Steps**:
1. Press MAG001_EXIT (GreenPower_2 button)
2. Verify TS handler log: `handleButtonClickEvent EXIT button -> unlocking lock`
3. Verify no `trigger_detection` MQTT message
4. Verify MAG001 lock toggles

**Result**: PASS (2026-02-22 19:26:09 JST)

**Log trace**:
```
zigbee2mqtt/MAG001_EXIT → action: "clicked"
→ handleButtonClickEvent in: {"lockAssetName":"MAG001_EXIT","action":"clicked"}
→ getZbLockByName → buttonType: EXIT, companionOf: 0xe4b323fffeb4b614
→ getZbLockById → MAG001 (LOCK, has cameras)
→ handleButtonClickEvent EXIT button -> unlocking lock 0xe4b323fffeb4b614
→ unlockZbLock → publish zigbee2mqtt/MAG001/set {"state":"TOGGLE"}
→ MAG001 confirmed state: ON
```

No `trigger_detection` or `stop_detection` published. Pure TS handler path — py_handler not involved.

---

## Test 4: ONVIF Rejected (P2)

**Trigger**: Motion in front of Dahua camera (ONVIF notification)
**Expected**: Detection rejected — P2 camera does not detect on ONVIF motion (Decision 28)

**Steps**:
1. Wave hand / walk in front of Dahua camera to trigger ONVIF motion
2. PY handler receives ONVIF notification via HTTP server
3. `trigger_face_detection(cam_ip, lock_asset_id=None)` — no lock context
4. P2 gate: camera has locks → ONVIF trigger rejected

**Verify**:
- [ ] PY log: `trigger_face_detection` called with `lock_asset_id=None`
- [ ] PY log: rejection message (P2 camera, no lock trigger)
- [ ] No detection thread started
- [ ] Recording may still start (ONVIF triggers recording separately)

**Result**:

---

## Test 5: Occupancy Off → Stop Detection

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

## Test 6: No Match → Timeout

**Trigger**: Touch DC010 keypad, present unknown face or no face
**Expected**: Detection runs for TIMER_DETECT duration, then expires with no match

**Steps**:
1. Touch DC010 → `occupancy: true` → detection starts
2. Do not present a known face (look away or use unknown face)
3. Wait for TIMER_DETECT to expire

**Verify**:
- [ ] PY log: detection starts, frames processed
- [ ] PY log: no MATCH lines
- [ ] PY log: SESSION_END with `identified: False`
- [ ] No `member_detected` published
- [ ] No lock unlock

**Result**:

---

## Test 7: Force Detect

**Trigger**: Publish `force_detect` IoT topic
**Expected**: Detection starts regardless of P2 camera status, bypasses P1/P2 gate

**Steps**:
1. Publish `gocheckin/{coreName}/force_detect` with `{ "cam_ip": "192.168.11.62" }`
2. PY handler: `trigger_face_detection(cam_ip, 'force')` — `'force'` sentinel bypasses gate
3. Detection starts, runs for TIMER_DETECT duration

**Verify**:
- [ ] PY log: `trigger_face_detection` with `force`
- [ ] Detection starts on P2 camera (normally rejected for ONVIF)
- [ ] `'force'` not added to `active_occupancy` or `specific_locks`
- [ ] Timer-based session (no occupancy stop)

**Result**:

---

## Test 8: ONVIF Motion → Video Recording

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
- [ ] PY log: `handle_notification` with `is_motion_value: True`
- [ ] PY log: `start_recording` for camera IP
- [ ] PY log: `stop_recording` after motion ends
- [ ] Video clip file created locally
- [ ] S3 upload log (if uploader configured)
- [ ] `video_clipped` IoT message published
- [ ] Recording is independent of face detection (no `trigger_face_detection` needed)

**Result**:
