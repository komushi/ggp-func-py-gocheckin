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

### Test 6: Mixed Camera - ONVIF First, Then Occupancy (Scenario 4)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts |
| 2 | **Immediately** trigger occupancy:true on DC001 | Context merged |
| 3 | Stand in front of camera, wait for face match | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: ["0x1051dbfffe1844e0"]` |
| 4 | Check unlock | **BOTH** MAG001 AND DC001 unlocked |

---

### Test 7: Mixed Camera - Occupancy First, Then ONVIF (Scenario 5)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts |
| 2 | **Immediately** trigger ONVIF motion on 192.168.22.3 | Context merged |
| 3 | Stand in front of camera, wait for face match | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: ["0x1051dbfffe1844e0"]` |
| 4 | Check unlock | **BOTH** MAG001 AND DC001 unlocked |

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

### Test 10: Mixed - Occupancy Leaves, Legacy Continues (Scenario 10)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion on 192.168.22.3 | Detection starts |
| 2 | Trigger occupancy:true on DC001 | Context merged |
| 3 | Trigger occupancy:false on DC001 | DC001 removed, detection CONTINUES (has legacy) |
| 4 | Show face, wait for match | `member_detected` with `onvifTriggered: true`, `occupancyTriggeredLocks: []` |
| 5 | Check unlock | MAG001 unlocked, DC001 NOT unlocked (guest left) |

---

### Test 11: Timer Extension - Occupancy + Occupancy (Scenario 13)
**Camera:** 192.168.22.5 (Hikvision) - **Enable isDetecting first!**
**Locks:** DC001 (sensor), DC006 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, timer=10s |
| 2 | Wait 8 seconds | Timer at 2s remaining |
| 3 | Trigger occupancy:true on DC006 | Log: `extend_timer`, timer reset to 10s |
| 4 | Wait 5 seconds, show face | Face detected within extended window |
| 5 | Check | Both DC001 and DC006 unlocked |

---

### Test 12: Timer NOT Extended - Occupancy First, ONVIF Joins (Scenario 14/18)
**Camera:** 192.168.22.3 (Dahua)
**Locks:** MAG001 (legacy), DC001 (sensor)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger occupancy:true on DC001 | Detection starts, timer=10s, `started_by_onvif: false` |
| 2 | Wait 8 seconds | Timer at 2s remaining |
| 3 | Trigger ONVIF motion | Context merged, timer NOT extended |
| 4 | Show face within 2s | If matched: both unlocked |
| 5 | Or wait 2s, no face | Timer expires, detection stops, NO unlock |

---

### Test 13: Timer Extended - ONVIF First, ONVIF Again (Scenario 17)
**Camera:** 192.168.22.4 (Dahua)
**Locks:** MAG001 (legacy)

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Trigger ONVIF motion | Detection starts, `started_by_onvif: true` |
| 2 | Wait 8 seconds | Timer at 2s remaining |
| 3 | Trigger another ONVIF motion | Log: `extend_timer`, timer reset |
| 4 | Show face in extended window | MAG001 unlocked |

---

## Test Execution Checklist

| Test | Scenario | Camera | Status | Notes |
|------|----------|--------|--------|-------|
| 1 | Legacy-only ONVIF | 192.168.22.4 | | |
| 2 | Sensor-only ONVIF skip | 192.168.22.5 | | |
| 3 | Sensor-only occupancy | 192.168.22.5 | | |
| 4 | Mixed ONVIF only | 192.168.22.3 | | |
| 5 | Mixed occupancy only | 192.168.22.3 | | |
| 6 | Mixed ONVIF->occupancy | 192.168.22.3 | | |
| 7 | Mixed occupancy->ONVIF | 192.168.22.3 | | |
| 8 | Occupancy false no face | 192.168.22.5 | | |
| 9 | Multi-occupancy one leaves | 192.168.22.5 | | |
| 10 | Mixed occupancy leaves | 192.168.22.3 | | |
| 11 | Timer extend occ+occ | 192.168.22.5 | | |
| 12 | Timer NO extend occ->ONVIF | 192.168.22.3 | | |
| 13 | Timer extend ONVIF->ONVIF | 192.168.22.4 | | |

---

## Related Documentation

- [Lock-Triggered Detection Design](./lock_triggered_detection.md)
- [ONVIF Notifications](./onvif_notifications.md)
