# Lock-Triggered Face Detection (Python)

## Overview

Receives `trigger_detection` / `stop_detection` from TypeScript component to start/stop face detection. For TypeScript implementation, see [lock_occupancy_handler.md](../../ggp-func-ts-gocheckin/doc/lock_occupancy_handler.md).

## Implementation Status

| Component | Status | Notes |
|----------|--------|-------|
| `trigger_lock_context` global | ✅ DONE | `py_handler.py:99-101` |
| `trigger_face_detection()` | ✅ DONE | `py_handler.py:1317` - accepts `lock_asset_id`, merges context |
| `handle_occupancy_false()` | ✅ DONE | `py_handler.py:1377` - stops detection early if no other triggers |
| MQTT handlers | ✅ DONE | `py_handler.py:164-175` - both `trigger_detection` and `stop_detection` |
| `member_detected` payload | ✅ DONE | `py_handler.py:991-1004` - adds `onvifTriggered`, `occupancyTriggeredLocks` |
| `handle_notification()` | ✅ DONE | `py_handler.py:1309-1312` - calls `trigger_face_detection(cam_ip, None)` |
| `function.conf` | ✅ DONE | Both topics in inputTopics |
| Timer extension | ✅ DONE | `py_handler.py:1403-1423` - extend on occupancy (always) or ONVIF (if started by ONVIF) |
| `extend_timer()` in GStreamer | ✅ DONE | `gstreamer_threading.py:639-661` - new method |
| `started_by_onvif` in context | ✅ DONE | `py_handler.py:1368` - tracks initial trigger source |

---

## Flow Diagrams

### Occupancy-Triggered (Sensor-Enabled Locks)
```
gocheckin/trigger_detection { cam_ip, lock_asset_id }
    → trigger_face_detection(cam_ip, lock_asset_id)
    → merge context, feed_detecting()
    → face matched → member_detected { onvifTriggered: false, occupancyTriggeredLocks: [lock_id] }
    → TS unlocks specific lock only
```

### ONVIF-Triggered (Legacy Locks)
```
ONVIF motion → handle_notification()
    → trigger_face_detection(cam_ip, None)
    → check: has legacy locks? NO → SKIP detection
    → check: has legacy locks? YES → feed_detecting()
    → face matched → member_detected { onvifTriggered: true, occupancyTriggeredLocks: [] }
    → TS unlocks legacy locks only (category !== KEYPAD_LOCK)
```

---

## Lock Context Data Structure

```python
trigger_lock_context = {
    cam_ip: {
        'started_by_onvif': True/False,  # Boolean: True if INITIALLY started by ONVIF, set once
        'onvif_triggered': True/False,   # Boolean: True if ONVIF motion triggered (for unlock)
        'specific_locks': set(),         # Lock IDs to unlock (removed on occupancy:false)
        'active_occupancy': set()        # Locks with active occupancy sensor
    }
}
```

**Field Details:**
- `started_by_onvif`: Boolean, set to `(lock_asset_id is None)` when context is first created. Never changes after initial set. Used for timer extension logic.
- `onvif_triggered`: Boolean, can be set/merged later when ONVIF event arrives. Used for unlock logic.

## Lock Sensor Flag (`withKeypad`)

- `withKeypad: true` → has occupancy sensor, requires occupancy trigger
- `withKeypad: false` or missing → legacy lock, unlocks via ONVIF motion

**Note:** `camera_item['locks']` is enriched with `withKeypad` by the TypeScript component when processing shadow updates.

---

## MQTT Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `gocheckin/trigger_detection` | Input | `{ "cam_ip": "...", "lock_asset_id": "..." }` |
| `gocheckin/stop_detection` | Input | `{ "cam_ip": "...", "lock_asset_id": "..." }` |
| `gocheckin/member_detected` | Output | `{ ..., "onvifTriggered": bool, "occupancyTriggeredLocks": [...] }` |

---

## Scenarios

### Scenario 1: Camera with ONLY legacy locks (no sensors)
```
Locks: [Lock A (LOCK), Lock B (LOCK)]

ONVIF motion
    → triggers detection (lock_asset_id = None)
    → context = { onvif_triggered: true, specific_locks: [] }
    → member_detected
    → TS unlocks Lock A, Lock B (all legacy)
```

### Scenario 2: Camera with ONLY sensor-enabled locks
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (KEYPAD_LOCK)]

ONVIF motion
    → check: any legacy locks? NO (all KEYPAD_LOCK)
    → SKIP detection entirely (saves CPU)
    → NO member_detected, NO unlock

Occupancy from Lock A
    → triggers detection (lock_asset_id = Lock A)
    → context = { onvif_triggered: false, specific_locks: [Lock A] }
    → member_detected
    → TS unlocks Lock A (only this one)
```

### Scenario 3: Camera with MIXED locks
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK), Lock C (LOCK)]

ONVIF motion
    → triggers detection (lock_asset_id = None)
    → context = { onvif_triggered: true, specific_locks: [] }
    → member_detected
    → TS unlocks Lock B, Lock C (legacy)
    → TS skips Lock A (KEYPAD_LOCK, wait for occupancy)

Occupancy from Lock A
    → triggers detection (lock_asset_id = Lock A)
    → context = { onvif_triggered: false, specific_locks: [Lock A] }
    → member_detected
    → TS unlocks Lock A (only this one)
```

### Scenario 4: Both triggers arrive (ONVIF first, then occupancy)
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

ONVIF motion arrives first
    → triggers detection
    → context = { onvif_triggered: true, specific_locks: [] }
    → detection running (is_feeding = True)

Occupancy from Lock A arrives while detecting
    → context MERGED: { onvif_triggered: true, specific_locks: [Lock A] }
    → detection continues (feed_detecting returns early)

member_detected (with merged context)
    → TS unlocks Lock B (legacy, because onvif_triggered=true)
    → TS unlocks Lock A (specific, because it's in occupancyTriggeredLocks)
```

### Scenario 5: Occupancy first, then ONVIF (reverse race)
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

Occupancy from Lock A arrives first
    → triggers detection
    → context = { onvif_triggered: false, specific_locks: [Lock A] }
    → detection running (is_feeding = True)

ONVIF motion arrives while detecting
    → context MERGED: { onvif_triggered: true, specific_locks: [Lock A] }
    → detection continues (feed_detecting returns early)

member_detected (with merged context)
    → TS unlocks Lock A (specific)
    → TS unlocks Lock B (legacy, because onvif_triggered=true)
```

---

## Occupancy:false Scenarios

### Scenario 6: Sensor-only - face detected before occupancy:false
```
Locks: [Lock A (KEYPAD_LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { onvif_triggered: false, specific_locks: [A], active_occupancy: [A] }

Face detected!
    → member_detected { occupancyTriggeredLocks: [A], onvifTriggered: false }
    → TS unlocks Lock A
    → clear context

occupancy:false from Lock A (after unlock)
    → context already cleared, IGNORED
```

### Scenario 7: Sensor-only - no face, occupancy:false stops detection
```
Locks: [Lock A (KEYPAD_LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { specific_locks: [A], active_occupancy: [A] }
    → detection running...

occupancy:false from Lock A (no face detected)
    → remove A from specific_locks → []
    → remove A from active_occupancy → []
    → check: active_occupancy empty? YES
    → check: has legacy locks? NO (only KEYPAD_LOCK)
    → STOP detection (call stop_feeding())
    → clear context
    → NO member_detected published, NO unlock
```

### Scenario 8: Multiple occupancy - one leaves, one stays
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (KEYPAD_LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { specific_locks: [A], active_occupancy: [A] }

occupancy:true from Lock B (while detecting)
    → MERGE: specific_locks: [A, B], active_occupancy: [A, B]

occupancy:false from Lock A
    → remove A from specific_locks → [B]
    → remove A from active_occupancy → [B]
    → check: active_occupancy empty? NO (B still active)
    → CONTINUE detection

Face detected!
    → member_detected { occupancyTriggeredLocks: [B], onvifTriggered: false }
    → TS unlocks Lock B (A was removed, not unlocked)
```

### Scenario 9: Mixed locks - occupancy only, then occupancy:false, no face
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { specific_locks: [A], active_occupancy: [A] }

occupancy:false from Lock A (no face detected yet)
    → remove A from specific_locks → []
    → remove A from active_occupancy → []
    → check: active_occupancy empty? YES
    → check: has legacy locks? YES (Lock B is LOCK)
    → CONTINUE detection (wait for ONVIF or timeout)

No face detected, TIMER_DETECT expires
    → detection stops naturally
    → NO member_detected, NO unlock
```

> **Note:** Detection continues because legacy Lock B exists - someone might trigger ONVIF motion before timeout.

### Scenario 10: Mixed locks - ONVIF + occupancy, occupancy leaves
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

ONVIF motion
    → START detection
    → context = { onvif_triggered: true, specific_locks: [], active_occupancy: [] }

occupancy:true from Lock A (while detecting)
    → MERGE: onvif_triggered: true, specific_locks: [A], active_occupancy: [A]

occupancy:false from Lock A (before face detected)
    → remove A from specific_locks → []
    → remove A from active_occupancy → []
    → check: active_occupancy empty? YES
    → check: has legacy locks? YES (Lock B is LOCK)
    → CONTINUE detection

Face detected!
    → member_detected { occupancyTriggeredLocks: [], onvifTriggered: true }
    → TS unlocks Lock B (legacy, because onvifTriggered=true)
    → Lock A NOT unlocked (removed from occupancyTriggeredLocks)
```

### Scenario 11: Occupancy first, ONVIF joins, occupancy leaves
```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { onvif_triggered: false, specific_locks: [A], active_occupancy: [A] }

ONVIF motion (while detecting)
    → MERGE: onvif_triggered: true, specific_locks: [A], active_occupancy: [A]

occupancy:false from Lock A
    → remove A from specific_locks → []
    → remove A from active_occupancy → []
    → check: active_occupancy empty? YES
    → check: has legacy locks? YES (Lock B is LOCK)
    → CONTINUE detection

Face detected!
    → member_detected { occupancyTriggeredLocks: [], onvifTriggered: true }
    → TS unlocks Lock B (legacy, because onvifTriggered=true)
    → Lock A NOT unlocked (guest left)
```

### Scenario 12: Detection timeout (TIMER_DETECT expires naturally)
```
Locks: [Lock A (KEYPAD_LOCK)]

occupancy:true from Lock A
    → START detection
    → context = { specific_locks: [A], active_occupancy: [A] }

(No face detected, no occupancy:false received)

TIMER_DETECT expires
    → stop_feeding() called by timer
    → detection stops naturally
    → context remains (orphaned)

occupancy:false from Lock A (after timeout)
    → remove A from active_occupancy
    → detection not running (is_feeding=false)
    → clear orphaned context
```

---

## Stop Detection Conditions

| Condition | Stop? | Reason |
|-----------|-------|--------|
| `active_occupancy` empty + no legacy locks | YES | All guests left, no legacy locks |
| `active_occupancy` empty + has legacy locks | NO | Face match could unlock legacy |
| `active_occupancy` NOT empty | NO | Other occupancy sensors active |
| TIMER_DETECT expires | YES | Natural timeout |
| Face detected | YES | Goal achieved |

---

## Timer Extension Scenarios

### Current Behavior (No Extension)

When `feed_detecting()` is called while already feeding, it returns early without resetting the timer:
```python
if self.is_feeding:
    return  # Timer NOT extended
```

### Problem: Second Occupancy Trigger Gets Short Window

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (KEYPAD_LOCK)]

t=0: occupancy:true from Lock A
    → START detection, timer set for t=10
    → context = { specific_locks: [A], active_occupancy: [A] }

t=8: occupancy:true from Lock B
    → context MERGED: { specific_locks: [A, B], active_occupancy: [A, B] }
    → feed_detecting() returns early (already feeding)
    → Timer still expires at t=10 ⚠️
    → Lock B person only has 2 seconds for face detection!

t=10: TIMER_DETECT expires
    → Detection stops
    → If no face detected, neither lock unlocks
```

### Timer Extension Rules

Extension depends on both the **initial trigger** and the **new trigger**:

| Initial Trigger | New Trigger | Extend? | Reason |
|-----------------|-------------|---------|--------|
| Occupancy | Occupancy | ✅ YES | Another sensor person waiting |
| Occupancy | ONVIF | ❌ NO | Occupancy person is priority |
| ONVIF | Occupancy | ✅ YES | Sensor person waiting |
| ONVIF | ONVIF | ✅ YES | Legacy lock person still waiting |

**Summary**:
- Occupancy trigger always extends (new person at sensor lock)
- ONVIF trigger only extends if detection was initially started by ONVIF

### Scenario 13: Multiple occupancy triggers - timer extended

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (KEYPAD_LOCK)]

t=0: occupancy:true from Lock A
    → START detection, timer set for t=10
    → context = { specific_locks: [A], active_occupancy: [A] }

t=8: occupancy:true from Lock B
    → context MERGED: { specific_locks: [A, B], active_occupancy: [A, B] }
    → Timer EXTENDED to t=18 ✓ (lock_asset_id is NOT None)

t=15: Face detected
    → member_detected { occupancyTriggeredLocks: [A, B] }
    → Both Lock A and Lock B unlocked ✓
```

### Scenario 14: Occupancy first, ONVIF joins - timer NOT extended

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

t=0: occupancy:true from Lock A
    → START detection, timer set for t=10
    → context = { onvif_triggered: false, specific_locks: [A], active_occupancy: [A] }

t=8: ONVIF motion arrives
    → context MERGED: { onvif_triggered: true, specific_locks: [A], active_occupancy: [A] }
    → Timer NOT extended (lock_asset_id is None)
    → Timer still expires at t=10

t=10: TIMER_DETECT expires (no face detected)
    → Detection stops
    → Neither lock unlocks
```

### Scenario 15: ONVIF first, occupancy joins - timer extended

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK)]

t=0: ONVIF motion
    → START detection, timer set for t=10
    → context = { onvif_triggered: true, specific_locks: [], active_occupancy: [] }

t=8: occupancy:true from Lock A
    → context MERGED: { onvif_triggered: true, specific_locks: [A], active_occupancy: [A] }
    → Timer EXTENDED to t=18 ✓ (lock_asset_id is NOT None)

t=15: Face detected
    → member_detected { occupancyTriggeredLocks: [A], onvifTriggered: true }
    → Lock A unlocked (specific)
    → Lock B unlocked (legacy)
```

### Scenario 16: Three occupancy triggers in sequence

```
Locks: [Lock A, Lock B, Lock C] (all KEYPAD_LOCK)

t=0: occupancy:true from Lock A
    → START detection, timer → t=10
    → context = { started_by_onvif: false, specific_locks: [A], active_occupancy: [A] }

t=5: occupancy:true from Lock B
    → context MERGED: { specific_locks: [A, B], active_occupancy: [A, B] }
    → Timer EXTENDED → t=15 (occupancy always extends)

t=12: occupancy:true from Lock C
    → context MERGED: { specific_locks: [A, B, C], active_occupancy: [A, B, C] }
    → Timer EXTENDED → t=22 (occupancy always extends)

t=20: Face detected
    → member_detected { occupancyTriggeredLocks: [A, B, C] }
    → All three locks unlocked ✓
```

### Scenario 17: ONVIF first, another ONVIF - timer extended

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK - legacy)]

t=0: ONVIF motion (person at legacy Lock B)
    → START detection, timer → t=10
    → context = { started_by_onvif: true, onvif_triggered: true, specific_locks: [] }

t=8: Another ONVIF motion (same person still waiting)
    → context unchanged (already onvif_triggered: true)
    → Timer EXTENDED → t=18 (ONVIF extends because started_by_onvif=true)

t=15: Face detected
    → member_detected { occupancyTriggeredLocks: [], onvifTriggered: true }
    → Lock B unlocked (legacy) ✓
```

### Scenario 18: Occupancy first, ONVIF joins - timer NOT extended

```
Locks: [Lock A (KEYPAD_LOCK), Lock B (LOCK - legacy)]

t=0: occupancy:true from Lock A
    → START detection, timer → t=10
    → context = { started_by_onvif: false, onvif_triggered: false, specific_locks: [A] }

t=8: ONVIF motion arrives
    → context MERGED: { started_by_onvif: false, onvif_triggered: true, specific_locks: [A] }
    → Timer NOT extended (started_by_onvif=false, ONVIF can't extend)
    → Timer still expires at t=10

t=9: Face detected
    → member_detected { occupancyTriggeredLocks: [A], onvifTriggered: true }
    → Lock A unlocked (specific) ✓
    → Lock B unlocked (legacy) ✓
```

### Implementation Note

Timer extension should happen in `trigger_face_detection()` (Python), NOT in `feed_detecting()` (GStreamer), because:
1. `trigger_face_detection()` knows the `lock_asset_id` value
2. `trigger_face_detection()` has access to `started_by_onvif` context
3. `feed_detecting()` doesn't have context about trigger source

```python
def trigger_face_detection(cam_ip, lock_asset_id=None):
    global trigger_lock_context

    is_new_detection = cam_ip not in trigger_lock_context

    # Initialize context for new detection
    if is_new_detection:
        trigger_lock_context[cam_ip] = {
            'started_by_onvif': (lock_asset_id is None),  # Set once, never changes
            'onvif_triggered': False,
            'specific_locks': set(),
            'active_occupancy': set()
        }

    context = trigger_lock_context[cam_ip]

    # Merge trigger info
    if lock_asset_id is None:
        context['onvif_triggered'] = True
    else:
        context['specific_locks'].add(lock_asset_id)
        context['active_occupancy'].add(lock_asset_id)

    # ... validation checks ...

    # Handle timer extension if already detecting
    if thread_gstreamer.is_feeding:
        should_extend = False

        if lock_asset_id is not None:
            # Occupancy trigger - ALWAYS extend
            should_extend = True
        elif context['started_by_onvif']:
            # ONVIF trigger - only extend if detection started by ONVIF
            should_extend = True

        if should_extend:
            thread_gstreamer.extend_timer(TIMER_DETECT)

        return  # Context already merged, detection continues

    # Start new detection
    thread_gstreamer.feed_detecting(TIMER_DETECT)
```

**GStreamer needs new `extend_timer()` method:**
```python
def extend_timer(self, running_seconds):
    """Extend the detection timer without resetting detection state"""
    if self.feeding_timer is not None:
        self.feeding_timer.cancel()

    self.feeding_timer = threading.Timer(running_seconds, self.stop_feeding)
    self.feeding_timer.name = f"Thread-SamplingStopper-{self.cam_ip}"
    self.feeding_timer.start()
    logger.info(f"{self.cam_ip} extend_timer - timer extended to {running_seconds}s")
```

---

## Files Modified

| File | Changes |
|------|---------|
| `py_handler.py` | Added `trigger_lock_context` with `started_by_onvif`, updated `trigger_face_detection()` with timer extension logic, added `handle_occupancy_false()`, MQTT handlers, updated `member_detected` payload, updated `handle_notification()` |
| `gstreamer_threading.py` | Added `extend_timer()` method |
| `function.conf` | Added `stop_detection` to inputTopics |

---

## Related Documentation

- [TypeScript: Lock Occupancy Handler](../../ggp-func-ts-gocheckin/doc/lock_occupancy_handler.md)
- [TypeScript: Bidirectional Lock-Camera](../../ggp-func-ts-gocheckin/doc/bidirectional_lock_camera.md)
