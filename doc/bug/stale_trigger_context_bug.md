# Stale Trigger Context Bug (`onvifTriggered=True` When ONVIF Disabled)

## Bug Summary

The `trigger_lock_context` dictionary persists after detection ends without a face match, causing subsequent triggers to reuse stale context data. This results in incorrect `onvifTriggered` values in `member_detected` events.

## Symptom

`member_detected` payload shows `onvifTriggered=True` even when:
- ONVIF subscription is disabled (`isSubscription: false`)
- No ONVIF notification was received
- Only occupancy sensor triggered detection

## Error Example

```
# ONVIF is disabled in config
[INFO] subscribe_onvif camera_item: {..., 'onvif': {'isSubscription': False}, ...}

# Only occupancy triggers
[INFO] trigger_face_detection in cam_ip: 192.168.22.3, lock_asset_id: 0x1051dbfffe1844e0

# But member_detected shows onvifTriggered=True (WRONG!)
[INFO] member_detected with trigger context: onvifTriggered=True, occupancyTriggeredLocks=['0x1051dbfffe1844e0']
```

## Root Cause

### The Context System

`trigger_lock_context` tracks what triggered detection for each camera:

```python
trigger_lock_context = {
    'cam_ip': {
        'started_by_onvif': bool,      # Who started this detection session
        'onvif_triggered': bool,        # Was ONVIF involved at any point
        'specific_locks': set(),        # Which locks triggered via occupancy
        'active_occupancy': set()       # Currently active occupancy sensors
    }
}
```

### Context Lifecycle Bug

| Event | Expected | Actual |
|-------|----------|--------|
| Face detected | Context deleted | Context deleted (correct) |
| Timer expires (no face) | Context deleted | **Context persists** |
| Pipeline crashes | Context deleted | **Context persists** |
| occupancy:false when `onvif_triggered=True` | Context deleted | **Context persists** |

### Code Analysis

**Context only deleted after face match** (`py_handler.py:1005-1006`):
```python
if message['type'] == 'member_detected':
    # ... use context ...
    if cam_ip in trigger_lock_context:
        del trigger_lock_context[cam_ip]  # Only deleted HERE
```

**`handle_occupancy_false` won't delete when `onvif_triggered=True`** (`py_handler.py:1461`):
```python
if len(context['active_occupancy']) == 0 and not context['onvif_triggered']:
    # This condition FAILS when onvif_triggered=True
    # So context is NOT deleted
```

**New triggers reuse existing context** (`py_handler.py:1365`):
```python
is_new_detection = cam_ip not in trigger_lock_context
# If stale context exists, is_new_detection = False
# Stale context is reused instead of creating fresh one
```

## Timeline Example

```
13:28:13  ONVIF trigger → context created with onvif_triggered=True
    │
13:28:14  Pipeline CRASH → no face detected → context NOT deleted
    │
13:28:35  occupancy:false → context NOT deleted (onvif_triggered=True blocks it)
    │
    │     ══════════════════════════════════════════════════
    │     ║  STALE CONTEXT PERSISTS FOR 23 MINUTES         ║
    │     ══════════════════════════════════════════════════
    │
13:48:26  ONVIF DISABLED in config (but context still in memory)
    │
13:51:42  Occupancy trigger → finds STALE context → reuses it
    │
13:51:44  Face detected → reports onvifTriggered=True (WRONG!)
    │
13:51:45  Context finally deleted (after member_detected)
```

## Solution

### Approach: Clear Stale Context on New Trigger

Check if existing context is stale (detection not running) before reusing it.

### Code Change

**File:** `py_handler.py`
**Function:** `trigger_face_detection()`

**Before** (current code order):
```python
is_new_detection = cam_ip not in trigger_lock_context

if is_new_detection:
    trigger_lock_context[cam_ip] = {...}

context = trigger_lock_context[cam_ip]

# ... later ...
if cam_ip not in thread_gstreamers:
    return

thread_gstreamer = thread_gstreamers[cam_ip]
```

**After** (fixed code order):
```python
# Validate GStreamer exists first
if cam_ip not in thread_gstreamers:
    logger.warning('trigger_face_detection - gstreamer not found: %s', cam_ip)
    return

thread_gstreamer = thread_gstreamers[cam_ip]

# Check if existing context is stale (detection not running)
if cam_ip in trigger_lock_context and not thread_gstreamer.is_feeding:
    logger.info('trigger_face_detection - clearing stale context for: %s', cam_ip)
    del trigger_lock_context[cam_ip]

# Now check if this is a new detection
is_new_detection = cam_ip not in trigger_lock_context

if is_new_detection:
    trigger_lock_context[cam_ip] = {...}

context = trigger_lock_context[cam_ip]
```

### Why This Works

| Scenario | Before Fix | After Fix |
|----------|-----------|-----------|
| ONVIF triggers, crashes, occupancy triggers later | Reuses stale context | Clears stale context, creates fresh |
| Detection times out, new trigger arrives | Reuses stale context | Clears stale context, creates fresh |
| Normal operation (detection running) | Context kept | Context kept (no change) |

### Detection Logic

```
if context exists AND is_feeding=False:
    → Context is STALE (detection ended without face match)
    → Delete it
    → Treat as new detection

if context exists AND is_feeding=True:
    → Detection is running
    → Keep context, merge new trigger info
```

## Testing the Fix

### Full Regression Test (Reproduce Original Bug Scenario)

To verify the fix handles stale contexts:

| Step | Action | What to Look For |
|------|--------|------------------|
| 1 | Enable ONVIF (`isSubscription: true`) | Config updated |
| 2 | Restart Greengrass | Service starts |
| 3 | Trigger ONVIF motion (wave at camera) | `onvif_triggered=True` in context |
| 4 | Wait for detection timeout (~10s) | Detection stops, but context persists |
| 5 | Disable ONVIF (`isSubscription: false`) | Config updated (don't restart!) |
| 6 | Trigger occupancy sensor (DC001) | **Key test moment** |
| 7 | Check logs | Should see: `clearing stale context` |
| 8 | Verify `member_detected` | `onvifTriggered=False` |

### Log Messages to Look For

**Success indicators:**
```
# Stale context detected and cleared (new log from fix)
trigger_face_detection - clearing stale context for: 192.168.22.3

# Fresh context created with correct values
member_detected with trigger context: onvifTriggered=False, occupancyTriggeredLocks=['0x1051dbfffe1844e0']
```

**Failure indicator (bug still present):**
```
# Wrong! Should be False when ONVIF disabled
member_detected with trigger context: onvifTriggered=True
```

## Workaround

Restart Greengrass service to clear all in-memory state including `trigger_lock_context`.

```bash
sudo systemctl restart greengrass
```

This clears stale contexts but doesn't fix the underlying bug.

## Related Files

- `py_handler.py`: Contains `trigger_lock_context` and `trigger_face_detection()`
- `gstreamer_threading.py`: Contains `is_feeding` flag and `stop_feeding()`

## Related Issues

This bug is separate from the "not-negotiated" GStreamer error documented in `gstreamer_not_negotiated_error.md`. However, the GStreamer crashes can trigger this bug by leaving contexts in a stale state.

## Date Identified

2026-01-17
