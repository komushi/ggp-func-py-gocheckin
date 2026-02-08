# Bug #10: Backend Comparison Test Results

**Parent:** `bug_hailo_recognition_failure.md`
**Date:** 2026-02-07

---

## Test Conditions

- **Subject:** CuteBaby (printed photo), NiceDaddy (live person)
- **Lighting:** Daylight + lamp (consistent)
- **Frames per session:** 101
- **Registration:** Digital photo via `/recognise` endpoint

---

## Performance Comparison

| Backend | Model | Hardware | Duration/Frame | Notes |
|---------|-------|----------|----------------|-------|
| Hailo | mobilefacenet | NPU | ~85-103ms | Fastest |
| Hailo | arcface_r50 | NPU | ~130-145ms | +40ms vs mobilefacenet |
| InsightFace | buffalo_sc | CPU | ~87-110ms | Comparable to Hailo |

**Note:** Duration includes detection (SCRFD) + recognition (ArcFace). InsightFace on CPU is surprisingly competitive with Hailo NPU.

---

## Results Summary

### CuteBaby (Same Position, Same Lighting)

| Backend | Similarity | Match Rate | Variance | Threshold |
|---------|------------|------------|----------|-----------|
| Hailo MobileFaceNet (degraded) | 0.27-0.35 | 64% | ±0.04 | 0.30 |
| Hailo MobileFaceNet (fresh) | 0.31-0.36 | 98% | ±0.03 | 0.30 |
| **InsightFace** | **0.49-0.54** | **100%** | **±0.02** | 0.45 |

### NiceDaddy (Distance Test) - Hailo MobileFaceNet

| Distance | Similarity | Result |
|----------|------------|--------|
| Far | 0.07-0.15 | No match |
| Medium | 0.15-0.28 | No match |
| Close | 0.28-0.32 | MATCH (barely) |

### NiceDaddy (Distance Test) - InsightFace

| Distance | Similarity | pre_norm | Result |
|----------|------------|----------|--------|
| Far | 0.08-0.28 | 15-18 | No match |
| Medium | 0.32-0.48 | 19-21 | **MATCH** |
| Close | 0.48-0.59 | 22-24 | **MATCH** |

**Session:** 100 frames, 97 faces, 63 matched (63%)

### NiceDaddy Distance Comparison (All Backends)

| Distance | mobilefacenet | arcface_r50 | InsightFace | Winner |
|----------|---------------|-------------|-------------|--------|
| Far | 0.07-0.15 (fail) | 0.07-0.24 (fail) | 0.08-0.28 (fail) | All fail |
| Medium | 0.15-0.28 (fail) | 0.05-0.29 (fail) | **0.32-0.48** | **InsightFace** |
| Close | 0.28-0.32 | **0.42-0.67** | 0.48-0.59 | **arcface_r50** |
| Close match rate | ~30% | **100%** | 63% | **arcface_r50** |
| Overall match rate | ~30% | 21-79% | **63%** | **InsightFace** |

---

## Embedding Statistics

| Metric | Hailo MobileFaceNet | Hailo arcface_r50 | InsightFace |
|--------|---------------------|-------------------|-------------|
| pre_norm (registration) | ~6.3 | ~13.5 | ~24 |
| pre_norm (live) | 3.5-7.1 | 10-21 | 23.6-24.2 |
| pre_norm threshold | **≥ 6.0** (gradual) | **≥ 10** (cliff) | N/A |
| Threshold behavior | 0%→39%→73%→100% | 0%→100% | Graceful |
| Quality gap | ~15% | ~5% | ~2% |
| Embedding std | 0.044 | 0.50-0.60 | 1.05 |

---

## Key Findings

1. **arcface_r50 outperforms InsightFace at close range** - sim 0.67 vs 0.59, 100% match rate
2. **InsightFace wins at medium distance** - 63% match vs 7.9% for arcface_r50
3. **InsightFace is more stable across distances** (±0.02 variance)
4. **Both Hailo models require close-range** for reliable matching
5. **Hailo degrades over runtime** - Pi reboot restores performance
6. **pre_norm correlates with distance/quality** - All backends show lower pre_norm at far distance
7. **arcface_r50 is better than mobilefacenet** - higher similarity and match rate at close range
8. **Distance is critical for Hailo** - arcface_r50 goes from 100% (close) to 7.9% (medium)
9. **pre_norm = 10 is the hard threshold for arcface_r50** - 0% match below, 100% match above
10. **mobilefacenet has gradual degradation** - pre_norm 5.5 threshold with 0%→39%→73%→100% transition
11. **mobilefacenet is more distance-tolerant than r50** - 73% match at mid-range vs 7.9% for r50
12. **pre_norm scale is model-dependent** - mobilefacenet uses 3-7, r50 uses 10-21 (different architectures)

---

## Models Tested

| Model | Backend | Params | Quantization | Status |
|-------|---------|--------|--------------|--------|
| arcface_mobilefacenet | Hailo | 2M | INT8 | Tested (problematic) |
| arcface_r50 | Hailo | 31M | INT8 | **Tested (good)** |
| buffalo_sc | InsightFace | 31M | Float32 | Tested (works well) |

---

## Model Thresholds Summary

### arcface_r50 (Validated 2026-02-07)

| Parameter | Threshold | Valid Range | Behavior |
|-----------|-----------|-------------|----------|
| **pre_norm** | **≥ 10** | 10 - 21 | Hard gate: 0% match below, 100% above |
| **similarity** | **≥ 0.30** | 0.30 - 0.67 | Tight margin (garbage ends at 0.28) |

- pre_norm is a **gate**, not a predictor (r = -0.29, weak correlation)
- Similarity varies ±0.04 due to angle/lighting/quantization noise

### arcface_mobilefacenet (Validated 2026-02-08)

| Parameter | Threshold | Valid Range | Behavior |
|-----------|-----------|-------------|----------|
| **pre_norm** | **≥ 6.0** | 3.0 - 7.1 | Gradual transition |
| **similarity** | **≥ 0.30** | 0.07 - 0.60 | Same threshold as r50 |

**pre_norm vs Match Rate (combined NiceDaddy + CuteBaby tests):**
| pre_norm | Match Rate | Reliability |
|----------|------------|-------------|
| < 5.0 | 0-10% | Unreliable |
| 5.0-5.5 | 3-19% | Unreliable |
| 5.5-6.0 | 29-39% | Transitional |
| **≥ 6.0** | **73-84%** | **Reliable** |
| ≥ 7.0 | 100% | Very reliable |

**Practical threshold: pre_norm ≥ 6.0** for reliable matching (73%+ match rate).

**Key difference:** Gradual degradation vs r50's cliff behavior.

### InsightFace buffalo_sc

| Parameter | Threshold | Valid Range | Behavior |
|-----------|-----------|-------------|----------|
| **pre_norm** | More tolerant | 15 - 24 | Graceful degradation |
| **similarity** | **≥ 0.45** | 0.28 - 0.60 | Wide margin, stable |

---

## Hailo arcface_r50 Test Results (2026-02-07)

### CuteBaby (Same Position as Other Tests)

| Metric | arcface_mobilefacenet | **arcface_r50** | InsightFace |
|--------|----------------------|-----------------|-------------|
| Similarity | 0.27-0.36 | **0.30-0.43** | 0.49-0.54 |
| Match rate | 64-98% | **97%** (98/101) | 100% |
| pre_norm | 5.0-6.3 | 11-13.5 | 23-24 |
| ArcFace std | 0.22-0.24 | 0.50-0.60 | 1.05 |
| Headroom | 0.00-0.06 | **0.00-0.13** | 0.04-0.09 |

### NiceDaddy Distance Test - arcface_r50

| Distance | pre_norm | Similarity | Match Rate | Result |
|----------|----------|------------|------------|--------|
| Far | 6-12 | 0.07-0.24 | 0% | No match |
| Medium | 11-16 | 0.05-0.29 | 7.9% | Mostly fail |
| **Close** | **16-21** | **0.42-0.67** | **100%** | **MATCH** |

**Close-range session:** 73 faces detected, 58 matched (79%), best sim **0.67**

### NiceDaddy Free Walking Test - arcface_r50 (2026-02-07 17:30)

Subject walked freely at varying distances from camera.

| pre_norm | Faces | Match Rate | Similarity Range |
|----------|-------|------------|------------------|
| **≤ 10** | 15 | **0%** | 0.02 - 0.28 |
| 10-12 | 7 | 100% | 0.31 - 0.57 |
| 12-14 | 21 | 100% | 0.30 - 0.58 |
| 14-16 | 27 | 100% | 0.33 - 0.48 |
| 16-18 | 17 | 100% | 0.42 - 0.56 |
| 18-21 | 21 | 100% | 0.40 - 0.59 |

**Total:** 93 NiceDaddy faces, 100% match rate (for pre_norm > 10)

**Key Finding:** pre_norm = 10 is the **hard threshold** for arcface_r50:
- Below 10: similarity caps at ~0.28 (below 0.30 threshold)
- Above 10: similarity jumps to 0.30+ (reliable match)

### NiceDaddy Free Walking Test - arcface_mobilefacenet (2026-02-08 08:57)

Subject walked freely at varying distances from camera.

| pre_norm | Faces | Match Rate | Similarity Range |
|----------|-------|------------|------------------|
| 3.0-4.0 | 28 | **0%** | 0.07 - 0.24 |
| 4.0-4.5 | 17 | **0%** | 0.11 - 0.23 |
| 4.5-5.0 | 26 | **0%** | 0.09 - 0.30 |
| 5.0-5.5 | 36 | 3% | 0.08 - 0.32 |
| 5.5-6.0 | 67 | 39% | 0.07 - 0.54 |
| 6.0-7.0 | 60 | 73% | 0.13 - 0.57 |
| ≥ 7.0 | 2 | **100%** | 0.55 - 0.60 |

**Total:** 236 NiceDaddy faces, 73 matched (31%)

**Key Finding:** mobilefacenet has a **gradual transition** instead of a hard cliff:
- Below 5.0: 0% match (garbage embeddings)
- 5.0-6.0: gradual improvement (3% → 39%)
- 6.0-7.0: good performance (73%)
- Above 7.0: reliable (100%)

---

## Why pre_norm Scales Differ Between Models

The pre_norm (embedding magnitude before L2 normalization) differs significantly between models:

| Model | Typical pre_norm | Architecture |
|-------|------------------|--------------|
| mobilefacenet | 3 - 7 | MobileNet (2M params) |
| arcface_r50 | 10 - 21 | ResNet-50 (31M params) |
| InsightFace | 15 - 24 | ResNet (Float32) |

**Why?** Different architectures produce different weight scales in their final FC layer:
- Deeper networks (ResNet-50) accumulate larger activations
- This is like Celsius vs Fahrenheit - same measurement, different scale

**Does it matter?** No, for similarity. After L2 normalization, both become unit vectors. The scale cancels out in cosine similarity.

**What matters:** Each model has its own **minimum viable pre_norm** where embeddings become too sparse for reliable matching.

---

## Conclusion

### Rankings by Use Case

**Close-range (< 1m):**
1. **arcface_r50** - Best (sim 0.67, 100% match)
2. **InsightFace** - Good (sim 0.59, 63% match)
3. **mobilefacenet** - Fair (sim 0.32, ~30% match)

**Medium/Far distance:**
1. **InsightFace** - Best (sim 0.32-0.48, 63% match)
2. **mobilefacenet** - Poor (sim 0.15-0.28, ~30% match)
3. **arcface_r50** - Poor (sim 0.05-0.29, 7.9% match)

### Trade-offs

| Backend | Close Sim | Close Match | Medium Match | Duration | Hardware |
|---------|-----------|-------------|--------------|----------|----------|
| arcface_r50 | **0.42-0.67** | **100%** | 7.9% | ~130-145ms | NPU |
| InsightFace | 0.48-0.59 | 63% | **63%** | ~90-110ms | CPU |
| mobilefacenet | 0.28-0.32 | ~30% | ~30% | ~85-103ms | NPU |

**Surprises:**
1. arcface_r50 OUTPERFORMS InsightFace at close range (sim 0.67 vs 0.59)
2. InsightFace (CPU) is faster than arcface_r50 (NPU)
3. arcface_r50 is extremely distance-sensitive (100% → 7.9%)

### Recommendation

- **Close-range deployment (< 1m):** Use **arcface_r50** - best accuracy (sim 0.67, 100% match)
- **Variable distance:** Use **InsightFace** - consistent performance at all distances (63% match)
- **Mid-range with NPU preference:** Consider **mobilefacenet** - 73% match at mid-range vs 7.9% for r50
- **Speed priority:** Use **mobilefacenet** - fastest at ~85ms vs ~130ms for r50

### Updated Understanding (2026-02-08)

mobilefacenet was previously labeled "inferior" but new testing reveals it is **more distance-tolerant** than arcface_r50:

| Distance | mobilefacenet | arcface_r50 |
|----------|---------------|-------------|
| Close | ~100% | 100% |
| Mid | **73%** | 7.9% |
| Far | 39% | 0% |

The smaller model (2M params) handles distance degradation **better** than the larger model (31M params), likely because its lower pre_norm scale (3-7) is less sensitive to face size reduction.

---

## INT16 Quantization Research (2026-02-07)

### Question: Can Hailo match InsightFace quality?

**Short answer:** No pre-compiled INT16 HEF files exist. Manual compilation required.

### Hailo Model Zoo Status

| Model | INT8 HEF | INT16 HEF |
|-------|----------|-----------|
| arcface_mobilefacenet | ✅ Available | ❌ Not available |
| arcface_r50 | ✅ Available | ❌ Not available |

### INT16 Compilation Option

INT16 quantization **is supported** by Hailo hardware but requires manual compilation:

```python
# In .alls config file during optimization
quantization_param(precision_mode=a16_w16)
```

**Requirements:**
- Hailo Dataflow Compiler (DFC)
- ONNX model source
- Calibration dataset
- Significant compilation effort

### INT16 vs INT8 Trade-offs

| Precision | Embedding Quality | Throughput | Availability |
|-----------|-------------------|------------|--------------|
| INT8 | ~0.30-0.43 sim | Fastest | Pre-compiled |
| INT16 | Better (estimated) | ~50% slower | Manual compile |
| Float32 | ~0.49-0.54 sim | CPU-bound | InsightFace ready |

### Conclusion

**InsightFace advantages:**
1. Best accuracy (~0.20 higher similarity)
2. Faster single-model inference (~90ms vs ~130ms for r50)
3. Graceful distance degradation
4. No state degradation issues

**Hailo advantages:**
1. **Memory efficient** - HEF models are small (~2-31MB), InsightFace loads large models into RAM
2. **Mixed workloads** - Can load multiple HEF models (detection, recognition, object detection, etc.) on single NPU
3. **CPU offloading** - Frees CPU for GStreamer, HTTP server, other processing
4. **Scalability** - Better for multi-camera setups where CPU becomes bottleneck

**Recommendation:**

| Use Case | Recommendation |
|----------|----------------|
| Single camera, simple setup | InsightFace (best accuracy) |
| Multi-camera, mixed AI workloads | **Hailo** (memory + CPU efficiency) |
| Close-range deployment | Hailo arcface_r50 (best close-range accuracy) |
| Variable distance, NPU preferred | Hailo mobilefacenet (73% mid-range vs 7.9% for r50) |
