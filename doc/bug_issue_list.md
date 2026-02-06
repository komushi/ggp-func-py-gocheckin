# Bug/Issue List

This document tracks all identified bugs and issues in the GoCheckin Face Recognition system.

## Summary

| # | Bug | Status | Priority | File |
|---|-----|--------|----------|------|
| 1 | Stale Trigger Context (`onvifTriggered=True` when ONVIF disabled) | **FIXED** | High | `stale_trigger_context_bug.md` |
| 2 | GStreamer "not-negotiated" Error | **NETWORK** | Medium | `gstreamer_not_negotiated_error.md` |
| 3 | OOM / Memory Leak Issue | **OPEN** | High | `OOM_MEMORY_LEAK_ISSUE.md` |
| 4 | ONVIF `isSubscription` Setting Not Checked | **FIXED** | Medium | `bug_onvif_isSubscription_not_checked.md` |
| 5 | Occupancy Context Race Condition (Security) | **FIXED** | **Critical** | `bug_occupancy_context_race_condition.md` |
| 6 | Dual-Pipeline H265 Frame Decode Failure | **FIXED** | High | `bug_dual_pipeline_h265_frame_decode.md` |
| 7 | Stale Embeddings Matrix After Member Update | **TEMP FIX** | High | `bug_stale_embeddings_matrix.md` |
| 8 | Multi-Face Per Frame Collision | **FIXED** | High | `bug_multi_face_per_frame.md` |
| 9 | Multi-Member Multi-Lock Detection | **FUTURE** | Low | `future_multi_member_multi_lock.md` |
| 10 | Hailo Recognition Failure After Lighting Change | **PENDING** | High | `bug_hailo_recognition_failure.md` |

---

## Bug #1: Stale Trigger Context

**Status:** FIXED (2026-01-18)

### Summary
The `trigger_lock_context` dictionary persists after detection ends without a face match, causing subsequent triggers to reuse stale context data. This results in incorrect `onvifTriggered=True` values in `member_detected` events.

### Symptom
`member_detected` payload shows `onvifTriggered=True` even when:
- ONVIF subscription is disabled (`isSubscription: false`)
- No ONVIF notification was received
- Only occupancy sensor triggered detection

### Root Cause
Context only deleted after face match. When detection ends due to timeout or pipeline crash, context persists and is reused by subsequent triggers.

### Fix Applied
In `py_handler.py`, function `trigger_face_detection()`:
1. Moved GStreamer validation before context creation
2. Added stale context check: if context exists AND `is_feeding=False`, delete it

```python
# Check if existing context is stale (detection not running)
if cam_ip in trigger_lock_context and not thread_gstreamer.is_feeding:
    logger.info('trigger_face_detection - clearing stale context for: %s', cam_ip)
    del trigger_lock_context[cam_ip]
```

### Documentation
See `stale_trigger_context_bug.md` for full details and regression test.

---

## Bug #2: GStreamer "not-negotiated" Error

**Status:** NETWORK (Updated 2026-01-25)

### Summary
The decode pipeline (`appsrc → h265parse → avdec → appsink`) intermittently fails with "not-negotiated" error after idle periods. **Root cause identified: WiFi network instability.** LAN testing confirmed error does not occur with wired connections.

### Conclusion (2026-01-25)
**LAN Test Results:**
- Tested multiple LAN cameras for hours
- Error **never occurred** on LAN connections
- Error only occurs on WiFi cameras (rulin environment)

**Root Cause:** WiFi packet loss/jitter causes RTSP stream corruption. When corrupted data reaches the decode pipeline after idle, GStreamer cannot negotiate the stream format.

**Resolution:** This is a network infrastructure issue, not a code bug. Recommendations:
1. Use wired LAN connections for cameras when possible
2. Accept that WiFi cameras may experience intermittent decode errors
3. Current self-recovery mechanism (eventual restart after crash loop) is sufficient

### Error Signature
```
gst-stream-error-quark: Internal data stream error. (1)
../libs/gst/base/gstbasesrc.c(3132): gst_base_src_loop (): /GstPipeline:pipeline*/GstAppSrc:m_appsrc:
streaming stopped, reason not-negotiated (-4)
```

### Two Distinct Failure Modes Identified

| Mode | Trigger | Timing | Description |
|------|---------|--------|-------------|
| **Startup Race** | Pipeline restart + immediate ONVIF | ~0-10s after start | Decode pipeline not yet PLAYING |
| **Resume After Idle** | Long idle period + ONVIF trigger | After minutes of no data | Decode pipeline PLAYING but stale |

### NEW: Resume After Idle Problem (2026-01-18)

**Evidence from log analysis:**
```
14:08:57  GStreamer thread started, decode pipeline reaches PLAYING
14:09-14:15  Detection working normally
14:15:34  Last detection frame, timer expires → is_feeding = False
          ════ 5 MINUTES IDLE - NO DATA TO DECODE PIPELINE ════
14:20:30  ONVIF trigger → feed_detecting → is_feeding = True
14:20:31  ERROR: not-negotiated (301ms after resume)
```

**Root Cause:** The decode pipeline (`appsrc is-live=true format=time`) cannot handle extended data gaps:
- No stream discontinuity signal when stopping
- No flush/caps renegotiation when resuming
- Decoder internal state becomes stale
- Stale buffered frames pushed with old timestamps

### Diagnostic Changes Made
1. Line 450: Log decode pipeline `set_state(PLAYING)` return value
2. Line 772: Changed decode pipeline state changes from DEBUG to INFO

### Fix Attempts

| Attempt | Approach | Result |
|---------|----------|--------|
| 1 | Flush on resume | Race condition |
| 2 | Flush on stop | Stale after ~67 min |
| 3 | Flush + set_state(PLAYING) | Still stale |
| 4 | `is-live=false` | Silent stall (no error) |
| 5 | Trickle feed (1 frame/5sec) | FAILED (crash loop) |
| 6 | Continuous feed + skip-frame | **REVERTED** (high CPU) |
| **7** | **Per-camera cleanup + 3s restart delay** | **PARTIAL** (26 min recovery) |

**Current Status**: Baseline + Attempt 7, testing on LAN cameras.

**Attempt 6 Reverted (2026-01-23)**: Continuous feed causes high CPU with multiple cameras. Reverted to baseline frame handling.

**Attempt 7 Results (2026-01-22)**: Error reproduced, crash loop lasted 26 minutes before self-recovery. Quick recovery (~8s) NOT achieved, but eventual self-recovery works without manual intervention.

**LAN Test (2026-01-23 → 2026-01-25)**: Tested on napir environment (LAN cameras at 192.168.11.x) for extended hours. **Result: Error NEVER occurred on LAN.** This confirms the hypothesis - WiFi network instability is the root cause, not a code bug.

### Camera Restart Observation (2026-01-20)

After reverting to baseline, a crash loop occurred (error on every first detection). **Camera restart fixed the crash loop** with the same codebase. This suggests:
- The RTSP stream from the camera can enter a "bad state"
- Camera restart provides a fresh stream that allows normal operation
- The bug may be partially camera-related, not purely code-related

### Documentation
See `gstreamer_not_negotiated_error.md` for full details.

---

## Bug #3: OOM / Memory Leak Issue

**Status:** OPEN (Investigation Required)
**Discovered:** 2026-01-11

### Summary
Python Lambda killed by Linux OOM (Out of Memory) killer after ~4 hours of runtime. Memory usage grew to 1.5GB RSS before crash.

### Symptom
- HTTP server on port 7777 becomes unavailable
- TypeScript Lambda's `/recognise` call fails with `ECONNREFUSED`
- Face embedding computation fails
- Members saved to DynamoDB without `faceEmbedding` field

### Potential Sources (by Suspect Level)
1. **HIGH**: GStreamer video buffers not properly released
2. **MEDIUM-HIGH**: Face detection numpy arrays / InsightFace model
3. **MEDIUM**: Unbounded queue growth (`scanner_output_queue`, `cam_queue`)
4. **MEDIUM**: Thread accumulation (GStreamer, Monitor, HTTP handler threads)

### Workarounds
- Scheduled Greengrass restart every 2-3 hours
- Add swap space to delay OOM
- Set systemd MemoryMax limit

### Documentation
See `OOM_MEMORY_LEAK_ISSUE.md` for full details and investigation steps.

---

## Bug #4: ONVIF `isSubscription` Setting Not Checked

**Status:** FIXED (2026-01-15)

### Summary
The `onvif.isSubscription` setting in camera configuration was not being checked before subscribing to ONVIF events. ONVIF subscriptions were created even when user explicitly disabled them.

### Root Cause
Code only checked `isDetecting` and `isRecording`, but ignored `isSubscription`:
```python
# Before (buggy):
if camera_item['isDetecting'] or camera_item['isRecording']:
    onvif_sub_address = onvif_connectors[cam_ip].subscribe(...)
```

### Fix Applied
Added check for `isSubscription` before subscribing:
```python
# After (fixed):
is_subscription_enabled = onvif_settings.get('isSubscription', False)
if is_subscription_enabled and (camera_item['isDetecting'] or camera_item['isRecording']):
    onvif_sub_address = onvif_connectors[cam_ip].subscribe(...)
```

### Documentation
See `bug_onvif_isSubscription_not_checked.md` for full details.

---

## Bug #5: Occupancy Context Race Condition (Security)

**Status:** FIXED (2026-01-25)
**Priority:** Critical (Security Risk)

### Summary
When a face is detected, the `occupancyTriggeredLocks` is read from the current context at **process time**, not at **frame capture time**. This causes a race condition where `occupancy:false` events can clear the context before the face match is processed, resulting in an empty `occupancyTriggeredLocks` array and triggering the "unlock all" fallback behavior.

### Security Risk
The "unlock all" fallback is a **security vulnerability**:
- Guest touches their room's lock (DC006)
- Race condition causes empty context
- Fallback unlocks ALL locks on camera
- **Result:** Other rooms (DC001) also unlock - security violation

### Symptom
- User touches keypad lock, shows face while lock is active
- Lock's 10-second MCU timer expires, sends `occupancy:false`
- Face match processed AFTER context is cleared
- `member_detected` shows `occupancyTriggeredLocks: []`
- TypeScript triggers "unlock all" fallback → ALL locks on camera unlock

### Root Cause
Context is read in `py_handler.py:fetch_scanner_output_queue()` at process time (~T+19s), but the face was captured earlier (~T+15s). If `occupancy:false` arrives between capture and processing, context is already empty.

### Timeline Example
```
T+0     DC001 occupancy:true        context: {DC001}
T+5     DC006 occupancy:true        context: {DC001, DC006}
T+10    DC001 occupancy:false       context: {DC006}
T+15    Frame captured (face)       context: {DC006} ← face in frame
T+17    DC006 occupancy:false       context: {} ← CLEARED
T+19    Face match processed        context: {} ← reads EMPTY
```

### Required Fix (Two Parts)
1. **Capture context at frame time** - Store context snapshot with `detecting_txn`
2. **Remove "unlock all" fallback** - Never use "unlock all" as fallback (security risk)

### Documentation
See `bug_occupancy_context_race_condition.md` for full details and fix options.

---

## Bug #6: Dual-Pipeline H265 Frame Decode Failure

**Status:** FIXED (2026-02-02)
**Discovered:** 2026-02-01

### Summary
In the dual-pipeline GStreamer architecture, only ~10% of H265 frames could be properly decoded for face detection. **Root cause:** Creating `Gst.Sample.new()` with modified caps broke P-frame decoding.

### Actual Root Cause
The code was modifying caps to embed `frame_time` metadata, then creating a new sample:
```python
# BROKEN - This breaks P-frame decoding!
new_caps = Gst.Caps.from_string(caps_string + ',frame-time=...')
return Gst.Sample.new(sample_buffer, new_caps, ...)
```
GStreamer treats modified caps as a caps change event, resetting decoder state and breaking P-frame references.

### Fix Applied
Use PTS-based metadata store instead of modifying caps:
```python
# FIXED - Push original sample, use PTS for metadata lookup
self.metadata_store[pts] = current_time  # Store metadata by PTS
return sample  # Return ORIGINAL sample
```

### Files Changed
1. `gstreamer_threading.py` - PTS-based metadata store, push original sample
2. `face_recognition.py` - Removed P-frame skip logic
3. `face_recognition_hailo.py` - Removed P-frame skip logic
4. `py_handler.py` - Re-enabled Hailo auto-detection

### Test Results
| Test | Before Fix | After Fix |
|------|------------|-----------|
| P-frame decode | ~10% success | 100% success |
| Face detection | ~10% of frames | 100% of frames |

### Documentation
See `bug_dual_pipeline_h265_frame_decode.md` for full details.

---

## Bug #7: Stale Embeddings Matrix After Member Update

**Status:** TEMP FIX (2026-02-04)
**Discovered:** 2026-02-04
**Introduced in:** commit 627b22a (matrix comparison)

### Summary
`_build_member_embeddings()` was only called during `FaceRecognition.__init__()`. When `py_handler.py` updates `thread_detector.active_members` at runtime via `fetch_members()`, the numpy embeddings matrix is never rebuilt. The detector matches against the stale matrix from startup.

### Root Cause
Commit 627b22a replaced per-member loop comparison with vectorized matrix comparison but only pre-computed the matrix in `__init__`. The runtime assignment `thread_detector.active_members = active_members` in `py_handler.py:894` replaced the list but left `member_embeddings` and `member_norms` stale.

### Temp Fix Applied
Converted `active_members` to a `@property` with a setter that calls `_build_member_embeddings()` on every assignment. This is correct but does a full O(N) rebuild each time.

### Remaining Work (Future Improvement)
Implement incremental matrix update. Currently adequate — max 12 members per reservation, full rebuild takes ~1s.

**TS side investigation (2026-02-04):** `reservations.service.refreshReservation()` uses delete-all-then-rebuild. The AWS IoT classic shadow delta only signals *which* reservation changed (`action: UPDATE`), not what changed within it. The TS side fetches the full named shadow (complete snapshot), deletes all members from local DynamoDB, re-inserts all, re-runs `/recognise` embedding extraction on every member (even unchanged ones), then triggers `fetch_members()` on the Python side.

The TS side *could* diff (it has old members from `getMembers()` and new from the shadow snapshot) but doesn't today. When scale requires it, the optimization is two layers: (1) TS side diffs and only re-extracts changed members' embeddings, (2) Python side applies incremental matrix insert/delete/update instead of full rebuild. Also needs thread-safe atomic swap of matrix + member list.

### Files Changed
1. `face_recognition.py` - Property setter for `active_members`
2. `face_recognition_hailo.py` - Property setter for `active_members`

### Documentation
See `bug_stale_embeddings_matrix.md` for full details, performance analysis, and incremental update design.

---

## Bug #8: Multi-Face Per Frame Collision

**Status:** FIXED (2026-02-04)
**Discovered:** 2026-02-04

### Summary
When a single frame contains 2+ recognized faces, the `for face in faces` loop produced multiple `member_detected` queue entries from the same frame. The downstream processing assumed one match per detection session, causing three cascading failures:

1. **Snapshot collision**: Both faces write to the same `.jpg` (filename derived from `frame_time`, identical for all faces in a frame). Second face overwrites first.
2. **Context loss**: `context_snapshots[snapshot_key]` deleted after first match. Second match falls back to wrong context (`onvifTriggered=False`).
3. **Upload failure**: First match uploads and consumes the `.jpg` file. Second match gets `FileNotFoundError`.

### Fix Applied (Option A: Aggregate)
Split the `for face in faces` loop into two phases:
1. **Phase 1**: Match all faces, collect results into `matched_faces[]`
2. **Phase 2**: Build one composite snapshot with all bounding boxes, one queue entry with `members[]` list

`py_handler.py:fetch_scanner_output_queue()` updated to process `members[]` list: context lookup once, `stop_feeding` once, snapshot upload once, per-member IoT messages.

### Remaining Issue: Duplicate Lock TOGGLE
Multi-face detection sends one `member_detected` IoT message per member. TS handler sends `TOGGLE` to the same lock for each message. Currently harmless (TOGGLE only unlocks, lock auto-relocks), but would break with a true toggle lock. See `bug_multi_face_per_frame.md` for solution options.

### Files Changed
1. `face_recognition.py` - Two-phase match + aggregate
2. `face_recognition_hailo.py` - Same change
3. `py_handler.py` - `fetch_scanner_output_queue()` handles `members[]` list

### Test Results (2026-02-04)
- Single-face regression: PASS
- Multi-face (2 members in same frame): PASS — one snapshot with 2 bboxes, 2 IoT messages, context correct, `stop_feeding` once

### Documentation
See `bug_multi_face_per_frame.md` for full details, log evidence, and fix options.

---

## Issue #9: Multi-Member Multi-Lock Detection

**Status:** FUTURE
**Created:** 2026-02-04
**Priority:** Low (current single-lock setup unaffected)

### Summary
The `identified = True` flag stops detection after the first matched frame. If different members appear in different frames within the same detection window, only the first member's lock is unlocked. This matters when different members are associated with different locks.

### Current Impact
None. All cameras currently map to a single lock. Detecting any member is sufficient to unlock.

### When This Matters
When the system supports cameras with multiple locks where different members map to different locks, and members may appear in different frames (not the same frame).

### Proposed Approach
Replace boolean `identified` with `identified_members` set. Continue processing frames until timer expires or no new members found for N consecutive frames.

### Documentation
See `future_multi_member_multi_lock.md` for full details.

---

## Related Issues

### Pipeline Crash → Stale Context Chain
Bug #2 (GStreamer crash) can trigger Bug #1 (stale context):
1. ONVIF triggers → context created with `onvif_triggered=True`
2. Pipeline crashes with "not-negotiated" → no face detected
3. Context persists (wasn't deleted)
4. Later trigger reuses stale context → wrong `onvifTriggered` value

Bug #1 fix addresses the stale context, but Bug #2 (root cause of crashes) remains.

### Context Management Issues (Bug #1 and Bug #5)
Both bugs relate to `detection_contexts` timing:
- **Bug #1 (Stale Context):** Context persists TOO LONG (after detection ends)
- **Bug #5 (Race Condition):** Context read TOO LATE (after `occupancy:false` clears it)

Bug #1 was fixed by clearing stale contexts. Bug #5 requires capturing context earlier (at frame capture time, not at `member_detected` process time).

### Dual-Pipeline Architecture Issues (Bug #2, Bug #3, Bug #6)
Multiple bugs relate to the dual-pipeline GStreamer architecture:
- **Bug #2 (not-negotiated):** Pipeline state issues after idle periods (NETWORK - WiFi instability)
- **Bug #3 (OOM):** Dual-pipeline uses more memory than single pipeline (OPEN)
- **Bug #6 (H265 decode):** **FIXED** - Was caused by modifying caps, not appsrc itself

Bug #6 was fixed by using PTS-based metadata store instead of modifying caps. The dual-pipeline architecture works correctly when samples are pushed without cap modifications.

---

## Testing Checklist

### Bug #1 Regression Test
- [ ] Enable ONVIF → trigger → let timeout
- [ ] Disable ONVIF (don't restart!)
- [ ] Trigger occupancy sensor
- [ ] Verify log shows "clearing stale context"
- [ ] Verify `member_detected` shows `onvifTriggered=False`

### Bug #2 Reproduction Test
- [ ] Fresh restart of Greengrass
- [ ] Wait for pipeline to reach PLAYING
- [ ] Trigger ONVIF notification
- [ ] Check if "not-negotiated" error occurs
- [ ] If no error, repeat multiple times to check intermittency

---

## Revision History

| Date | Author | Changes |
|------|--------|---------|
| 2026-01-17 | - | Identified Bug #1 and Bug #2 |
| 2026-01-18 | - | Fixed Bug #1, added diagnostic logging for Bug #2 |
| 2026-01-18 | - | Bug #2: New analysis - identified "Resume After Idle" failure mode distinct from startup race condition |
| 2026-01-20 | - | Bug #2: All 4 fix attempts failed, reverted to baseline for observation |
| 2026-01-20 | - | Bug #2: **Camera restart fixed crash loop** - suggests issue may be partially camera-related |
| 2026-01-20 | - | Bug #2: **Attempt 5 FAILED** - Trickle feed caused crash loop (timestamp discontinuities) |
| 2026-01-20 | - | Bug #2: **Attempt 6** - Continuous feed with skip-frame property (push ALL frames, skip-frame=2 during idle) |
| 2026-01-22 | - | Bug #2: **Attempt 7 IMPLEMENTED** - Per-camera cleanup + 3s restart delay |
| 2026-01-22 | - | Bug #2: **Attempt 7 TESTED** - Crash loop lasted 26 min, eventual self-recovery works, quick recovery (~8s) not achieved |
| 2026-01-23 | - | Bug #2: **Attempt 6 REVERTED** - High CPU with multiple cameras, reverted to baseline frame handling |
| 2026-01-23 | - | Bug #2: **LAN TEST** - Testing on napir (LAN) vs rulin (WiFi) to isolate network as a factor |
| 2026-01-25 | - | Bug #5: **NEW** - Occupancy Context Race Condition discovered during Test 9 |
| 2026-01-25 | - | Bug #5: **FIXED** - Context snapshots + removed "unlock all" fallback |
| 2026-01-25 | - | Bug #5: **TEST 9 PASSED** - Both directions verified (DC001 first, DC006 first) |
| 2026-01-25 | - | Bug #2: **ROOT CAUSE IDENTIFIED** - WiFi network instability. LAN cameras tested for hours with no errors. Status changed to NETWORK, priority lowered to Medium. |
| 2026-02-01 | - | Bug #6: **NEW** - Dual-Pipeline H265 Frame Decode Failure discovered during Hailo integration |
| 2026-02-01 | - | Bug #6: Initial theory - appsrc breaks H265 reference frame chain |
| 2026-02-01 | - | Bug #6: Confirmed with both Hailo and InsightFace backends - same ~10% detection rate |
| 2026-02-02 | - | Bug #6: **ROOT CAUSE FOUND** - Modifying caps breaks P-frame decoding, not appsrc itself |
| 2026-02-02 | - | Bug #6: **FIXED** - Push original sample, use PTS-based metadata store for frame_time |
| 2026-02-02 | - | Bug #6: Re-enabled Hailo auto-detection in py_handler.py |
| 2026-02-04 | - | Bug #7: **NEW** - Stale Embeddings Matrix discovered (introduced in 627b22a) |
| 2026-02-04 | - | Bug #7: **TEMP FIX** - Property setter rebuilds matrix on assignment, needs incremental update |
| 2026-02-04 | - | Bug #8: **NEW** - Multi-Face Per Frame causes snapshot collision, context loss, and upload failure |
| 2026-02-04 | - | Bug #7: **TS SIDE INVESTIGATED** - `refreshReservation()` does delete-all-then-rebuild from full shadow snapshot. Incremental update deferred as future improvement (max 12 members/reservation, full rebuild ~1s) |
| 2026-02-04 | - | Bug #8: **FIXED** - Option A (Aggregate) applied. Two-phase match, composite snapshot, single queue entry with `members[]` list. Tested with 1-face and 2-face scenarios. |
| 2026-02-04 | - | Bug #8: **NOTE** - Duplicate lock TOGGLE identified when multiple members match same frame. Currently harmless (TOGGLE only unlocks). Solution options documented. |
| 2026-02-04 | - | Issue #9: **NEW** - Future request for multi-member multi-lock detection. `identified` flag stops after first matched frame, would miss members in later frames if they map to different locks. Low priority (single-lock setup today). |
| 2026-02-04 | - | Bug #10: **NEW** - Hailo recognition fails after brief lighting change. Same person, 101 frames detected, zero matches. Similarity dropped from 0.31 to 0.10-0.27. Lighting was restored but recognition did not recover. Root cause unknown. |
| 2026-02-05 | - | Bug #10: **FIXED** - Two root causes: (1) BGR fed to RGB-expecting HEF models, (2) manual uint8 dequantization less accurate than HailoRT's internal `FormatType.FLOAT32` auto-dequantization, especially for SCRFD landmarks affecting face alignment. Fix: BGR→RGB conversion + `FormatType.FLOAT32` on all model outputs. Result: sim 0.35–0.47 (was 0.10–0.27), 101/101 frames matched at close range. |
| 2026-02-06 | - | Bug #10: **REGRESSION** - Similarity dropped overnight from 0.45 to 0.20 for same person (CuteBaby), same conditions. Greengrass restart did not help. Added diagnostic logging (ArcFace output mean/std, live embedding pre_norm, best_match name). Status changed to PENDING. |
