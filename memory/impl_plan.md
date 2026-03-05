# Implementation Plan: SECURITY_USE_CASES.md Alignment + analyze_faces Fix

## Constraint
`face_recognition.py` (InsightFace/UC1-only) is NOT modified.
All changes go to `face_recognition_base.py`, `face_recognition_hailo.py`, `match_handler.py`, `py_handler.py`.

---

## Step 1 — `py_handler.py`: Toggle rename + analyze_faces bug fix (isolated, no deps)

### 1a. Toggle rename
- `has_config_changed` critical_fields: `'enable_uc1_uc2'` → `'enable_uc1'`
- `get_uc_toggles` docstring: remove "UC2 (Tailgating)" reference, "UC2-UC5" → "UC3-UC5"
- `get_uc_toggles`: `camera_item.get('enable_uc1_uc2', True)` → `camera_item.get('enable_uc1', True)`
- `get_uc_toggles` return dict key: `'uc1_uc2_enabled'` → `'uc1_enabled'`
- `is_hailo_device` docstring: "UC2-UC5, UC8 supported" → "UC3-UC5, UC8 supported"

### 1b. analyze_faces bug fix (lines 656–672)
`HailoUC8App.get()` returns a 3-tuple `(faces, person_count, max_simultaneous)` but
`FaceAnalysisChild.get()` returns a plain list. The current code assigns either directly to
`faces`, causing `len()` to always return 3 for Hailo and `reference_faces[0].embedding` to crash.

Fix: unpack the tuple before using the result.
```python
result = face_app.get(img_data, det_size=size)
faces = result[0] if isinstance(result, tuple) else result
if len(faces) > 0:
    logger.info(f'analyze_faces out with {len(faces)} faces')
    return faces
```

Note: the detection-size retry loop has no effect on Hailo (HEF ignores det_size),
so Hailo always returns on the first iteration. This is harmless.

---

## Step 2 — `face_recognition_base.py`: Comment fix + multi-category member support

### 2a. Comment fix
- Line 75: `# UC2/UC4: Call session end handler` → `# UC4: Call session end handler`

### 2b. Add multi-category member support (new alongside existing `active_members`)

Keep `active_members`, `find_match`, `_build_member_embeddings` entirely unchanged.
InsightFace path uses only these — no breakage.

Add NEW fields to `__init__`:
```python
self._members_by_category = {}        # category -> list of member dicts
self.category_embeddings = {}         # category -> np.ndarray (N, 512)
self.category_norms = {}              # category -> np.ndarray (N,)
```

Add NEW property + setter `all_members_by_category`:
```python
@property
def all_members_by_category(self):
    return self._members_by_category

@all_members_by_category.setter
def all_members_by_category(self, value):
    # value: dict {category: list_of_member_dicts}
    # rebuild per-category embedding matrices
    self._members_by_category = value or {}
    self._build_category_embeddings()
```

Add NEW method `_build_category_embeddings()`:
Iterates over each category in `_members_by_category`, builds embedding matrix + norms.
Same logic as `_build_member_embeddings` but per-category.

Add NEW method `find_match_with_category(face_embedding, threshold)`:
Priority order: `['BLOCKLIST', 'ACTIVE', 'INACTIVE', 'STAFF']`
- For each category in order, compute cosine similarities against that category's matrix.
- Return first category where max_sim >= threshold: `(member_copy_with_category_field, sim, name, category)`
- Track best overall (across all categories) for UNKNOWN logging.
- If no category matches: return `(None, best_sim, best_name, None)`.
  best_name is the globally closest member name (for UC3 forensic logging).

Add NEW helper `has_any_members()`:
Returns `True` if any category in `_members_by_category` has at least one member.
Used by Hailo's skip condition.

---

## Step 3 — `face_recognition_hailo.py`: Multi-category matching + skip condition fix

### 3a. Comment fix
- Line 58: `# Structure: { cam_ip: {'uc8_enabled': bool, 'uc1_uc2_enabled': bool, ...} }`
  → `'uc1_enabled'`

### 3b. `process_frame` skip condition (lines 1036–1045)
Current: `if not self.active_members: skip`
New: `if not self.active_members and not self.has_any_members(): skip`

This ensures face recognition runs whenever ANY category has data, per design:
"SCRFD+ArcFace inference runs if ANY database has data (ACTIVE, INACTIVE, STAFF, or BLOCKLIST)."

### 3c. `process_frame` matching (lines 1058–1078)
Replace `self.find_match(face.embedding, threshold)` with
`self.find_match_with_category(face.embedding, threshold)`.

Return signature changes:
```python
member, sim, best_name, category = self.find_match_with_category(face.embedding, threshold)
```

- If category is not None (any category matched):
  → Set `member['category'] = category` (already set by find_match_with_category)
  → Append to `matched_faces` regardless of category (ACTIVE/INACTIVE/STAFF/BLOCKLIST)
  → match_handler does the routing by category
- If no match (category is None):
  → Append to `unmatched_faces` as before (UC3 unknown face)
  → Log includes best_name + best_sim across all categories for forensics

Log message update: include `category` in the match log line.

---

## Step 4 — `match_handler.py`: UC2 removal + toggle rename + STAFF routing + authorized_member_matched

### 4a. `SessionState`
- Remove: `unlock_count: int = 0`, `first_unlock_at: float = 0.0` (UC2-only)
- Add: `authorized_member_matched: bool = False`
- Update docstring: remove "UC2 (tailgating)" reference

### 4b. `SecurityHandlerChain` docstring
Remove UC2 from priority list. Updated list:
```
1. UC5: Blocklist check (priority 5) - checked first
2. UC1: Member identification + unlock (priority 10) — ACTIVE and STAFF
3. UC5: Non-active member alert (priority 20) — INACTIVE only
4. UC3: Unknown face logging (priority 40)
Session-level: UC4 group size validation
```

### 4c. `_is_uc_enabled`
- `uc_field == 'uc1_uc2_enabled'` → `uc_field == 'uc1_enabled'`
- `os.environ.get('UC1_UC2_ALWAYS_ENABLED')` → `os.environ.get('UC1_ALWAYS_ENABLED')`

### 4d. `on_match` loop
Replace current body with category-based routing:
```python
uc1_enabled = self._is_uc_enabled(cam_ip, 'uc1_enabled')   # renamed
uc3_enabled = self._is_uc_enabled(cam_ip, 'uc3_enabled')
uc5_enabled = self._is_uc_enabled(cam_ip, 'uc5_enabled')

for face, member, sim in event.matched_faces:
    category = member.get('category', 'ACTIVE')
    member_key = f"{member['reservationCode']}-{member['memberNo']}"

    # Priority 1: BLOCKLIST → UC5 (highest priority, blocks unlock)
    if uc5_enabled and category == 'BLOCKLIST':
        self._handle_uc5_blocklist(event, member, session)
        continue

    # Priority 2 & 4: ACTIVE or STAFF → UC1 (unlock, snapshot, member_detected)
    if uc1_enabled and category in ('ACTIVE', 'STAFF'):
        self._handle_uc1_member(event, member, sim, session)

    # Priority 3: INACTIVE → UC5 non-active alert only (no unlock)
    if uc5_enabled and category == 'INACTIVE':
        self._handle_uc5_non_active(event, member, session)

    session.distinct_members.add(member_key)

# UC3: unknown faces
if uc3_enabled and event.unmatched_faces:
    self._handle_uc3_unknown(event)

# Default snapshot+queue: only for ACTIVE and STAFF matched faces
active_staff_faces = [
    (f, m, s) for f, m, s in event.matched_faces
    if m.get('category', 'ACTIVE') in ('ACTIVE', 'STAFF')
]
if active_staff_faces:
    import copy
    filtered_event = copy.copy(event)
    filtered_event.matched_faces = active_staff_faces
    self.default_match_handler.on_match(filtered_event)
```

### 4e. `_handle_uc1_member`
- Add: `session.authorized_member_matched = True`
- Remove: `session.unlock_count = 1`, `session.first_unlock_at = ...` (UC2 tracking gone)
- Keep: `session.unlocked = True` (still used for session audit / block_further_unlocks logic)

### 4f. Remove methods
- Delete `_handle_uc2_tailgating` entirely
- Delete `_publish_tailgating_alert` entirely

### 4g. `on_session_end` (UC4)
- Replace `if final_state.unlock_count >= 1:` (or similar unlock_count check)
  with `if final_state.authorized_member_matched:`
- Update log message to use `authorized_member_matched`

---

## Step 5 — `py_handler.py`: Multi-category member fetch (Hailo path only)

### 5a. Add reservation fetch helpers (after `get_active_reservations`)

```python
def get_inactive_reservations(days_back=30):
    """Past guests: checkOutDate < today AND checkOutDate >= today - days_back."""

def get_staff_reservations():
    """Staff members: reservations with isStaff=True (or equivalent flag)."""

def get_blocklist_reservations():
    """Blocklisted individuals: reservations with isBlocklisted=True (or equivalent flag)."""
```

Schema note: exact filter attribute names (e.g. `isStaff`, `isBlocklisted`) must match
the TBL_RESERVATION schema. Use `Attr('isStaff').eq(True)` / `Attr('isBlocklisted').eq(True)`.

### 5b. Add `get_members_for_reservations(reservations, category)`
Extracts TBL_MEMBER records for a list of reservations, stamps each with `member['category'] = category`.
Shared query logic (same as `get_active_members` internals).

### 5c. Add `get_all_category_members()`
```python
def get_all_category_members():
    """Returns dict: {category: [member_dicts]} for ACTIVE, INACTIVE, STAFF, BLOCKLIST."""
    active = get_members_for_reservations(get_active_reservations_filtered(), 'ACTIVE')
    inactive = get_members_for_reservations(get_inactive_reservations(), 'INACTIVE')
    staff = get_members_for_reservations(get_staff_reservations(), 'STAFF')
    blocklist = get_members_for_reservations(get_blocklist_reservations(), 'BLOCKLIST')
    return {'ACTIVE': active, 'INACTIVE': inactive, 'STAFF': staff, 'BLOCKLIST': blocklist}
```

Note: `get_active_reservations` currently has the FilterExpression commented out —
restore the filter (`checkInDate <= today <= checkOutDate`) and rename to
`get_active_reservations_filtered()` to distinguish from original.
The existing `get_active_members()` for InsightFace path is kept as-is.

### 5d. Update `fetch_members`
```python
def fetch_members(forced=False):
    ...
    if FACE_BACKEND == 'hailo':
        all_members = get_all_category_members()
        # Keep active_members global for backward compat (used by InsightFace path)
        active_members = all_members.get('ACTIVE', [])
        if thread_detector is not None:
            thread_detector.active_members = active_members          # for base find_match compat
            thread_detector.all_members_by_category = all_members   # new multi-category setter
    else:
        active_members = get_active_members()
        if thread_detector is not None:
            thread_detector.active_members = active_members
```

---

## Execution Order

1. Step 1 — `py_handler.py` (toggle rename + analyze_faces fix) — independent, do first
2. Step 2 — `face_recognition_base.py` (comments + multi-category support)
3. Step 3 — `face_recognition_hailo.py` (depends on Step 2)
4. Step 4 — `match_handler.py` (UC2 removal + category routing — depends on category field from Step 3)
5. Step 5 — `py_handler.py` (multi-category fetch — depends on Steps 2+3)

Steps 2–5 should be done in one pass (they form a coherent feature).

---

## Files NOT changed
- `face_recognition.py` — InsightFace UC1-only, stays as-is
- `gstreamer_threading.py`, `s3_uploader.py`, `onvif_process.py`, `web_image_process.py` — not affected
