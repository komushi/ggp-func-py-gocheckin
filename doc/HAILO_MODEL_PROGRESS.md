# Hailo Model Conversion Progress

**Last Updated:** 2026-02-15

---

## Model Inventory

### Recognition Models (HEF)

| Model | Quantization | Calibration | Compiled | Deployed | Tested | Result |
|-------|--------------|-------------|----------|----------|--------|--------|
| arcface_mobilefacenet | INT8 | Hailo default | - | pi_neoseed | Yes | Baseline, pre_norm>=6 works |
| arcface_r50 | INT8 | Hailo default | - | pi_neoseed | Yes | Baseline, pre_norm>=10 cliff |
| arcface_mobilefacenet_int16_v2 | INT16 selective | 1023 | Yes | pi_neoseed | **Pending** | |
| arcface_mobilefacenet_int16_v2 | INT16 selective | 2048 | Yes | No | Pending | |
| arcface_mobilefacenet_int16_v2 | INT16 selective | 7643 | Yes | No | Pending | |
| arcface_r50_int16_v2 | INT16 selective | 1023 | Yes | No | Pending | |
| arcface_r50_int16_v2 | INT16 selective | 2048 | Yes | No | Pending | |
| arcface_r50_int16_v2 | INT16 selective | 7643 | Yes | No | Pending | |
| w600k_mbf_int16_v2 | INT16 selective | 1023 | Yes | No | Pending | |
| w600k_mbf_int16_v2 | INT16 selective | 2048 | Yes | No | Pending | |
| w600k_mbf_int16_v2 | INT16 selective | 7643 | Yes | No | Pending | |
| w600k_r50_int16_v2 | INT16 selective | 1023 | Yes | No | Pending | |
| w600k_r50_int16_v2 | INT16 selective | 2048 | Yes | No | Pending | |
| w600k_r50_int16_v2 | INT16 selective | 7643 | Yes | No | Pending | |

### Detection Models (HEF)

| Model | Quantization | Status | Notes |
|-------|--------------|--------|-------|
| scrfd_10g | INT8 | Production | Works well, not the bottleneck |
| scrfd_2.5g | INT8 | Available | Faster, not tested |

---

## Compiled HEF Files (on rtx4900m)

Location: `~/hailo/shared_with_docker/hef/`

```
arcface_mobilefacenet_int16_v2_calib1023.hef   ~4.0MB
arcface_mobilefacenet_int16_v2_calib2048.hef   ~4.0MB
arcface_mobilefacenet_int16_v2_calib_all.hef   ~4.0MB
arcface_r50_int16_v2_calib1023.hef             ~35MB
arcface_r50_int16_v2_calib2048.hef             ~35MB
arcface_r50_int16_v2_calib_all.hef             ~35MB
w600k_mbf_int16_v2_calib1023.hef               ~9.1MB
w600k_mbf_int16_v2_calib2048.hef               ~9.1MB
w600k_mbf_int16_v2_calib_all.hef               ~9.1MB
w600k_r50_int16_v2_calib1023.hef               ~58MB
w600k_r50_int16_v2_calib2048.hef               ~58MB
w600k_r50_int16_v2_calib_all.hef               ~58MB
```

---

## INT16 v2 Configuration

### Selective Layers (from HAR inspection)

| Model | INT16 Layers |
|-------|--------------|
| arcface_mobilefacenet | fc1, conv33, dw16 |
| arcface_r50 | fc1, dw1, conv53 |
| w600k_mbf | fc1, conv33, dw16 |
| w600k_r50 | fc1, dw1, conv53 |

### .alls File Format
```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])
quantization_param(<model_name>/fc1, precision_mode=a16_w16)
quantization_param(<model_name>/conv33, precision_mode=a16_w16)
...
```

**Important:** Layer names in .alls must use HAR format (e.g., `arcface_mobilefacenet/fc1`) not ONNX format (e.g., `MatMul_95`).

---

## Test Plan

### Phase 1: Validate INT16 v2 Works
1. Deploy `arcface_mobilefacenet_int16_v2_calib1023.hef` to pi_neoseed
2. Run detection session with subject at varying distances
3. Check: Does pre_norm vary with distance? (v1 failed: constant ~2)
4. Check: Does similarity discriminate? (v1 failed: all faces 0.99)

### Phase 2: Compare Calibration Sizes
If Phase 1 passes:
1. Test calib1023 vs calib2048 vs calib_all
2. Measure similarity variance and match rate
3. Select best calibration size

### Phase 3: Compare Models
1. arcface_mobilefacenet vs arcface_r50 (same architecture, different size)
2. w600k_mbf vs w600k_r50 (WebFace600K training data)
3. Measure distance tolerance improvement over INT8 baseline

### Success Criteria
| Metric | INT8 Baseline | INT16 Target |
|--------|---------------|--------------|
| pre_norm | Constant (degenerate) or varies | Varies with distance |
| Similarity (same person) | 0.30-0.67 | Similar or better |
| Similarity (different person) | < 0.30 | < 0.30 |
| Medium distance match rate | 7.9% (r50) / 73% (mobile) | > 50% |

---

## Test Results Log

### 2026-02-15: arcface_mobilefacenet_int16_v2_calib1023

**Status:** Pending

**Setup:**
- Model: `/etc/hailo/models/arcface_mobilefacenet_int16_v2_calib1023.hef`
- Detection: scrfd_10g (INT8)
- Camera: 4mm lens, 2-6m range

**Results:**
```
(To be filled after testing)
```

---

## Known Issues

### 1. Layer Name Mismatch (RESOLVED)
- **Problem:** ONNX layer names (MatMul_95) differ from HAR layer names (arcface_mobilefacenet/fc1)
- **Solution:** Extract layer names from HAR file using Python, use HAR names in .alls

### 2. batch_norm Layer Warning (RESOLVED)
- **Problem:** `[warning] layers ['arcface_mobilefacenet/batch_norm1'] could not be found in scope`
- **Solution:** Don't include batch_norm in .alls (it gets fused with fc layer during optimization)

### 3. Calibration Data Path (RESOLVED)
- **Problem:** Calibration data in `/tmp/calib_all/` was cleared after docker restart
- **Solution:** Re-copy 7643 npy files before compilation

---

## Commands Reference

### Copy HEF to Pi
```bash
scp rtx4900m:~/hailo/shared_with_docker/hef/<model>.hef pi_neoseed:/tmp/
sudo mv /tmp/<model>.hef /etc/hailo/models/
```

### Update function.conf
```bash
# Edit on Pi
sudo nano /greengrass/ggc/deployment/lambda/.../function.conf
# Change HAILO_REC_HEF line
```

### Restart Greengrass
```bash
sudo systemctl restart greengrass
```

### Check Logs
```bash
sudo tail -f /greengrass/ggc/var/log/user/ap-northeast-1/769412733712/neoseed-py_handler.log
```

---

## Next Actions

- [ ] Deploy arcface_mobilefacenet_int16_v2_calib1023.hef to pi_neoseed
- [ ] Run test session with varying distances
- [ ] Record pre_norm and similarity values
- [ ] Compare with INT8 baseline
- [ ] If successful, test other calibration sizes and models
