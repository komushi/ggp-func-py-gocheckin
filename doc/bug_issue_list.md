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
