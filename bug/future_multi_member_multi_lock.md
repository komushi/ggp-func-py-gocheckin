# Future Request: Continue Detection for Multi-Member Multi-Lock Scenarios

**Status:** FUTURE
**Created:** 2026-02-04
**Priority:** Low (current single-lock setup unaffected)

---

## Summary

Currently, `face_recognition.py` sets `identified = True` after the first frame with any matched face. All subsequent frames in the same detection window are skipped. This works when all members share the same lock, but would fail in a future multi-lock scenario where different members are associated with different locks.

## Current Behavior

```
Detection window starts (feed_detecting)
  Frame 1: RULIN detected → MATCH → identified = True
  Frame 2: skipped (identified = True)
  Frame 3: skipped (Xu would have appeared here, but never processed)
Detection window ends (stop_feeding triggered by member_detected)
```

Result: Only RULIN's lock is unlocked. Xu's lock (if different) is never triggered.

## Why It Doesn't Matter Today

All cameras currently map to a single lock (e.g., `MAG002`). Any matched member triggers the same lock. Detecting one member is sufficient to unlock.

## When This Matters

If the system supports:
- Camera associated with multiple locks (e.g., `lock1` for room A, `lock2` for room B)
- Different members associated with different locks
- Both members may appear in front of the same camera but in different frames

## Possible Approach

Replace the boolean `identified` flag with a set of identified members. Continue processing frames until:
- The detection timer expires, OR
- No new members are found for N consecutive frames (early exit optimization)

Move `stop_feeding` out of `fetch_scanner_output_queue` — let the detection timer handle session end instead of stopping on first match.

```python
# Instead of:
self.cam_detection_his[cam_ip]['identified'] = True  # boolean, stops all detection

# Use:
self.cam_detection_his[cam_ip]['identified_members'] = set()  # track who's been found
# Continue processing frames, skip already-matched members
```

## Files Affected

| File | Change |
|---|---|
| `face_recognition.py` | Replace `identified` boolean with `identified_members` set |
| `face_recognition_hailo.py` | Same change |
| `py_handler.py` | Remove `stop_feeding` from `fetch_scanner_output_queue`; rely on detection timer |

## Dependency

Requires lock-per-member association data to be available in the Python runtime, either from DynamoDB or passed via IoT message.
