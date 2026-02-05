# Bug #10: Hailo Recognition Failure After Lighting Change

**Status:** FIX IMPLEMENTED (Testing Required)
**Discovered:** 2026-02-04
**Priority:** High
**Backend:** Hailo only (InsightFace unaffected)

---

## Summary

Hailo ArcFace recognition stops matching a previously recognized member after a brief lighting change, even after lighting is restored to original conditions. The face is detected in every frame but similarity scores drop significantly and never recover.

## Timeline

1. **19:53:03** — Detection triggered, Hailo backend, 1 member in embeddings matrix
2. **19:53:04** — Member "RULINBaby" matched on frame #11 with `sim: 0.3084` (borderline)
3. Light adjusted briefly, then changed back to original conditions
4. **19:53:53** — Detection triggered again, same person, same position
5. **19:53:54 – 19:54:03** — 101 frames processed, face detected in ALL frames, **zero matches**
6. Session ended with `identified_at: 0`

## Log Evidence

### Successful session (19:53:03)

```
face_recognition_hailo.py:628, detection frame #11 - age: 0.075 duration: 0.027 face(s): 1
face_recognition_hailo.py:646, detected: 11 age: 0.075 fullName: RULINBaby sim: 0.3084 (MATCH)
```

Best similarity values across frames: 0.19, 0.22, 0.20, 0.18, 0.21, 0.20, 0.27, 0.17, 0.17, 0.20, **0.31**

### Failed session 1 (19:53:53)

```
101 frames, all "no match"
best_sim range: 0.10 – 0.27
Session ended - detected: 101, face_detected_at: 1, face_detected_frames: 101, identified_at: 0
```

### Failed session 2 (20:59:37) — Additional anomalies

```
101 frames total:
  - Frames 1-55: face(s): 0 in ALL frames (face detection itself failed)
  - Frames 56-101: face(s): 1 or 2, but best_sim: 0.06 – 0.27 (no match)
  - Frames 94-95: NEGATIVE similarity scores (best_sim: -0.0329, -0.0011)
  - Multiple frames detected 2 faces when only 1 person was in camera view
Session ended - detected: 101, face_detected_at: 56, face_detected_frames: 45, identified_at: 0
```

## Key Observations

1. **Face detection works fine in some sessions, fails completely in others** — Session at 19:53:03 detected faces from frame 1. Session at 20:59:37 detected zero faces for 55 frames, then suddenly started detecting.
2. **Recognition fails** — ArcFace embeddings produce consistently low similarity when faces are detected
3. **Lighting was restored** — The light was changed briefly and then changed back, but recognition did not recover
4. **Same person, same position** — Nothing changed except the brief lighting adjustment
5. **Successful match was borderline** — 0.3084 is very close to threshold, suggesting Hailo embeddings have low discriminative power for this subject
6. **InsightFace comparison** — Earlier sessions with InsightFace produced sim 0.38–0.60 for the same person, much higher headroom
7. **Ghost face detection** — Hailo SCRFD detects 2 faces in frames where only 1 person is present (frames 65, 66, 72, 74-78). This is a false positive from the detection model.
8. **Negative similarity scores** — Frames 94-95 produced `best_sim: -0.0329` and `-0.0011`. Cosine similarity should range [-1, 1], so these are valid but indicate the embedding is essentially orthogonal/opposite to the reference — suggesting corrupted or degenerate embeddings.
9. **Detection latency gap** — Frames 1-55 had `duration: 0.008-0.010s` (no face detected, fast). Frames 56+ had `duration: 0.026-0.032s` (face detected, slower due to ArcFace inference). The sudden switch from 0→1 faces at frame 56 is unexplained.

## Similarity Score Comparison (Same Person)

| Backend | Session | Best sim | Result |
|---|---|---|---|
| InsightFace | Earlier tests | 0.38 – 0.60 | Consistent matches |
| Hailo | 19:53:03 | 0.3084 | Borderline match |
| Hailo | 19:53:53 | 0.2668 | No match (101 frames) |
| Hailo | 20:59:37 | 0.2693 | No match (101 frames), ghost faces, negative sims |

## Possible Causes (To Investigate)

1. **Hailo model state drift** — Something in the Hailo VDevice or inference pipeline state changes after extended use, affecting embedding quality. The 20:59 session (over 1 hour after init) shows worse behavior than 19:53.
2. **SCRFD false positives (ghost faces)** — The detection model hallucinates faces in regions without faces. This may be caused by score_threshold being too low, or quantization artifacts in the detection model.
3. **Degenerate embeddings** — Negative similarity scores suggest the ArcFace model is producing garbage embeddings for some face crops (possibly from ghost detections or poor alignment).
4. **Quantization sensitivity** — The HEF model uses uint8 quantization; small input changes may cause disproportionate output shifts.
5. **Alignment sensitivity** — If SCRFD landmark detection shifts slightly with lighting, the ArcFace alignment (SimilarityTransform) changes the face crop, producing different embeddings.
6. **Reference embedding quality** — The stored embedding may have been extracted under specific conditions that don't generalize well with the Hailo model.

## What Would Help

- Compare stored reference embedding (from `/recognise` endpoint) with live embeddings from failed frames
- Run both InsightFace and Hailo on the same frames to compare embedding stability
- Check if Hailo VDevice re-initialization restores recognition quality
- Log bounding box coordinates when 2 faces are detected (to verify ghost face location)
- Check SCRFD score_threshold — current default is 0.5, may need raising
- Test with a lower FACE_THRESHOLD_HAILO to see if lowering threshold helps (but risks false positives)

## Files Involved

| File | Relevance |
|---|---|
| `face_recognition_hailo.py` | Hailo FaceRecognition thread, `find_match()` |
| `face_recognition_hailo.py` | `HailoFaceApp._extract_embedding()` — embedding extraction |
| `face_recognition_hailo.py` | `HailoFaceApp._align_face()` — face alignment before recognition |
| `face_recognition_hailo.py` | `HailoFaceApp._postprocess_detection()` — NMS and score filtering |

---

## Investigation: Official Hailo Preprocessing Requirements (2026-02-05)

Research into official Hailo Model Zoo documentation revealed critical preprocessing specifications.

### Official Hailo Model Specifications

#### SCRFD (Face Detection)
Source: [hailo_model_zoo/cfg/base/scrfd.yaml](https://github.com/hailo-ai/hailo_model_zoo)

| Parameter | Value |
|---|---|
| Input size | 640×640×3 |
| Color format | **RGB** |
| Normalization | Built into HEF (`normalize_in_net: true`) |
| Mean | `[127.5, 127.5, 127.5]` |
| Std | `[128.0, 128.0, 128.0]` |

#### ArcFace MobileFaceNet (Face Recognition)
Source: [hailo_model_zoo/cfg/networks/arcface_mobilefacenet.yaml](https://github.com/hailo-ai/hailo_model_zoo)

| Parameter | Value |
|---|---|
| Input size | 112×112×3 |
| Color format | **RGB** |
| Normalization | Built into HEF (`normalize_in_net: true`) |
| Mean | `[127.5, 127.5, 127.5]` |
| Std | `[127.5, 127.5, 127.5]` |
| Output | 512-dimensional embedding |

### Key Finding: Two Potential Issues Identified

#### Issue 1: BGR vs RGB — CONFIRMED PROBLEM

- **Hailo models expect RGB input** (confirmed in official YAML configs)
- **OpenCV reads images in BGR by default**
- Current code may be feeding BGR directly to HEF models without conversion
- **Impact**: Channel swap causes R↔B confusion, degrading embedding quality

#### Issue 2: Normalization — NOT a Problem

- Normalization is **baked INTO the HEF model** during compilation
- The `.alls` config files show: `normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])`
- User should feed **raw uint8 (0-255) RGB** directly
- The HEF internally computes: `(pixel - mean) / std`
- **No external normalization required**

### The Fix

```python
# Before feeding to HEF model
rgb_image = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
# Feed rgb_image to HEF - no normalization needed (it's built-in)
```

### Sources

- [Hailo Model Zoo GitHub](https://github.com/hailo-ai/hailo_model_zoo)
- [Hailo Community: Comprehensive Guide to Face Recognition](https://community.hailo.ai/t/a-comprehensive-guide-to-building-a-face-recognition-system/8803)
- [DeGirum: Hailo Input Preprocessing](https://docs.degirum.com/hailo/intermediate-guides/model-properties/input-preprocessing)
- [hailo_model_zoo/cfg/alls/generic/scrfd_10g.alls](https://github.com/hailo-ai/hailo_model_zoo/blob/master/hailo_model_zoo/cfg/alls/generic/scrfd_10g.alls)
- [hailo_model_zoo/cfg/alls/generic/arcface_mobilefacenet.alls](https://github.com/hailo-ai/hailo_model_zoo/blob/master/hailo_model_zoo/cfg/alls/generic/arcface_mobilefacenet.alls)
- [Seeed Face Recognition API](https://github.com/Seeed-Solution/face-recognition-api)

### Fix Implementation (2026-02-05)

**Status: IMPLEMENTED**

Added BGR→RGB conversion in `face_recognition_hailo.py:267`:

```python
# In HailoFaceApp.get() method
# Convert BGR to RGB — Hailo HEF models (SCRFD, ArcFace) expect RGB input
# This matches InsightFace behavior which also converts BGR→RGB internally
img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
```

This fix affects both:
- **Detection (SCRFD)** — now receives RGB input as expected
- **Recognition (ArcFace)** — now receives RGB input as expected
- **`/recognise` endpoint** — uses same `face_app`, so reference embeddings will also be correct

### Next Steps

1. ~~Verify current code~~ — ✅ Confirmed BGR was fed directly without conversion
2. ~~Add BGR→RGB conversion~~ — ✅ Implemented in `HailoFaceApp.get()`
3. **Re-test** with same subject under same lighting conditions
4. **Compare similarity scores** before/after fix
5. **Re-register faces** — Existing Hailo-created embeddings may need to be re-created with the fix
