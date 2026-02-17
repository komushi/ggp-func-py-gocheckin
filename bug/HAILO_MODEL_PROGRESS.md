# Hailo Model Conversion Progress

**Last Updated:** 2026-02-15

---

## Model Inventory

### Recognition Models (HEF)

| Model | Quantization | Calibration | Compiled | Deployed | Tested | Result |
|-------|--------------|-------------|----------|----------|--------|--------|
| arcface_mobilefacenet | INT8 | Hailo default | - | pi_neoseed | Yes | Baseline, pre_norm>=6 works |
| arcface_r50 | INT8 | Hailo default | - | pi_neoseed | Yes | Baseline, pre_norm>=10 cliff |
| arcface_mobilefacenet_int16_v2 | INT16 selective | 1023 | Yes | pi_neoseed | Yes | **Not viable** — worse than INT8, 25% success rate, sim capped at 0.35 |
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

### 2026-02-17: arcface_mobilefacenet INT8 — Embedding Instability

**Model:** arcface_mobilefacenet.hef (INT8, default from Hailo Model Zoo)
**Detection:** scrfd_10g (INT8)
**Device:** pi_neoseed
**Subject:** Xu, same position/lighting across all sessions
**Threshold:** sim >= 0.30, pre_norm >= 5.0

| | Session 1 (11:39) | Session 2 (11:42) | Session 3 (11:55) | Session 4 (12:07) |
|---|---|---|---|---|
| Distance | baseline | same | same | **10cm closer** |
| Frames | ~23 | 97 | 100 | 99 |
| Identified | Yes | **No** | Yes (barely) | Yes |
| Matches | ~10 (frequent) | **0** | 5 (sporadic) | **~40** (frames 33-82) |
| Xu best_sim | **0.31-0.53** | 0.09-0.24 | 0.07-0.35 | 0.30-0.35 |
| pre_norm range | 5.8-6.2 | 6.1-6.5 | 5.9-6.7 | 6.3-6.9 |
| Pattern | Consistent | Total failure | Borderline, sporadic | Consistent but borderline |

**Key findings:**
1. Same person, same distance, similar pre_norm (~6.0-6.5), but similarity dropped from 0.53 to 0.15 across sessions. Degradation is **non-deterministic** — Session 2 (+3 min) was worse than Session 3 (+16 min). This is not gradual drift but unstable embedding generation from the Hailo NPU.
2. Session 4 (10cm closer) showed significant improvement — ~40 matches vs 0-5 at baseline distance. Closer distance yields higher pre_norm (6.3-6.9) and more consistent matching, but similarity scores (0.30-0.35) remain borderline at threshold.

### 2026-02-17: arcface_mobilefacenet_int16_v2_calib1023 — Worse Than INT8

**Model:** arcface_mobilefacenet_int16_v2_calib1023.hef (INT16 selective: fc1, conv33, dw16)
**Detection:** scrfd_10g (INT8)
**Device:** pi_neoseed
**Camera:** 192.168.11.62
**Subject:** Xu, close distance (~1-2m)
**Threshold:** sim >= 0.30, pre_norm >= 5.0
**Reference embeddings:** Generated by InsightFace float32 (not re-enrolled with INT16)

| Session | Time | Frames | Identified | Matches | Xu best_sim | Pattern |
|---------|------|--------|------------|---------|-------------|---------|
| S5 | 12:22 | 100 | **No** | 0 | 0.26 | Total failure |
| S6 | 12:27 | 99 | **No** | 0 | 0.18 | Total failure |
| S7 | 12:30 | 100 | Yes | 16 | 0.35 | Late burst (frames 84-100 only) |
| S8a | 12:36:23 | 99 | **No** | 0 | — | Total failure |
| S8b | 12:36:38 | 99 | **No** | 0 | — | Total failure |
| S8c | 12:36:49 | 100 | **No** | 0 | — | Total failure |
| S8d | 12:36:54 | 100 | Yes | ~30 | 0.35 | Two clusters (frames 34-42, 64-89) |
| S8e | 12:37:20 | 100 | **No** | 0 | — | Total failure |

**Success rate:** 2/8 sessions (25%) at close distance (~1-2m)

**Key findings:**
1. **Worse than INT8** — INT8 achieved sim 0.53 at best; INT16 v2 caps at 0.35 (barely above threshold).
2. **Same non-deterministic instability** — 6 of 8 sessions failed completely despite identical conditions.
3. **Embedding space mismatch** — Reference embeddings were generated by InsightFace (float32). INT16 quantization shifts the embedding space, making similarity structurally lower. Re-enrollment with INT16 model might help but would tie the system to one specific quantization.
4. **Two faces per frame** — SCRFD consistently detects 2 faces (face 1: pre_norm ~6.0-7.0, face 2: ~4.8-5.5). Only face 1 ever matches. Face 2 may be a reflection or background object.

**Verdict:** INT16 v2 calib1023 is **not viable** for production. Does not improve over INT8 baseline.

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
