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

### NiceDaddy Distance Comparison

| Distance | Hailo sim | InsightFace sim | Winner |
|----------|-----------|-----------------|--------|
| Far | 0.07-0.15 | 0.08-0.28 | Similar (both fail) |
| Medium | 0.15-0.28 (fail) | **0.32-0.48** | **InsightFace** |
| Close | 0.28-0.32 | **0.48-0.59** | **InsightFace** |

---

## Embedding Statistics

| Metric | Hailo MobileFaceNet | InsightFace |
|--------|---------------------|-------------|
| pre_norm (registration) | ~6.3 | ~24 |
| pre_norm (live) | 5.0-5.6 | 23.6-24.2 |
| Quality gap | ~15% | ~2% |
| Embedding std | 0.044 | 1.05 |

---

## Key Findings

1. **InsightFace similarity is ~0.20 higher** than Hailo for same subject
2. **InsightFace has 100% match rate** vs Hailo's 64-98% (CuteBaby static test)
3. **InsightFace is more stable** (±0.02 vs ±0.04 variance)
4. **Hailo requires close-range** for reliable matching
5. **Hailo degrades over runtime** - Pi reboot restores performance
6. **pre_norm correlates with distance/quality** - Both backends show lower pre_norm at far distance
7. **InsightFace works at medium distance** where Hailo fails (sim 0.32-0.48 vs 0.15-0.28)

---

## Models Tested

| Model | Backend | Params | Quantization | Status |
|-------|---------|--------|--------------|--------|
| arcface_mobilefacenet | Hailo | 2M | INT8 | Tested (problematic) |
| arcface_r50 | Hailo | 31M | INT8 | Downloaded, not tested |
| buffalo_sc | InsightFace | 31M | Float32 | Tested (works well) |

---

## Conclusion

InsightFace (CPU, float32) significantly outperforms Hailo MobileFaceNet (INT8) in:
- Absolute similarity scores (+0.20 higher)
- Match rate consistency (100% vs 64-98% for static subject)
- Frame-to-frame stability (±0.02 vs ±0.04)
- **Medium distance tolerance** (works where Hailo fails)

Both backends fail at far distance (low face resolution).

**Trade-off:** InsightFace uses CPU (~100ms/frame) vs Hailo NPU (~30ms/frame).

**Recommendation:** Use InsightFace for reliability, or test Hailo arcface_r50 for potential improvement.
