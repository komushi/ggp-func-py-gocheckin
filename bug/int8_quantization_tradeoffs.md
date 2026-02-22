# INT8 Quantization Trade-offs: InsightFace vs Hailo

**Date:** 2026-02-09
**Related:** `bug_hailo_recognition_failure.md`, `bug10_backend_comparison.md`

---

## Community Reports: Similar Issues

Others have experienced similar quantization and accuracy issues with Hailo and edge AI face recognition. Below are relevant community discussions and research findings.

### Hailo Community Reports

#### 1. HEF Gives Incorrect Results (Data Type Mismatch)
**URL:** https://community.hailo.ai/t/hef-gives-seemingly-incorrect-results-quantized-har-is-fine/6582

**Summary:** User deployed an image detection model where the quantized `.har` file worked correctly, but the compiled `.hef` file returned ~50/50 random results. Root cause was passing `uint8` data when the model expected `float32` input. Solution: Add normalization layer to the model script to ensure proper float32 input.

**Relevance:** Similar to our FormatType.FLOAT32 fix - data type handling during inference affects accuracy.

---

#### 2. InceptionResNetV2 Quantization Accuracy Collapse
**URL:** https://community.hailo.ai/t/inceptionresnetv2-quantisation-accuracy-issues/12755

**Summary:** User reported accuracy dropping from 85%+ to ~2% after INT8 quantization at optimization levels 2-4. The SDK_NATIVE emulator maintained accuracy, but SDK_QUANTIZED caused severe degradation. Issue is model-specific (InceptionV3/ResNet50 worked fine). No solution posted as of March 2025.

**Relevance:** Demonstrates that INT8 quantization can cause catastrophic accuracy loss in specific models.

---

#### 3. TensorFlow to TFLite INT8 Performance Deterioration
**URL:** https://community.hailo.ai/t/model-quantization-issue-from-tensorflow-to-tensorflow-lite/6145

**Summary:** After converting a TensorFlow model to TensorFlow Lite with INT8 quantization, model performance deteriorated and yielded incorrect results.

**Relevance:** INT8 quantization causing accuracy issues is a known problem across frameworks.

---

#### 4. Face Alignment Critical for Embeddings
**URL:** https://community.hailo.ai/t/a-comprehensive-guide-to-building-a-face-recognition-system/8803

**Summary:** Hailo's official guide emphasizes that "without alignment, variations in pose, orientation, and scale could lead to inconsistent embeddings, reducing the system's reliability." Proper landmark detection and face alignment are critical for embedding quality.

**Relevance:** Our SCRFD landmark accuracy issue (caused by manual dequantization) directly affected face alignment and downstream embedding quality.

---

#### 5. Mixed-Mode Debugging for Quantization Loss
**URL:** https://community.hailo.ai/t/using-mixed-mode-fp-quant-for-network-evaluation/144

**Summary:** When debugging accuracy loss, Hailo recommends using "mixed-mode" emulation to run some layers in full precision (32-bit) vs quantized. This helps identify which specific layers cause the majority of accuracy loss.

**Relevance:** Could be used to identify which layers in SCRFD/ArcFace are most sensitive to INT8 quantization.

---

#### 6. Calibration Dataset Requirements
**URL:** https://community.hailo.ai/t/hailo-calibration-quantization-process/2152

**Summary:** During quantization, the calibration dataset should be in floating point format so the algorithm understands the range and distribution of data to properly map floating-point values to integers.

**Relevance:** Poor calibration data can lead to suboptimal quantization and accuracy loss.

---

### Academic Research: INT8 Quantization Effects

#### 7. Quantization Impact on Embedding Similarity
**URL:** https://arxiv.org/html/2501.10534v1

**Summary:** Research on 4-bit and INT8 quantization for vector embeddings found:
- Quantizing from FP32 to INT8 introduces quantization loss
- Reduces accuracy of cosine similarity calculation
- Distribution peak shifts left (lower similarity scores)
- INT8 shows up to 4% degradation in correlation coefficients

**Relevance:** Explains why our Hailo embeddings produce lower similarity scores than InsightFace.

---

#### 8. INT8 Quantization Accuracy Drop Range
**URL:** https://zilliz.com/ai-faq/how-does-quantization-such-as-int8-quantization-or-using-float16-affect-the-accuracy-and-speed-of-sentence-transformer-embeddings-and-similarity-calculations

**Summary:** INT8 quantization typically drops accuracy by 1-5% depending on model and calibration. A poorly calibrated INT8 model might misrank pairs (returning 0.85 instead of 0.92 for a critical match).

**Relevance:** Our observed similarity score differences (Hailo ~0.30-0.43 vs InsightFace ~0.49-0.54) align with this research.

---

### Third-Party Implementations

#### 9. DeGirum Face Recognition Guide
**URL:** https://community.degirum.com/t/hailo-guide-comprehensive-guide-to-building-a-face-recognition-system/143

**Summary:** Comprehensive guide for building face recognition on Hailo, covering detection, alignment, embedding extraction, and database matching. Uses cosine similarity with configurable threshold.

**Relevance:** Reference implementation with similar architecture to ours. Does not address quantization accuracy issues.

**Note:** Other repositories like [Seeed-Solution/face-recognition-api](https://github.com/Seeed-Solution/face-recognition-api) exist but are simply REST API wrappers around the standard SCRFD + ArcFace pipeline without addressing any quantization or accuracy issues.

---

### Summary of Community Findings

| Issue Type | Frequency | Our Experience |
|------------|-----------|----------------|
| Data type mismatch (uint8 vs float32) | Common | Yes - FormatType.FLOAT32 fix |
| Quantization accuracy collapse | Reported | Yes - cliff behavior at pre_norm threshold |
| Calibration data issues | Common | Unknown - using default calibration |
| Model-specific sensitivity | Reported | Yes - r50 vs mobilefacenet behave differently |
| Alignment affecting embeddings | Documented | Yes - SCRFD landmark accuracy critical |

### What's Unique to Our Findings

The following observations have **not been widely documented** in the community:

1. **pre_norm cliff behavior** - 0% → 100% match rate at specific threshold
2. **Distance-dependent accuracy collapse** - quantified curves by distance
3. **Model comparison** (mobilefacenet vs r50 vs InsightFace) - side-by-side benchmarks
4. **Manual vs HailoRT dequantization** - FormatType.FLOAT32 solution
5. **pre_norm as quality gate** - using embedding magnitude to filter low-quality detections

---

## Overview

This document explains why InsightFace (Float32) has better distance tolerance than Hailo (INT8), and the fundamental trade-offs between precision and efficiency in quantized neural networks.

---

## The Core Trade-off

**INT8 quantization sacrifices precision for speed/efficiency.**

| Backend | Precision | Speed | Memory | Distance Tolerance |
|---------|-----------|-------|--------|-------------------|
| InsightFace | Float32 | ~90-110ms | ~1GB RAM | Excellent |
| Hailo | INT8 | ~85-145ms | ~2-31MB HEF | Poor |

---

## Numerical Precision Comparison

### Dynamic Range

| Type | Bits | Discrete Levels | Dynamic Range |
|------|------|-----------------|---------------|
| INT8 | 8 | 256 | 0 to 255 |
| Float32 | 32 | ~4 billion | ±3.4×10³⁸ |

### Resolution

| Type | Smallest Distinguishable Difference |
|------|-------------------------------------|
| INT8 | 1/256 ≈ 0.004 (0.4%) |
| Float32 | ~10⁻⁷ (0.00001%) |

**Example:** Two similar facial features with values `0.1523` and `0.1527`:
- **Float32:** Distinguishes them (different values)
- **INT8:** Both quantize to `39` (identical, information lost)

---

## Why Distance Matters

### Face Size vs Signal Strength

| Distance | Face Size | Pixels | Signal Strength |
|----------|-----------|--------|-----------------|
| Close (<1m) | Large | ~200×200 | Strong |
| Medium (1-3m) | Medium | ~80×80 | Medium |
| Far (>3m) | Small | ~40×40 | Weak |

### What Happens to Weak Signals

```
Far face (weak signal)
        ↓
Neural network activations are small
        ↓
    ┌───────────────────────────────────────┐
    │                                       │
    ▼                                       ▼
Float32                                   INT8
Preserves tiny differences              Quantization noise
0.00012 vs 0.00013                      Both become 0
        ↓                                       ↓
Good embedding                          Noisy embedding
(pre_norm 15-18)                        (pre_norm 6-10)
        ↓                                       ↓
Reliable match                          Below threshold
```

---

## The Quantization Cliff

### Hailo arcface_r50 Behavior

| pre_norm | Match Rate | What's Happening |
|----------|------------|------------------|
| < 10 | **0%** | Signal lost in quantization noise |
| ≥ 10 | **100%** | Signal exceeds noise floor |

This is a **hard cliff**, not a gradual degradation:

```
Match Rate
100% ─────────────────────────────●●●●●●●●●●
                                 │
                                 │ CLIFF
                                 │
  0% ●●●●●●●●●●●●●●●●●●●●●●●●●●●│
    ─────────────────────────────────────────
         6    8    10   12   14   16   18   20
                    pre_norm →
```

### InsightFace Behavior

No cliff - graceful degradation:

```
Match Rate
100% ───────────────────────●●●●●●●●●●●●●●●●●
 80% ─────────────────●●●●●
 60% ───────────●●●●●
 40% ───────●●●
 20% ───●●
    ─────────────────────────────────────────
         10   12   14   16   18   20   22   24
                    pre_norm →
```

---

## Error Accumulation Through Layers

A face recognition network has ~50-100 layers. Each layer introduces quantization error.

### Per-Layer Error

```
                    Quantize        Compute         Dequantize
Input (Float) ──────────→ INT8 ──────────→ INT8 ──────────→ Output
                    ↓                              ↓
               ±0.5 LSB error                 ±0.5 LSB error
```

### Cumulative Effect

| Layers | Float32 Error | INT8 Error |
|--------|---------------|------------|
| 1 | ~10⁻⁷ | ±1 level |
| 10 | ~10⁻⁶ | ±3 levels |
| 50 | ~10⁻⁵ | ±7 levels |
| 100 | ~10⁻⁴ | ±10 levels |

For strong signals (close faces), ±10 levels out of 200+ is acceptable.
For weak signals (far faces), ±10 levels out of 20 is **catastrophic**.

---

## Audio Recording Analogy

| Recording Scenario | Bit Depth | Result |
|--------------------|-----------|--------|
| Loud concert + 8-bit | Low | Sounds OK (signal dominates noise) |
| Whisper + 8-bit | Low | Hissy, noisy (noise dominates signal) |
| Whisper + 32-bit | High | Clean, clear |

**Far faces are like whispers** - they need higher precision to capture subtle details.

---

## Measured Results

### pre_norm by Distance

| Distance | InsightFace | Hailo r50 | Hailo mobilefacenet |
|----------|-------------|-----------|---------------------|
| Close | 22-24 | 16-21 | 6.0-7.1 |
| Medium | 19-21 | 11-16 | 5.5-6.0 |
| Far | 15-18 | 6-12 | 3.0-5.0 |

InsightFace maintains higher pre_norm at all distances.

### Match Rate by Distance

| Distance | InsightFace | Hailo r50 | Hailo mobilefacenet |
|----------|-------------|-----------|---------------------|
| Close | 63% | **100%** | ~100% |
| Medium | **63%** | 7.9% | 73% |
| Far | Graceful | 0% | 39% |

---

## Why Hailo Can Beat InsightFace at Close Range

At close range, the signal is strong enough that:
1. Quantization error is small relative to signal
2. INT8 precision is sufficient
3. Hailo's optimized NPU pipeline is more efficient

**arcface_r50 close-range:** 0.67 similarity, 100% match
**InsightFace close-range:** 0.59 similarity, 63% match

The 31M parameter r50 model, even quantized to INT8, extracts better features than InsightFace's buffalo_sc when signal is strong.

---

## The Fundamental Trade-off

### What INT8 Gains

| Benefit | Impact |
|---------|--------|
| Memory | 4× smaller (8 bits vs 32 bits) |
| Bandwidth | 4× less data movement |
| Compute | Specialized INT8 units (NPU) |
| Power | Lower power consumption |
| Throughput | Can run on dedicated NPU, freeing CPU |

### What INT8 Loses

| Cost | Impact |
|------|--------|
| Precision | 256 levels vs 4 billion |
| Dynamic range | Limited weak signal capture |
| Distance tolerance | Hard threshold behavior |
| Stability | Can degrade over runtime |

---

## Practical Implications

### When to Use Hailo (INT8)

1. **Close-range deployment** (< 1m) - kiosks, door locks, turnstiles
2. **Multi-camera setups** - NPU offloads CPU for GStreamer/HTTP
3. **Resource-constrained** - Limited RAM or need for mixed AI workloads
4. **Speed priority** - mobilefacenet at ~85ms is fastest

### When to Use InsightFace (Float32)

1. **Variable distance** - hallways, lobbies, outdoor areas
2. **Single camera** - CPU can handle the load
3. **Accuracy priority** - consistent 63% match at all distances
4. **Simple deployment** - No pre_norm threshold tuning needed

---

## Model Selection Guide

```
                        ┌─────────────────────┐
                        │ What's your camera  │
                        │    placement?       │
                        └──────────┬──────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
         Close Only           Variable              Far Only
          (< 1m)             Distance               (> 3m)
              │                    │                    │
              ▼                    ▼                    ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │ Hailo arcface_r50│   │   InsightFace   │   │   InsightFace   │
    │ Best: sim 0.67  │   │ Consistent 63%  │   │ Only viable     │
    │ 100% match rate │   │ at all ranges   │   │ option          │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
```

---

## Model Architecture Comparison

### InsightFace buffalo_sc (Appears as 1 Model)

```
buffalo_sc/
├── det_10g.onnx      # Detection (SCRFD 10G)
├── w600k_r50.onnx    # Recognition (ArcFace R50 variant)
└── ...
```

The InsightFace API abstracts this - `app.get(img)` runs both models internally.

### Hailo (Explicitly 2 Models)

```
/etc/hailo/models/
├── scrfd_10g.hef           # Detection (SCRFD 10G)
└── arcface_mobilefacenet.hef  # Recognition (ArcFace)
```

**They use the same architecture** - both are SCRFD + ArcFace. The difference is precision (Float32 vs INT8), not model design.

---

## Can Hailo Run InsightFace Models?

### Short Answer: Yes, But You Lose Float32 Advantage

The Hailo NPU **cannot run Float32** - it only supports quantized formats:

| Precision | Discrete Levels | Availability |
|-----------|-----------------|--------------|
| INT4 | 16 | Supported (worse quality) |
| **INT8** | **256** | **Current (pre-compiled)** |
| INT16 | 65,536 | Supported (no pre-compiled HEF) |
| Float32 | 4 billion | **Not supported on NPU** |

### The Conversion Path

```
InsightFace ONNX (Float32)
        │
        ▼
┌─────────────────────────┐
│ Hailo Dataflow Compiler │
│  - Parse ONNX           │
│  - Quantize weights     │
│  - Calibrate activations│
│  - Compile to HEF       │
└─────────────────────────┘
        │
        ▼
Hailo HEF (INT8 or INT16)
        │
        ▼
Same precision limitations as current Hailo models
```

Even converting InsightFace's exact ONNX files to Hailo would result in INT8 quantization and the same distance tolerance issues.

---

## INT16 Quantization: A Middle Ground?

INT16 offers 256× more precision than INT8:

| Precision | Levels | Smallest Difference | Speed | Distance Tolerance |
|-----------|--------|---------------------|-------|-------------------|
| INT8 | 256 | 0.4% | Fast | Poor (cliff) |
| **INT16** | **65,536** | **0.0015%** | ~50% slower | **Better (estimated)** |
| Float32 | 4 billion | 0.00001% | CPU only | Excellent |

### Why INT16 Might Help

The quantization cliff occurs when signal ≈ noise:
- INT8: noise floor is ~1/256 of signal range
- INT16: noise floor is ~1/65536 of signal range

For weak signals (far faces), INT16's lower noise floor could preserve enough detail to avoid the cliff.

### Requirements for INT16 Compilation

No pre-compiled INT16 HEF files exist. Manual compilation requires:

1. **Hailo Dataflow Compiler (DFC)** - proprietary tool
2. **Original ONNX models** - SCRFD + ArcFace
3. **Calibration dataset** - representative face images
4. **Compilation config:**
   ```python
   # In .alls optimization script
   quantization_param(precision_mode=a16_w16)
   ```

### INT16 Trade-offs

| Benefit | Cost |
|---------|------|
| 256× more precision | ~50% slower inference |
| Better distance tolerance | Larger HEF file size |
| May eliminate cliff behavior | Compilation effort required |
| Still runs on NPU (frees CPU) | No pre-compiled models available |

---

## Summary

| Aspect | InsightFace (Float32) | Hailo (INT8) |
|--------|----------------------|--------------|
| Precision | ~7 decimal digits | 256 levels |
| Weak signal handling | Preserved | Lost in noise |
| Error accumulation | Negligible | Compounds through layers |
| Distance sensitivity | Graceful degradation | Hard cliff |
| Close-range accuracy | Good (0.59) | **Excellent (0.67)** |
| Medium-range accuracy | **Good (63%)** | Poor (7.9% r50 / 73% mobile) |
| Memory footprint | ~1GB | ~2-31MB |
| CPU usage | Higher | **Offloaded to NPU** |
| Models | 2 (appears as 1) | 2 (explicit) |

**Bottom line:** INT8 is a trade-off, not an upgrade. Choose based on your deployment scenario.

---

## How to Compile INT16 HEF Files (Recognition Only)

INT16 quantization targets **recognition models only**. Detection (SCRFD) works fine with INT8 and is not the bottleneck.

---

### New Machine Setup Guide

#### Prerequisites

| Requirement | Specification |
|-------------|---------------|
| OS | Ubuntu 22.04 LTS |
| GPU | NVIDIA with CUDA 11.8 support (e.g., RTX 4090, RTX 3090, A100) |
| RAM | 32GB+ recommended |
| Disk | 50GB+ free space |
| Docker | With NVIDIA Container Toolkit |

**Note:** Blackwell GPUs (RTX 5000 series) require CUDA 12.x which is NOT compatible with current Hailo DFC.

#### Step 1: Install NVIDIA Driver + CUDA 11.8

```bash
# Install NVIDIA driver (if not installed)
sudo apt update
sudo apt install -y nvidia-driver-535

# Install CUDA 11.8 toolkit
wget https://developer.download.nvidia.com/compute/cuda/11.8.0/local_installers/cuda_11.8.0_520.61.05_linux.run
sudo sh cuda_11.8.0_520.61.05_linux.run --toolkit --silent

# Add to PATH
echo 'export PATH=/usr/local/cuda-11.8/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda-11.8/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc

# Verify
nvcc --version  # Should show 11.8
nvidia-smi      # Should show GPU
```

#### Step 2: Install Docker + NVIDIA Container Toolkit

```bash
# Install Docker
sudo apt install -y docker.io
sudo systemctl enable docker
sudo usermod -aG docker $USER

# Install NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo systemctl restart docker

# Verify GPU in Docker
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

#### Step 3: Download Hailo AI SW Suite

1. Login to [Hailo Developer Zone](https://hailo.ai/developer-zone/)
2. Download: **Hailo AI Software Suite – Docker (2025-10)**
3. Transfer to machine and load:

```bash
# Load Docker image
unzip hailo_ai_sw_suite_2025-10_docker.zip
docker load -i hailo_ai_sw_suite_2025-10.tar
```

#### Step 4: Transfer Prepared Data

From supermicro (or backup location):
```bash
# On source machine
scp supermicro:~/hailo/shared_with_docker_minimal.tar.gz ~/

# Extract
mkdir -p ~/hailo
cd ~/hailo
tar -xzf ~/shared_with_docker_minimal.tar.gz
```

**Contents transferred:**
| Directory | Contents | Size |
|-----------|----------|------|
| `recognition/` | 7 ONNX models | ~1.1GB |
| `alls/` | 7 INT16 config files | 32KB |
| `calibration/` | 500 face images (112x112) | 3.6MB |
| `detection/` | 3 SCRFD ONNX models | 35MB |
| `hef/` | Output directory | Empty |

#### Step 5: Start Hailo Container

```bash
# Start container with GPU and volume mount
docker run -d --name hailo_sw_suite \
  --gpus all \
  -v ~/hailo/shared_with_docker:/local/shared_with_docker \
  hailo8_ai_sw_suite_2025-10:1 \
  tail -f /dev/null

# Verify GPU access
docker exec hailo_sw_suite nvidia-smi

# Verify Hailo DFC
docker exec hailo_sw_suite hailo --version
```

#### Step 6: Verify Setup

```bash
# Check all components
docker exec hailo_sw_suite bash -c "
  echo '=== ONNX Models ===' && \
  ls -la /local/shared_with_docker/recognition/*/*.onnx && \
  echo '' && \
  echo '=== ALLS Files ===' && \
  ls /local/shared_with_docker/alls/*.alls && \
  echo '' && \
  echo '=== Calibration Images ===' && \
  ls /local/shared_with_docker/calibration/*.jpg | wc -l
"
```

Expected output:
- 7 ONNX models
- 7 .alls files
- 500 calibration images

---

### GPU Compatibility Notes

| GPU | CUDA | Hailo DFC | Status |
|-----|------|-----------|--------|
| RTX 4090/4080 | 11.8 | ✅ Works | **Recommended** |
| RTX 3090/3080 | 11.8 | ✅ Works | Good |
| A100/A10 | 11.8 | ✅ Works | Good |
| RTX 5090 (Blackwell) | 12.x+ | ❌ Fails | Not supported |

**If GPU fails:** Use CPU mode with `CUDA_VISIBLE_DEVICES=` prefix (slower, optimization_level=0).

---

### Current Environment Status

| Component | Status | Location |
|-----------|--------|----------|
| Hailo AI SW Suite | ✅ Docker | `~/hailo/` |
| Hailo DFC | ✅ v3.33.0 | Inside Docker |
| ONNX Models | ✅ 7 models | `~/hailo/shared_with_docker/recognition/` |
| INT16 .alls | ✅ 7 configs | `~/hailo/shared_with_docker/alls/` |
| Calibration | ✅ 500 images | `~/hailo/shared_with_docker/calibration/` |
| **Backup tarball** | ✅ 1.3GB | `~/hailo/shared_with_docker_minimal.tar.gz` |

**Docker commands:**
```bash
# Start container
docker start hailo_sw_suite

# With GPU (RTX 4090 or older)
docker exec hailo_sw_suite bash -c "<command>"

# Without GPU / CPU mode (Blackwell or fallback)
docker exec hailo_sw_suite bash -c "CUDA_VISIBLE_DEVICES= <command>"
```

---

### Recognition Models (Priority Order)

#### New Models (Quality-Aware)

| Priority | Model | Size | Why | ONNX Location |
|----------|-------|------|-----|---------------|
| **1** | **MagFace iResNet100** | 250MB | Magnitude = quality (addresses pre_norm cliff) | `recognition/magface/magface_ir100.onnx` |
| **2** | **AdaFace IR101** | 249MB | Quality-adaptive margin (better varying distances) | `recognition/adaface/adaface_ir101_webface12m.onnx` |

#### Current Hailo Models (INT8 → INT16 Comparison)

| Priority | Model | Size | Why | ONNX Location |
|----------|-------|------|-----|---------------|
| **3** | **arcface_r50** | 119MB | Current Hailo model - compare INT8 vs INT16 | `recognition/hailo_models/arcface_r50.onnx` |
| **4** | **arcface_mobilefacenet** | 7.9MB | Current default - lightweight INT16 test | `recognition/hailo_models/arcface_mobilefacenet.onnx` |

#### Fallback / Additional

| Priority | Model | Size | Why | ONNX Location |
|----------|-------|------|-----|---------------|
| 5 | w600k_r50 | 167MB | Same architecture, WebFace600K training | `recognition/insightface/w600k_r50.onnx` |
| 6 | glintr100 | 249MB | Largest training dataset (Glint360K) | `recognition/insightface/glintr100.onnx` |
| 7 | w600k_mbf | 13MB | Lightweight alternative | `recognition/insightface/w600k_mbf.onnx` |

**Note:** Detection uses existing SCRFD_10G with INT8 (not the bottleneck).

**Testing Strategy:**
1. First: arcface_r50 INT16 vs INT8 (isolate quantization effect)
2. Then: MagFace/AdaFace INT16 (test new architectures)

---

### Step 1: Prepare Calibration Data ✅ DONE

Calibration images prepared from LFW dataset:

| Item | Status |
|------|--------|
| Location | `~/hailo/shared_with_docker/calibration/` |
| Count | 500 images |
| Size | 112x112 RGB (aligned) |
| Format | JPEG |
| Manifest | `calibration_manifest.txt` |

Generated using `scripts/prepare_calibration_faces.py`.

---

### Step 2: Create INT16 .alls Config ✅ DONE

All 7 INT16 .alls files created in `~/hailo/shared_with_docker/alls/`:

| File | Model |
|------|-------|
| `magface_ir100_int16.alls` | MagFace iResNet100 |
| `adaface_ir101_int16.alls` | AdaFace IR101 |
| `arcface_r50_int16.alls` | ArcFace R50 |
| `arcface_mobilefacenet_int16.alls` | ArcFace MobileFaceNet |
| `w600k_r50_int16.alls` | W600K R50 |
| `glintr100_int16.alls` | GlintR100 |
| `w600k_mbf_int16.alls` | W600K MobileFaceNet |

Each file contains:
```
normalization1 = normalization([127.5, 127.5, 127.5], [127.5, 127.5, 127.5])
quantization_param(*, precision_mode=a16_w16)
```

---

### Step 3: Compile to HEF

**Start priority: arcface_r50 (compares INT16 vs existing INT8 HEF)**

```bash
# Compile arcface_r50 (Priority 3 - direct comparison with existing INT8)
docker exec hailo_sw_suite bash -c "CUDA_VISIBLE_DEVICES= hailomz compile \
  --ckpt /local/shared_with_docker/recognition/hailo_models/arcface_r50.onnx \
  --calib-path /local/shared_with_docker/calibration/ \
  --hw-arch hailo8l \
  --alls /local/shared_with_docker/alls/arcface_r50_int16.alls \
  --output /local/shared_with_docker/hef/arcface_r50_int16.hef"

# Compile MagFace (Priority 1 - quality-aware)
docker exec hailo_sw_suite bash -c "CUDA_VISIBLE_DEVICES= hailomz compile \
  --ckpt /local/shared_with_docker/recognition/magface/magface_ir100.onnx \
  --calib-path /local/shared_with_docker/calibration/ \
  --hw-arch hailo8l \
  --alls /local/shared_with_docker/alls/magface_ir100_int16.alls \
  --output /local/shared_with_docker/hef/magface_ir100_int16.hef"

# Compile AdaFace (Priority 2 - quality-adaptive)
docker exec hailo_sw_suite bash -c "CUDA_VISIBLE_DEVICES= hailomz compile \
  --ckpt /local/shared_with_docker/recognition/adaface/adaface_ir101_webface12m.onnx \
  --calib-path /local/shared_with_docker/calibration/ \
  --hw-arch hailo8l \
  --alls /local/shared_with_docker/alls/adaface_ir101_int16.alls \
  --output /local/shared_with_docker/hef/adaface_ir101_int16.hef"
```

**All models compilation script:**
```bash
# Create output directory
mkdir -p ~/hailo/shared_with_docker/hef/

# Batch compile all 7 models
for model in arcface_r50 arcface_mobilefacenet magface_ir100 adaface_ir101 w600k_r50 glintr100 w600k_mbf; do
  echo "Compiling $model..."
  # Command depends on model location - see table above
done
```

---

### Step 4: Deploy and Test

Copy HEF to Raspberry Pi:
```bash
scp ~/hailo/shared_with_docker/magface_ir100_int16.hef pi@<device>:/etc/hailo/models/
```

Update `function.conf`:
```
HAILO_REC_HEF = "/etc/hailo/models/magface_ir100_int16.hef"
```

---

### Expected Results (INT16 vs INT8)

| Metric | INT8 (current) | INT16 (target) |
|--------|----------------|----------------|
| Precision levels | 256 | 65,536 |
| Distance tolerance | Poor (cliff) | Better (estimated) |
| Inference speed | ~85-130ms | ~130-200ms |
| HEF file size | ~2-31MB | ~60-100MB |

---

### MagFace vs AdaFace

| Issue | MagFace | AdaFace |
|-------|---------|---------|
| pre_norm cliff (0%→100%) | **Directly solves** (magnitude = quality) | Indirectly |
| Low similarity at medium distance | Indirectly | **Directly solves** |
| Provides confidence score | **Yes** (magnitude) | No |

**Recommendation:** Try MagFace first - magnitude gives explicit quality signal for debugging.

---

### Troubleshooting

**"Layer not supported in INT16"**
- Some layers may not support INT16. Use selective quantization on final embedding layers only.

**Compilation fails with memory error**
- Ensure 32GB RAM or add swap space.

**Poor accuracy after INT16**
- Calibration data may not be representative. Add more diverse face images.

---

### Community Experience with INT16

Based on Hailo community research, INT16 compilation has mixed results. **No documented success exists for face recognition.**

| Finding | Implication |
|---------|-------------|
| Not all layers support INT16 | May need selective quantization |
| Post-processing expects UINT8 | May need code changes for UINT16 output |
| No face recognition examples | We would be pioneers |

**References:**
- [16-bit quantization on final layers](https://community.hailo.ai/t/16-bit-quantization-on-final-layers/2292)
- [How to apply 16-bits quantization](https://community.hailo.ai/t/how-to-apply-16-bits-quantization-to-all-the-convolution-layers-in-the-model/2837)

---

## INT16 Experiment Conclusion (2026-02-11)

**Status: FAILED — Do not use INT16 for face recognition on Hailo**

### Test Results

| Model | Compiled | pre_norm | Similarity | Result |
|-------|----------|----------|------------|--------|
| arcface_mobilefacenet INT16 | ✅ | ~2 (constant) | 0.9960 (wrong person) | **Degenerate** |
| w600k_mbf INT16 | ✅ | ~4.7 (constant) | False positives | **Degenerate** |

**Symptoms:**
- pre_norm is constant regardless of face distance/quality
- All faces produce nearly identical embeddings
- Model matches wrong persons with extremely high confidence (0.99+)
- Complete loss of discriminative ability

### Root Cause Analysis

INT16 quantization (`precision_mode=a16_w16`) destroyed embedding discriminability. But the root cause may not be "INT16 is unsupported" — it could be our compilation approach.

#### All Possible Causes

| # | Possible Cause | Evidence | Likelihood | Testable? |
|---|----------------|----------|------------|-----------|
| 1 | **Insufficient calibration data** | Used 500 images, Hailo recommends 1000+ | **High** | ✅ Yes |
| 2 | **Wrong .alls config (all layers INT16)** | Used `quantization_param(*)` for ALL layers | **High** | ✅ Yes |
| 3 | **Wrong calibration format** | Used JPEG, Hailo uses TFRecord | Medium | ✅ Yes |
| 4 | **Normalization mismatch** | We used std=127.5, SCRFD uses std=128.0 | Low | ✅ Yes |
| 5 | **Layer incompatibility** | adaface_ir101 failed on maxpool INT16 | Medium | ⚠️ Partial |
| 6 | **Output format handling** | Maybe INT16 needs different output config | Low | ✅ Yes |
| 7 | **Hailo DFC not validated for INT16 face recognition** | No official INT16 face models exist | Medium | ❌ No |
| 8 | **Hardware silicon limits** | Some ops may only have INT8 implementation | Low | ❌ No |

**Most likely culprits:** #1 (insufficient calibration) and #2 (all-layers INT16)

### Official Hailo Approach (Research Findings)

From [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo):

**Official arcface_mobilefacenet.alls contains ONLY:**
```
normalization1 = normalization([127.5, 127.5, 127.5], [127.5, 127.5, 127.5])
```
**No `quantization_param` = INT8 by default**

**Official INT8 accuracy on LFW benchmark:**

| Model | Float Accuracy | INT8 HW Accuracy | Difference |
|-------|----------------|------------------|------------|
| arcface_mobilefacenet | 99.4% | **99.5%** | +0.1% (improved) |
| arcface_r50 | 99.7% | **99.7%** | 0% |

Hailo's INT8 models **match or exceed** float accuracy when properly calibrated.

### Key Insight

Our distance tolerance issues are **not caused by INT8 bit-width**:
- Hailo INT8 achieves 99.5% on LFW (close-range, aligned faces)
- Our issues occur at medium/far distance where face resolution drops
- This is a **signal-to-noise ratio** problem, not a **precision** problem

### Current Recommendation

| Option | Use When |
|--------|----------|
| Hailo INT8 arcface_r50 | Close range (<1m), 100% match rate |
| InsightFace Float32 | Variable distance, 63% match at all ranges |
| **INT16 (current attempt)** | ❌ Broken — needs fixes below |

---

## INT16 Round 2: Implementation (2026-02-11)

All fixes prepared on `rtx4900m:~/hailo/shared_with_docker/`

### Changes Applied

| Fix | v1 (Failed) | v2 (New) | Status |
|-----|-------------|----------|--------|
| Calibration images | 1023 | **7643** | ✅ Ready |
| Quantization scope | ALL layers `{*}` | **Final layers only** | ✅ Ready |
| Normalization std | 127.5 | **128.0** | ✅ Ready |
| Hardware target | hailo8l | **hailo8** | ✅ Ready |

---

### Fix 1: More Calibration Data (7643 images)

```bash
# All 7643 aligned face images copied to docker
docker exec hailo_sw_suite ls /tmp/calib_all/*.npy | wc -l
# Output: 7643
```

---

### Fix 2: Selective INT16 (Final Layers Only)

Layer names extracted from ONNX models:

**arcface_mobilefacenet** (`alls/arcface_mobilefacenet_int16_v2.alls`):
```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])

# INT16 only on final embedding layers (verified from ONNX)
quantization_param(MatMul_95, precision_mode=a16_w16)
quantization_param(BatchNormalization_96, precision_mode=a16_w16)
quantization_param(Conv_91, precision_mode=a16_w16)
quantization_param(Conv_93, precision_mode=a16_w16)
```

**arcface_r50** (`alls/arcface_r50_int16_v2.alls`):
```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])

quantization_param(Gemm_129, precision_mode=a16_w16)
quantization_param(BatchNormalization_130, precision_mode=a16_w16)
quantization_param(Conv_127, precision_mode=a16_w16)
quantization_param(Flatten_128, precision_mode=a16_w16)
```

**w600k_mbf** (`alls/w600k_mbf_int16_v2.alls`):
```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])

quantization_param(Gemm_96, precision_mode=a16_w16)
quantization_param(BatchNormalization_97, precision_mode=a16_w16)
quantization_param(Flatten_95, precision_mode=a16_w16)
quantization_param(Conv_93, precision_mode=a16_w16)
```

**w600k_r50** (`alls/w600k_r50_int16_v2.alls`):
```
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])

quantization_param(Gemm_128, precision_mode=a16_w16)
quantization_param(BatchNormalization_129, precision_mode=a16_w16)
quantization_param(Flatten_127, precision_mode=a16_w16)
quantization_param(Conv_124, precision_mode=a16_w16)
```

---

### Fix 3: Normalization std=128.0

All v2 .alls files updated:
```
# v1 (wrong)
normalization1 = normalization([127.5, 127.5, 127.5], [127.5, 127.5, 127.5])

# v2 (correct)
normalization1 = normalization([127.5, 127.5, 127.5], [128.0, 128.0, 128.0])
```

---

### Compilation Scripts

**Sequential** (`compile_int16_v2.sh`):
```bash
docker exec hailo_sw_suite bash /local/shared_with_docker/compile_int16_v2.sh
```

**Parallel** (`compile_int16_v2_parallel.sh`):
```bash
docker exec hailo_sw_suite bash /local/shared_with_docker/compile_int16_v2_parallel.sh
```

---

### Models to Compile

| Model | ONNX | .alls v2 |
|-------|------|----------|
| arcface_mobilefacenet | hailo_models/arcface_mobilefacenet.onnx | arcface_mobilefacenet_int16_v2.alls |
| arcface_r50 | hailo_models/arcface_r50.onnx | arcface_r50_int16_v2.alls |
| w600k_mbf | insightface/w600k_mbf.onnx | w600k_mbf_int16_v2.alls |
| w600k_r50 | insightface/w600k_r50.onnx | w600k_r50_int16_v2.alls |

---

### Expected Outcomes

| Metric | v1 (Failed) | v2 (Expected) |
|--------|-------------|---------------|
| pre_norm | Constant (~2) | Varies with distance |
| Similarity | 0.99 (wrong person) | < 0.30 (different), > 0.40 (same) |
| Match rate | 100% false positive | TBD |

---

### Run Compilation

```bash
# SSH to rtx4900m
ssh rtx4900m

# Start tmux session
tmux new -s hailo_compile

# Run parallel compilation inside docker
docker exec hailo_sw_suite bash /local/shared_with_docker/compile_int16_v2_parallel.sh

# Monitor logs
docker exec hailo_sw_suite tail -f /tmp/compile_arcface_mobilefacenet_v2.log
```

---

### Compilation Commands for Round 2

#### Test 1: More Calibration Data
```bash
# On rtx4900m
ssh rtx4900m

# Generate 2048 calibration images (run on host with InsightFace)
cd ~/hailo/shared_with_docker
python /path/to/prepare_calibration_faces.py --num-images 2048 --output-dir calibration_2048

# Compile with new calibration
docker exec hailo_sw_suite bash -c "
  cd /local/shared_with_docker && \
  hailo parser onnx recognition/hailo_models/arcface_mobilefacenet.onnx \
    --hw-arch hailo8 --har-path arcface_mbf.har && \
  hailo optimize arcface_mbf.har \
    --hw-arch hailo8 \
    --calib-set-path calibration_2048 \
    --model-script alls/arcface_mobilefacenet_int16.alls \
    --output-har-path arcface_mbf_int16_opt.har && \
  hailo compiler arcface_mbf_int16_opt.har \
    --hw-arch hailo8 \
    --output-dir hef_int16_calib2048
"
```

#### Test 2: Selective INT16
```bash
# Create new .alls file first
cat > ~/hailo/shared_with_docker/alls/arcface_mobilefacenet_selective_int16.alls << 'EOF'
normalization1 = normalization([127.5, 127.5, 127.5], [127.5, 127.5, 127.5])
# Only apply INT16 to final embedding layer
quantization_param(pre_fc1*, precision_mode=a16_w16)
quantization_param(fc1*, precision_mode=a16_w16)
EOF

# Compile
docker exec hailo_sw_suite bash -c "
  cd /local/shared_with_docker && \
  hailo parser onnx recognition/hailo_models/arcface_mobilefacenet.onnx \
    --hw-arch hailo8 --har-path arcface_mbf.har && \
  hailo optimize arcface_mbf.har \
    --hw-arch hailo8 \
    --calib-set-path calibration_2048 \
    --model-script alls/arcface_mobilefacenet_selective_int16.alls \
    --output-har-path arcface_mbf_selective_int16_opt.har && \
  hailo compiler arcface_mbf_selective_int16_opt.har \
    --hw-arch hailo8 \
    --output-dir hef_selective_int16
"
```

---

### Expected Outcomes

| Test | Success Criteria | Failure Indicates |
|------|------------------|-------------------|
| More calibration | pre_norm varies with distance | Calibration wasn't the issue |
| Selective INT16 | pre_norm varies, sim > 0.30 | Layer selection wrong |
| Both combined | Match rate > 50% at medium distance | INT16 fundamentally unsupported |

---

### References

- [Hailo Model Zoo - Face Recognition](https://github.com/hailo-ai/hailo_model_zoo/blob/master/docs/public_models/HAILO8/HAILO8_face_recognition.rst)
- [Official arcface_mobilefacenet.alls](https://github.com/hailo-ai/hailo_model_zoo/blob/master/hailo_model_zoo/cfg/alls/generic/arcface_mobilefacenet.alls)
- [Hailo Community: 16-bit quantization on final layers](https://community.hailo.ai/t/16-bit-quantization-on-final-layers/2292)
- [Hailo Community: How to apply 16-bits quantization](https://community.hailo.ai/t/how-to-apply-16-bits-quantization-to-all-the-convolution-layers-in-the-model/2837)
