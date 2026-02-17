# Bug #10: Hailo Recognition Failure Investigation

**Status:** RESOLVED (2026-02-05)
**Related:** `HAILO_SUMMARY.md`, `bug10_backend_comparison.md`

---

## Summary

Hailo ArcFace recognition stops matching after lighting changes, even when lighting is restored. Root causes identified and fixed.

---

## Timeline (2026-02-04 to 2026-02-05)

1. **19:53:03** — Member matched with `sim: 0.3084` (borderline)
2. Light adjusted briefly, then restored
3. **19:53:53** — Same person, same position, 101 frames, **zero matches** (best sim: 0.27)
4. **20:59:37** — Worse behavior: ghost faces, negative similarity scores

---

## Root Causes Identified

### Issue 1: BGR vs RGB (Fixed 2026-02-05)

- **Problem:** OpenCV gives BGR, Hailo HEF expects RGB
- **Fix:** `cv2.cvtColor(img, cv2.COLOR_BGR2RGB)` before inference

### Issue 2: Manual Dequantization (Fixed 2026-02-05)

- **Problem:** Manual dequantization `(raw - zp) * scale` was less accurate than HailoRT's internal method
- **Impact:** SCRFD landmark accuracy affected face alignment, degrading ArcFace embeddings
- **Fix:** Use `FormatType.FLOAT32` on model outputs before `configure()`

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

---

## Resolution

After both fixes:
- Close range: 101/101 frames MATCH, sim 0.35-0.47
- Medium range: sim 0.30-0.38, consistent matches
- Far range: sim 0.20-0.29, borderline (expected for INT8)

---

## Remaining Limitation: Distance Tolerance

Even with fixes applied, Hailo INT8 has poor distance tolerance compared to InsightFace Float32. This is a fundamental limitation of INT8 quantization, not a bug.

See `int8_quantization_tradeoffs.md` for detailed analysis.

---

## Official Hailo Preprocessing Requirements

### SCRFD (Detection)
| Parameter | Value |
|-----------|-------|
| Input | 640x640x3, RGB |
| Normalization | Built into HEF (`normalize_in_net: true`) |
| Mean/Std | [127.5, 127.5, 127.5], [128.0, 128.0, 128.0] |

### ArcFace (Recognition)
| Parameter | Value |
|-----------|-------|
| Input | 112x112x3, RGB |
| Normalization | Built into HEF |
| Mean/Std | [127.5, 127.5, 127.5], [127.5, 127.5, 127.5] |
| Output | 512-dim embedding |

**User should feed raw uint8 RGB (0-255) directly. No external normalization needed.**

---

## Files Modified

| File | Change |
|------|--------|
| `face_recognition_hailo.py` | BGR->RGB conversion, FormatType.FLOAT32 |

---

## References

- [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo)
- [Hailo Community: Face Recognition Guide](https://community.hailo.ai/t/a-comprehensive-guide-to-building-a-face-recognition-system/8803)
