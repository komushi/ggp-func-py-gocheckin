# Test Procedure: Lock-Triggered Face Detection

## Overview

This document describes how to run Test 3 (Lock-Triggered Face Detection) with automated log monitoring and timeline generation using Claude Code.

## Quick Start Command

Tell Claude:
```
Monitor the z2m, TS and Python logs on demo for Test 3.
Create a timeline table when I say "done". I will tell you if this was a delayed unlock.
```

## What Claude Will Do

1. **Start Background Log Monitors** (2 parallel commands)
   ```bash
   # Terminal 1: Zigbee2MQTT logs
   ssh demo "docker logs -f zigbee2mqtt"

   # Terminal 2: TS and Python Lambda logs
   ssh demo "sudo tail -f /greengrass/ggc/var/log/user/ap-northeast-1/769412733712/demo-py_handler.log \
     /greengrass/ggc/var/log/user/ap-northeast-1/769412733712/demo-ts_handler.log"
   ```

2. **Wait for User to Complete Test**

3. **On "done" Command - Generate Timeline Table**
   - Kill the background monitors
   - Extract key events from logs:
     - `occupancy: true` - Zigbee sensor triggered
     - `occupancy: false` - MCU timeout (20 seconds after physical touch)
     - `trigger_detection` publish
     - `detecting_txn` frames
     - `fullName` matches
     - `member_detected` events
     - `TOGGLE` command
     - `state: ON` confirmation
   - **Calculate actual touch time**: `occupancy: false` timestamp - 20 seconds

## Timeline Table Format

**IMPORTANT: Calculate actual touch time first!**
```
Actual touch time = occupancy:false timestamp - 20 seconds
```

| Time | Δ from touch | Component | Event |
|------|-------------|-----------|-------|
| HH:MM:SS.000 | **0ms** | **MCU** | **Actual physical touch** (= occupancy:false - 20s) |
| HH:MM:SS.mmm | Xms | Z2M | `occupancy: true` published |
| HH:MM:SS.mmm | Xms | TS | Received `zigbee2mqtt/{LOCK}` → `occupancy: true` |
| HH:MM:SS.mmm | Xms | TS | Published `trigger_detection` → camera {IP} |
| HH:MM:SS.mmm | Xms | PY | Received `trigger_detection`, started face detection |
| HH:MM:SS.mmm | Xms | PY | Frame N: X faces |
| HH:MM:SS.mmm | Xms | PY | **Match: {Name}, similarity: {score}** |
| HH:MM:SS.mmm | Xms | PY | Published `member_detected` |
| HH:MM:SS.mmm | Xms | TS | Received `member_detected` |
| HH:MM:SS.mmm | Xms | TS | Sent `zigbee2mqtt/{LOCK}/set` → `TOGGLE` |
| HH:MM:SS.mmm | Xms | Z2M | `state: "ON"` published |
| HH:MM:SS.000 | 20,000ms | Z2M | `occupancy: false` published (MCU timeout) |

**Timings Summary:**
- **Touch → Z2M occupancy:true**: Zigbee latency (normal: <1s, delayed: >1s)
- Z2M → TS receive: MQTT/Lambda latency
- Detection start → Face match (N frames)
- Face match → TOGGLE sent
- TOGGLE → Lock ON
- **Total (touch → TOGGLE sent)**

**How to detect Zigbee delay:**
- Gap between `occupancy: true` and `occupancy: false` should be ~20 seconds
- If gap is significantly less (e.g., 5 seconds), there was a Zigbee delay
- Zigbee delay = `20 - (occupancy:false - occupancy:true)` seconds

**Example Calculation:**
```
occupancy:false = 18:15:17
occupancy:true  = 18:14:58
Gap = 19 seconds (normal, ~20s expected)
Actual touch = 18:15:17 - 20s = 18:14:57
Zigbee latency = 18:14:58 - 18:14:57 = 1 second
```

## Test Scenarios

### Test 3: Sensor-Only Camera (Normal)
- Trigger: Touch occupancy sensor on lock (e.g., DC006)
- Expected: Face detected within 1-3 seconds, lock unlocks

### Test 3: Delayed Unlock (Issue Investigation)
- Same trigger as above
- Symptom: Lock takes much longer than expected to unlock
- Purpose: Capture logs to identify bottleneck

## Discovered Issue: Zigbee Network Delay

**Date Discovered**: 2026-01-11

### Symptom
Sometimes the unlock process is delayed by several seconds (up to 15+ seconds).

### Evidence

**Normal Test (no delay):**
```
[2026-01-11 17:34:24] occupancy: true
[2026-01-11 17:34:28] state: ON
[2026-01-11 17:34:43] occupancy: false   ← 19 seconds after true (expected ~20s)
```

**Delayed Test:**
```
[2026-01-11 17:36:19] occupancy: true
[2026-01-11 17:36:20] state: ON
[2026-01-11 17:36:24] occupancy: false   ← Only 5 seconds after true!
```

### Analysis
- MCU sends `occupancy: false` exactly 20 seconds after physical touch
- In delayed test, gap was only 5 seconds → **15 seconds of Zigbee delay**
- Actual touch time: 17:36:24 - 20s = **17:36:04**
- `occupancy: true` arrived at z2m: **17:36:19**
- **Zigbee network delayed the message by ~15 seconds**

### Root Cause
The delay occurs in the Zigbee network layer, BEFORE the message reaches Zigbee2MQTT. Possible causes:
- Zigbee mesh routing delays
- Coordinator queue/busy
- RF interference
- Device communication retries
- Note: `linkquality: 255` suggests good signal strength, so likely not RF

### Impact
- User touches sensor but nothing happens for several seconds
- By the time system responds, user may have walked away
- Face detection starts late, may miss the user's face

### Investigation Commands
```bash
# Monitor z2m logs
ssh demo "docker logs -f zigbee2mqtt"

# Check Zigbee network status
ssh demo "docker exec zigbee2mqtt cat /app/data/coordinator_backup.json"
```

### Potential Solutions (To Investigate)
1. Check Zigbee coordinator firmware
2. Reduce Zigbee network congestion (fewer devices polling)
3. Move coordinator closer to sensor
4. Check for RF interference sources
5. Consider direct Zigbee binding vs. coordinator routing

## Log Grep Patterns

```bash
# Z2M logs - key events
grep -E 'DC006|occupancy|state'

# TS/PY logs - key events for timeline
grep -E 'occupancy|handleLockTouchEvent|trigger_detection|detecting_txn|fullName|member_detected|unlock|TOGGLE|state.*ON'

# Errors
grep -E 'ERROR|Error|error|ECONNREFUSED'

# Specific camera
grep -E '192.168.22.5'

# Calculate Zigbee delay from log file
grep 'occupancy' z2m.log | grep DC006
# Then compare timestamps: (occupancy:false - occupancy:true) should be ~20s
```

## Related Files

- **TS Handler**: `ggp-func-ts-gocheckin/packages/src/handler.ts`
- **TS Assets Service**: `ggp-func-ts-gocheckin/packages/src/functions/assets/assets.service.ts`
- **PY Handler**: `ggp-func-py-gocheckin/py_handler.py`
- **Face Recognition**: `ggp-func-py-gocheckin/face_recognition.py`

## SSH Alias

Ensure SSH config has:
```
Host demo
    HostName 192.168.22.2
    User pi
```

## Known Issues

- **OOM Kill**: Python Lambda may be killed by Linux OOM killer after ~4 hours
  - See: `doc/OOM_MEMORY_LEAK_ISSUE.md`
- **Race Condition**: If OOM kill happens during shadow deployment, `/recognise` fails
  - Workaround: Re-deploy after Python Lambda recovers
