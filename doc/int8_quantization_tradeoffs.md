# INT8 Quantization Trade-offs

**Date:** 2026-02-09
**Related:** `HAILO_SUMMARY.md`, `bug10_backend_comparison.md`, `HAILO_MODEL_PROGRESS.md`

---

## Overview

This document explains why INT8 quantization limits distance tolerance in face recognition, and how to compile INT16 HEF files for Hailo.

---

## The Core Trade-off

| Backend | Precision | Levels | Distance Tolerance |
|---------|-----------|--------|-------------------|
| InsightFace | Float32 | ~4 billion | Excellent |
| Hailo | INT8 | 256 | Poor (cliff behavior) |
| Hailo | INT16 | 65,536 | Better (theoretical) |

---

## Why Distance Matters

### Face Size vs Signal Strength

| Distance | Face Size | Signal |
|----------|-----------|--------|
| Close (<1m) | ~200x200 px | Strong |
| Medium (1-3m) | ~80x80 px | Medium |
| Far (>3m) | ~40x40 px | Weak |

### What Happens to Weak Signals

```
Close face (strong signal):
  Both backends: Signal >> Quantization noise → Good embeddings
  Winner: Hailo (model capacity matters)

Far face (weak signal):
  Float32: Signal preserved → Good embeddings
  INT8: Signal ≈ Quantization noise → Garbage embeddings
  Winner: InsightFace
```

---

## The Quantization Cliff

### Hailo arcface_r50

| pre_norm | Match Rate |
|----------|------------|
| < 10 | **0%** |
| >= 10 | **100%** |

This is a hard cliff, not gradual degradation. Gap between garbage (sim 0.28) and valid (sim 0.30) is only 0.02.

### InsightFace

No cliff - graceful degradation from far to close.

---

## Error Accumulation

A face recognition network has ~50-100 layers. Each layer adds quantization error.

| Layers | Float32 Error | INT8 Error |
|--------|---------------|------------|
| 1 | ~10^-7 | ±1 level |
| 50 | ~10^-5 | ±7 levels |
| 100 | ~10^-4 | ±10 levels |

For strong signals, ±10 levels out of 200+ is acceptable.
For weak signals, ±10 levels out of 20 is **catastrophic**.

---

## Community Reports

| Issue | Frequency | Our Experience |
|-------|-----------|----------------|
| Data type mismatch (uint8 vs float32) | Common | Yes - FormatType.FLOAT32 fix |
| Quantization accuracy collapse | Reported | Yes - cliff behavior |
| Calibration data issues | Common | Tested 500/1023/2048/7643 images |
| Model-specific sensitivity | Reported | Yes - r50 vs mobilefacenet differ |

---

## INT16 Compilation Guide

### Environment Setup

| Requirement | Specification |
|-------------|---------------|
| OS | Ubuntu 22.04 LTS |
| GPU | NVIDIA with CUDA 11.8 (RTX 4090, 3090, etc.) |
| Docker | Hailo AI SW Suite 2025-10 |
| RAM | 32GB+ recommended |

**Note:** Blackwell GPUs (RTX 5000 series) require CUDA 12.x which is NOT compatible.

### Compilation Steps

#### 1. Parse ONNX to HAR
```bash
docker exec hailo_sw_suite hailo parser onnx \
  /local/shared_with_docker/recognition/hailo_models/arcface_mobilefacenet.onnx \
  --hw-arch hailo8
```

#### 2. Optimize with INT16 config
```bash
docker exec hailo_sw_suite hailo optimize arcface_mobilefacenet.har \
  --calib-set-path /tmp/calib_all \
  --model-script /local/shared_with_docker/alls/arcface_mobilefacenet_int16_v2.alls
```

#### 3. Compile to HEF
```bash
docker exec hailo_sw_suite hailo compiler arcface_mobilefacenet.har \
  --hw-arch hailo8
```

### INT16 .alls File Format

**Important:** Use HAR layer names, not ONNX layer names.

```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])

# Selective INT16 on final embedding layers only
quantization_param(arcface_mobilefacenet/fc1, precision_mode=a16_w16)
quantization_param(arcface_mobilefacenet/conv33, precision_mode=a16_w16)
quantization_param(arcface_mobilefacenet/dw16, precision_mode=a16_w16)
```

### Layer Names by Model

| Model | INT16 Layers |
|-------|--------------|
| arcface_mobilefacenet | fc1, conv33, dw16 |
| arcface_r50 | fc1, dw1, conv53 |
| w600k_mbf | fc1, conv33, dw16 |
| w600k_r50 | fc1, dw1, conv53 |

---

## INT16 Experiment History

### Round 1: FAILED

- Applied INT16 to ALL layers (`quantization_param(*)`)
- Result: Degenerate (constant pre_norm ~2, all faces match at 0.99)

### Round 2: IN PROGRESS

Fixes applied:
1. More calibration: 500 → 7643 images
2. Selective INT16: final layers only
3. Normalization: std=127.5 → std=128.0
4. Target: hailo8l → hailo8

See `HAILO_MODEL_PROGRESS.md` for current status.

---

## When to Use Each Backend

### Hailo INT8
- Close-range deployment (< 1m)
- Multi-camera setups (offload CPU)
- Resource-constrained (low RAM)

### InsightFace Float32
- Variable distance
- Single camera
- Accuracy priority

### Hailo INT16 (if successful)
- Better distance tolerance than INT8
- Still offloads to NPU
- Requires custom compilation

---

## References

- [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo)
- [INT8 Quantization Effects on Embeddings](https://arxiv.org/html/2501.10534v1)
- [Hailo Community: 16-bit quantization](https://community.hailo.ai/t/16-bit-quantization-on-final-layers/2292)
