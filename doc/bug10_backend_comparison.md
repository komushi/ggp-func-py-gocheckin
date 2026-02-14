# Backend Comparison Test Results

**Date:** 2026-02-07 to 2026-02-08
**Related:** `HAILO_SUMMARY.md`, `int8_quantization_tradeoffs.md`

---

## Test Conditions

- **Subjects:** CuteBaby (printed photo), NiceDaddy (live person)
- **Lighting:** Daylight + lamp (consistent)
- **Frames per session:** ~100
- **Camera:** 4mm lens, distances 0.5m to 6m

---

## Performance Comparison

| Detection | Recognition | Hardware | Duration/Frame |
|-----------|-------------|----------|----------------|
| SCRFD_10G | mobilefacenet | NPU INT8 | ~85-103ms |
| SCRFD_10G | arcface_r50 | NPU INT8 | ~130-145ms |
| RetinaFace-500M | MobileFaceNet | CPU Float32 | ~87-110ms |

---

## Results by Distance

### All Backends Comparison

| Distance | mobilefacenet INT8 | arcface_r50 INT8 | InsightFace Float32 |
|----------|-------------------|------------------|---------------------|
| Far (>3m) | 0.07-0.15, 0% | 0.07-0.24, 0% | 0.08-0.28, fail |
| Medium (1-3m) | 0.15-0.28, ~30% | 0.05-0.29, 7.9% | **0.32-0.48, 63%** |
| Close (<1m) | 0.28-0.32, ~30% | **0.42-0.67, 100%** | 0.48-0.59, 63% |

**Key findings:**
- arcface_r50 best at close range (sim 0.67, 100% match)
- InsightFace best at medium distance (63% match)
- mobilefacenet more distance-tolerant than r50 (73% vs 7.9% at mid-range)

---

## pre_norm Thresholds

### arcface_r50 (Hard cliff behavior)

| pre_norm | Match Rate | Similarity |
|----------|------------|------------|
| **< 10** | **0%** | 0.02-0.28 |
| 10-12 | 100% | 0.31-0.57 |
| 12-21 | 100% | 0.30-0.59 |

**Threshold: pre_norm >= 10** (hard gate, 0% below, 100% above)

### arcface_mobilefacenet (Gradual degradation)

| pre_norm | Match Rate |
|----------|------------|
| < 5.0 | 0-10% |
| 5.0-5.5 | 3-19% |
| 5.5-6.0 | 29-39% |
| **>= 6.0** | **73-84%** |
| >= 7.0 | 100% |

**Threshold: pre_norm >= 6.0** (gradual, more tolerant)

---

## Embedding Statistics

| Metric | mobilefacenet INT8 | arcface_r50 INT8 | InsightFace |
|--------|-------------------|------------------|-------------|
| pre_norm (registration) | ~6.3 | ~13.5 | ~24 |
| pre_norm (live) | 3.5-7.1 | 10-21 | 23.6-24.2 |
| Embedding std | 0.044 | 0.50-0.60 | 1.05 |
| Variance | +/-0.04 | +/-0.04 | +/-0.02 |

---

## Model Recommendations

| Use Case | Recommendation | Why |
|----------|----------------|-----|
| Close-range (<1m) | Hailo arcface_r50 | Best sim (0.67), 100% match |
| Variable distance | InsightFace buffalo_sc | 63% match at all ranges |
| Mid-range + NPU needed | Hailo mobilefacenet | 73% mid-range vs 7.9% for r50 |
| Speed priority | Hailo mobilefacenet | ~85ms vs ~130ms |

---

## The Quantization Paradox

InsightFace with **weaker models** outperforms Hailo with **stronger models** at medium/far distance:

| Metric | InsightFace | Hailo |
|--------|-------------|-------|
| Detection model | RetinaFace-500M | SCRFD-10G (20x more compute) |
| Recognition model | MobileFaceNet 2M | arcface_r50 31M (15x larger) |
| **Actual medium-range match** | **63%** | **7.9%** |

**Reason:** Float32 (4 billion precision levels) vs INT8 (256 levels). The 16 million times more precision overwhelms the model capacity advantage.

---

## Configuration

### InsightFace (Recommended for variable distance)
```bash
INFERENCE_BACKEND=insightface
FACE_THRESHOLD_INSIGHTFACE=0.40
```

### Hailo arcface_r50 (Close-range)
```bash
INFERENCE_BACKEND=hailo
HAILO_REC_HEF=/etc/hailo/models/arcface_r50.hef
HAILO_PRE_NORM_THRESHOLD_R50=9.0
FACE_THRESHOLD_HAILO=0.30
```

### Hailo mobilefacenet (Mid-range, faster)
```bash
INFERENCE_BACKEND=hailo
HAILO_REC_HEF=/etc/hailo/models/arcface_mobilefacenet.hef
HAILO_PRE_NORM_THRESHOLD_MOBILEFACENET=5.0
FACE_THRESHOLD_HAILO=0.30
```
