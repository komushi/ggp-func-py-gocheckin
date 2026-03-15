# Edge-Side Considerations: Spaces Through Listing Shadow

## Overview

This document describes the edge-side implementation for receiving `spaces` via a **new Listing named shadow**. The cloud team should use this as a reference when implementing the shadow payload.

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

**New Service Method** (`assets.service.ts`):

```typescript
/**
 * Process listing shadow delta - syncs spaces for a listing
 */
private async processListingShadowDelta(listingId: string): Promise<any> {
  console.log('assets.service processListingShadowDelta in: ' + listingId);

  const getShadowResult = await this.iotService.getShadow({
    thingName: process.env.AWS_IOT_THING_NAME,
    shadowName: `listing:${listingId}`  // ← New shadow name format
  });

  const delta = getShadowResult.state.desired;

  if (delta && delta.spaces) {
    // Store listing spaces in local DDB for Python handler
    await this.assetsDao.upsertListingSpaces({
      listingId: delta.listingId,
      spaces: delta.spaces,
      lastUpdateOn: delta.lastRequestOn
    });
  }

  console.log('assets.service processListingShadowDelta out');
}
```

**New DAO Method** (`assets.dao.ts`):

```typescript
/**
 * Store listing spaces in local DDB
 */
public async upsertListingSpaces(data: {
  listingId: string;
  spaces: Space[];
  lastUpdateOn: string;
}): Promise<any> {
  // Upsert into TBL_ASSET or separate TBL_LISTING_SPACES
  console.log('assets.dao upsertListingSpaces in:', data);
  // Implementation: store { listingId, spaces: [...], lastUpdateOn }
}

/**
 * Fetch spaces for a listing
 */
public async getListingSpaces(listingId: string): Promise<Space[]> {
  console.log('assets.dao getListingSpaces in:', listingId);
  // Query local DDB for listing's spaces
}
```

**Handler Integration** (`handler.ts`):

```typescript
// Add listing shadow handling alongside reservations
if (event.state.listings) {
  if (getShadowResult.state.desired.listings) {
    await assetsService.processListingsShadow(
      event.state.listings,
      getShadowResult.state.desired.listings
    ).catch(err => {
      console.error('processListingsShadow error:' + err.message);
      throw err;
    });
  }
}
```

### Python Handler (`ggp-func-py-gocheckin`)

**Updated Data Fetching** (`py_handler.py`):

```python
def get_active_reservations():
    """Fetch active reservations WITHOUT spaces."""
    tbl_reservation = os.environ['TBL_RESERVATION']
    table = dynamodb.Table(tbl_reservation)

    # Only fetch reservation basics
    attributes_to_get = ['reservationCode', 'listingId', 'checkInDate', 'checkOutDate']

    response = table.scan(
        FilterExpression=filter_expression,
        ProjectionExpression=', '.join(attributes_to_get)
    )

    return response.get('Items', [])

def get_spaces_for_listing(listing_id: str) -> set:
    """Fetch spaces for a listing from local DDB."""
    # Query the listing spaces cache populated by TS handler
    tbl_listing_spaces = os.environ['TBL_LISTING_SPACES']  # New table or use TBL_ASSET
    table = dynamodb.Table(tbl_listing_spaces)

    response = table.get_item(
        Key={'listingId': listing_id}
    )

    spaces = response.get('Item', {}).get('spaces', [])
    return {s['uuid'] for s in spaces}

def get_members_for_reservations(reservations, category):
    """Fetch members with spaces resolved from listing."""
    results = []
    for reservation in reservations:
        # Get spaces from LISTING, not reservation
        authorized_spaces = get_spaces_for_listing(reservation['listingId'])

        # Fetch members for this reservation
        members = fetch_members_for_reservation(reservation['reservationCode'])
        for member in members:
            member['listingId'] = reservation['listingId']
            member['authorizedSpaces'] = authorized_spaces
        results.extend(members)

    return results
```

---

## Data Flow (Updated)

```
Cloud (reservations.service.ts / listings.service.ts)
    │
    ├─ createReservation() / renewReservation()
    │   └─ NO LONGER includes spaces in reservation
    │
    ├─ updateListing() or listing spaces change
    │   └─ Update listing named shadow: listing:<listingId>
    │       └─ { listingId, spaces: [{ uuid, ... }], lastRequestOn }
    │
    ▼
AWS IoT Shadow
    │
    ├─ Classic shadow delta: { listings: { "listing:xyz": { action: 'UPDATE' } } }
    └─ Named shadow: listing:<listingId> → { spaces: [...] }
    │
    ▼
TypeScript Edge Handler (handler.ts → assets.service.ts)
    │
    ├─ processListingsShadow() detects delta
    ├─ getShadow() fetches listing named shadow
    ├─ upsertListingSpaces() stores in local DDB
    └─ Publishes listing_spaces_updated
    │
    ▼
Python Handler (py_handler.py)
    │
    ├─ get_active_reservations() → [ { listingId, ... } ]  (no spaces!)
    ├─ get_spaces_for_listing(listingId) → { "adwJwZ", "xYz123" }
    ├─ get_members_for_reservations() → stamp with authorizedSpaces
    └─ fetch_scanner_output_queue() → filter locks by authorization
    │
    ▼
Result: Member only sees locks for spaces in their listing's shadow
```

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

3. **Reservations no longer need spaces** — remove any code that includes `spaces` in reservation shadow payloads

---

## Testing Checklist

- [ ] Listing named shadow `listing:<listingId>` exists and contains `spaces`
- [ ] Classic shadow triggers edge sync for listing changes
- [ ] TS handler stores listing spaces in local DDB
- [ ] Python handler fetches spaces from listing (not reservation)
- [ ] Python handler correctly filters locks by `roomCode in authorized_spaces`
- [ ] Members with listings that have no spaces get no lock access
- [ ] Locks in non-authorized spaces are filtered out

---

## Related Documents

- `REMOVE_SPACES_FROM_RESERVATION.md` — Cloud-side implementation plan
- `SECURITY_USE_CASES.md` — UC1-UC5 security use-case definitions
- `LOCK_BUTTON_ASSOCIATION.md` — Lock space assignment via `roomCode`