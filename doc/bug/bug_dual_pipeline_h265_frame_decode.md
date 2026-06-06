# Bug #6: Dual-Pipeline H265 Frame Decode Failure

**Status:** FIXED (2026-02-02)
**Discovered:** 2026-02-01
**Priority:** High

## Summary

In the dual-pipeline GStreamer architecture, only ~10% of H265 frames could be properly decoded for face detection. **Root cause:** Creating `Gst.Sample.new()` with modified caps broke P-frame decoding. **Fix:** Push original sample, use PTS-based metadata store for frame_time.

## Architecture Background

The system uses a dual-pipeline architecture to support both recording and detection:

```
Pipeline 1 (Capture):
  rtspsrc → rtph265depay → tee
                           ├── queue → h265parse → splitmuxsink (recording)
                           └── queue → appsink (raw NAL units)
                                          ↓
                                    [appsrc push]
                                          ↓
Pipeline 2 (Decode):
  appsrc → h265parse → avdec_h265 → videoconvert → videorate → appsink (BGR frames)
```

## Problem Description

### H265 Frame Types
- **I-frames (Keyframes):** Self-contained, full image data, no dependencies
- **P-frames (Predicted):** Require previous I-frame or P-frame as reference
- **B-frames (Bidirectional):** Require both previous and future frames as reference

### What Happens
1. Pipeline 1 captures RTSP stream and extracts raw NAL units
2. NAL units are pushed to Pipeline 2 via `appsrc`
3. Pipeline 2's `avdec_h265` decoder tries to decode frames
4. **Problem:** P/B frames reference previous frames that exist in Pipeline 1's decoder state, but Pipeline 2's decoder has no access to that state
5. Only I-frames decode correctly (they contain full image data)

### Observed Behavior

**Test 1: Hailo Backend**
```
Frames with faces: #7, #8, #15, #23, #31, #39, #47, #56, #62, #69, #76
Pattern: ~every 8 frames (GOP interval)
Duration on good frames: ~21-25ms
Duration on bad frames: ~10-12ms (fast rejection)
Pixel range on good frames: min=0, max=255, range=255
Pixel range on bad frames: partial range (even >100 fails)
```

**Test 2: InsightFace Backend**
```
Frames with faces: #15, #18, #19, #25, #35, #45, #55, #65, #75, #85, #95
Pattern: ~every 10 frames (11 detections out of 101 = ~11%)
Duration: ~80-120ms per frame (consistent, no fast rejection)
```

### Key Evidence

1. **Both backends fail on same frames** - confirms it's not backend-specific
2. **GOP interval correlation** - faces detected roughly every 8-10 frames matches typical I-frame interval
3. **Pixel range diagnostic** - only frames with full dynamic range (min=0, max=255) produce detectable faces
4. **Fast inference on bad frames (Hailo)** - Hailo quickly rejects invalid input (~10ms vs ~22ms)
5. **Standalone test works** - `face_detector_hailo_a.py` with single pipeline detects faces on every frame

## Root Cause (Initial Theory - INCORRECT)

Initial theory was that `appsrc` breaks the H265 reference frame chain. This was **incorrect**.

## Actual Root Cause (CONFIRMED)

The actual root cause was **modifying caps when creating new samples**:

```python
# BROKEN CODE - This breaks P-frame decoding!
def edit_sample_caption(self, sample, current_time):
    # Modify caps to embed frame_time metadata
    caps_string = sample_caps.to_string()
    caps_string += f',frame-time=(string){current_time}'
    new_caps = Gst.Caps.from_string(caps_string)
    return Gst.Sample.new(sample_buffer, new_caps, ...)  # <- BREAKS P-FRAMES
```

When `Gst.Sample.new()` is called with modified caps:
1. GStreamer treats it as a caps change event
2. The decoder resets its internal state
3. P-frames lose their reference to previous I-frames
4. Result: corrupted/gray output on P-frames

## Comparison: Single vs Dual Pipeline

| Aspect | Single Pipeline | Dual Pipeline |
|--------|-----------------|---------------|
| Decoder state | Maintained continuously | Separate per pipeline |
| P/B frame decode | Works (has reference frames) | Fails (no reference frames) |
| I-frame decode | Works | Works |
| Face detection rate | ~100% of frames | ~10% of frames |
| Recording | N/A (separate concern) | Works (Pipeline 1 records encoded stream) |

## Potential Solutions

### Option 1: Single Pipeline with Shared Decoded Frames
Decode once in a single pipeline, share decoded frames for both recording and detection.

**Pros:**
- All frames decoded correctly
- Efficient (decode once)

**Cons:**
- Recording needs encoded stream, not decoded frames
- Major architecture change required
- Cannot record original H265 stream directly

### Option 2: Pass Decoded Frames Through appsrc
Decode in Pipeline 1, pass decoded BGR/RGB frames to Pipeline 2 for detection.

**Pros:**
- All frames available for detection
- Recording still works (tee before decode)

**Cons:**
- High bandwidth through appsrc (uncompressed video)
- CPU overhead for passing large frames
- Memory pressure

### Option 3: I-Frame Only Stream for Detection
Configure camera to send I-frame only stream (or request IDR frames more frequently).

**Pros:**
- All frames decode correctly
- No architecture change needed

**Cons:**
- Significantly higher bandwidth from camera
- Higher latency (larger frames)
- Camera must support this configuration

### Option 4: Single Pipeline Architecture
Use one pipeline for everything, accept that detection and recording share resources.

**Pros:**
- Simplest fix
- All frames work

**Cons:**
- Original reason for dual-pipeline may resurface
- Resource contention between recording and detection

### Option 5: Accept Current Limitation
Keep dual-pipeline, accept ~10% frame detection rate.

**Pros:**
- No code changes
- Recording works perfectly
- Face detection still works (just needs more frames)

**Cons:**
- Lower detection reliability
- May miss faces if they appear only briefly
- Wastes CPU processing frames that will fail

## Fix Applied (2026-02-02)

### Solution: Push Original Sample, Use PTS-based Metadata Store

Instead of modifying caps to embed metadata, use PTS (Presentation Timestamp) as a key to pass metadata between pipelines:

```python
# FIXED CODE - gstreamer_threading.py
def edit_sample_caption(self, sample, current_time):
    sample_buffer = sample.get_buffer()
    pts = sample_buffer.pts

    # Store metadata keyed by PTS (don't modify caps!)
    with self.metadata_lock:
        self.metadata_store[pts] = current_time

    # Only create new sample if framerate fix is needed
    if sample_framerate == 0:
        # Fix framerate only, no custom metadata in caps
        return Gst.Sample.new(sample_buffer, new_caps, ...)

    # Return ORIGINAL sample - metadata passed via PTS lookup
    return sample

def on_new_sample_decode(self, sink, _):
    buffer = sample.get_buffer()
    pts = buffer.pts

    # Look up frame_time using PTS
    with self.metadata_lock:
        frame_time = self.metadata_store.get(pts)
```

### Files Changed

1. **gstreamer_threading.py** - PTS-based metadata store, push original sample
2. **face_recognition.py** - Removed P-frame skip logic (no longer needed)
3. **face_recognition_hailo.py** - Removed P-frame skip logic (no longer needed)
4. **py_handler.py** - Re-enabled Hailo auto-detection

### Test Results After Fix

| Test | Before Fix | After Fix |
|------|------------|-----------|
| Modified caps | 15.9% good P-frames | N/A |
| Original sample | N/A | 100% good P-frames |
| Face detection | ~10% of frames | 100% of frames |

### Backend Selection

Auto-detection re-enabled:
```python
# py_handler.py - detect_face_backend()
if not HAILO_IMPORT_AVAILABLE:
    return 'insightface'
try:
    vdevice = VDevice()  # Probe for Hailo hardware
    return 'hailo'
except:
    return 'insightface'
```

## Test Results Summary

| Backend | Detection Rate | Notes |
|---------|---------------|-------|
| Hailo | ~12% (1 in 8) | Fast rejection on bad frames |
| InsightFace | ~11% (1 in 10) | Consistent timing on all frames |
| Standalone (single pipeline) | ~100% | Hailo works on every frame |

## Files Involved

- `gstreamer_threading.py` - Dual-pipeline implementation
- `py_handler.py` - Backend selection (`detect_face_backend()`)
- `face_recognition.py` - InsightFace detection
- `face_recognition_hailo.py` - Hailo detection

## Related Issues

- **Bug #2 (GStreamer not-negotiated):** May be related to pipeline state issues
- **Bug #3 (OOM):** Dual-pipeline uses more memory than single pipeline

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-01 | Issue discovered during Hailo integration testing |
| 2026-02-01 | Confirmed with InsightFace - same ~10% detection rate |
| 2026-02-01 | Initial theory: appsrc breaks H265 reference frame chain |
| 2026-02-01 | Documented with test results from both backends |
| 2026-02-02 | **ROOT CAUSE FOUND**: Modifying caps breaks P-frame decoding |
| 2026-02-02 | **FIX APPLIED**: Push original sample, use PTS-based metadata store |
| 2026-02-02 | Test confirmed: 100% P-frames decode correctly after fix |
| 2026-02-02 | Removed P-frame skip logic from face_recognition.py and face_recognition_hailo.py |
| 2026-02-02 | Re-enabled Hailo auto-detection in py_handler.py |
| 2026-02-02 | **Status: FIXED** |
