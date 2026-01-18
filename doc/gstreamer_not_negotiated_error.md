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

### Attempt 2: Flush on Stop (IMPLEMENTED)

**Approach**: Move flush from `feed_detecting()` to `stop_feeding()`. Flush when stopping detection, not when resuming.

```python
def stop_feeding(self):
    with self.detecting_lock:
        self.is_feeding = False
        self.feeding_count = 0
        self.decoding_count = 0

    # FIX: Flush decode pipeline on STOP (not on resume)
    # This resets decoder state while no frames are being pushed.
    # Pipeline will recover to PLAYING during idle period,
    # so it's ready when next detection starts.
    logger.info(f"{self.cam_ip} stop_feeding flushing decode pipeline")
    self.decode_appsrc.send_event(Gst.Event.new_flush_start())
    self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

    with self.metadata_lock:
        self.metadata_store.clear()


def feed_detecting(self, running_seconds):
    if self.is_feeding:
        return

    # No flush here - pipeline already recovered during idle
    with self.detecting_lock:
        self.detecting_buffer.clear()
        self.is_feeding = True
        ...
```

**Why this works**:

| Timing | Flush on Resume (failed) | Flush on Stop (fix) |
|--------|--------------------------|---------------------|
| **On stop** | Nothing | Flush → PAUSED |
| **During idle** | Decoder state stale | Pipeline recovers to PLAYING |
| **On resume** | Flush → PAUSED → race condition | Already PLAYING → ready |

**Benefits**:
- Flush happens when `is_feeding = False`, so no frames are pushed during PAUSED state
- Pipeline has entire idle period (~seconds to minutes) to recover to PLAYING
- On resume, pipeline is already in PLAYING state - no race condition

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

3. **Flush on stop** (new fix):
   ```
   192.168.22.3 stop_feeding flushing decode pipeline
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
| 2026-01-19 | **NEW FIX**: Moved flush to `stop_feeding()` - flush on stop, not on resume |
