# Bug #5: Occupancy Context Race Condition

**Status:** FIXED (2026-01-25)
**Discovered:** 2026-01-25
**Priority:** High

## Summary

When a face is detected, the `occupancyTriggeredLocks` is read from the current context at **process time** (in `fetch_scanner_output_queue`), not at **frame capture time**. This causes a race condition where `occupancy:false` events can clear the context before the face match is processed, resulting in an empty `occupancyTriggeredLocks` array and triggering the "unlock all" fallback behavior.

## Symptom

- User touches keypad lock (e.g., DC006)
- User shows face while lock is still active (within 10-second MCU timer)
- Lock's MCU timer expires, sends `occupancy:false`
- Face match is processed AFTER `occupancy:false` clears the context
- `member_detected` payload shows `occupancyTriggeredLocks: []` (empty)
- TypeScript component triggers "unlock all" fallback, unlocking ALL locks on the camera

## Root Cause

### Timeline Showing the Race Condition

```
Time        Event                           Context State
────────────────────────────────────────────────────────────────
00:01:02    DC001 occupancy:true            {DC001}
00:01:08    DC006 occupancy:true            {DC001, DC006}
00:01:12    DC001 occupancy:false (auto)    {DC006}
00:01:15    Frame captured (face shown)     {DC006} ← face in this frame
00:01:17    DC006 occupancy:false (auto)    {} ← CONTEXT CLEARED
00:01:19    Face match processed            {} ← reads EMPTY context

Result: occupancyTriggeredLocks=[] → TS "unlock all" fallback → BOTH locks unlock
```

### Code Flow Analysis

1. **Frame captured** in `gstreamer_threading.py` → put in `cam_queue`
2. **Face detected** in `face_recognition.py` → put `member_detected` in `scanner_output_queue`
3. **Context read** in `py_handler.py:fetch_scanner_output_queue()` → reads CURRENT context

The problem: Step 3 reads the context at process time, but the face was captured at step 1. If `occupancy:false` arrives between step 1 and step 3, the context is already cleared.

### Relevant Code

**py_handler.py:1000** (where context is read - TOO LATE):
```python
context = detection_contexts.get(cam_ip, {})
message['payload']['onvifTriggered'] = context.get('onvif_triggered', False)
message['payload']['occupancyTriggeredLocks'] = list(context.get('specific_locks', set()))
```

**assets.service.ts:555-562** (fallback behavior):
```typescript
// 3. Fallback: if no trigger context provided, unlock all locks (legacy behavior)
if (!memberDetectedItem.onvifTriggered &&
    (!memberDetectedItem.occupancyTriggeredLocks || memberDetectedItem.occupancyTriggeredLocks.length === 0)) {
  console.log('assets.service unlockByMemberDetected - no trigger context, using legacy behavior (unlock all)');
  for (const lockAssetId of Object.keys(cameraItem.locks)) {
    zbLockPromises.push(this.unlockZbLock(lockAssetId));
  }
}
```

## Evidence from Logs

### Python Handler Log
```
[00:01:17.949] handle_occupancy_false - context after removal: {..., 'specific_locks': set(), 'active_occupancy': set()}
[00:01:19.285] member_detected with trigger context: onvifTriggered=False, occupancyTriggeredLocks=[]
```

### TypeScript Handler Log
```
[00:01:19.436] assets.service unlockByMemberDetected - no trigger context, using legacy behavior (unlock all)
[00:01:19.442] Publishing message on topic "zigbee2mqtt/DC006/set" with Payload "{"state":"TOGGLE"}"
[00:01:19.442] Publishing message on topic "zigbee2mqtt/DC001/set" with Payload "{"state":"TOGGLE"}"
```

## MTR001DC Lock Behavior

The MTR001DC keypad lock has a built-in 10-second timer:
- `occupancy:true` sent when user touches keypad
- `occupancy:false` automatically sent after 10 seconds (MCU powers off)

This automatic timeout creates a tight window for face recognition to complete.

## Impact

- **SECURITY RISK:** "Unlock all" fallback unlocks locks that should remain locked
- **Security Issue:** Locks that should NOT unlock (because their occupancy period ended) are unlocked
- **User Confusion:** All locks on the camera unlock instead of just the one the user touched
- **Test Failures:** Test 9 (Multiple Occupancy - One Leaves) fails due to this race condition

### Security Risk Details

The combination of the race condition + "unlock all" fallback creates a security vulnerability:

1. Camera has multiple locks (e.g., DC001 for Room A, DC006 for Room B)
2. Guest touches DC006 (their room)
3. Race condition causes `occupancyTriggeredLocks=[]`
4. Fallback triggers: ALL locks unlock
5. **Result:** DC001 (Room A - NOT the guest's room) also unlocks

This is a **security violation** - a guest can inadvertently unlock doors they don't have access to.

## Proposed Fix

### Part 1: Capture Context at Frame Time (Required)

**Goal:** Ensure `occupancyTriggeredLocks` reflects the locks that were active when the face was captured, not when `member_detected` is processed.

**Option A: Context Snapshot by detecting_txn (Recommended)**

1. **`face_recognition.py`** - Add `detecting_txn` to `scanner_output_queue` message
2. **`py_handler.py`** - Store context snapshots keyed by `(cam_ip, detecting_txn)`
3. **`py_handler.py`** - Look up context snapshot by `detecting_txn` instead of reading current context

**Option B: Capture Context at Face Match Time**

1. Pass `detection_contexts` reference to `FaceRecognition` class
2. Read context when face is matched (in `face_recognition.py`)
3. Include `occupancyTriggeredLocks` in the `scanner_output_queue` message

### Part 2: Remove "Unlock All" Fallback (Required - Security Fix)

**Issue:** The current fallback in TypeScript is a security risk:

```typescript
// assets.service.ts:555-562 (CURRENT - DANGEROUS)
if (!memberDetectedItem.onvifTriggered &&
    (!memberDetectedItem.occupancyTriggeredLocks || memberDetectedItem.occupancyTriggeredLocks.length === 0)) {
  console.log('assets.service unlockByMemberDetected - no trigger context, using legacy behavior (unlock all)');
  for (const lockAssetId of Object.keys(cameraItem.locks)) {
    zbLockPromises.push(this.unlockZbLock(lockAssetId));  // UNLOCKS ALL LOCKS!
  }
}
```

**Why this is dangerous:**
- If context is empty due to race condition, ALL locks on the camera unlock
- User only touched one lock, but multiple locks unlock
- Security violation: locks that should remain locked are unlocked

**Required change:**

```typescript
// assets.service.ts:555-562 (FIXED - SAFE)
if (!memberDetectedItem.onvifTriggered &&
    (!memberDetectedItem.occupancyTriggeredLocks || memberDetectedItem.occupancyTriggeredLocks.length === 0)) {
  console.warn('assets.service unlockByMemberDetected - no trigger context, skipping unlock (security)');
  // DO NOT unlock anything - fail safe
}
```

**Rationale:**
1. "Unlock all" should NEVER be a fallback - it's a security risk
2. If context is empty, something went wrong - fail safely by not unlocking
3. With Part 1 fix, context should always be captured correctly
4. If context is legitimately empty, detection shouldn't have started

### Fix Summary

| Part | Location | Change |
|------|----------|--------|
| 1 | `py_handler.py`, `face_recognition.py` | Capture context at frame time |
| 2 | `assets.service.ts` | Remove "unlock all" fallback |

Both parts are required for a complete fix.

## User Experience (With Fix)

### Before Fix (Current Buggy Behavior)
```
User touches DC006 → Shows face at T+15s → DC006 timeout at T+17s → Face processed at T+19s
Result: Context empty → "unlock all" → BOTH DC001 and DC006 unlock (WRONG)
```

### After Fix (Expected Behavior)
```
User touches DC006 → Shows face at T+15s → Context {DC006} saved with frame
→ DC006 timeout at T+17s → Face processed at T+19s → Uses saved context {DC006}
Result: Only DC006 unlocks (CORRECT)
```

## Related Issues

- **Bug #1 (Stale Context):** Similar context management issue, but for stale data persisting
- **Test 9:** This bug causes Test 9 to fail

## Testing

### Reproduction Steps
1. Enable `isDetecting` on camera 192.168.22.5
2. Trigger `occupancy:true` on DC001
3. Wait 5 seconds
4. Trigger `occupancy:true` on DC006
5. Wait for DC001's 10-second timer to expire (`occupancy:false`)
6. Show face (before DC006's timer expires)
7. Observe: Both locks unlock instead of just DC006

### Verification After Fix
1. Same steps as above
2. Expected: Only DC006 unlocks
3. Verify log shows `occupancyTriggeredLocks: ["0x1051dbfffe182b18"]` (DC006 only)

## Revision History

| Date | Changes |
|------|---------|
| 2026-01-25 | Bug discovered during Test 9 execution |
| 2026-01-25 | Root cause identified: context read at process time, not capture time |
| 2026-01-25 | TS fallback "unlock all" behavior confirmed in logs |
| 2026-01-25 | **FIX IMPLEMENTED**: Part 1 - Context snapshots captured at detection start |
| 2026-01-25 | **FIX IMPLEMENTED**: Part 2 - Removed "unlock all" fallback in TypeScript |
| 2026-01-25 | **FIX UPDATED**: Part 1 - Snapshot now updated on occupancy:false to remove departed locks |
