# Timer Extension Scenarios

This document describes detection triggering and timer extension based on camera-lock configurations.

## Camera-Lock Configurations

Cameras can have different types of locks associated:

| Config | Locks | ONVIF Trigger | Occupancy Trigger |
|--------|-------|---------------|-------------------|
| **Legacy-only** | All `withKeypad=false` | Allowed | Not possible (no sensors) |
| **Occupancy-only** | All `withKeypad=true` | **Skipped** (line 1369) | Allowed |
| **Mixed** | Both types | Allowed | Allowed |
| **No locks** | Empty | **Skipped** (no legacy) | Not possible |

### Code Reference: ONVIF Skip Check (py_handler.py:1365-1371)

```python
if lock_asset_id is None:  # ONVIF trigger
    camera_locks = camera_item.get('locks', {})
    has_legacy = any(not lock.get('withKeypad', False) for lock in camera_locks.values())
    if not has_legacy:
        logger.info('trigger_face_detection - skipping ONVIF trigger, no legacy locks')
        return
```

---

## Trigger Combinations by Camera Type

### 1. Legacy-Only Camera

```
Example: Camera 192.168.22.4
Locks: MAG001 (withKeypad=false)
```

| First | Second | Possible? | Extends? | Reason |
|-------|--------|-----------|----------|--------|
| ONVIF | - | Yes | N/A | Normal detection |
| ONVIF | ONVIF | Yes | **NO** | ONVIF doesn't extend ONVIF |
| Occupancy | - | **NO** | - | No keypad sensors |

**Result:** Only ONVIF triggers, no timer extension possible.

---

### 2. Occupancy-Only Camera

```
Example: Camera 192.168.22.5 (if all locks are keypads)
Locks: DC001 (withKeypad=true), DC006 (withKeypad=true)
```

| First | Second | Possible? | Extends? | Reason |
|-------|--------|-----------|----------|--------|
| ONVIF | - | **NO** | - | Skipped (no legacy locks) |
| Occupancy | - | Yes | N/A | Normal detection |
| Occupancy | Occupancy | Yes | **YES** | Deliberate actions extend |

**Result:** Only occupancy triggers, timer extends on additional occupancy.

---

### 3. Mixed Camera (Both Lock Types)

```
Example: Camera 192.168.22.3
Locks: MAG001 (withKeypad=false), DC001 (withKeypad=true)
```

| First | Second | Possible? | Extends? | Reason |
|-------|--------|-----------|----------|--------|
| ONVIF | - | Yes | N/A | Has legacy locks |
| ONVIF | ONVIF | Yes | **NO** | ONVIF doesn't extend |
| ONVIF | Occupancy | Yes | **YES** | Deliberate action extends |
| Occupancy | - | Yes | N/A | Has keypad sensors |
| Occupancy | ONVIF | Yes | **NO** | ONVIF doesn't extend |
| Occupancy | Occupancy | Yes | **YES** | Deliberate actions extend |

**Result:** All combinations possible. Only occupancy as second trigger extends.

---

### 4. No Locks Camera

```
Example: Camera with empty locks dict
Locks: {}
```

| First | Second | Possible? | Extends? | Reason |
|-------|--------|-----------|----------|--------|
| ONVIF | - | **NO** | - | Skipped (no legacy locks) |
| Occupancy | - | **NO** | - | No sensors to trigger |

**Result:** No detection triggering possible.

---

## Timer Extension Logic

### Code Reference (py_handler.py:1421-1430)

```python
if thread_gstreamer.is_feeding:
    if lock_asset_id is not None:
        # Occupancy trigger - extend timer
        thread_gstreamer.extend_timer(int(os.environ['TIMER_DETECT']))
        logger.info('occupancy trigger, timer extended')
    else:
        # ONVIF trigger - do NOT extend
        logger.info('ONVIF trigger, timer NOT extended')
    return  # Context merged, detection continues
```

### Extension Rules Summary

| Second Trigger | Extends Timer? | Reason |
|----------------|----------------|--------|
| Occupancy | **YES** | Deliberate user action |
| ONVIF | **NO** | Passive, can trigger constantly |

---

## Complete Scenario Matrix (Two Triggers)

| # | Camera Type | First | Second | Extends? |
|---|-------------|-------|--------|----------|
| 1 | Legacy-only | ONVIF | ONVIF | NO |
| 2 | Occupancy-only | Occupancy | Occupancy | **YES** |
| 3 | Mixed | ONVIF | ONVIF | NO |
| 4 | Mixed | ONVIF | Occupancy | **YES** |
| 5 | Mixed | Occupancy | ONVIF | NO |
| 6 | Mixed | Occupancy | Occupancy | **YES** |

---

## Timer and Frame Limit

### The Problem (Bug Found & Fixed 2026-01-24)

When extending the timer, two things must be updated:

1. **Timer**: When `stop_feeding()` is called
2. **Frame Limit**: How many frames can be pushed to decoder

```python
# Frame limit check in on_new_sample (line 259)
if self.feeding_count > self.framerate * self.running_seconds:
    return  # Skip pushing frame
```

### Before Fix (BUGGY)

`extend_timer()` created new Timer but did NOT update `running_seconds`:

```
T+0s    ONVIF → running_seconds=10, frame_limit=100
T+4s    Occupancy → Timer expires at T+14s
        BUT running_seconds still 10, frame_limit still 100!
T+10s   Frame 100 reached → no more frames pushed
T+14s   Timer expires, but 4 seconds wasted with no face detection
```

### After Fix (CORRECT)

`extend_timer()` now updates `running_seconds` = elapsed + new_duration:

```
T+0s    ONVIF → running_seconds=10, frame_limit=100
T+4s    Occupancy → Timer expires at T+14s
        elapsed=4s, running_seconds=4+10=14, frame_limit=140
T+10s   Frame 100 → continues (limit is 140)
T+14s   Timer expires, ~140 frames processed
```

### Fix Applied (gstreamer_threading.py:667-672)

```python
with self.detecting_lock:
    elapsed_seconds = self.feeding_count / self.framerate
    old_running_seconds = self.running_seconds
    self.running_seconds = elapsed_seconds + running_seconds
    logger.info(f"running_seconds: {old_running_seconds} -> {self.running_seconds:.1f}")
```

---

## Real-World User Flow

### Typical Mixed Camera Scenario

```
User approaches door with keypad lock (DC001) and legacy lock (MAG001)

Timeline:
├── T+0s   User walking toward door
│          ONVIF detects motion → detection starts
│          Timer: 10s, Frame limit: 100
│
├── T+4s   User touches keypad (DC001)
│          Occupancy:true → extend_timer(10)
│          Timer: expires at T+14s
│          Frame limit: 140 (elapsed 4s + 10s = 14s)
│
├── T+6s   User positions face for camera
│
├── T+8s   Face detected and matched!
│          → MAG001 unlocked (ONVIF triggered)
│          → DC001 unlocked (occupancy triggered)
│
└── Result: Both locks unlocked, user enters
```

---

## Testing Checklist

### Scenario 2: Occupancy → Occupancy (Occupancy-only camera, EXTENDS)

| Step | Action | Expected |
|------|--------|----------|
| 1 | Occupancy:true on DC001 | Detection starts |
| 2 | Wait 8s | Timer at 2s remaining |
| 3 | Occupancy:true on DC006 | `extend_timer`, `running_seconds` updated |
| 4 | Show face at T+12s | Face detected (within extended window) |
| 5 | Check | Both DC001 and DC006 unlocked |

### Scenario 4: ONVIF → Occupancy (Mixed camera, EXTENDS)

| Step | Action | Expected |
|------|--------|----------|
| 1 | ONVIF motion | Detection starts |
| 2 | Wait 4s | |
| 3 | Occupancy:true on DC001 | `extend_timer`, `running_seconds: 10 -> 14` |
| 4 | Show face at T+12s | Face detected (original would have expired at T+10) |
| 5 | Check | Both MAG001 and DC001 unlocked |

### Scenario 5: Occupancy → ONVIF (Mixed camera, NO extend)

| Step | Action | Expected |
|------|--------|----------|
| 1 | Occupancy:true on DC001 | Detection starts |
| 2 | Wait 8s | Timer at 2s remaining |
| 3 | ONVIF motion | `ONVIF trigger, timer NOT extended` |
| 4 | Wait 2s, no face | Timer expires at T+10s |
| 5 | Check | Detection stopped, no unlock |

---

## Related Files

| File | Location | Description |
|------|----------|-------------|
| `py_handler.py` | 1365-1371 | ONVIF skip check (no legacy locks) |
| `py_handler.py` | 1421-1430 | Timer extension decision |
| `gstreamer_threading.py` | 622-647 | `feed_detecting()` - starts timer |
| `gstreamer_threading.py` | 650-680 | `extend_timer()` - extends timer |
| `gstreamer_threading.py` | 259 | Frame limit check |

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-24 | Created document with all camera-lock configurations |
| 2026-01-24 | Identified and fixed frame limit bug in extend_timer |
