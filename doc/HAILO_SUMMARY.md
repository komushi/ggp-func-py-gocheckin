# Hailo Face Recognition Project Summary

**Purpose:** Executive summary for Claude sessions working on this project
**Last Updated:** 2026-02-15

---

## Project Goal

Deploy face recognition on Raspberry Pi using Hailo-8 NPU to offload CPU for multi-camera video processing.

---

## Current Status

| Backend | Status | Use Case |
|---------|--------|----------|
| InsightFace (Float32, CPU) | **Production ready** | Variable distance, single camera |
| Hailo INT8 arcface_r50 | **Production ready** | Close range (<1m), multi-camera |
| Hailo INT16 (selective) | **Testing** | Attempting to improve distance tolerance |

---

## Key Findings

### 1. INT8 Quantization Limits Distance Tolerance

| Distance | InsightFace | Hailo INT8 |
|----------|-------------|------------|
| Close (<1m) | 63% match | **100% match** |
| Medium (1-3m) | **63% match** | 7.9% match |
| Far (>3m) | Graceful degradation | 0% match |

**Root cause:** INT8 (256 levels) vs Float32 (4 billion levels). Weak signals from distant faces are lost in quantization noise.

### 2. pre_norm Threshold Behavior

| Model | Threshold | Behavior |
|-------|-----------|----------|
| arcface_r50 | pre_norm >= 10 | **Hard cliff** (0% below, 100% above) |
| arcface_mobilefacenet | pre_norm >= 6.0 | Gradual (0% -> 39% -> 73% -> 100%) |

pre_norm = embedding magnitude before L2 normalization. Correlates with face size/quality.

### 3. Model Comparison

| Model | Close Range | Medium Range | Speed | Recommendation |
|-------|-------------|--------------|-------|----------------|
| arcface_r50 (INT8) | Best (sim 0.67) | Poor (7.9%) | ~130ms | Close-range deployments |
| mobilefacenet (INT8) | Good | Better (73%) | ~85ms | Mid-range, speed priority |
| InsightFace buffalo_sc | Good (sim 0.59) | Best (63%) | ~100ms | Variable distance |

### 4. Critical Fixes Applied

| Fix | Impact |
|-----|--------|
| BGR -> RGB conversion | Hailo HEF expects RGB, OpenCV gives BGR |
| FormatType.FLOAT32 output | HailoRT auto-dequantization is more accurate than manual |

---

## INT16 Experiment Status

### Round 1: FAILED
- Applied INT16 to ALL layers (`quantization_param(*)`)
- Result: Degenerate embeddings (constant pre_norm ~2, all faces match at 0.99)

### Round 2: IN PROGRESS (2026-02-11)

**Fixes applied:**
1. More calibration data: 500 -> 7643 images
2. Selective INT16: Only final embedding layers (fc1, conv33, etc.)
3. Normalization: std=127.5 -> std=128.0 (official Hailo value)
4. Target hardware: hailo8l -> hailo8

**Models compiled:**
- arcface_mobilefacenet_int16_v2 (3 calibration sizes: 1023, 2048, 7643)
- arcface_r50_int16_v2 (3 calibration sizes)
- w600k_mbf_int16_v2 (3 calibration sizes)
- w600k_r50_int16_v2 (3 calibration sizes)

See `HAILO_MODEL_PROGRESS.md` for detailed status.

---

## Environment

### Compilation Machine
- **Host:** rtx4900m (RTX 4090)
- **Docker:** hailo_ai_sw_suite_2025-10
- **DFC Version:** 3.33.0
- **Shared folder:** `~/hailo/shared_with_docker/`

### Deployment Target
- **Device:** pi_neoseed (Raspberry Pi with Hailo-8)
- **Models path:** `/etc/hailo/models/`
- **Config:** `HAILO_REC_HEF` env var in function.conf

---

## File Structure

### Documentation
| File | Content |
|------|---------|
| `HAILO_SUMMARY.md` | This summary |
| `HAILO_MODEL_PROGRESS.md` | Model conversion tracking |
| `int8_quantization_tradeoffs.md` | Technical deep-dive on quantization |
| `bug_hailo_recognition_failure.md` | Original bug investigation |
| `bug10_backend_comparison.md` | Test results and benchmarks |

### Code
| File | Purpose |
|------|---------|
| `face_recognition_hailo.py` | HailoFaceApp class, detection + recognition |
| `face_recognition.py` | InsightFace backend |
| `py_handler.py` | Main orchestrator, backend selection |

### Compilation Assets (on rtx4900m)
```
~/hailo/shared_with_docker/
├── recognition/           # ONNX models
│   ├── hailo_models/     # arcface_r50, arcface_mobilefacenet
│   └── insightface/      # w600k_r50, w600k_mbf
├── alls/                  # Quantization config files (*_int16_v2.alls)
├── calibration/           # 500 JPEG images (112x112)
├── /tmp/calib_all/        # 7643 NPY images (inside docker)
└── hef/                   # Compiled HEF output
```

---

## Key Commands

### Compile HEF (on rtx4900m)
```bash
docker exec hailo_sw_suite bash -c "
  hailo parser onnx /local/shared_with_docker/recognition/hailo_models/arcface_mobilefacenet.onnx \
    --hw-arch hailo8 && \
  hailo optimize arcface_mobilefacenet.har \
    --calib-set-path /tmp/calib_all \
    --model-script /local/shared_with_docker/alls/arcface_mobilefacenet_int16_v2.alls && \
  hailo compiler arcface_mobilefacenet.har \
    --hw-arch hailo8
"
```

### Deploy to Pi
```bash
scp ~/hailo/shared_with_docker/hef/arcface_mobilefacenet_int16_v2_calib1023.hef \
    pi_neoseed:/etc/hailo/models/
```

### Test on Pi
```bash
python3 face_detector_hailo_a.py h265 "rtsp://user:pass@camera/stream"
```

---

## What Another Claude Session Needs to Know

1. **The problem:** INT8 quantization causes hard cliff behavior at medium/far distance
2. **Current approach:** Selective INT16 on final layers with more calibration data
3. **Models compiled:** 12 HEF files (4 models x 3 calibration sizes)
4. **Next step:** Test the v2 HEF files on pi_neoseed and measure pre_norm/similarity
5. **Success criteria:** pre_norm varies with distance (not constant), similarity < 0.30 for different people

---

## Related Documents

- `int8_quantization_tradeoffs.md` - Why INT8 has distance issues, INT16 compilation details
- `bug_hailo_recognition_failure.md` - Original bug timeline, FormatType.FLOAT32 fix
- `bug10_backend_comparison.md` - Detailed test results with tables
- `HAILO_MODEL_PROGRESS.md` - Model conversion tracking spreadsheet
