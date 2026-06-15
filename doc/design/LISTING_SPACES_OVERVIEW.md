# Listing Spaces Sync - Implementation Overview

This document provides a quick reference for the listing spaces sync implementation across cloud and edge components.

---

## Problem

When a listing's `spaces` change (assets added/removed), existing reservation records retain **stale `spaces` data** (copied at create/renew time). This causes:
- Member authorization failures
- Incorrect lock access decisions
- Group size validation errors

---

## Solution

**Cloud**: Generate listing shadow deltas (`listing:<listingId>`) with current `spaces`
**Edge**: Sync shadows to local DDB, read `spaces` at runtime (not from reservations)

---

## Component Map

| Component | Location | Status | Doc |
|-----------|----------|--------|-----|
| **Cloud - Listing Shadow** | Cloud (external) | TODO | `doc/design/cloud/REMOVE_SPACES_FROM_RESERVATION.md` |
| **TS Edge - Shadow Sync** | `ggp-func-ts-gocheckin/doc/LISTING_SPACES_SYNC.md` | ✅ Implemented | See below |
| **TS Edge - Reservation Refresh** | `ggp-func-ts-gocheckin/doc/RESERVATION_REFRESH.md` | ✅ No changes needed | TS doesn't use `spaces` |
| **Python Edge - Spaces Fetch** | `ggp-func-py-gocheckin/doc/design/LISTING_SPACES_EDGE_SYNC.md` | ❌ Needs fix | See below |

**Key Finding**: The TS edge component (`reservations.service.ts`) does NOT use `spaces` at all. The fix is **Python-only**.

---

## Quick Links

### TS Edge (`ggp-func-ts-gocheckin`)

| Doc | Purpose |
|-----|---------|
| [`LISTING_SPACES_SYNC.md`](../ggp-func-ts-gocheckin/doc/LISTING_SPACES_SYNC.md) | How listing shadows sync to local DDB |
| [`RESERVATION_REFRESH.md`](../ggp-func-ts-gocheckin/doc/RESERVATION_REFRESH.md) | How reservation refresh works (no changes needed) |

**Key Files**:
- `listings.service.ts` - Process listing shadow deltas ✅
- `listings.dao.ts` - Store/fetch spaces in local DDB ✅
- `reservations.service.ts` - Does NOT use `spaces` (no changes needed)

### Python Edge (`ggp-func-py-gocheckin`)

| Doc | Purpose |
|-----|---------|
| [`design/LISTING_SPACES_EDGE_SYNC.md`](doc/design/LISTING_SPACES_EDGE_SYNC.md) | How Python fetches spaces from local DDB |

**Key Files**:
- `py_handler.py:fetch_members()` - Fetches members for face recognition ❌ (needs fix)
- `py_handler.py:get_active_reservations()` - Fetches reservations ❌ (reads stale spaces)

### Cloud Reference (External)

| Doc | Purpose |
|-----|---------|
| [`doc/design/cloud/REMOVE_SPACES_FROM_RESERVATION.md`](doc/design/cloud/REMOVE_SPACES_FROM_RESERVATION.md) | Cloud-side design for removing spaces from reservations |
| [`doc/design/cloud/SPACES_THROUGH_SHADOW_EDGE_REQUIREMENTS.md`](doc/design/cloud/SPACES_THROUGH_SHADOW_EDGE_REQUIREMENTS.md) | Shadow payload requirements for edge |

---

## Implementation Priority

### High Priority (Fix Now)

1. **Python Edge**: Update `fetch_members()` to read `spaces` from `TBL_LISTING` (not `TBL_RESERVATION`)
   - See: `doc/design/LISTING_SPACES_EDGE_SYNC.md`

### Medium Priority (Verify)

2. **TS Edge**: Verify `listings.service.processListingsShadow()` correctly syncs to local DDB
   - See: `../ggp-func-ts-gocheckin/doc/LISTING_SPACES_SYNC.md`

### Low Priority (Reference)

3. **Cloud**: Coordinate with cloud team on shadow generation (external to this repo)

---

## Data Flow

```
Cloud Listing Update
    │
    ▼
IoT Shadow Delta (listing:<listingId>)
    │
    ▼
TS Edge: listings.service → TBL_LISTING (spaces updated) ✅
    │
    ▼
Python: fetch_members() reads TBL_LISTING.spaces ❌ (needs fix)
    │
    ▼
Member authorization with current spaces
```

---

## Testing Checklist

- [ ] **Python**: `fetch_members()` reads from `TBL_LISTING` (not `TBL_RESERVATION`)
- [ ] **TS**: `refreshReservation()` requires no changes (it does not use `spaces`)
- [ ] **End-to-end**: Update listing → verify Python sees current spaces within 1 second
- [ ] **Edge case**: Missing listing in `TBL_LISTING` → graceful fallback (empty `authorized_spaces`)

---

## Related Docs

- **Security Use Cases**: `design/SECURITY_USE_CASES.md` - UC1-UC9 definitions
- **Refactor Design**: `design/REFACTOR_DETECTION_BUSINESS_LOGIC.md` - Detection architecture
- **Bug Tracker**: `bug/bug_issue_list.md` - Known issues and fixes
