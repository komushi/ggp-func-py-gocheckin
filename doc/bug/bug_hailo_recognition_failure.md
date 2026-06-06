# Bug #10: Hailo Recognition Failure After Lighting Change

**Status:** RESOLVED (2026-02-07) — Use InsightFace for production
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

### Fix 1: BGR→RGB Conversion (2026-02-05)

Added BGR→RGB conversion in `face_recognition_hailo.py` `HailoFaceApp.get()`:

```python
img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
```

This fix affects Detection (SCRFD), Recognition (ArcFace), and `/recognise` endpoint.

**Result:** Similarity improved slightly but still too low (0.15–0.27). Not sufficient alone.

### Fix 2: FormatType.FLOAT32 Auto-Dequantization (2026-02-05)

**Root cause:** Manual dequantization `(raw_uint8 - qp_zp) * qp_scale` using quant params from `get_output_vstream_infos()` does not match HailoRT's internal dequantization precision. This primarily affected SCRFD landmark accuracy, causing face alignment errors that degraded ArcFace embeddings.

**Fix:** Use `FormatType.FLOAT32` on all model outputs before `configure()`, letting HailoRT auto-dequantize on the host:

```python
from hailo_platform import FormatType

# Detection model
for output_info in self.det_infer_model.hef.get_output_vstream_infos():
    self.det_infer_model.output(output_info.name).set_format_type(FormatType.FLOAT32)
self.det_configured = self.det_infer_model.configure()

# Recognition model
self.rec_infer_model.output().set_format_type(FormatType.FLOAT32)
self.rec_configured = self.rec_infer_model.configure()
```

Output buffers changed from `np.uint8` to `np.float32`, manual dequantization removed.

**Result:**
- Close range: **101/101 frames MATCH**, sim 0.35–0.47 (avg ~0.44)
- Medium range: sim 0.30–0.38, consistent matches
- Far range: sim 0.20–0.29, borderline (expected for quantized model at low face resolution)
- Comparable to InsightFace performance (0.38–0.60)

### Resolution (2026-02-05)

Both fixes combined resolved the bug:
1. ~~BGR→RGB conversion~~ — ✅ Correct color input to HEF models
2. ~~FormatType.FLOAT32~~ — ✅ Accurate dequantization via HailoRT
3. ~~Re-test~~ — ✅ Verified with live detection at multiple distances
4. ~~Re-register faces~~ — ✅ Embeddings re-created after fix

---

## Regression: Similarity Dropped Overnight (2026-02-06)

### Symptoms

After ~19.5 hours of runtime, similarity scores dropped significantly:

| Time | Person | Best sim | Result |
|------|--------|----------|--------|
| 2026-02-05 14:04 | CuteBaby | 0.33–0.45 | MATCH (working) |
| 2026-02-06 09:07 | CuteBaby | 0.13–0.23 | No match (101 frames) |
| 2026-02-06 09:25 | CuteBaby | 0.15–0.25 | No match (after restart) |

### Investigation

1. **Code unchanged** — Deployed code confirmed to have FormatType.FLOAT32 fix
2. **Stored embeddings unchanged** — CuteBaby: mean=-0.0031, std=0.0441 (identical)
3. **Greengrass restarted** — Did not fix the issue (09:15 restart, still failing at 09:25)
4. **Root cause unknown** — Restart disproved "runtime drift" hypothesis

### Hypothesis

The issue is in **live embedding generation**, not stored embeddings. Need to compare:
- `ArcFace output: mean, std` between working and failing states
- `Live embedding: pre_norm` between working and failing states

### Diagnostic Logging Added

```python
# face_recognition_hailo.py:514
logger.info(f"ArcFace output: {output_name}, shape={raw.shape}, dtype={raw.dtype}, mean={raw.mean():.4f}, std={raw.std():.4f}")

# face_recognition_hailo.py:532
logger.info(f"Live embedding: pre_norm={norm:.4f}, mean={embedding.mean():.4f}, std={embedding.std():.4f}")

# face_recognition_hailo.py:663
logger.info(f"... best_match: {best_name} best_sim: {sim:.4f} (no match)")
```

### Next Steps

1. Deploy code with new logging
2. Test immediately — expect sim ~0.45-0.50, capture stats
3. If/when degradation occurs — capture logs with sim ~0.20
4. Compare ArcFace output and pre_norm between the two states
5. If needed, re-register faces using same images

---

## Comprehensive Testing (2026-02-06 to 2026-02-07)

**See:** [`bug10_backend_comparison.md`](bug10_backend_comparison.md) for detailed test results.

### Summary

| Backend | Subject | Similarity | Match Rate |
|---------|---------|------------|------------|
| Hailo MobileFaceNet (degraded) | CuteBaby | 0.27-0.35 | 64% |
| Hailo MobileFaceNet (fresh) | CuteBaby | 0.31-0.36 | 98% |
| **InsightFace** | CuteBaby | **0.49-0.54** | **100%** |
| Hailo MobileFaceNet | NiceDaddy (close) | 0.28-0.32 | MATCH |
| Hailo MobileFaceNet | NiceDaddy (far) | 0.07-0.15 | FAIL |

### Key Findings

1. **Pi reboot restores Hailo** - Greengrass restart alone does NOT fix degradation
2. **InsightFace is ~0.20 higher** - More stable, better distance tolerance
3. **Hailo requires close range** - Far/medium distance fails

---

## Root Causes Identified

1. **Hailo VDevice State Degradation** - Needs periodic reboot/reinit
2. **INT8 Quantization Sensitivity** - Threshold 0.30 in middle of variance band (±0.04)
3. **Distance/Resolution Limitation** - 112×112 input needs sufficient face resolution

---

## Models Tested

| Model | File | Params | Size | Status |
|-------|------|--------|------|--------|
| arcface_mobilefacenet | `/etc/hailo/models/arcface_mobilefacenet.hef` | 2M | ~2MB | ❌ Unreliable |
| arcface_r50 | `/etc/hailo/models/arcface_r50.hef` | 31M | ~31MB | ⚠️ Better but still limited |
| InsightFace buffalo_sc | CPU | 31M | ~31MB | ✅ **Recommended** |

### arcface_r50 Thresholds (Validated 2026-02-07)

| Parameter | Threshold | Tested Range | Notes |
|-----------|-----------|--------------|-------|
| **pre_norm** | **≥ 10** | 7.9 - 20.7 | Hard gate: 0% below, 100% above |
| **similarity** | **≥ 0.30** | 0.02 - 0.67 | Margin is tight (garbage ends at 0.28) |

**Behavior:**
- pre_norm < 10: similarity caps at ~0.28 (never matches)
- pre_norm ≥ 10: similarity jumps to 0.30-0.67 (always matches)
- Once above pre_norm threshold, similarity varies ±0.04 independent of pre_norm
- Correlation between pre_norm and similarity: r = -0.29 (weak, not predictive)

### arcface_mobilefacenet Thresholds (Validated 2026-02-08)

| Parameter | Threshold | Tested Range | Notes |
|-----------|-----------|--------------|-------|
| **pre_norm** | **≥ 6.0** | 3.0 - 7.1 | Gradual transition (not cliff) |
| **similarity** | **≥ 0.30** | 0.07 - 0.60 | Same threshold as r50 |

**Behavior (gradual, not cliff):**
- pre_norm < 5.0: 0-10% match rate (unreliable)
- pre_norm 5.0-6.0: 3-39% match rate (transitional)
- pre_norm ≥ 6.0: **73-84%** match rate (reliable)
- pre_norm ≥ 7.0: 100% match rate (very reliable)

**Practical threshold: pre_norm ≥ 6.0** for reliable operation.

**Key difference from arcface_r50:** mobilefacenet degrades gradually instead of cliff behavior.

### Configuration to Switch Models

```bash
# InsightFace (CPU, full precision) - RECOMMENDED
INFERENCE_BACKEND=insightface

# Hailo ResNet-50 (if CPU offloading needed)
INFERENCE_BACKEND=hailo
HAILO_REC_HEF=/etc/hailo/models/arcface_r50.hef

# Hailo MobileFaceNet (avoid - unreliable)
INFERENCE_BACKEND=hailo
```

---

## Final Resolution (2026-02-07)

### Completed Tests

1. ✅ **InsightFace + NiceDaddy at distance** — Works at medium distance where Hailo fails
2. ✅ **Hailo arcface_r50** — 97% match rate, better than mobilefacenet but still lower sim than InsightFace
3. ✅ **INT16 quantization research** — No pre-compiled HEF available, manual compilation required

### Root Cause

**INT8 quantization limits embedding precision.** Hailo NPU uses 256 discrete levels (INT8) vs InsightFace's 4+ billion (Float32), resulting in:
- ~0.20 lower similarity scores
- Higher variance (±0.04 vs ±0.02)
- State degradation over runtime (requires Pi reboot)
- **Hard pre_norm threshold of 10** for arcface_r50 (0% match below, 100% above)

### Similarity Threshold Selection

#### InsightFace: Easy to Set Threshold

InsightFace produces **stable similarity** regardless of distance:

| Distance | pre_norm | Similarity | Behavior |
|----------|----------|------------|----------|
| Far | 15-18 | 0.28-0.35 | Clear reject |
| Medium | 19-21 | 0.32-0.48 | Near threshold |
| Close | 22-24 | 0.48-0.59 | Clear accept |

- Variance: ±0.02 (predictable)
- Threshold 0.45 sits in a safe zone with margin on both sides
- Degrades **gracefully** with distance

#### Hailo arcface_r50: Cliff Behavior

Hailo has a **bimodal distribution** with almost no margin:

| pre_norm | Similarity | Behavior |
|----------|------------|----------|
| < 10 | 0.02-0.28 | Garbage (never matches) |
| ≥ 10 | 0.30-0.59 | Valid (matches) |

- Gap between garbage (0.28) and valid (0.30) is only **0.02**
- Threshold 0.30 must be set exactly at the cliff edge
- One wrong condition flips between 0% and 100% match rate

#### Threshold Selection Limitations

Current thresholds are based on limited testing (2 identities):
- **InsightFace 0.45**: Industry default, validated by library authors
- **Hailo 0.30**: Ad-hoc, lowered because INT8 produces lower similarities

Proper validation requires:
1. Impostor testing (different people compared to registered members)
2. False Accept Rate (FAR) measurement
3. Genuine vs impostor distribution analysis

---

### pre_norm Threshold Discovery (2026-02-07)

Free walking test with NiceDaddy revealed a **hard pre_norm threshold** for arcface_r50:

| pre_norm | Faces | Match Rate | Similarity |
|----------|-------|------------|------------|
| **≤ 10** | 15 | **0%** | 0.02 - 0.28 |
| 10-12 | 7 | 100% | 0.31 - 0.57 |
| 12-14 | 21 | 100% | 0.30 - 0.58 |
| 14-16 | 27 | 100% | 0.33 - 0.48 |
| 16-21 | 38 | 100% | 0.40 - 0.59 |

**Key insight:** pre_norm = 10 is the minimum threshold for arcface_r50.
- pre_norm correlates with face size in pixels (closer = larger = higher pre_norm)
- Below 10: INT8 quantization produces insufficient embedding precision
- Above 10: 100% match rate regardless of exact pre_norm value

**Implication:** Camera placement and lens selection must ensure face size produces pre_norm ≥ 10.

### pre_norm vs Similarity Correlation (2026-02-07 18:37)

CuteBaby static test (87 frames, 10 seconds):

| Metric | Value |
|--------|-------|
| pre_norm range | 15.09 - 16.83 |
| sim range | 0.30 - 0.39 |
| Correlation (r) | **-0.29** (weak) |
| Match rate | 100% |

**Finding:** Once above the pre_norm threshold (10), higher pre_norm does NOT predict higher similarity.
- Similarity varies ±0.04 even with constant pre_norm
- Other factors dominate: face angle, lighting micro-variations, quantization noise
- pre_norm is a **gate** (pass/fail), not a **predictor** of match quality

### Final Recommendation

**Use arcface_r50 for close-range deployments:**
- Best accuracy at close range (sim 0.42-0.67, 100% match)
- Outperforms InsightFace at close range (sim 0.67 vs 0.59)
- Offloads to NPU, freeing CPU
- Requires pre_norm ≥ 10 (sufficient face size)

**Use InsightFace for variable distance deployments:**
- Consistent performance at all distances (63% match)
- Works at medium distance where Hailo fails
- Fastest overall (~100ms vs ~135ms)
- More tolerant of smaller faces

**Avoid mobilefacenet:**
- Inferior to both alternatives
- Lower similarity and match rate

---

## The Quantization Paradox (2026-02-09)

### "Weaker" Models Beat "Stronger" Models

A counterintuitive finding: InsightFace with objectively weaker models outperforms Hailo with stronger models at medium/far distance.

#### Model Specifications (On Paper)

| Component | InsightFace buffalo_sc | Hailo |
|-----------|----------------------|-------|
| Detection | RetinaFace-**500M** | SCRFD_**10G** (20× more compute) |
| Recognition | MobileFaceNet (**2M** params) | arcface_r50 (**31M** params, 15× larger) |
| Recognition accuracy | 71.87% MR-ALL | ~90% MR-ALL |
| **Expected winner** | - | **Hailo by far** |

#### Actual Test Results

| Metric | InsightFace buffalo_sc | Hailo SCRFD_10G + r50 | Winner |
|--------|----------------------|-----------------|--------|
| Medium distance match | **63%** | 7.9% | **InsightFace** |
| Similarity consistency | **±0.02** | ±0.04 | **InsightFace** |
| Distance tolerance | **Graceful** | Hard cliff | **InsightFace** |
| Close-range match | 63% | **100%** | Hailo |
| Close-range similarity | 0.48-0.59 | **0.42-0.67** | Hailo |

#### Why This Happens

| Factor | InsightFace | Hailo | Ratio |
|--------|-------------|-------|-------|
| Model capacity | 2M params | 31M params | **15× advantage Hailo** |
| Numerical precision | Float32 (4B levels) | INT8 (256 levels) | **16M× advantage InsightFace** |
| **Net effect** | Precision wins | Model capacity irrelevant | - |

The **16 million times** more precision levels completely overwhelms the **15×** model capacity advantage.

#### Signal vs Noise at Different Distances

```
Close (strong signal):
  Both backends: Signal >> Quantization noise → Good embeddings
  Winner: Hailo (model capacity matters when signal is strong)

Medium (moderate signal):
  InsightFace: Signal preserved by Float32 → Good embeddings
  Hailo: Signal ≈ INT8 noise → Degraded embeddings
  Winner: InsightFace (precision matters)

Far (weak signal):
  InsightFace: Signal partially preserved → Usable embeddings
  Hailo: Signal < INT8 noise → Garbage embeddings
  Winner: InsightFace
```

#### Implications for Upgrades

| Upgrade Path | Expected Impact |
|--------------|-----------------|
| Better ONNX model (w600k_r50) | ❌ Won't help much - still INT8 quantized |
| Larger detection (SCRFD_10G→34G) | ❌ Won't help - detection not the bottleneck |
| **INT16 quantization** | ✅ **Should help significantly** (256× more levels) |
| **Stay with InsightFace** | ✅ Already proven to work |

#### Conclusion

**INT8 quantization is so destructive that it negates:**
- 20× more detection compute (SCRFD_10G vs RetinaFace_500M)
- 15× more recognition parameters (31M vs 2M)
- +18% higher model accuracy (90% vs 72% MR-ALL)

Model upgrades alone will not fix the distance tolerance problem. The solution is either:
1. **INT16 quantization** - reduce quantization noise
2. **InsightFace (Float32)** - eliminate quantization entirely

See `int8_quantization_tradeoffs.md` for detailed analysis and INT16 compilation steps.
