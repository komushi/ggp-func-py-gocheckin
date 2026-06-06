# Listing Spaces Edge Sync (Python)

## Problem

The Python face recognition module needs current `spaces` data for member authorization. Currently, it reads `spaces` from `TBL_RESERVATION`, which contains **stale data** (copied from the listing at reservation create/renew time).

When a listing's `spaces` change (assets added/removed), the reservation's `spaces` become outdated, causing:
- Member authorization failures
- Group size validation errors
- Incorrect lock access decisions

---

## Solution

Read `spaces` from `TBL_LISTING` (synced by TS edge from listing shadows) instead of `TBL_RESERVATION`.

---

## Data Flow

```
Cloud Listing Update
    │
    ▼
IoT Shadow Delta (listing:<listingId>)
    │
    ▼
TS Edge: listings.service → TBL_LISTING (spaces updated)
    │
    ▼
Python: fetch_members() reads TBL_LISTING.spaces
    │
    ▼
Member authorization with current spaces
```

---

## Current Implementation (BROKEN)

### Location: `py_handler.py:fetch_members()`

**Current Code** (lines ~1000-1020):
```python
def get_active_reservations():
    """Fetch active reservations from TBL_RESERVATION."""
    table = dynamodb.Table(os.environ['TBL_RESERVATION'])
    
    response = table.scan(
        ProjectionExpression='reservationCode, listingId, #spaces',
        ExpressionAttributeNames={'#spaces': 'spaces'}
    )
    
    items = response.get('Items', [])
    for item in items:
        # ❌ STALE: spaces copied from listing at create/renew time
        authorized_spaces = {s['uuid'] for s in item.get('spaces', [])}
```

**Problem**: `item.get('spaces', [])` returns stale data from `TBL_RESERVATION`.

---

## Required Fix

### Step 1: Add `get_listing_spaces()` Function

```python
def get_listing_spaces(host_id: str, listing_id: str) -> List[dict]:
    """Fetch current spaces for a listing from TBL_LISTING."""
    table = dynamodb.Table(os.environ['TBL_LISTING'])
    
    response = table.get_item(
        Key={
            'hostId': host_id,
            'listingId': listing_id
        }
    )
    
    spaces = response.get('Item', {}).get('spaces', [])
    logger.debug(f'get_listing_spaces [{listing_id}]: {len(spaces)} spaces')
    return spaces
```

### Step 2: Update `get_active_reservations()` and Similar Functions

```python
def get_active_reservations():
    """Fetch active reservations WITHOUT spaces (fetched separately)."""
    table = dynamodb.Table(os.environ['TBL_RESERVATION'])
    
    response = table.scan(
        ProjectionExpression='reservationCode, listingId, checkInDate, checkOutDate, isStaff, isBlocklisted'
        # ❌ REMOVED: 'spaces' from ProjectionExpression
    )
    
    items = response.get('Items', [])
    return items

def get_members_for_reservations(reservations, category):
    """Fetch members for reservations, with current spaces from TBL_LISTING."""
    host_id = os.environ['AWS_IOT_HOST_ID']
    tbl_listing = os.environ['TBL_LISTING']
    listing_table = dynamodb.Table(tbl_listing)
    
    for reservation in reservations:
        listing_id = reservation['listingId']
        
        # ✅ NEW: Fetch current spaces from TBL_LISTING
        listing_response = listing_table.get_item(
            Key={'hostId': host_id, 'listingId': listing_id}
        )
        spaces = listing_response.get('Item', {}).get('spaces', [])
        authorized_spaces = {s['uuid'] for s in spaces}
        
        # ... rest of member fetch logic ...
        for member in members:
            member['listingId'] = listing_id
            member['authorizedSpaces'] = authorized_spaces
```

### Step 3: Update Environment Variables

Add to `function.conf`:
```conf
environmentVariables {
    # ... existing ...
    TBL_LISTING = "gocheckin_listing"  # New: for spaces lookup
    AWS_IOT_HOST_ID = "your-host-id"   # Host ID for listing lookup
}
```

---

## Implementation Options

### Option A: Fetch Spaces Per Reservation (Simple)

**Pros**: Simple, minimal changes
**Cons**: N+1 query pattern (one query per reservation)

```python
for reservation in reservations:
    listing_id = reservation['listingId']
    spaces = get_listing_spaces(host_id, listing_id)  # Per-reservation query
    # ... use spaces ...
```

### Option B: Batch Fetch All Spaces (Optimized)

**Pros**: Single query for all listings
**Cons**: More complex, requires batch get API

```python
# Collect all listing IDs
listing_ids = {r['listingId'] for r in reservations}

# Batch fetch all spaces
response = ddb_client.batch_get_item(
    RequestItems={
        TBL_LISTING: {
            'Keys': [{'hostId': host_id, 'listingId': lid} for lid in listing_ids]
        }
    }
)

# Build lookup dict
spaces_by_listing = {
    item['listingId']: item['spaces']
    for item in response.get('Responses', {}).get(TBL_LISTING, [])
}

# Use in member fetch
for reservation in reservations:
    listing_id = reservation['listingId']
    authorized_spaces = {s['uuid'] for s in spaces_by_listing.get(listing_id, [])}
```

### Recommendation

Start with **Option A** (simple). If performance becomes an issue (many reservations), optimize to **Option B**.

---

## Testing

### Test 1: Listing Update → Python Sees Current Spaces

1. Update listing spaces in cloud (add/remove asset)
2. Wait for shadow sync (~1 second)
3. Trigger `fetch_members()` in Python
4. Verify: Python sees updated `spaces` in member authorization

**Expected**: `authorized_spaces` reflects current listing state

### Test 2: Reservation Renewal

1. Renew reservation in cloud (changes checkIn/checkOut)
2. TS edge refreshes reservation
3. Python fetches members
4. Verify: `spaces` are current (not stale from original reservation)

**Expected**: `spaces` match current listing, not original reservation

### Test 3: Missing Listing Record

1. Delete listing from `TBL_LISTING` (simulate sync failure)
2. Python fetches members for that listing
3. Verify: Graceful fallback (empty `authorized_spaces`, log warning)

**Expected**: `authorized_spaces = {}`, log warning about missing listing

---

## Related Docs

- **TS Edge**: `../ggp-func-ts-gocheckin/doc/LISTING_SPACES_SYNC.md` - How TS syncs listing shadows
- **TS Edge**: `../ggp-func-ts-gocheckin/doc/RESERVATION_REFRESH.md` - How TS refreshes reservations
- **Cloud**: `cloud/REMOVE_SPACES_FROM_RESERVATION.md` - Cloud-side design (reference only)
