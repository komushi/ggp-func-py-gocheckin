# Bug #7: Stale Embeddings Matrix After Member Update

**Status:** TEMP FIX (property setter), needs incremental update
**Discovered:** 2026-02-04
**Priority:** High
**Introduced in:** commit 627b22a (matrix comparison)

---

## Summary

`_build_member_embeddings()` is only called during `FaceRecognition.__init__()`. When `py_handler.py` updates `thread_detector.active_members` at runtime via `fetch_members()`, the numpy embeddings matrix (`member_embeddings`, `member_norms`) is never rebuilt. The detector continues matching against the stale matrix from startup.

## Symptom

- New guests who check in after startup are never recognized
- Guests who check out after startup continue to match (phantom matches)
- `session ended` log shows `identified_at: 0` for every session even when guests are present

## Root Cause

In commit 627b22a, face matching was changed from per-member loop to vectorized matrix comparison. The matrix is pre-computed in `__init__`:

```python
# face_recognition.py (and face_recognition_hailo.py)
def __init__(self, ...):
    self.active_members = active_members
    self._build_member_embeddings()  # Only called here
```

But `py_handler.py:894` replaces the member list at runtime without rebuilding:

```python
# py_handler.py:fetch_members()
thread_detector.active_members = active_members  # Matrix NOT rebuilt
```

`fetch_members()` is called from:
- `py_handler.py:298` - `init_face_detector()` (startup, no issue)
- `py_handler.py:1568` - `trigger_face_detection()` (every detection trigger, BUG)

## Temporary Fix Applied (2026-02-04)

Converted `active_members` to a Python `@property` with a setter that automatically rebuilds the matrix on every assignment:

```python
@property
def active_members(self):
    return self._active_members

@active_members.setter
def active_members(self, value):
    self._active_members = value
    self._build_member_embeddings()
```

### Files Changed
1. `face_recognition.py` - Added property setter, changed `__init__` to use `self._active_members`
2. `face_recognition_hailo.py` - Same changes

### Why This Is Temporary

The property setter does a **full rebuild** every time. This is correct but does not scale:

| Phase | Operation | 50K members estimate (RPi) |
|---|---|---|
| `get_active_members()` | Sequential DynamoDB query per reservation | R x ~5-10ms |
| `float()` conversion | `np.array([float(v) for v in item['faceEmbedding']])` x 50K | ~5-10s |
| `_build_member_embeddings()` | Python loop + `np.array()` on (50K, 512) matrix (~100MB) | ~2-5s |
| **Total** | | **~10-20s + R x 10ms** |

During the rebuild window:
- Detector thread may read partially-built matrix (no thread safety)
- Detection is effectively paused for the duration

### Thread Safety Issue

The setter runs in the `fetch_members()` caller thread (main/timer thread), while `find_match()` reads `self.member_embeddings` in the detector thread. There is no lock protecting the matrix swap. A detection frame processed mid-rebuild could read inconsistent state.

---

## Required Fix: Incremental Matrix Update

### Goal

Instead of rebuilding the entire matrix on every `fetch_members()` call, diff the old and new member sets and apply only the delta.

### Member Identity Key

Each member is uniquely identified by `reservationCode-memberNo` (composite key in DynamoDB).

### Data Structures Involved

```
self._active_members     list[dict]       Row index maps 1:1 to matrix
self.member_embeddings   np.ndarray(N,512)  Pre-computed embeddings
self.member_norms        np.ndarray(N,)     Pre-computed L2 norms
```

`find_match()` uses `self.active_members[max_idx]` to map matrix row back to member dict.

### Incremental Algorithm

```
old_keys = {f"{m['reservationCode']}-{m['memberNo']}": (idx, m) for idx, m in enumerate(old_members)}
new_keys = {f"{m['reservationCode']}-{m['memberNo']}": m for m in new_members}

deleted = old_keys.keys() - new_keys.keys()    # rows to remove
added   = new_keys.keys() - old_keys.keys()     # rows to append
common  = old_keys.keys() & new_keys.keys()      # check for embedding changes
```

| Operation | Matrix action | Cost |
|---|---|---|
| **Delete** (checkout) | `np.delete(matrix, indices, axis=0)` + remove from list | O(N) copy but no recompute |
| **Insert** (new checkin) | `np.vstack([matrix, new_rows])` + append to list | O(delta) |
| **Update** (re-upload photo) | `matrix[idx] = new_embedding` in place | O(1) per member |

### Thread Safety Requirement

The matrix swap must be atomic from the detector thread's perspective:

```python
@active_members.setter
def active_members(self, value):
    # Build new matrix and list in local variables
    new_members, new_embeddings, new_norms = self._compute_incremental(value)
    # Atomic swap (Python GIL guarantees attribute assignment is atomic)
    self._active_members = new_members
    self.member_embeddings = new_embeddings
    self.member_norms = new_norms
```

The Python GIL ensures each individual attribute assignment is atomic, but the three assignments are not collectively atomic. To be safe, build the new state into a single container object and swap that in one assignment.

### Startup Bottleneck: `get_active_members()` Does Not Scale

Even with incremental matrix updates for runtime refreshes, the **first load at startup** must fetch all members from DynamoDB. The current implementation does not scale for 50,000 users:

1. **Sequential queries per reservation** (`py_handler.py:831-841`): One `table.query()` call per reservation, executed in a `for` loop. If there are 10,000 reservations, that is 10,000 sequential HTTP round-trips to DynamoDB Local. At ~5-10ms each, this alone takes **50-100 seconds**.

2. **No pagination** (`py_handler.py:835-841`): `table.query()` returns at most 1MB per call. If a single reservation has many members with 512-element embeddings, results may be truncated silently (only the first page is read, `LastEvaluatedKey` is not checked).

3. **Pure Python float conversion** (`py_handler.py:853`): `np.array([float(value) for value in item['faceEmbedding']])` runs 512 `float()` calls per member in a Python loop. At 50K members that is 25.6 million `float()` calls, taking ~5-10 seconds on RPi.

4. **No scan filter** (`py_handler.py:770-772`): `get_active_reservations()` does a `table.scan()` with the `FilterExpression` commented out, returning **all** reservations regardless of check-in/check-out date. This means every reservation ever created is queried for members.

These issues exist independently of the matrix rebuild bug. At startup there is no prior state to diff against, so the full fetch + full matrix build is unavoidable. Optimizations needed:

- **Re-enable `FilterExpression`** in `get_active_reservations()` to only return current reservations
- **Parallel queries** using `ThreadPoolExecutor` to fetch members for multiple reservations concurrently
- **Add pagination** handling for `table.query()` responses (`LastEvaluatedKey`)
- **Use `np.fromiter` or `struct.unpack`** instead of Python-level `float()` loop for embedding conversion
- **Local embedding cache** (file-based) so restarts don't require a full DynamoDB re-fetch

### Acceptance Criteria

1. After `fetch_members()`, new members are immediately matchable
2. After `fetch_members()`, removed members no longer match
3. Updated embeddings (re-uploaded face photo) take effect immediately
4. No detection gap during matrix update
5. Log shows `Built embeddings matrix: N members` with correct count after update
6. Log shows incremental stats: `+2 added, -1 removed, 0 updated` (or similar)

---

## Testing

### Verify Temp Fix
1. Start system with 0 members
2. Add a reservation + member with face embedding to DynamoDB
3. Trigger detection (ONVIF or occupancy)
4. Verify log shows `Built embeddings matrix: 1 members` during detection
5. Verify face is recognized

### Verify Bug Still Exists Without Fix
1. Revert property setter
2. Start system, verify initial member count
3. Add new member to DynamoDB
4. Trigger detection
5. Verify new member is NOT recognized (stale matrix)
