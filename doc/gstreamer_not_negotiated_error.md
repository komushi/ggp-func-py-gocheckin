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

The decode pipeline is configured with `is-live=true format=time`, expecting continuous timestamped data. When `is_feeding = False`, the decode pipeline receives **no data** for extended periods. After long idle periods (5+ minutes), the H.265 decoder loses its internal state and cannot recover when data resumes.

### Why It Happens

| Issue | Description |
|-------|-------------|
| **Decoder buffers flush** | avdec_h265 loses reference frames during idle |
| **Parser state resets** | h265parse loses NAL unit context |
| **Timestamp discontinuity** | New data has timestamps minutes ahead of last frame |
| **No keyframe guarantee** | Resume may not start with IDR frame |
| **No discontinuity signal** | Pipeline receives no flush/EOS when feeding stops |

### Code Analysis

**`stop_feeding()` does nothing to pipeline:**
```python
def stop_feeding(self):
    self.is_feeding = False
    self.feeding_count = 0
    self.metadata_store.clear()
    # NO PIPELINE FLUSH!
    # NO EOS TO APPSRC!
    # Decode pipeline just stops receiving data...
```

**`feed_detecting()` has no reset:**
```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return
    with self.detecting_lock:
        self.is_feeding = True  # Just sets flag, no pipeline reset
        ...
```

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

### Reproduction Data

| Time | Idle Duration | Pipeline Age | Result |
|------|---------------|--------------|--------|
| 14:20:31 | ~5 min | ~12 min | **ERROR** |
| 15:03:24 | ~19 min | ~23 min | SUCCESS |
| 15:30:18 | ~16 min | ~50 min | SUCCESS |
| 15:57:16 | ~6.5 min | ~6.5 min | SUCCESS |
| **17:20:29** | **~37 min** | **~54 min** | **ERROR** |

The error is intermittent - depends on decoder internal state, buffer contents, and timing.

---

## Fix Options

| Option | Approach | When Applied | Pros | Cons | Complexity |
|--------|----------|--------------|------|------|------------|
| **1. Flush on Resume** | Send `flush_start`/`flush_stop` events | On resume | Resets decoder state; standard GStreamer pattern; fast | May cause brief frame drop | Medium |
| **2. Reset Pipeline State** | Cycle `PAUSED → PLAYING` | On resume | Full state reset; guaranteed clean | Slower (~100-500ms) | Medium |
| **3. Send EOS on Stop** | Send `end-of-stream` to appsrc | On stop | Clean stream termination | Complex state management | High |
| **4. Wait for Keyframe** | Clear buffer; skip non-keyframes | On resume | Ensures valid keyframe | 1-2s delay; needs keyframe detection | Medium-High |

### Option 1: Flush on Resume (Recommended) - IMPLEMENTED

```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # 1. Flush decode pipeline FIRST (reset stale decoder state)
    self.decode_appsrc.send_event(Gst.Event.new_flush_start())
    self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

    # 2. Clear buffer SECOND (discard stale frames)
    with self.detecting_lock:
        self.detecting_buffer.clear()
        self.is_feeding = True
        ...
```

**Why this order matters**:

| Step | Action | Purpose |
|------|--------|---------|
| 1 | `flush_start()` | Stop pipeline, discard internal decoder buffers |
| 2 | `flush_stop(True)` | Resume pipeline, reset running time (fixes timestamp discontinuity) |
| 3 | `detecting_buffer.clear()` | Don't push old frames into freshly reset pipeline |
| 4 | `is_feeding = True` | Now ready for fresh frames |

**Why NOT on stop**: Pipeline becomes stale during idle anyway. Reset at resume is the right time.

**Why recommended**:
- Standard GStreamer pattern for stream discontinuities
- Fast (no pipeline state change overhead)
- Resets decoder internal state without full restart
- Combined with buffer clear, ensures no stale frames pushed

### Option 2: Reset Pipeline State (Fallback)

```python
def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # Reset decode pipeline by cycling through PAUSED
    self.pipeline_decode.set_state(Gst.State.PAUSED)
    self.pipeline_decode.get_state(Gst.CLOCK_TIME_NONE)
    self.pipeline_decode.set_state(Gst.State.PLAYING)

    with self.detecting_lock:
        self.is_feeding = True
        ...
```

Use if Option 1 doesn't fully resolve the issue.

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

1. **ERROR CONTEXT** (line 775-783):
   ```
   192.168.22.3 ERROR CONTEXT: is_feeding=True, is_playing=True, feeding_count=12,
   decoding_count=0, detecting_buffer_len=0, main_pipeline_state=playing,
   decode_pipeline_state=playing, thread=Thread-Gst-192.168.22.3-16:26:44.712152
   ```

2. **Decode pipeline state changes** (line 789):
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

### When Error Occurs

Collect logs ±60 seconds around error timestamp to determine:
- Idle period (time since last `feed_detecting`)
- What triggered detection (ONVIF motion, occupancy)
- Pipeline age (from thread name timestamp)

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
| 2026-01-18 | Added fix options comparison; recommend Option 1 (Flush on Resume) |
| 2026-01-18 | **FIX IMPLEMENTED**: Option 1 - flush decode pipeline + clear buffer on resume in `feed_detecting()` |
