# Test Plan: Lock-Triggered Face Detection

## Current Camera-Lock Associations

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          CURRENT SETUP                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐        │
│  │ 192.168.22.3    │     │ 192.168.22.4    │     │ 192.168.22.5    │        │
│  │ Dahua           │     │ Dahua           │     │ Hikvision       │        │
│  │ isDetecting:YES │     │ isDetecting:YES │     │ isDetecting:NO  │        │
│  │ MIXED           │     │ LEGACY-ONLY     │     │ SENSOR-ONLY     │        │
│  └────────┬────────┘     └────────┬────────┘     └────────┬────────┘        │
│           │                       │                       │                  │
│     ┌─────┴─────┐                 │              ┌────────┴────────┐        │
│     │           │                 │              │                 │        │
│     ▼           ▼                 ▼              ▼                 ▼        │
│ ┌───────┐  ┌───────┐         ┌───────┐     ┌───────┐         ┌───────┐     │
│ │MAG001 │  │DC001  │         │MAG001 │     │DC001  │         │DC006  │     │
│ │LOCK   │  │KEYPAD │         │LOCK   │     │KEYPAD │         │KEYPAD │     │
│ │legacy │  │sensor │         │legacy │     │sensor │         │sensor │     │
│ └───────┘  └───────┘         └───────┘     └───────┘         └───────┘     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Summary Table

| Camera | IP | isDetecting | Locks | Type |
|--------|-----|-------------|-------|------|
| Dahua | 192.168.22.3 | YES | MAG001 (legacy), DC001 (sensor) | **MIXED** |
| Dahua | 192.168.22.4 | YES | MAG001 (legacy) | **LEGACY-ONLY** |
| Hikvision | 192.168.22.5 | NO | DC001 (sensor), DC006 (sensor) | **SENSOR-ONLY** |

| Lock | AssetId | Category | withKeypad | Cameras |
|------|---------|----------|------------|---------|
| MAG001 | 0xe4b323fffeb70268 | LOCK | false | .3, .4 |
| DC001 | 0x1051dbfffe1844e0 | KEYPAD_LOCK | true | .3, .5 |
| DC006 | 0x1051dbfffe182b18 | KEYPAD_LOCK | true | .5 |

---

## Prerequisites

- **Enable isDetecting on 192.168.22.5** for sensor-only tests (currently OFF)
- Have a registered guest face in the system

---

## Test Cases

### Test 1: Legacy-Only Camera (Scenario 1)
**Camera:** 192.168.22.4 (Dahua)
**Locks:** MAG001 (legacy)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.4 | Log: `ONVIF Motion detected... cam_ip=192.168.22.4` |
| 2 | Stand in front of camera | Face detection starts |
| 3 | Wait for face match | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: []` |
| 4 | Check unlock | MAG001 unlocked |

---

### Test 2: Sensor-Only Camera - ONVIF Skipped (Scenario 2)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC001 (sensor), DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.5 | Log: `SKIP detection` (no legacy locks) |
| 2 | Verify | NO `member_detected`, NO unlock |

---

### Test 3: Sensor-Only Camera - Occupancy Trigger (Scenario 2)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC006 | MQTT `gocheckin/trigger_detection` with `lock_asset_id=0x1051dbfffe182b18` |
| 2 | Stand in front of camera | Face detection starts |
| 3 | Wait for face match | `member_detected` with `onvifTriggered: false`, `occupancyTriggeredLocks: ["0x1051dbfffe182b18"]` |
| 4 | Check unlock | DC006 unlocked, DC001 NOT unlocked |

---

### Test 4: Mixed Camera - ONVIF Only (Scenario 3)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Log: `ONVIF Motion detected... cam_ip=192.168.22.3` |
| 2 | Stand in front of camera | Face detection starts |
| 3 | Wait for face match | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: []` |
| 4 | Check unlock | MAG001 unlocked, DC001 NOT unlocked |

---

### Test 5: Mixed Camera - Occupancy Only (Scenario 3)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 (for cam 192.168.22.3) | MQTT `gocheckin/trigger_detection` |
| 2 | Stand in front of camera | Face detection starts |
| 3 | Wait for face match | `member_detected` with `onvifTriggered: false`, `occupancyTriggeredLocks: ["0x1051dbfffe1844e0"]` |
| 4 | Check unlock | DC001 unlocked, MAG001 NOT unlocked |

---

### Test 6: Mixed Camera - ONVIF First, Then Occupancy (Timer EXTENDS)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts, `running_seconds=10` |
| 2 | Wait 4 seconds | ~40 frames processed |
| 3 | Trigger occupancy:true on DC001 | Log: `extend_timer`, `running_seconds: 10 -> 14` (elapsed 4s + 10s) |
| 4 | Wait until T+12s, show face | Face detected (original timer would have expired at T+10s) |
| 5 | Check unlock | **BOTH** MAG001 AND DC001 unlocked |

---

### Test 7: Mixed Camera - Occupancy First, Then ONVIF (Timer NOT extended, face matched)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, `running_seconds=10` |
| 2 | Wait 4 seconds | ~40 frames processed |
| 3 | Trigger ONVIF motion on 192.168.22.3 | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | Show face before T+10s | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: ["0x1051dbfffe1844e0"]` |
| 5 | Check unlock | **BOTH** MAG001 AND DC001 unlocked |

---

### Test 8: Occupancy False - No Face (Scenario 7)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC006 | Detection starts |
| 2 | **Do NOT show face** | Detection running... |
| 3 | Trigger occupancy:false on DC006 | `stop_detection` received |
| 4 | Check | Detection STOPPED, NO `member_detected`, NO unlock |

---

### Test 9: Multiple Occupancy - One Leaves (Scenario 8)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC001 (sensor), DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, `specific_locks: [DC001]` |
| 2 | Trigger occupancy:true on DC006 | Context merged, `specific_locks: [DC001, DC006]` |
| 3 | Trigger occupancy:false on DC001 | DC001 removed, `specific_locks: [DC006]`, detection CONTINUES |
| 4 | Show face, wait for match | `member_detected` with `occupancyTriggeredLocks: ["0x1051dbfffe182b18"]` (DC006 only) |
| 5 | Check unlock | DC006 unlocked, DC001 NOT unlocked |

---

### Test 10: Mixed Camera - No Face, Context Update (Scenario 10)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

**Similar to Test 8** but on mixed camera (ONVIF + occupancy contexts).

**What This Test Verifies**:
- `occupancyTriggeredLocks` is correctly updated when occupancy:false is received
- Detection stops when timer expires with no face shown
- Mixed context (ONVIF + occupancy) behaves correctly

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts, timer=10s |
| 2 | Trigger occupancy:true on DC001 | Context merged, timer extended to 10s |
| 3 | **Do NOT show face** | Detection running... |
| 4 | Wait for occupancy:false on DC001 (~10s) | Log: DC001 removed from `occupancyTriggeredLocks` |
| 5 | Check | Detection STOPPED, NO `member_detected`, NO unlock |

**Note**: Test 6 already covers "face shown while both contexts active" scenario.

---

### Test 11: Timer Extension - Occupancy + Occupancy (Sensor-only camera)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC001 (sensor), DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger occupancy:true on DC006 | Log: `extend_timer`, `running_seconds: 10 -> 18` (elapsed 8s + 10s) |
| 4 | Wait until T+15s, show face | Face detected (original timer would have expired at T+10s) |
| 5 | Check | Both DC001 and DC006 unlocked |

---

### Test 12: Timer NOT Extended - Occupancy → ONVIF (face matched)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | Show face within 2s (before T+10s) | Both MAG001 and DC001 unlocked |

---

### Test 13: Timer NOT Extended - ONVIF → ONVIF (Legacy-only, face matched)
**Camera:** 192.168.22.4 (Dahua)
**Locks:** MAG001 (legacy)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger another ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | Show face within 2s (before T+10s) | MAG001 unlocked |

---

### Test 14: Timer NOT Extended - ONVIF → ONVIF (Mixed, face matched)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger another ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | Show face within 2s (before T+10s) | MAG001 unlocked (DC001 NOT unlocked - no occupancy) |

**Timer Extension Rules**:
- Occupancy + Occupancy → **extend** (deliberate action)
- ONVIF + Occupancy → **extend** (deliberate action joins)
- Occupancy + ONVIF → **NO extend**
- ONVIF + ONVIF → **NO extend** (avoid indefinite detection)

---

### Test 15: Timer Extended - Mixed Camera Occupancy + Occupancy
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

**Note**: Current setup has only 1 keypad (DC001) on mixed camera. To test this scenario, either:
- Add another keypad lock to 192.168.22.3, OR
- Use the same keypad (DC001) with two occupancy:true events

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, timer=10s, `running_seconds=10` |
| 2 | Wait 8 seconds | Timer at 2s remaining, ~80 frames processed |
| 3 | Trigger occupancy:true on DC001 again | Log: `extend_timer`, `running_seconds: 10 -> 18` |
| 4 | Wait 5 seconds, show face | Face detected within extended window |
| 5 | Check | DC001 unlocked, MAG001 NOT unlocked |

**Alternative**: Accept Test 11 (Sensor-only Occ+Occ) as sufficient coverage since the extension logic is the same regardless of camera type.

---

### Test 16: Timer NOT Extended - Occupancy → ONVIF (timer expires, no face)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | **Do NOT show face**, wait 2s | Timer expires at T+10s |
| 5 | Check | Detection stops, NO `member_detected`, NO unlock |

---

### Test 17: Timer NOT Extended - ONVIF → ONVIF (Legacy-only, timer expires)
**Camera:** 192.168.22.4 (Dahua)
**Locks:** MAG001 (legacy)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger another ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | **Do NOT show face**, wait 2s | Timer expires at T+10s |
| 5 | Check | Detection stops, NO `member_detected`, NO unlock |

---

### Test 18: Timer NOT Extended - ONVIF → ONVIF (Mixed, timer expires)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts, `running_seconds=10` |
| 2 | Wait 8 seconds | ~80 frames processed |
| 3 | Trigger another ONVIF motion | Log: `ONVIF trigger, timer NOT extended`, `running_seconds` unchanged |
| 4 | **Do NOT show face**, wait 2s | Timer expires at T+10s |
| 5 | Check | Detection stops, NO `member_detected`, NO unlock |

---

### Test 19: Timer Extended - Verify detection continues past original timer
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

**Purpose**: Verify that after timer extension, face detection actually works between T+10s and T+14s (the extended window).

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts, `running_seconds=10` |
| 2 | Wait 4 seconds | ~40 frames processed |
| 3 | Trigger occupancy:true on DC001 | Log: `extend_timer`, `running_seconds: 10 -> 14` |
| 4 | **Do NOT show face**, wait until T+11s | Detection still running (past original T+10s) |
| 5 | Show face at T+12s | Face detected within extended window |
| 6 | Check | **BOTH** MAG001 AND DC001 unlocked |

---

## Test Execution Checklist

| Test | Scenario | Camera | Status | Notes |
|------|----------|--------|--------|-------|
| 1 | Legacy-only ONVIF | 192.168.22.4 | PASS | 2026-01-20 |
| 2 | Sensor-only ONVIF skip | 192.168.22.5 | PASS | 2026-01-20 |
| 3 | Sensor-only occupancy | 192.168.22.5 | PASS | 2026-01-20 |
| 4 | Mixed ONVIF only | 192.168.22.3 | PASS | 2026-01-20 |
| 5 | Mixed occupancy only | 192.168.22.3 | PASS | 2026-01-20 |
| 6 | Mixed ONVIF→Occ (extends, face matched) | 192.168.22.3 | | Retest with fix |
| 7 | Mixed Occ→ONVIF (no extend, face matched) | 192.168.22.3 | | Retest with fix |
| 8 | Occupancy false no face | 192.168.22.5 | | |
| 9 | Multi-occupancy one leaves | 192.168.22.5 | | |
| 10 | Mixed: no face, context update | 192.168.22.3 | | |
| 11 | Timer extend Occ+Occ (sensor-only) | 192.168.22.5 | | |
| 12 | Timer NO extend Occ→ONVIF (face matched) | 192.168.22.3 | | |
| 13 | Timer NO extend ONVIF→ONVIF (legacy, face) | 192.168.22.4 | | |
| 14 | Timer NO extend ONVIF→ONVIF (mixed, face) | 192.168.22.3 | | |
| 15 | Timer extend Occ+Occ (mixed) | 192.168.22.3 | | |
| 16 | Timer NO extend Occ→ONVIF (expires) | 192.168.22.3 | | |
| 17 | Timer NO extend ONVIF→ONVIF (legacy, expires) | 192.168.22.4 | | |
| 18 | Timer NO extend ONVIF→ONVIF (mixed, expires) | 192.168.22.3 | | |
| 19 | Timer extended, face at T+12s | 192.168.22.3 | | Verify extended window works |

---

## Related Documentation

- [Lock-Triggered Detection Design](./lock_triggered_detection.md)
- [ONVIF Notifications](./onvif_notifications.md)
