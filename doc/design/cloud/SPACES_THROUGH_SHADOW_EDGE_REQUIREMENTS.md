# Edge-Side Considerations: Spaces Through Listing Shadow (Cloud Reference)

> **Note**: This document describes **cloud-side requirements** for the listing shadow pattern. For **edge implementation**, see:
> - **TS Edge**: `../../LISTING_SPACES_SYNC.md` - How listing shadows sync to local DDB
> - **TS Edge**: `../../RESERVATION_REFRESH.md` - How reservation refresh works at edge
> - **Python Edge**: `LISTING_SPACES_EDGE_SYNC.md` - How Python fetches spaces from local DDB

## Overview

This document describes the edge-side implementation for receiving `spaces` via a **Listing named shadow**. The cloud team should use this as a reference when implementing the shadow payload.

## Design Decision

### Updated Architecture

**Problem**: Storing `spaces` directly in `ReservationItem` leads to stale data when listings change.

**New Solution**:
1. **Remove `spaces` from ReservationItem** — reservations no longer carry space data
2. **Add a Listing named shadow** — `spaces` are synced via `listing:<listingId>` named shadow
3. **Edge resolves at runtime** — Python handler fetches listing's spaces when processing reservations

**Rationale**:
- Calendar operations don't need `spaces` persisted (they use transient values)
- Lock authorization requires `spaces` at runtime on the edge
- Listing shadow provides a single source of truth — all reservations for a listing share the same spaces

---

## Shadow Payload Structure

### NEW: Listing Named Shadow

The cloud must create/update a **Listing named shadow** that carries the space assignments:

```json
{
  "state": {
    "desired": {
      "listingId": "1225414147364900825",
      "propertyCode": "WIP",
      "spaces": [
        { "uuid": "adwJwZ", "assetName": "Entrance", "category": "SPACE" },
        { "uuid": "xYz123", "assetName": "Living Room", "category": "SPACE" }
      ],
      "lastRequestOn": "2026-03-15T10:00:00.000Z"
    }
  }
}
```

### Shadow Name Format

```
$aws/things/<thingName>/shadow/name/listing:<listingId>
```

Example: `listing:1225414147364900825`

### Critical Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `listingId` | `string` | Yes | The listing ID (matches reservation's listingId) |
| `spaces` | `Space[]` | Yes | List of space objects for this listing |
| `spaces[].uuid` | `string` | Yes | **Space UUID** (not assetId!) — used for lock authorization |

---

## Edge Implementation Changes Needed

### TypeScript Edge Handler (`ggp-func-ts-gocheckin`)

**Status**: ✅ Already implemented

The TS edge component (`listings.service.ts` + `listings.dao.ts`) already syncs listing shadows to local DDB (`TBL_LISTING`).

**No changes needed to `reservations.service.ts`**: The reservation refresh flow does NOT use `spaces` at all. It only syncs members to local DDB.

### Python Handler (`ggp-func-py-gocheckin`)

**Required Fix** (`py_handler.py`):

Change `get_members_for_reservations()` to fetch `spaces` from `TBL_LISTING` instead of `TBL_RESERVATION`:

```python
# NEW: Fetch spaces from TBL_LISTING (current)
def get_listing_spaces(host_id: str, listing_id: str) -> List[dict]:
    """Fetch current spaces for a listing from TBL_LISTING."""
    table = dynamodb.Table(os.environ['TBL_LISTING'])
    response = table.get_item(Key={'hostId': host_id, 'listingId': listing_id})
    return response.get('Item', {}).get('spaces', [])

# In get_members_for_reservations():
for reservation in reservations:
    listing_id = reservation['listingId']
    # Fetch current spaces from TBL_LISTING (not TBL_RESERVATION)
    listing_spaces = get_listing_spaces(os.environ['AWS_IOT_HOST_ID'], listing_id)
    authorized_spaces = {s['uuid'] for s in listing_spaces}
    # ... stamp members with authorized_spaces ...
```

**What to remove**:
- Remove `#spaces` from `ProjectionExpression` in `get_active_reservations()`, `get_staff_reservations()` etc.
- Remove `reservation.get('spaces', [])` - this is stale data

---

## Data Flow (Updated)

```
Cloud: Update listing → IoT Shadow Delta (listing:<listingId>)
    │
    ▼
TS Edge: listings.service → TBL_LISTING.spaces (current)
    │
    ▼
Python: fetch_members() → get_listing_spaces(listingId) → TBL_LISTING
    │
    ▼
Python: authorized_spaces = {s['uuid'] for s in listing_spaces}
    │
    ▼
Lock authorization uses current spaces ✅
```

**Key Change**: Python reads `spaces` from `TBL_LISTING` (current) instead of `TBL_RESERVATION` (stale).

**TS does NOT need changes**: `reservations.service.ts` only syncs members, does NOT use `spaces`.

---

## Critical Implementation Notes

### 1. Space Identifier Field

**DO NOT** use `assetId` for spaces. The edge uses `uuid`:

```python
# CORRECT (edge implementation):
authorized_spaces = {s['uuid'] for s in reservation.get('spaces', [])}

# WRONG (would break authorization):
authorized_spaces = {s['assetId'] for s in reservation.get('spaces', [])}
```

**Why**: DynamoDB `gocheckin_asset` table stores spaces with:
- `uuid`: `"adwJwZ"` ← This is the space identifier
- **No `assetId` field** on SPACE records

### 2. Lock `roomCode` Matches Space `uuid`

Lock records carry `roomCode` which is the space UUID:

```json
{
  "assetId": "0xe4b323fffeb4b614",
  "roomCode": "adwJwZ",  // ← Matches space.uuid
  "category": "LOCK"
}
```

Authorization check:
```python
lock_items.get(lock_id, {}).get('roomCode') in authorized_spaces
# "adwJwZ" in {"adwJwZ", "xYz123"} → True
```

### 3. Listing Shadow Sync Latency

When listing spaces change, there may be a delay before the edge receives the update. Consider:
- **Grace period**: Allow edge to cache listing spaces with TTL
- **Force refresh**: Cloud can trigger immediate sync via IoT publish

### 4. Empty/Missing Listing Spaces = No Access

If a listing has no spaces in its shadow:
- `authorized_spaces` = empty set
- `member_clicked_locks` = empty list
- **Members cannot access any locks**

This is intentional — if the cloud doesn't send spaces, guests have no authorization.

---

## Cloud-Side Requirements

The cloud team must:

1. **Create/Update Listing Named Shadow** when listing spaces change:
   ```typescript
   const shadowName = `listing:${listingId}`;
   const shadowPayload = {
     state: {
       desired: {
         listingId,
         propertyCode,
         spaces: listingItem.spaces.map(s => ({
           uuid: s.uuid,
           assetName: s.assetName,
           category: 'SPACE'
         })),
         lastRequestOn: new Date().toISOString()
       }
     }
   };
   await iotService.updateThingShadow(thingName, shadowName, shadowPayload);
   ```

2. **Update Classic Shadow** to trigger edge sync:
   ```typescript
   const classicPayload = {
     state: {
       desired: {
         listings: {
           [shadowName]: { action: 'UPDATE', lastRequestOn: ... }
         }
       }
     }
   };
   await iotService.updateThingShadow(thingName, undefined, classicPayload);
   ```

3. **Optional**: Remove `spaces` from reservation shadow payloads (not used by edge anymore)

---

## Testing Checklist

- [ ] **TS**: Listing shadow sync → `TBL_LISTING` updated within 1 second
- [ ] **Python**: `get_listing_spaces()` reads from `TBL_LISTING` (not `TBL_RESERVATION`)
- [ ] **Python**: Update listing spaces → verify Python sees current spaces within 1 second
- [ ] **Python**: Face recognition with member → verify lock authorization uses current spaces
- [ ] **TS**: No changes needed to `reservations.service.ts` - it doesn't use `spaces`

---

## Related Documents

### Edge Implementation
- **TS Edge**: `../../LISTING_SPACES_SYNC.md` — How TS syncs listing shadows
- **TS Edge**: `../../RESERVATION_REFRESH.md` — How reservation refresh works (no changes needed)
- **Python Edge**: `LISTING_SPACES_EDGE_SYNC.md` — Python fix details
- **Python Overview**: `LISTING_SPACES_OVERVIEW.md` — Cross-component overview

### Cloud Reference
- `REMOVE_SPACES_FROM_RESERVATION.md` — Cloud-side implementation plan
- `SECURITY_USE_CASES.md` — UC1-UC5 security use-case definitions
- `LOCK_BUTTON_ASSOCIATION.md` — Lock space assignment via `roomCode`