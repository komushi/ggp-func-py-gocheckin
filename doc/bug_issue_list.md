# Bug/Issue List

This document tracks all identified bugs and issues in the GoCheckin Face Recognition system.

## Summary

| # | Bug | Status | Priority | File |
|---|-----|--------|----------|------|
| 1 | Stale Trigger Context (`onvifTriggered=True` when ONVIF disabled) | **FIXED** | High | `stale_trigger_context_bug.md` |
| 2 | GStreamer "not-negotiated" Error | **OBSERVING** | High | `gstreamer_not_negotiated_error.md` |
| 3 | OOM / Memory Leak Issue | **OPEN** | High | `OOM_MEMORY_LEAK_ISSUE.md` |
| 4 | ONVIF `isSubscription` Setting Not Checked | **FIXED** | Medium | `bug_onvif_isSubscription_not_checked.md` |

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

**Status:** OBSERVING (Updated 2026-01-20)

### Summary
The decode pipeline (`appsrc → h265parse → avdec → appsink`) intermittently fails with "not-negotiated" error after idle periods. Multiple fix attempts have failed. Currently observing baseline error frequency.

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

### Fix Attempts (All Failed)

| Attempt | Approach | Result |
|---------|----------|--------|
| 1 | Flush on resume | Race condition |
| 2 | Flush on stop | Stale after ~67 min |
| 3 | Flush + set_state(PLAYING) | Still stale |
| 4 | `is-live=false` | Silent stall (no error) |

**Current Status**: Reverted to baseline (`is-live=true`, no flush) to observe error frequency.

**Root Cause**: Unknown. The H.265 decoder becomes stale after idle periods regardless of mitigation strategy.

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

## Related Issues

### Pipeline Crash → Stale Context Chain
Bug #2 (GStreamer crash) can trigger Bug #1 (stale context):
1. ONVIF triggers → context created with `onvif_triggered=True`
2. Pipeline crashes with "not-negotiated" → no face detected
3. Context persists (wasn't deleted)
4. Later trigger reuses stale context → wrong `onvifTriggered` value

Bug #1 fix addresses the stale context, but Bug #2 (root cause of crashes) remains.

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
