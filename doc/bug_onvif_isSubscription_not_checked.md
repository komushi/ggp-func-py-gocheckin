# Bug: ONVIF `isSubscription` Setting Not Checked

**Status:** FIXED (2026-01-15)

## Summary

The `onvif.isSubscription` setting in camera configuration is not being checked before subscribing to ONVIF events. This causes ONVIF subscriptions to be created even when the user explicitly disables them.

## Date Discovered

2026-01-15

## Affected File

`py_handler.py` - `subscribe_onvif()` function (line 1477-1529)

## Bug Description

### Current Behavior (Buggy)

```python
# Line 1511 - Current code:
if camera_item['isDetecting'] or camera_item['isRecording']:
    onvif_sub_address = onvif_connectors[cam_ip].subscribe(...)
```

The code only checks `isDetecting` and `isRecording`, but **ignores** the `onvif.isSubscription` setting.

### Evidence

Camera 192.168.22.5 configuration:

| Setting | Value |
|---------|-------|
| `onvif.isSubscription` | **false** |
| `onvif.isPullpoint` | false |
| `isDetecting` | true |
| `isRecording` | true |

Despite `isSubscription: false`, ONVIF notifications are still being received and processed:

```
[2026-01-15T18:18:15.32+09:00][INFO]-py_handler.py:533,ONVIF Motion detected: is_motion_value=True, cam_ip=192.168.22.5
[2026-01-15T18:18:21.234+09:00][INFO]-gstreamer_threading.py:412,New video file created...
```

### Commented Out Code

There is a commented out check at line 1521:

```python
# if camera_item['onvif']['isSubscription']:
```

However, this was in the wrong location (unsubscribe block) and is now commented out.

---

## Expected Behavior

### Rule

```
IF isSubscription == false:
    → DO NOT subscribe to ONVIF (user's explicit choice)

IF isSubscription == true AND (isDetecting OR isRecording):
    → Subscribe to ONVIF
```

### Scenario Matrix

| # | isSubscription | isDetecting | isRecording | Lock Types | Subscribe? | Reason |
|---|----------------|-------------|-------------|------------|------------|--------|
| 1 | **false** | any | any | any | **NO** | User disabled ONVIF |
| 2 | true | false | true | any | YES | Recording needs motion trigger |
| 3 | true | true | false | legacy | YES | Detection triggered by ONVIF |
| 4 | true | true | false | sensor-only | YES | May need for future recording |
| 5 | true | true | true | legacy | YES | Both recording and detection |
| 6 | true | true | true | sensor-only | YES | Recording works, detection skipped |
| 7 | true | true | any | mixed | YES | Detection for legacy locks |

### Use Cases

#### Use Case 1: Sensor-Only Camera Without Recording

- **Config:** `isSubscription: false`, `isDetecting: true`, `isRecording: false`
- **Locks:** All have sensors (withKeypad: true)
- **Expected:** No ONVIF subscription, detection triggered only by occupancy sensors
- **Benefit:** Reduces log noise, saves resources

#### Use Case 2: Legacy Camera With Recording

- **Config:** `isSubscription: true`, `isDetecting: true`, `isRecording: true`
- **Locks:** Legacy locks (withKeypad: false)
- **Expected:** ONVIF subscription active, motion triggers both recording and detection

#### Use Case 3: Recording Only

- **Config:** `isSubscription: true`, `isDetecting: false`, `isRecording: true`
- **Expected:** ONVIF subscription active, motion triggers recording only

---

## Fix Required

### Location

`py_handler.py` - `subscribe_onvif()` function

### Change

**Before (line 1511):**
```python
if camera_item['isDetecting'] or camera_item['isRecording']:
    onvif_sub_address = onvif_connectors[cam_ip].subscribe(...)
```

**After (FIXED):**
```python
# Check if ONVIF subscription is enabled in camera settings
onvif_settings = camera_item.get('onvif', {})
is_subscription_enabled = onvif_settings.get('isSubscription', False)

logger.info(f"{cam_ip} subscribe_onvif ... is_subscription_enabled: {is_subscription_enabled}")

# Only subscribe if isSubscription is enabled AND (isDetecting OR isRecording)
if is_subscription_enabled and (camera_item['isDetecting'] or camera_item['isRecording']):
    onvif_sub_address = onvif_connectors[cam_ip].subscribe(...)
    camera_item['onvifSubAddress'] = onvif_sub_address
else:
    # Unsubscribe if isSubscription is disabled or both isDetecting and isRecording are false
    if old_onvif_sub_address is not None:
        onvif_connectors[cam_ip].unsubscribe(cam_ip, old_onvif_sub_address)
        camera_item['onvifSubAddress'] = None
    onvif_connectors[cam_ip] = None
    del onvif_connectors[cam_ip]
```

### Additional Consideration

When `isSubscription` changes from `true` to `false`, the system should:
1. Unsubscribe from existing ONVIF subscription
2. Clear the `onvifSubAddress` in camera_item

---

## Impact

### Without Fix

- ONVIF subscriptions created regardless of user settings
- Unnecessary log noise
- Wasted resources processing unwanted notifications
- User cannot disable ONVIF for specific cameras

### With Fix

- `isSubscription: false` properly disables ONVIF subscription
- Cleaner logs for sensor-only cameras
- User has full control over ONVIF behavior per camera

---

## Related Files

- `py_handler.py` - Main handler with subscribe_onvif()
- `onvif_process.py` - OnvifConnector class

## Related Documentation

- [Lock-Triggered Detection Design](./lock_triggered_detection.md)
- [Test Plan](./test_plan_lock_triggered_detection.md)
