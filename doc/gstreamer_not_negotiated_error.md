# GStreamer "not-negotiated" Error Analysis

## Error Signature

```
gst-stream-error-quark: Internal data stream error. (1)
../libs/gst/base/gstbasesrc.c(3132): gst_base_src_loop (): /GstPipeline:pipeline*/GstAppSrc:m_appsrc:
streaming stopped, reason not-negotiated (-4)
```

## Error Location

- **File**: `gstreamer_threading.py`
- **Line 767**: `on_message_decode()` receives ERROR message from decode pipeline bus
- **Line 468**: Error breaks the message loop, triggering pipeline restart

## What "not-negotiated" Means

In GStreamer, a "not-negotiated" error (flow return -4) occurs when an element tries to push data but **caps (capabilities/format) negotiation** with downstream elements hasn't completed. The downstream elements don't know what data format to expect.

## System Architecture

The system uses two separate GStreamer pipelines:

### Main Pipeline (RTSP Capture)
```
rtspsrc → rtpdepay → parse → appsink
```
- Captures RTSP stream from IP camera
- Outputs encoded H.264/H.265 frames to `appsink`
- `on_new_sample()` callback receives frames

### Decode Pipeline (Face Detection)
```
appsrc → queue → h264parse/h265parse → avdec_h264/h265 → videoconvert → appsink
```
- Receives encoded frames pushed to `decode_appsrc`
- Decodes frames for face recognition
- `on_new_sample_decode()` outputs BGR frames

## Data Flow

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

## Root Cause

**The decode pipeline is NOT PLAYING when data is pushed to `decode_appsrc`.**

### The Crash Cycle

1. Pipeline crashes (initial trigger unknown or from this same error)
2. Pipeline monitor detects crash and initiates restart
3. Restart takes ~10+ seconds:
   - First `set_state(PLAYING)` attempt returns NOT SUCCESS
   - Waits 10 seconds
   - Second attempt succeeds
4. **During this restart window**, ONVIF notifications keep arriving
5. `feed_detecting()` is called, sets `is_feeding = True`
6. `on_new_sample()` sees `is_feeding = True`, pushes to `decode_appsrc`
7. `decode_appsrc` hasn't negotiated caps yet (decode pipeline not ready)
8. **"not-negotiated" error** triggers pipeline crash
9. Cycle repeats from step 2

### Evidence: Timestamp Correlation

| ONVIF Motion Detected | Error Occurs | Gap |
|-----------------------|--------------|-----|
| 13:22:52.064 | 13:22:52.400 | 0.34s |
| 13:23:11.320 | 13:23:11.660 | 0.34s |
| 13:24:26.443 | 13:24:26.850 | 0.41s |
| 13:25:02.457 | 13:25:02.821 | 0.36s |

Critical observation:
- **13:22:52.064** - `feed_detecting in` called
- **13:22:52.400** - ERROR occurs
- **13:23:04.330** - Pipeline actually becomes PLAYING (12 seconds later!)

The error occurs **before** the pipeline reaches PLAYING state.

### Pipeline Number Evidence

The incrementing pipeline numbers show continuous crash-restart cycles:
```
pipeline3 → pipeline5 → pipeline7 → pipeline9 → pipeline11 →
pipeline19 → pipeline21 → pipeline24 → pipeline27 → pipeline30 →
pipeline33 → pipeline36
```

## Code Analysis

### feed_detecting() - No Pipeline State Check

```python
def feed_detecting(self, running_seconds):
    logger.info(f"{self.cam_ip} feed_detecting in")

    if self.is_feeding:
        logger.info(f"{self.cam_ip} feed_detecting out, already feeding")
        return

    # Missing: Check if self.is_playing is True

    with self.detecting_lock:
        self.is_feeding = True  # Sets flag without verifying pipeline state
        ...
```

### on_new_sample() - Pushes to decode_appsrc When is_feeding

```python
def on_new_sample(self, sink, _):
    ...
    if not self.is_feeding:
        self.add_detecting_frame(...)
    else:
        self.push_detecting_buffer()  # Pushes to decode_appsrc
        ...
        ret = self.decode_appsrc.emit('push-sample', ...)  # Can fail if not negotiated
```

### on_message_decode() - Error Causes Pipeline Crash

```python
def on_message_decode(self, bus, message):
    ...
    elif message.type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        logger.error(f"{self.cam_ip} on_message_decode Gst.MessageType.ERROR: {err}, {debug}")
        raise ValueError(...)  # This breaks the message loop and triggers restart
```

## Error Log Pattern

```
[timestamp][ERROR]-gstreamer_threading.py:767,192.168.22.3 on_message_decode Gst.MessageType.ERROR:
gst-stream-error-quark: Internal data stream error. (1),
../libs/gst/base/gstbasesrc.c(3132): gst_base_src_loop ():
/GstPipeline:pipeline*/GstAppSrc:m_appsrc:

[timestamp][ERROR]-streaming stopped, reason not-negotiated (-4)

[timestamp][ERROR]-gstreamer_threading.py:468,192.168.22.3 Error in message loop:
[same error repeated]
```

## Impact

1. **Pipeline Instability**: Continuous crash-restart cycle every 20-40 seconds
2. **Failed Face Detection**: Detection attempts fail because pipeline isn't ready
3. **Resource Waste**: Constant pipeline teardown and recreation
4. **Delayed Detection**: Even when pipeline recovers, first ~10 seconds of motion may be missed

## Potential Fix

Add pipeline state check before enabling face detection feeding:

```python
def feed_detecting(self, running_seconds):
    logger.info(f"{self.cam_ip} feed_detecting in")

    # Check if pipeline is playing before starting detection
    if not self.is_playing:
        logger.warning(f"{self.cam_ip} feed_detecting out, pipeline not playing yet")
        return

    if self.is_feeding:
        logger.info(f"{self.cam_ip} feed_detecting out, already feeding")
        return
    ...
```

Additionally, add safety check in `on_new_sample()`:

```python
else:
    # Safety check: don't push to decode pipeline if not playing
    if not self.is_playing:
        logger.warning(f"{self.cam_ip} on_new_sample, skipping decode push - pipeline not playing")
        return Gst.FlowReturn.OK

    self.push_detecting_buffer()
    ...
```

## Related Files

- `gstreamer_threading.py`: StreamCapture class with pipeline management
- `py_handler.py`: Calls `feed_detecting()` when ONVIF motion or occupancy triggers detection

## Date Identified

2026-01-17

---

## UPDATE: New Analysis (2026-01-18)

### Original Hypothesis May Be Incorrect

The original root cause analysis assumed the error occurs during **pipeline startup race condition** (ONVIF notification arriving before decode pipeline is ready). However, new log analysis reveals a different pattern.

### New Finding: "Resume After Idle" Problem

The error can occur when the decode pipeline has been **idle for an extended period** (not receiving any data) and then suddenly receives data when detection resumes.

### Evidence: Error Timeline (2026-01-18 14:20:31)

```
14:08:57.016  GStreamer thread started (Thread-Gst-192.168.22.3-14:08:57.016477)
14:09:07.946  Decode pipeline set_state(PLAYING) → GST_STATE_CHANGE_ASYNC
14:09:09.656  Main pipeline: paused → playing
14:09:09.793  Decode pipeline: paused → playing ← Pipeline fully ready

... Detection working normally for several minutes ...

14:15:34.365  Last face detection frame processed (frame 101)
14:15:44     Timer expired (~10s after last trigger) → is_feeding = False
             Pipeline still PLAYING, but no data being pushed to decode pipeline

══════════════════════════════════════════════════════════════════════════
       ~5 MINUTES OF IDLE - NO DATA PUSHED TO DECODE PIPELINE
══════════════════════════════════════════════════════════════════════════

14:20:30.979  ONVIF Motion detected
14:20:30.980  trigger_face_detection - clearing stale context
14:20:30.980  feed_detecting in/out → is_feeding = True → START pushing data
14:20:31.281  ERROR: not-negotiated (301ms after resume)
```

**Key observation**: The GStreamer thread was running for **11.5 minutes** before the error. The decode pipeline was in PLAYING state and had been working correctly earlier. The error occurred when resuming after 5 minutes of idle.

### Root Cause: No Stream Discontinuity Handling

#### appsrc Configuration
```python
appsrc name=m_appsrc emit-signals=true is-live=true format=time
```
- `is-live=true`: Expects continuous live data
- `format=time`: Uses timestamped buffers

#### When `is_feeding = False` (Idle State)
```python
# on_new_sample() - line 251-252
if not self.is_feeding:
    self.add_detecting_frame(...)  # Buffer frames only
    # NO data pushed to decode pipeline's appsrc
```
The decode pipeline receives **nothing** while idle.

#### When `is_feeding` transitions `False → True`
```python
# feed_detecting() - lines 626-627
with self.detecting_lock:
    self.is_feeding = True
# NO pipeline flush, NO caps renegotiation, NO EOS
```

#### Next `on_new_sample()` after transition
```python
# on_new_sample() - lines 254-266
else:  # is_feeding = True
    self.push_detecting_buffer()  # Push ALL buffered frames immediately
    ret = self.decode_appsrc.emit('push-sample', ...)  # Current frame
```

#### `stop_feeding()` does nothing to pipeline
```python
def stop_feeding(self):
    self.is_feeding = False
    self.feeding_count = 0
    self.metadata_store.clear()
    # NO PIPELINE FLUSH!
    # NO EOS TO APPSRC!
    # Decode pipeline just stops receiving data...
```

### Why the Error Occurs

After 5 minutes of receiving no data:

| Issue | Description |
|-------|-------------|
| **Timestamp Discontinuity** | `format=time` expects continuous timestamps. After 5 min gap, new timestamps confuse pipeline |
| **No Stream Discontinuity Signal** | When stopping, no EOS or flush sent to decode pipeline |
| **No Restart Signal on Resume** | When resuming, no flush or caps renegotiation triggered |
| **Decoder State Stale** | h265parse/avdec may have flushed internal buffers, lost decoder context |
| **Stale Buffered Frames** | `detecting_buffer` contains frames from ~3 seconds ago with old timestamps |
| **No Keyframe Check** | Resume pushes whatever is buffered, may not start with keyframe |

### Two Distinct Failure Modes

| Mode | Trigger | Timing | Description |
|------|---------|--------|-------------|
| **Startup Race** | Pipeline restart + immediate ONVIF | ~0-10s after start | Decode pipeline not yet PLAYING |
| **Resume After Idle** | Long idle period + ONVIF trigger | After minutes of no data | Decode pipeline PLAYING but stale |

### Potential Fixes for Resume After Idle

#### Option 1: Flush decode pipeline on resume
```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # Flush decode pipeline to reset state
    self.decode_appsrc.send_event(Gst.Event.new_flush_start())
    self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

    with self.detecting_lock:
        self.is_feeding = True
        ...
```

#### Option 2: Reset decode pipeline state on resume
```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # Reset decode pipeline
    self.pipeline_decode.set_state(Gst.State.PAUSED)
    self.pipeline_decode.set_state(Gst.State.PLAYING)

    with self.detecting_lock:
        self.is_feeding = True
        ...
```

#### Option 3: Send EOS when stopping, recreate stream on resume
```python
def stop_feeding(self):
    ...
    self.is_feeding = False
    # Signal end of stream
    self.decode_appsrc.emit('end-of-stream')
```

#### Option 4: Clear detecting_buffer and wait for keyframe
```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    with self.detecting_lock:
        self.detecting_buffer.clear()  # Don't push stale frames
        self.is_feeding = True
        ...
```

### Updated Impact Assessment

The "resume after idle" failure mode is particularly problematic because:
1. It can occur during **normal operation** (not just startup)
2. It affects cameras that have **intermittent motion** (common in real deployments)
3. The longer the idle period, the more likely the error

### Reproduction Attempts (2026-01-18)

The error is **highly intermittent** and difficult to reproduce on demand:

| Time | Idle Duration | Pipeline Age | Result |
|------|---------------|--------------|--------|
| 14:20:31 | ~5 min | ~12 min | **ERROR** |
| 15:03:24 | ~19 min | ~23 min | SUCCESS |
| 15:30:18 | ~16 min | ~50 min | SUCCESS |

The error depends on factors that are hard to control:
- Decoder internal state (varies based on video content/keyframes)
- Buffer state (what's in `detecting_buffer` at resume time)
- Timing (exact microsecond when data hits the decoder)
- H.265 codec state (parser/decoder synchronization)

---

## Monitoring & Future Investigation

### CloudWatch Insights Query

Use this query to find occurrences of the error:

```sql
SELECT `@timestamp`, `@message` FROM $source
WHERE `@message` like '%streaming stopped, reason not-negotiated%' AND `@message` like '%ERROR%'
ORDER BY `@timestamp` DESC
LIMIT 1000;
```

**Log group**: `/aws/greengrass/Lambda/ap-northeast-1/769412733712/demo-py_handler`

### Diagnostic Logging Added

The following diagnostic logging was added to capture state when the error occurs:

1. **Line 450** (`gstreamer_threading.py`): Log decode pipeline `set_state(PLAYING)` return value
   ```
   192.168.22.3 Decode pipeline set_state(PLAYING) returned: <enum GST_STATE_CHANGE_ASYNC ...>
   ```

2. **Line 789** (`gstreamer_threading.py`): Decode pipeline state changes (changed from DEBUG to INFO)
   ```
   192.168.22.3 Decode Pipeline state changed from paused to playing with pending_state void-pending
   ```

3. **Line 775-783** (`gstreamer_threading.py`): **ERROR CONTEXT** logged automatically with error
   ```
   192.168.22.3 ERROR CONTEXT: is_feeding=True, is_playing=True, feeding_count=5, decoding_count=0, detecting_buffer_len=3, main_pipeline_state=playing, decode_pipeline_state=playing, thread=Thread-Gst-192.168.22.3-14:08:57.016477
   ```

   | Field | Description |
   |-------|-------------|
   | `is_feeding` | Was the decode pipeline receiving data? |
   | `is_playing` | Was the main pipeline marked as playing? |
   | `feeding_count` | How many frames pushed to decode pipeline |
   | `decoding_count` | How many decoded frames produced |
   | `detecting_buffer_len` | How many buffered frames waiting |
   | `main_pipeline_state` | Actual GStreamer state of main pipeline |
   | `decode_pipeline_state` | Actual GStreamer state of decode pipeline |
   | `thread` | Thread name (contains creation timestamp for pipeline age) |

### When Error Occurs Next

The **ERROR CONTEXT** log now captures most diagnostic information automatically. When the error is detected via CloudWatch, collect logs around the error timestamp (±60 seconds) to also capture:

1. **`feed_detecting in/out`** - to determine when feeding resumed and calculate idle period
2. **`ONVIF Motion detected`** or **`init_gst_app`** - to see what triggered the error scenario
3. **`clearing stale context`** - to check if stale context was involved

### Analysis Checklist

When analyzing the next occurrence, the ERROR CONTEXT provides most answers directly:

| Question | Source |
|----------|--------|
| Pipeline age? | `thread` field contains creation timestamp |
| Was decode pipeline ready? | `decode_pipeline_state` field |
| Was main pipeline ready? | `main_pipeline_state` field |
| Were we pushing data? | `is_feeding` field |
| How many frames pushed? | `feeding_count` field |
| Buffered frames? | `detecting_buffer_len` field |

**Still need logs ±60s to determine:**
- [ ] How long was the idle period? (time since last `feed_detecting out`)
- [ ] Was this startup race or resume-after-idle? (check for `init_gst_app` vs long idle)
- [ ] What triggered detection? (ONVIF motion, occupancy, or config change)

---

### Revision History

| Date | Changes |
|------|---------|
| 2026-01-17 | Initial analysis: startup race condition hypothesis |
| 2026-01-18 | New analysis: "resume after idle" problem identified |
| 2026-01-18 | Added reproduction attempts, CloudWatch query, and future investigation approach |
| 2026-01-18 | Added ERROR CONTEXT logging to capture pipeline state automatically when error occurs |
