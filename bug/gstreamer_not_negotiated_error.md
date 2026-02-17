# GStreamer "not-negotiated" Error Analysis

## Error Signature

```
gst-stream-error-quark: Internal data stream error. (1)
../libs/gst/base/gstbasesrc.c(3132): gst_base_src_loop (): /GstPipeline:pipeline*/GstAppSrc:m_appsrc:
streaming stopped, reason not-negotiated (-4)
```

**Location**: `gstreamer_threading.py` - `on_message_decode()` receives ERROR from decode pipeline bus

---

## System Architecture

### Two Separate GStreamer Pipelines

**Main Pipeline (RTSP Capture)**
```
rtspsrc → rtpdepay → parse → appsink
```

**Decode Pipeline (Face Detection)**
```
appsrc → queue → h265parse → avdec_h265 → videoconvert → appsink
```

### Data Flow

```
RTSP Camera
    │
    ▼
Main Pipeline (appsink)
    │
    ▼ on_new_sample()
    │
    ├── add_recording_frame() ──► Recording Buffer
    │
    └── [if is_feeding] ──► decode_appsrc.emit('push-sample', ...)
                                    │
                                    ▼
                            Decode Pipeline
                                    │
                                    ▼
                            Face Recognition
```

### Feeding Control

- `is_feeding = False`: Frames buffered only, decode pipeline receives nothing
- `is_feeding = True`: Frames pushed to decode pipeline for face detection
- Controlled by `feed_detecting()` (start) and `stop_feeding()` (stop)

---

## Root Cause: Decoder State Stale After Idle

### The Problem

The decode pipeline is configured with `is-live=true format=time`, expecting continuous timestamped data. When `is_feeding = False`, the decode pipeline receives **no data** for extended periods. After idle periods, the H.265 decoder loses its internal state and cannot recover when data resumes.

**Current Status**: Back to baseline (`is-live=true`) for observation after Attempt 4 failed.

### Why It Happens

| Issue | Description |
|-------|-------------|
| **Decoder buffers flush** | avdec_h265 loses reference frames during idle |
| **Parser state resets** | h265parse loses NAL unit context |
| **Timestamp discontinuity** | New data has timestamps minutes ahead of last frame |
| **No keyframe guarantee** | Resume may not start with IDR frame |
| **No discontinuity signal** | Pipeline receives no flush/EOS when feeding stops |

---

## Confirmed Evidence (2026-01-18 17:20:29)

### Timeline

```
16:26:44.712  Pipeline created (Thread-Gst-192.168.22.3-16:26:44.712152)
16:43:31.987  Last successful feed_detecting
             ════════════════════════════════════════════════════════════
                      ~37 MINUTES IDLE - NO DATA TO DECODE PIPELINE
             ════════════════════════════════════════════════════════════
17:20:29.577  ONVIF Motion detected
17:20:29.578  feed_detecting in/out → is_feeding = True
17:20:29.803  ERROR: not-negotiated (225ms after resume)
```

### ERROR CONTEXT Output

```
192.168.22.3 ERROR CONTEXT: is_feeding=True, is_playing=True, feeding_count=12,
decoding_count=0, detecting_buffer_len=0, main_pipeline_state=playing,
decode_pipeline_state=playing, thread=Thread-Gst-192.168.22.3-16:26:44.712152
```

### Critical Finding: Decoder Stuck

| Metric | Value | Meaning |
|--------|-------|---------|
| `feeding_count` | **12** | 12 frames were PUSHED to decode pipeline |
| `decoding_count` | **0** | 0 frames came OUT of decoder |
| `decode_pipeline_state` | `playing` | GStreamer thinks pipeline is fine |
| Idle period | ~37 min | Decoder state became stale |
| Pipeline age | ~54 min | Pipeline was running for almost an hour |

**Conclusion**: 12 frames pushed, 0 decoded. The decoder was completely stuck after 37 minutes of idle.

---

## Fix Attempts

### Attempt 1: Flush on Resume (FAILED)

**Approach**: Send `flush_start`/`flush_stop` events in `feed_detecting()` before setting `is_feeding = True`.

```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # Flush decode pipeline to reset stale decoder state
    self.decode_appsrc.send_event(Gst.Event.new_flush_start())
    self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

    with self.detecting_lock:
        self.detecting_buffer.clear()
        self.is_feeding = True
        ...
```

**Why it failed**: Flush causes the decode pipeline to transition to PAUSED state. It takes ~1 second to recover back to PLAYING. But `is_feeding = True` is set immediately, so frames get pushed to the PAUSED pipeline before it recovers.

**Evidence from logs (2026-01-19 01:00:38):**
```
01:00:37.783  flush → is_feeding = True (immediate)
01:00:37.927  Decode Pipeline state changed from paused to paused (pending paused)
01:00:38.128  ERROR: not-negotiated (pipeline still PAUSED, 4 frames pushed)
```

**Successful flush (for comparison):**
```
00:15:20.114  flush
00:15:20.228  paused → paused (pending paused)
00:15:20.635  paused → paused (pending playing)
00:15:21.042  paused → playing  ← ~1 second to recover
```

The error is a **race condition**: if frames arrive before the pipeline recovers to PLAYING (~1 second), the error occurs.

---

### Attempt 2: Flush on Stop (PARTIALLY WORKED)

**Approach**: Move flush from `feed_detecting()` to `stop_feeding()`. Flush when stopping detection, not when resuming.

**Result**: Worked for short idle periods (~11 minutes), but **failed after ~67 minutes idle**.

**Evidence from logs (2026-01-19 06:50:04):**
```
05:43:27.538  stop_feeding + flush
             ════════════════════════════════════════════════════════════
                      ~67 MINUTES IDLE (pipeline in PAUSED state)
             ════════════════════════════════════════════════════════════
06:50:04.592  feed_detecting in
06:50:04.934  ERROR: decode_pipeline_state=paused, feeding_count=4, decoding_count=0
```

**Why it failed**: After flush, pipeline stays in PAUSED state. After very long idle (~67 min), something in the pipeline becomes stale even though we flushed.

---

### Attempt 3: Flush on Stop + set_state(PLAYING) (FAILED)

**Approach**: After flush, explicitly set the decode pipeline back to PLAYING state so it doesn't sit idle in PAUSED.

**Result**: Still failed after extended idle periods. The flush + set_state(PLAYING) approach did not prevent decoder staleness.

```python
def stop_feeding(self):
    with self.detecting_lock:
        self.is_feeding = False
        self.feeding_count = 0
        self.decoding_count = 0

    # FIX: Flush decode pipeline on STOP (not on resume)
    # This resets decoder state while no frames are being pushed.
    # Then re-assert PLAYING state so pipeline is ready for next detection.
    logger.info(f"{self.cam_ip} stop_feeding flushing decode pipeline")
    self.decode_appsrc.send_event(Gst.Event.new_flush_start())
    self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

    # Re-assert PLAYING state after flush to ensure pipeline is ready
    # Without this, pipeline stays in PAUSED and may have issues after long idle
    self.pipeline_decode.set_state(Gst.State.PLAYING)
    logger.info(f"{self.cam_ip} stop_feeding set decode pipeline to PLAYING")

    with self.metadata_lock:
        self.metadata_store.clear()


def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # No flush here - pipeline already in PLAYING state from stop_feeding
    with self.detecting_lock:
        self.detecting_buffer.clear()
        self.is_feeding = True
        ...
```

**Why this should work**:

| Timing | Flush on Stop (Attempt 2) | Flush + set_state(PLAYING) (Attempt 3) |
|--------|---------------------------|----------------------------------------|
| **On stop** | Flush → PAUSED | Flush → PAUSED → PLAYING |
| **During idle** | Sits in PAUSED (stale after long time) | Stays in PLAYING (active, ready) |
| **On resume** | May fail after long idle | Pipeline already PLAYING → ready |

**Benefits**:
- Flush resets decoder state (clears stale buffers)
- `set_state(PLAYING)` ensures pipeline is active during idle
- Pipeline is immediately ready when next detection starts
- No race condition on resume

---

### Attempt 4: Change `is-live=true` to `is-live=false` (FAILED)

**Problem Identified (2026-01-20)**: Analysis of crash loop pattern revealed errors occurring on newly created pipelines with `decode_pipeline_state=paused`.

**Hypothesis**: With `appsrc is-live=true`:
- Pipeline needs data flow to transition from PAUSED to PLAYING
- `set_state(PLAYING)` only sets the **target** state, not the actual state
- Pipeline stays PAUSED until data arrives, causing "not-negotiated" error

**Change Applied**: Decode pipeline appsrc from `is-live=true` to `is-live=false`.

**Result**: **FAILED - Silent Stall**

Tested on 2026-01-20:
- Worked for 31 detection cycles (~2.5 hours, 14:14 to 16:45)
- At 16:46, decoder silently stopped producing frames
- `feed_detecting in/out` logged successfully
- Frames pushed to decode pipeline (no error)
- But **ZERO frames decoded** - no `detecting_txn` logs
- **No error reported** - silent failure

**Evidence from logs (2026-01-20 16:46:38):**
```
16:45:13  Last successful frame (frame 100)
16:45:17  Timer expires, stop_feeding called
16:46:38  ONVIF Motion → feed_detecting in/out (success)
16:46:39  extend_timer in/out (success)
          ════ NO detecting_txn LOGS - DECODER SILENTLY STUCK ════
```

**Why it failed**:

| Setting | After Idle | Failure Mode |
|---------|-----------|--------------|
| `is-live=true` | Decoder stalls | **Error reported** ("not-negotiated") |
| `is-live=false` | Decoder stalls | **Silent failure** (no error, no frames) |

The underlying decoder staleness issue is the same. `is-live=false` just **masks the error** instead of fixing it. The decoder still gets stuck, but no error is thrown - making it harder to detect and recover.

**Conclusion**: `is-live=false` is worse than `is-live=true` because failures are silent.

---

### Attempt 5: Trickle Feed Keep-Alive (FAILED)

**Date**: 2026-01-20

**Problem Analysis**: All previous attempts failed because the decoder becomes stale after extended idle periods. The root cause is that the decode pipeline receives NO data during idle, causing decoder internal state to become invalid.

**Approach**: Keep the decoder "warm" by pushing a small trickle of frames even when not detecting:
- Push 1 frame every 5 seconds during idle periods
- Decoded output is automatically discarded (existing `is_feeding` check)
- Decoder stays active without wasting CPU on face detection

**Implementation**:

```python
# In __init__:
self.last_keepalive_time = 0  # For trickle feed to keep decoder warm

# In on_new_sample(), when not is_feeding:
if current_time - self.last_keepalive_time >= 5.0:
    self.last_keepalive_time = current_time
    ret = self.decode_appsrc.emit('push-sample', sample)
    # Decoded output auto-discarded in on_new_sample_decode() when is_feeding=False
```

**Result**: **FAILED - Crash Loop**

Tested on 2026-01-20:
- Decode pipeline reached PLAYING immediately (good)
- 4 minutes later: continuous "not-negotiated" errors
- Crash loop: error → restart → error → restart

**Why It Failed**:

The 5-second gaps between trickle frames caused **timestamp discontinuities** that the H.265 decoder couldn't handle:

```
Frame at T=0   → decode OK
(5 sec gap - no data to decoder)
Frame at T=5   → decoder sees 5-second timestamp jump → "not-negotiated" error
```

The decoder expects continuous timestamp progression. Sparse frames with large gaps confuse it worse than no frames at all.

**Conclusion**: Trickle feed is worse than baseline because it actively causes errors instead of just failing after idle.

**Resource Impact (theoretical, not reached)**:

| Metric | Value |
|--------|-------|
| Frames/minute | 12 (1 per 5 sec) |
| CPU impact | Minimal (decode only, no face detection) |
| Memory impact | None (frames discarded after decode) |

**Data Flow**:

```
Idle period (is_feeding = False)
    │
    ├── Every 5 sec: push keepalive frame
    │       │
    │       ▼
    │   decode_appsrc → decoder → on_new_sample_decode()
    │                                   │
    │                                   ▼
    │                              is_feeding=False → DISCARDED
    │                              (no face detection)
    │
    └── Decoder stays warm and ready
```

---

### Attempt 6: Continuous Feed with skip-frame (TESTING)

**Date**: 2026-01-20

**Problem Analysis**:
- Attempt 5 (trickle feed) failed because sparse frames caused timestamp discontinuities
- The H.265 decoder needs continuous frames to maintain reference frame chain
- But continuous full decoding wastes CPU during idle

**Key Insight**: GStreamer's avdec_h265 has a `skip-frame` property that can skip heavy decoding work while still processing the bitstream:

```bash
$ gst-inspect-1.0 avdec_h265 | grep -A5 skip-frame
  skip-frame          : Which types of frames to skip during decoding
                        flags: readable, writable
                        Enum "GstLibAVVidDecSkipFrame" Default: 0, "Skip nothing"
                           (0): Skip nothing     - full decode
                           (1): Skip B-frames    - decode I and P only
                           (2): Skip IDCT/Dequantization - skip heavy math
```

**Approach**: Push ALL frames continuously, but use `skip-frame` to minimize CPU during idle:
- Idle mode (is_feeding=False): `skip-frame=2` (skip IDCT/Dequant)
- Detection mode (is_feeding=True): `skip-frame=0` (full decode)

**Implementation**:

```python
# In __init__: Get decoder element and set initial idle mode
self.decode_avdec = self.pipeline_decode.get_by_name('m_avdec')
if self.decode_avdec is not None:
    self.decode_avdec.set_property('skip-frame', 2)  # idle mode

# In on_new_sample(): Always push ALL frames to decoder
edited_sample = self.edit_sample_caption(sample, current_time)
ret = self.decode_appsrc.emit('push-sample', edited_sample)

# In feed_detecting(): Switch to full decode
if self.decode_avdec is not None:
    self.decode_avdec.set_property('skip-frame', 0)  # full decode

# In stop_feeding(): Switch back to idle mode
if self.decode_avdec is not None:
    self.decode_avdec.set_property('skip-frame', 2)  # idle mode
```

**Why This Should Work**:

| Issue | How Continuous Feed + skip-frame Addresses It |
|-------|----------------------------------------------|
| Timestamp discontinuity | No gaps - ALL frames pushed continuously |
| Reference frame chain | Maintained - continuous frame sequence |
| CPU during idle | Reduced - skip-frame=2 skips heavy IDCT/Dequant |
| Decoder staleness | Prevented - decoder always processing frames |

**Data Flow**:

```
Idle (is_feeding=False, skip-frame=2):
    │
    ├── ALL frames pushed to decoder (continuous)
    │       │
    │       ▼
    │   decoder skips IDCT/Dequant (minimal CPU)
    │       │
    │       ▼
    │   on_new_sample_decode() → DISCARDED (is_feeding=False)
    │
    └── Decoder maintains state, ready for detection

Detection (is_feeding=True, skip-frame=0):
    │
    ├── ALL frames pushed to decoder (continuous)
    │       │
    │       ▼
    │   decoder does full decode
    │       │
    │       ▼
    │   on_new_sample_decode() → face detection
```

**Expected Resource Impact**:

| Mode | skip-frame | Frames/sec | CPU | Face Detection |
|------|------------|-----------|-----|----------------|
| Idle | 2 | 15 (all) | Low | No |
| Detection | 0 | 15 (all) | Normal | Yes |

---

### Attempt 7: Per-Camera Thorough Cleanup with Restart Delay (TESTED - PARTIAL SUCCESS)

**Date**: 2026-01-22

**Goal**: Enable automatic self-recovery when "not-negotiated" error occurs.

The error may still happen, but the pipeline should recover automatically in ~8 seconds instead of entering a crash loop that requires manual service restart. This achieves the same recovery effect as a Greengrass service restart, but per-camera only (other cameras unaffected).

**Test Results (2026-01-22 18:01)**: See "Attempt 7 Test Results" section below.

**Problem Analysis**:

When crash loop occurs, three types of restarts have different effects:

| Restart Type | Recovery | Why |
|--------------|----------|-----|
| GStreamer thread restart | ✗ Loop continues | Old state persists (TCP connections, GStreamer global state) |
| Greengrass service restart | ✓ Works | Complete process restart, all resources released |
| Camera restart | ✓ Works | Camera RTSP server reset |

Key observation (2026-01-21):
- Crash loop occurred and persisted through ~600 thread restarts over 3+ hours
- **Single Greengrass service restart fixed it immediately**
- This proves the issue is Pi-side state, not camera-side

**Hypothesis**: Thread restart doesn't fully release resources because:
1. **TCP connections** in TIME_WAIT state (~60-120 seconds)
2. **GStreamer global state** (plugin registry, type factory cache)
3. **Python object references** preventing garbage collection
4. **No delay** between old thread exit and new thread start

**Approach**: Three changes to simulate service restart effect per-camera:

**Change 1: Explicit Unreferencing (gstreamer_threading.py)**

In the `finally` block, after `gc.collect()`, explicitly set pipeline references to None:

```python
# In finally block, after gc.collect()

                gc.collect()

                # Attempt 7: Explicitly unreference pipeline elements
                # This ensures Python releases references so GStreamer can fully cleanup
                self.pipeline = None
                self.pipeline_decode = None
                self.decode_appsrc = None
                self.decode_avdec = None
                self.decode_appsink = None
                logger.info(f"{self.cam_ip} Pipeline elements unreferenced")

                self.is_playing = False
```

**Change 2: Restart Delay (py_handler.py)**

In `monitor_stop_event()`, add 3s delay before starting new thread:

```python
# In monitor_stop_event()

    # Clear previous references before restarting
    thread_gstreamers[cam_ip] = None
    thread_monitors[cam_ip] = None
    del thread_monitors[cam_ip]

    # Attempt 7: Add delay before restart to allow resource cleanup
    # This gives time for:
    # - GStreamer global state to release
    # - Python garbage collection to complete
    # Note: 3s is sufficient as actual pipeline state transition takes ~1.7s
    logger.info(f"{cam_ip} waiting 3s before restart for resource cleanup...")
    time.sleep(3)

    new_thread_gstreamer, _ = start_gstreamer_thread(host_id=os.environ['HOST_ID'], cam_ip=cam_ip)
```

**Change 3: Reduce start_playing Interval (gstreamer_threading.py)**

Reduced wait interval from 10s to 3s since actual pipeline state transition takes only ~1.7s:

```python
def start_playing(self, count = 0, playing = False):
    logger.info(f"{self.cam_ip} start_playing, count: {count} playing: {playing}")
    interval = 3  # Reduced from 10s - actual state transition takes ~1.7s
```

**Evidence for 3s interval**: Startup logs show `set_state(PLAYING)` returns `ASYNC`, but actual state transition (null → ready → paused → playing) completes in ~1.7 seconds:

```
18:20:23.414 - start_playing count:1, result: True
18:20:23.419 - Pipeline: null → ready
18:20:23.420 - Pipeline: ready → paused
18:20:25.125 - Pipeline: paused → playing  (1.7s later)
```

**Why This Should Work**:

| Issue | How This Fix Addresses It |
|-------|---------------------------|
| GStreamer global state | 3s delay + unreferencing allows GStreamer to release per-camera state |
| Python references | Explicit `= None` + gc.collect() ensures objects are freed |
| Immediate restart race | 3s delay prevents race with old resources |
| Excessive wait times | Reduced from 10s to 3s based on actual state transition timing |

**Per-Camera Isolation**:

- `self.pipeline = None` only affects **this camera's instance**
- `time.sleep(3)` only blocks **this camera's monitor thread**
- Other cameras continue running normally during cleanup

**Recovery Timeline (Expected vs Actual)**:

| Phase | Before | Expected | Actual (2026-01-22 test) |
|-------|--------|----------|--------------------------|
| Old thread cleanup | ~0.5s | ~0.5s | ~0.5s ✓ |
| Restart delay | 0s | 3s | 3s ✓ |
| start_playing wait | 10s | 3s | 3s ✓ |
| Pipeline state transition | ~1.7s | ~1.7s | ERROR (loop) |
| **Total recovery time** | **~12s** | **~8s** | **~26 minutes** |

**Note**: The mechanism works correctly, but the decode pipeline fails immediately after restart (~50ms). Recovery only happens after ~249 restart attempts over 26 minutes.

**Expected Behavior** (theory):

```
Error in on_message_decode()
    │
    ▼
finally block: cleanup + unreference + gc.collect()
    │
    ▼
Thread exits (resources released)
    │
    ▼
Monitor detects thread stopped
    │
    ▼
Wait 3 seconds (GStreamer cleanup)
    │
    ▼
start_gstreamer_thread() - fresh start
    │
    ▼
start_playing (3s wait if ASYNC)
    │
    ▼
Pipeline PLAYING (~1.7s state transition)
    │
    ▼
Should work (like after service restart)
```

**Actual Behavior** (2026-01-22 test):

```
Error in on_message_decode()
    │
    ▼
finally block: cleanup + unreference + gc.collect() ✓
    │
    ▼
Thread exits ✓
    │
    ▼
Monitor detects thread stopped ✓
    │
    ▼
Wait 3 seconds ✓
    │
    ▼
start_gstreamer_thread() ✓
    │
    ▼
start_playing ✓
    │
    ▼
Pipeline: null → ready → paused → ERROR (not-negotiated) ✗
    │
    ▼
LOOP REPEATS (~249 times over 26 minutes)
    │
    ▼
Eventually: paused → playing ✓ (random success after ~26 min)
```

---

## Attempt 7 Test Results (2026-01-22)

### Test Execution

**Error reproduced**: 2026-01-22 18:01:33 (camera 192.168.22.3)

### Expected vs Actual

| Metric | Expected | Actual |
|--------|----------|--------|
| Recovery time | ~8 seconds | **~26 minutes** |
| Restart attempts | 1-2 | **~249** |
| Error duration | 18:01:33 → 18:01:41 | 18:01:33 → **18:27:36** |
| Manual intervention | None | **None** (self-recovered) |

### Timeline

```
18:01:33.584  ERROR: not-negotiated (-4)
18:01:33.713  Pipeline elements unreferenced ✓
18:01:33.713  Monitor detects stop, will restart
18:01:33.713  "waiting 3s before restart for resource cleanup..." ✓
18:01:36.724  New thread starts (Thread-Gst-192.168.22.3-18:01:36.713933)
18:01:36.825  start_playing NOT SUCCESS, sleeping 3s
18:01:39.879  ERROR: not-negotiated (-4)  ← Error recurs ~50ms after decode pipeline starts
             ════════════════════════════════════════════════════════════
                   CRASH LOOP: ~249 restart cycles over 26 minutes
                   Each cycle: error → cleanup → 3s delay → restart → error
             ════════════════════════════════════════════════════════════
18:27:29.802  Last error
18:27:32.917  New thread starts (Thread-Gst-192.168.22.3-18:27:32.907270)
18:27:36.058  Decode Pipeline: paused → playing ✓  ← SUCCESS (same sequence, randomly worked)
18:27:36.159  Main Pipeline: paused → playing ✓
```

### Analysis

**What worked:**
- Thread restart mechanism ✓
- 3s cleanup delay executed ✓
- Pipeline elements unreferenced ✓
- Monitor detected thread stop ✓

**What didn't work:**
- Quick recovery (~8s) ✗
- Thread restart did NOT clear the problematic state

**The error pattern:**
```
Failed attempt:  null → ready → paused → ERROR (not-negotiated) ~50ms
Success attempt: null → ready → paused → playing ✓ ~36ms
```

The decode pipeline consistently failed at the `paused → playing` transition for 26 minutes, then randomly succeeded. Nothing special triggered the recovery.

### Why Thread Restart Doesn't Provide Quick Recovery

| Theory | Explanation |
|--------|-------------|
| **Process-level GStreamer state** | Thread restart doesn't clear GStreamer's global plugin registry, type factory cache, or internal state |
| **TCP connection state** | RTSP connections may linger in TIME_WAIT; camera may cache session state |
| **Non-deterministic timing** | The `paused → playing` transition has a race condition that occasionally succeeds (~0.4% rate = 1/249) |
| **~30 min timeout** | Something (RTSP session? TCP connection? GStreamer cache?) times out after ~26-30 minutes |

### Conclusion

**Attempt 7 provides EVENTUAL self-recovery, not QUICK self-recovery.**

| Aspect | Result |
|--------|--------|
| Thread restart mechanism | ✅ Works correctly |
| 3s cleanup delay | ✅ Executes correctly |
| Explicit unreferencing | ✅ Executes correctly |
| Quick recovery (~8s) | ❌ Failed |
| Eventual self-recovery | ✅ Works (~26 min, no manual intervention) |

**Key insight**: The issue is **process-level state** that persists across thread restarts. Only service restart (full process kill) or waiting ~26 minutes resolves it. Thread-level cleanup is necessary but not sufficient.

---

## Current Status: Baseline + Attempt 7 (LAN Test)

**Date**: 2026-01-23

### Configuration Change: Attempt 6 Reverted

**Reason**: Attempt 6 (continuous feed + skip-frame) increases CPU load significantly with multiple cameras. To test whether the "not-negotiated" error is network-related (WiFi vs LAN), we reverted to baseline frame handling while keeping Attempt 7's recovery mechanism.

### Current Configuration

| Setting | Value | Notes |
|---------|-------|-------|
| `is-live` | `true` | Baseline |
| Continuous feed | **No** | Reverted - only push when `is_feeding=True` |
| skip-frame | **Removed** | No skip-frame switching |
| start_playing interval | 3s | Attempt 7 |
| Restart delay | 3s | Attempt 7 |
| Explicit unreferencing | Yes | Attempt 7 |
| Diagnostic logging | Yes | ERROR CONTEXT |

### What Was Changed (2026-01-23)

**Removed (Attempt 6):**
- skip-frame initialization in `__init__`
- Continuous frame push in `on_new_sample()` - reverted to baseline
- skip-frame=0 in `feed_detecting()`
- skip-frame=2 in `stop_feeding()`

**Kept (Attempt 7):**
- 3s delay before restart (`py_handler.py`)
- Explicit pipeline unreferencing in finally block
- 3s start_playing interval

### LAN vs WiFi Test

Testing on **napir environment** (LAN cameras) to compare with **rulin environment** (WiFi cameras):

| Environment | Core | Cameras | Network | Error frequency |
|-------------|------|---------|---------|-----------------|
| rulin/demo_Core | 192.168.22.2 | .3, .4, .5 | **WiFi** | Frequent (26 min crash loops) |
| napir/JSH | 192.168.11.66 | .62, .25 | **LAN** | **Testing...** |

**Hypothesis**: If error occurs less frequently on LAN, network instability (WiFi) is a contributing factor.

### Summary

| Attempt | Purpose | Result |
|---------|---------|--------|
| **Attempt 6** | Keep decoder warm during idle | **REVERTED** (high CPU with multiple cameras) |
| **Attempt 7** | Proper cleanup before thread restart | **Kept** (eventual self-recovery) |

### Summary of All Attempts

| Attempt | Approach | Result | Failure Mode |
|---------|----------|--------|--------------|
| Baseline | `is-live=true`, no flush | Errors occur | "not-negotiated" after idle |
| 1 | Flush on resume | FAILED | Race condition (~1s recovery) |
| 2 | Flush on stop | FAILED | Stale after ~67 min idle |
| 3 | Flush on stop + set_state(PLAYING) | FAILED | Still stale after idle |
| 4 | `is-live=false` | FAILED | Silent stall after ~2.5 hrs |
| 5 | Trickle feed (1 frame/5sec) | FAILED | Crash loop (timestamp gaps) |
| 6 | Continuous feed + skip-frame | **REVERTED** | High CPU with multiple cameras |
| **7** | **Per-camera cleanup + 3s restart delay** | **PARTIAL** | Eventual recovery (~26 min), not quick (~8s) |

**Two separate issues identified**:

1. **Decoder staleness after idle** → Attempt 6 was tested but reverted due to high CPU
2. **Crash loop not self-recovering** → Attempt 7 (cleanup + delay) enables eventual self-recovery

**Current behavior**: Baseline frame handling + Attempt 7 recovery. Testing on LAN to determine if network stability affects error frequency.

**Why quick recovery failed**: Thread restart does not clear process-level GStreamer state. Only full process restart (service restart) or waiting ~26 minutes for some internal timeout provides quick recovery.

**Current test**: Comparing WiFi (rulin) vs LAN (napir) to isolate network as a factor.

---

## Observation: Camera Restart Fixed Crash Loop (2026-01-20)

### The Problem

After reverting to baseline (`is-live=true`, no flush), a crash loop occurred:
- Error on every first detection attempt
- ERROR CONTEXT showed `decode_pipeline_state=paused`
- Greengrass restart did NOT fix the issue
- Same codebase (identical to commit 48fbd32) that worked before

### Evidence (Crash Loop at 22:48)

```
22:48:41  feed_detecting in/out
22:48:41  ERROR: not-negotiated, decode_pipeline_state=paused
22:48:42  Thread restarted
22:48:43  feed_detecting in/out
22:48:43  ERROR: not-negotiated, decode_pipeline_state=paused
...repeating...
```

### Resolution

**Camera restart (192.168.22.3) fixed the crash loop** with the exact same codebase.

### Evidence (Working After Camera Restart at 23:07)

```
23:07:27.042  feed_detecting in/out
23:07:27.4    detecting_txn frame 1
23:07:27.6    detecting_txn frame 2
...
23:07:28.041  Decode Pipeline state changed from paused to playing
...
23:07:37.688  detecting_txn frame 100
```

### Key Observation

With `is-live=true`, the decode pipeline transitions from PAUSED to PLAYING **after** data starts flowing. This is normal behavior. The crash loop was caused by something in the camera/RTSP stream, not the code.

### Implications

| Factor | Before Camera Restart | After Camera Restart |
|--------|----------------------|---------------------|
| Codebase | Baseline (is-live=true) | Same |
| Greengrass | Restarted multiple times | Same |
| Camera | Running continuously | Restarted |
| Result | Crash loop | Works |

This suggests:
1. The RTSP stream from the camera can enter a "bad state"
2. A stale camera stream prevents proper GStreamer pipeline negotiation
3. Camera restart provides a fresh RTSP stream that allows normal operation
4. The intermittent nature of this bug may be partially camera-related, not purely code-related

---

## Monitoring

### CloudWatch Insights Query

```sql
SELECT `@timestamp`, `@message` FROM $source
WHERE `@message` like '%streaming stopped, reason not-negotiated%' AND `@message` like '%ERROR%'
ORDER BY `@timestamp` DESC
LIMIT 1000;
```

**Log group**: `/aws/greengrass/Lambda/ap-northeast-1/769412733712/demo-py_handler`

### Diagnostic Logging

When error occurs, these logs are captured automatically:

1. **ERROR CONTEXT**:
   ```
   192.168.22.3 ERROR CONTEXT: is_feeding=True, is_playing=True, feeding_count=12,
   decoding_count=0, detecting_buffer_len=0, main_pipeline_state=playing,
   decode_pipeline_state=playing, thread=Thread-Gst-192.168.22.3-16:26:44.712152
   ```

2. **Decode pipeline state changes**:
   ```
   192.168.22.3 Decode Pipeline state changed from paused to playing
   ```

### ERROR CONTEXT Fields

| Field | Description |
|-------|-------------|
| `is_feeding` | Was decode pipeline receiving data? |
| `is_playing` | Was main pipeline marked as playing? |
| `feeding_count` | Frames pushed to decode pipeline |
| `decoding_count` | Decoded frames produced |
| `detecting_buffer_len` | Buffered frames waiting |
| `main_pipeline_state` | GStreamer state of main pipeline |
| `decode_pipeline_state` | GStreamer state of decode pipeline |
| `thread` | Thread name (contains creation timestamp) |

---

## Related Files

- `gstreamer_threading.py`: StreamCapture class with pipeline management
- `py_handler.py`: Calls `feed_detecting()` when ONVIF/occupancy triggers detection

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-17 | Initial analysis |
| 2026-01-18 | Identified "resume after idle" as root cause |
| 2026-01-18 | Added ERROR CONTEXT diagnostic logging |
| 2026-01-18 | **ROOT CAUSE CONFIRMED**: feeding_count=12, decoding_count=0 proves decoder stuck after 37 min idle |
| 2026-01-18 | Implemented flush on resume in `feed_detecting()` |
| 2026-01-19 | **Flush on resume FAILED**: Causes race condition - pipeline goes PAUSED, frames arrive before recovery |
| 2026-01-19 | **Attempt 2**: Moved flush to `stop_feeding()` - flush on stop, not on resume |
| 2026-01-19 | **Attempt 2 FAILED**: Error still occurred after ~67 min idle - pipeline stuck in PAUSED |
| 2026-01-19 | **Attempt 3**: Added `set_state(PLAYING)` after flush to keep pipeline active during idle |
| 2026-01-20 | Observed **Startup Race / Crash Loop** pattern: new pipelines after crash fail immediately |
| 2026-01-20 | Flush on creation considered but rejected - flushing empty pipeline is meaningless |
| 2026-01-20 | **Attempt 4**: Changed decode pipeline `is-live=true` to `is-live=false` (flush logic removed for clean test) |
| 2026-01-20 | **Attempt 4 FAILED**: Silent stall after ~2.5 hrs - decoder stuck but no error reported |
| 2026-01-20 | Reverted to baseline (`is-live=true`, no flush) for observation - comparing error frequency |
| 2026-01-20 | **Crash Loop Observed**: After reverting to baseline, crash loop occurred (error on every first detection) |
| 2026-01-20 | **Camera Restart Fixed Crash Loop**: Restarting camera 192.168.22.3 resolved the crash loop with same codebase |
| 2026-01-20 | **Attempt 5**: Implemented trickle feed keep-alive (1 frame/5sec during idle) to prevent decoder staleness |
| 2026-01-20 | **Attempt 5 FAILED**: Trickle feed caused crash loop - timestamp gaps between sparse frames confused decoder |
| 2026-01-20 | **Attempt 6**: Continuous feed with skip-frame property - push ALL frames, use skip-frame=2 during idle to save CPU |
| 2026-01-21 | **Attempt 5 Crash Loop**: 3+ hour crash loop (~600 restarts) during Attempt 5 testing |
| 2026-01-21 | **Service Restart Fixed Loop**: Single Greengrass restart fixed crash loop immediately (not camera restart this time) |
| 2026-01-21 | **Key Insight**: Thread restart ≠ service restart. Service restart clears Pi-side state that thread restart doesn't |
| 2026-01-22 | **Attempt 7 IMPLEMENTED**: Per-camera thorough cleanup with 3s restart delay (reduced from initial 10s plan) |
| 2026-01-22 | **Optimization**: Reduced `start_playing` interval from 10s to 3s - actual state transition takes only ~1.7s |
| 2026-01-22 | **Attempt 7 TESTED**: Error reproduced at 18:01:33. Crash loop lasted 26 minutes (~249 restarts) before spontaneous recovery at 18:27:36. Quick recovery (~8s) NOT achieved - thread restart doesn't clear process-level state. Eventual self-recovery confirmed (no manual intervention needed). |
| 2026-01-23 | **Attempt 6 REVERTED**: Continuous feed + skip-frame causes high CPU with multiple cameras. Reverted to baseline frame handling (only push when `is_feeding=True`). Attempt 7 kept for recovery. |
| 2026-01-23 | **LAN Test Started**: Testing on napir environment (LAN cameras at 192.168.11.x) to compare with rulin environment (WiFi cameras at 192.168.22.x). Hypothesis: if error is less frequent on LAN, network instability is a contributing factor. |
