# Remove `withKeypad` from Camera Lock Entries

## Problem

### Decision 28 makes `withKeypad` obsolete

Per **Decision 28** in SECURITY_USE_CASES.md:

> "All locks require a 'clicked' signal to unlock. The `withKeypad` flag is no longer used for unlock decisions."

Every lock now has either a built-in sensor (KEYPAD_LOCK) or a companion LOCK_BUTTON — there are no "legacy" locks without a click mechanism. ONVIF motion only starts sessions in surveillance mode; it never directly unlocks.

### Race condition

The `withKeypad` enrichment in `processCamerasShadowDelta()` queries LOCK_BUTTON records via `hasEntryButtonsForLock()`. If camera shadow processes before LOCK_BUTTON associations sync, the query returns false, setting `withKeypad=false` on the camera lock entry. This was observed in production (Dahua camera's MAG002 lock entry).

---

## What Changes

### TS handler (ggp-func-ts-gocheckin)

1. **`GoCheckInLock` interface**: Remove `withKeypad` field. Keep `assetId`, `assetName`, `category`.

2. **`processCamerasShadowDelta()`**: Remove `hasEntryButtonsForLock()` call and `withKeypad` enrichment. Just set `assetId` and `category` on each lock entry.

3. **`assets.dao.ts`**: Remove `hasEntryButtonsForLock()` method entirely.

4. **`unlockByMemberDetected()`**: Remove section 2 (legacy lock unlock on ONVIF trigger). Per Decision 28, ONVIF never directly unlocks — a clicked event is always required.

### PY handler (py_handler.py)

1. **`trigger_face_detection()`**: Remove the legacy lock gate that skips ONVIF triggers when no `withKeypad=false` locks exist. ONVIF triggers now always start surveillance-mode detection.

2. **`handle_occupancy_false()`**: Remove the `has_legacy` check for early stop. If `active_occupancy` is empty and `onvif_triggered` is false, always stop detection early.

---

## What Stays

- **`Z2mLock.withKeypad`**: Kept — still reflects hardware capability at Zigbee discovery time (KEYPAD_LOCK=true, LOCK/LOCK_BUTTON=true via `ZB_CAT_WITH_KEYPAD`). Only removed from `GoCheckInLock` (the camera lock entry type).

---

## Impact Analysis

- **Cloud**: No impact — cloud never stores or reads `withKeypad` on camera lock entries (verified in `LOCK_BUTTON_COMPANION.md`)
- **Edge TS handler**: Camera lock entries no longer have `withKeypad`. `unlockByMemberDetected()` no longer unlocks legacy locks on ONVIF trigger.
- **Edge PY handler**: ONVIF motion always triggers surveillance-mode detection. No `withKeypad` checks.

---

## Before / After

### Camera lock entry in DDB

**Before:**
```json
{
  "locks": {
    "0xe4b323fffeb4b614": {
      "assetId": "0xe4b323fffeb4b614",
      "assetName": "MAG002",
      "withKeypad": true,
      "category": "LOCK"
    }
  }
}
```

**After:**
```json
{
  "locks": {
    "0xe4b323fffeb4b614": {
      "assetId": "0xe4b323fffeb4b614",
      "assetName": "MAG002",
      "category": "LOCK"
    }
  }
}
```

---

## Related Documents

- `ggp-func-py-gocheckin/doc_design/SECURITY_USE_CASES.md` — Decision 28
- `ggp-func-ts-gocheckin/doc/design/LOCK_BUTTON_ASSOCIATION.md` — Updated to reflect removal
