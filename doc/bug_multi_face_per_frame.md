# Bug #8: Multi-Face Per Frame Causes Snapshot Collision, Context Loss, and Upload Failure

**Status:** OPEN
**Discovered:** 2026-02-04
**Priority:** High

---

## Summary

When a single frame contains 2 or more recognized faces, the `for face in faces` loop in `face_recognition.py` produces multiple `member_detected` queue entries from the same frame. The downstream processing in `py_handler.py:fetch_scanner_output_queue()` assumes one match per detection session, causing three cascading failures:

1. Snapshot file collision (both faces overwrite the same `.jpg`)
2. Context snapshot deleted after first match (second match gets wrong context)
3. S3 upload fails for the second match (`FileNotFoundError`)

## Root Cause

The system was designed around a **one-match-per-session** assumption. Multiple code paths break when `for face in faces` produces more than one match in a single frame iteration.

### Sub-issue A: Snapshot filename collision

**Location:** `face_recognition.py:148-160`

The snapshot filename is derived from `frame_time`:

```python
date_folder = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
time_filename = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
local_file_path = os.path.join(..., date_folder, time_filename + ext)
```

All faces in the same frame share the same `frame_time`, so they produce the same filename. Face 2's `cv2.imwrite` overwrites Face 1's snapshot. Both queue entries reference the same `local_file_path`.

Additionally, the bounding box rectangle drawn on the image (line 158) mutates `raw_img` via `img = raw_img.astype(np.uint8)` — this creates a copy, but each face's snapshot only shows its own bounding box, not both.

### Sub-issue B: Context snapshot consumed on first match

**Location:** `py_handler.py:1112-1114`

```python
# Clear context snapshot after use
if snapshot_key and snapshot_key in context_snapshots:
    del context_snapshots[snapshot_key]
```

Both queue entries share the same `detecting_txn`, so they share the same `snapshot_key = (cam_ip, detecting_txn)`. The first `member_detected` deletes the context snapshot. The second falls through to the warning path:

```
[WARN] fetch_scanner_output_queue, no context snapshot found for detecting_txn=..., using current context
```

The fallback context has `onvifTriggered=False` (wrong) because `trigger_lock_context` was already deleted at line 1117-1118 by the first match.

### Sub-issue C: S3 upload fails for second match

**Location:** `py_handler.py:1129`

```python
uploader_app.put_object(object_key=property_object_key, local_file_path=local_file_path)
```

The first match uploads and the S3 uploader (or the OS) removes/moves the local file. When the second match tries to upload the same `local_file_path`, the file no longer exists:

```
[ERROR] FileNotFoundError: [Errno 2] No such file or directory: '/etc/insightface/192.168.11.62/2026-02-04/02:02:32.jpg'
```

### Sub-issue D: Duplicate stop_feeding calls

**Location:** `py_handler.py:1121-1122`

```python
logger.info(f"fetch_scanner_output_queue, member_detected WANT TO stop_feeding NOW")
thread_gstreamers[cam_ip].stop_feeding()
```

Called once per queue item. With 2 matches, `stop_feeding()` is called twice. The second call is harmless but unnecessary.

## Log Evidence

```
face(s): 2                                          ← 2 faces in frame
fullName: RULIN sim: 0.5044 (MATCH)                 ← Face 1 matches
snapshot taken at .../02:02:32.jpg                   ← Face 1 writes snapshot
fullName: Xu sim: 0.5338 (MATCH)                    ← Face 2 matches
snapshot taken at .../02:02:32.jpg                   ← Face 2 OVERWRITES same file

using context snapshot for detecting_txn=...         ← Face 1 consumes context (OK)
member_detected WANT TO stop_feeding NOW             ← Face 1 stops feeding
session ended                                        ← Session ends

no context snapshot found for detecting_txn=...      ← Face 2 context MISSING
member_detected WANT TO stop_feeding NOW             ← Face 2 stops feeding (redundant)
FileNotFoundError: .../02:02:32.jpg                  ← Face 2 upload FAILS
```

## Impact

- Second matched member's snapshot is lost (upload fails)
- Second matched member gets wrong trigger context (`onvifTriggered=False`)
- IoT message for second member is published with incorrect metadata
- First member's snapshot is actually Face 2's image (overwritten by `cv2.imwrite`)

## Proposed Fix

### Option A: Aggregate all matches into a single queue entry

Collect all matched faces from the same frame into one `member_detected` event:

```python
matched_members = []
for face in faces:
    threshold = float(os.environ['FACE_THRESHOLD_INSIGHTFACE'])
    active_member, sim = self.find_match(face.embedding, threshold)
    if active_member is not None:
        matched_members.append((face, active_member, sim))

if matched_members:
    # Build single queue entry with all matches
    # Save one composite snapshot with all bounding boxes
    # Put one item on scanner_output_queue with members list
```

Pros: Single context lookup, single `stop_feeding`, single snapshot file.
Cons: Requires changes to `fetch_scanner_output_queue` to handle a list of members.

### Option B: Per-face unique filenames + shared context

Keep separate queue entries but fix each sub-issue independently:

1. **Filename**: Append face index to filename: `02:02:32_face0.jpg`, `02:02:32_face1.jpg`
2. **Context**: Don't delete `context_snapshots` after first use; use reference counting or delete only when `stop_feeding` is called
3. **stop_feeding**: Only call on the first `member_detected` for a given `detecting_txn`

Pros: Minimal structural change.
Cons: Multiple IoT messages per frame, context lifecycle becomes more complex.

### Recommended: Option A

Option A is cleaner and matches the real-world semantics — one frame produces one detection event, potentially with multiple recognized faces. This avoids all the shared-state issues.

## Files Affected

| File | Lines | Issue |
|---|---|---|
| `face_recognition.py` | 109-197 | `for face in faces` loop produces multiple queue entries |
| `face_recognition_hailo.py` | 635-723 | Same loop, same issue |
| `py_handler.py` | 1085-1140 | `fetch_scanner_output_queue` assumes single match per session |

## Testing

### Reproduce
1. Register 2 members with face embeddings
2. Have both people stand in front of the same camera
3. Trigger detection (ONVIF or occupancy)
4. Observe log for `face(s): 2` followed by two MATCH lines
5. Verify `FileNotFoundError` and `no context snapshot found` warnings

### Verify Fix
1. Same setup as above
2. Both members should appear in the detection result
3. Snapshot should contain both bounding boxes
4. No `FileNotFoundError` in logs
5. Trigger context (`onvifTriggered`, `occupancyTriggeredLocks`) correct for all members
6. Single `stop_feeding` call per detection session
