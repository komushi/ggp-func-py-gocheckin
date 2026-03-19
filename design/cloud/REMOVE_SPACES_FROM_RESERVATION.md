# Remove `spaces` from ReservationItem

## Problem

`spaces` is copied from `Listing → Reservation` at create/renew time via `addListingInfo()`. When `listing.spaces` is later updated (assets added, renamed, or removed), all existing `ReservationItem` records retain stale `spaces` data.

Additionally, when an asset is removed from a listing's `spaces`, orphaned calendar entries remain in the calendar table for that asset's date ranges across all active reservations.

## Solution (Option B — Read-through)

Never store `spaces` on a reservation. Always fetch it live from the listing when needed. Clean up orphaned calendar entries whenever a listing's `spaces` changes.

**No changes needed to:**
- `schema.graphql` — `Reservation` type never had a `spaces` field
- `serverless.ts` — no references to `spaces`
- `vue-gocheckin-guest` — `spaces` is not used anywhere
- `vue-gocheckin-host` — `spaces` only appears in listing management UI, not reservation queries

---

## Implementation Plan

### File 1: `src/functions/reservations/reservations.models.ts`

1. Remove `spaces: ListingSpace[];` from `ReservationItem` interface (line 91)
2. Remove the `ListingSpace` interface (lines 73–76) — it is already defined in `listings.models.ts`

---

### File 2: `src/functions/reservations/reservations.service.ts`

#### `addListingInfo()` (line 716)
- Remove `reservationItem.spaces = listingItem.spaces;`
- Change return value from `reservationItem` to `{ reservationItem, spaces: listingItem.spaces }` so callers receive the live spaces without it being stored on the item

#### `generateReservation()` (line 58)
- Destructure result of `addListingInfo()`: `const { reservationItem: params, spaces } = await this.addListingInfo(params);`
  (currently `params = await this.addListingInfo(params)`)
- Replace `params.spaces` with `spaces` in the `validateCalendarAvailability` call (line 122)
- Pass `spaces` as a new argument to `reservationsDao.generateReservation(params, spaces)`

#### `renewReservation()` (line 173)
- Destructure result of `addListingInfo()`: `const { reservationItem: newReservation, spaces } = await this.addListingInfo(newReservation);`
- Pass `spaces` as a new argument to `validateCalendarChanges(newReservation, origReservation, spaces)`
- Pass `spaces` as a new argument to `reservationsDao.renewReservation(newReservation, origReservation, spaces)`

#### `validateCalendarChanges()` (line 867)
- Add `spaces` parameter
- Pass `spaces` to `validateSpaceAvailability` instead of `newReservation.spaces`

#### `removeReservation()` (line 255)
- After fetching `reservationItem` from DAO, fetch listing's current spaces:
  ```typescript
  const listingItem = await this.listingsService.getListing(
    { listingId: reservationItem.listingId, hostId: reservationItem.hostId },
    ['spaces']
  );
  ```
- Pass `listingItem.spaces` as a new argument to `reservationsDao.clearReservation(reservationItem, listingItem.spaces)`

---

### File 3: `src/functions/reservations/reservations.dao.ts`

#### `generateReservation()` (line 171)
- Add `spaces` parameter to method signature
- Replace `params.spaces` with `spaces` when calling `generateCalendarsWithOverlapSupport` (line 178)

#### `clearReservation()` (line 213)
- Add `spaces` parameter to method signature
- Replace `params.spaces` with `spaces` when calling `generateDelCalendarsWithOverlapSupport` (line 250)

#### `renewReservation()` (line 264)
- Add `spaces` parameter to method signature
- Replace `newReservation.spaces` with `spaces` in `generateCalendarsWithOverlapSupport` call (line 271)
- Replace `origReservation.spaces` with `spaces` in `generateDelCalendarsWithOverlapSupport` call (line 285)
  - Both old and new calendar operations use the same current listing spaces; orphan cleanup (see below) handles any previously removed assets
- Remove `#sp = :sp,` from `UpdateExpression` (line 303)
- Remove `'#sp': 'spaces'` from `ExpressionAttributeNames` (line 313)
- Remove `':sp': newReservation.spaces` from `ExpressionAttributeValues` (line 331)

#### New public method: `getReservationsByListingId(listingId: string)`
- Query `TBL_RESERVATION` with `KeyConditionExpression: 'listingId = :listingId'`
- Project only `['checkInDate', 'checkOutDate', 'reservationCode']` (minimal attributes needed for calendar cleanup)
- Used by `listings.service` during orphan cleanup

#### New public method: `deleteCalendarEntriesForSpaces(spaces, checkInDate, checkOutDate, reservationCode)`
- Delegate to `generateDelCalendarsWithOverlapSupport` and then `executeBatchOperations`
- Used by `listings.service` during orphan cleanup

---

### File 4: `src/functions/listings/listings.service.ts`

#### `updateListing()` (line 49)

**Dependency**: Inject `ReservationsDao` into `ListingsService`.

**Changes**:

1. Expand the pre-fetch to include `spaces` (currently only fetches `registeredOn`):
   ```typescript
   const rtnListing = await this.listingsDao.getListing(listingKey, ['registeredOn', 'spaces']);
   ```

2. After `listingsDao.updateListing(listingItem)`, compute removed spaces:
   ```typescript
   const newSpaceIds = new Set((listingItem.spaces ?? []).map(s => s.assetId));
   const removedSpaces = (rtnListing?.spaces ?? []).filter(s => !newSpaceIds.has(s.assetId));
   ```

3. If `removedSpaces.length > 0`, clean up orphaned calendar entries across all reservations for this listing:
   ```typescript
   const reservations = await this.reservationsDao.getReservationsByListingId(listingItem.listingId);
   await Promise.all(reservations.map(reservation =>
     this.reservationsDao.deleteCalendarEntriesForSpaces(
       removedSpaces,
       reservation.checkInDate,
       reservation.checkOutDate,
       reservation.reservationCode
     )
   ));
   ```

---

## Data flow after change

```
generateReservation / renewReservation
  └─ addListingInfo()        → fetches spaces from Listing (transient, not stored)
  └─ validateCalendar*()     → uses transient spaces
  └─ dao.generate/renew()    → uses transient spaces for calendar ops; spaces NOT written to DDB

removeReservation
  └─ getListing(['spaces'])  → fetches current spaces live
  └─ dao.clearReservation()  → uses live spaces for calendar deletion

listings.updateListing()
  └─ diff old vs new spaces
  └─ for each removed asset:
       dao.getReservationsByListingId()          → find affected reservations
       dao.deleteCalendarEntriesForSpaces()      → clean up orphaned calendar entries
```

## Edge cases

| Scenario | Behaviour |
|---|---|
| Asset removed from listing, reservation later cleared | Calendar entries for removed asset already cleaned up at listing update time; `clearReservation` operates on current spaces only |
| Asset removed from listing, reservation renewed | Same as above — orphaned entries cleaned at listing update; renew operates on current spaces |
| Listing spaces cleared entirely | All calendar entries for all removed assets cleaned up; future reservations will have no calendar entries (no spaces to block) |
| `rtnListing` is null (new listing) | `removedSpaces` will be empty; no cleanup needed |
