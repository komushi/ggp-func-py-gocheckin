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

### Attempt 5: Trickle Feed Keep-Alive (TESTING)

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

**Why This Should Work**:

| Issue | How Trickle Feed Addresses It |
|-------|------------------------------|
| Decoder buffers flush | Continuous (slow) data keeps buffers populated |
| Parser state resets | Parser always has recent NAL context |
| Timestamp discontinuity | No large gaps - max 5 seconds between frames |
| No keyframe guarantee | Regular frames include periodic IDR |
| Decoder staleness | Never idle long enough to become stale |

**Resource Impact**:

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

## Current Status: Testing Attempt 5

**Date**: 2026-01-20

Currently testing Attempt 5 (Trickle Feed Keep-Alive):

| Setting | Value |
|---------|-------|
| `is-live` | `true` |
| Trickle feed | Yes (1 frame per 5 seconds during idle) |
| Flush on stop_feeding | No |
| Flush on feed_detecting | No |
| Diagnostic logging | Yes (ERROR CONTEXT) |

**Hypothesis**: The decoder becomes stale because it receives NO data during idle periods. By feeding 1 frame every 5 seconds, we keep the decoder warm without significant CPU overhead.

### Summary of All Attempts

| Attempt | Approach | Result | Failure Mode |
|---------|----------|--------|--------------|
| Baseline | `is-live=true`, no flush | Errors occur | "not-negotiated" after idle |
| 1 | Flush on resume | FAILED | Race condition (~1s recovery) |
| 2 | Flush on stop | FAILED | Stale after ~67 min idle |
| 3 | Flush on stop + set_state(PLAYING) | FAILED | Still stale after idle |
| 4 | `is-live=false` | FAILED | Silent stall after ~2.5 hrs |
| **5** | **Trickle feed (1 frame/5sec)** | **TESTING** | - |

**Root cause identified**: The H.265 decoder becomes stale after extended idle periods with no data. Previous attempts tried to fix this with flush/state changes, but the real solution is to prevent the idle state entirely by keeping a trickle of data flowing.

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
