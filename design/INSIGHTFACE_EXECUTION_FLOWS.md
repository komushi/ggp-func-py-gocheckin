# InsightFace Backend — Execution Flows

## Context

When `FACE_BACKEND == 'insightface'`, only **UC1 (Member Identification)** is active.
UC3 (Unknown Face), UC4 (Group Size), UC5 (Non-Active Member), and UC8 (Body Detection)
all require Hailo hardware and are disabled by `get_uc_toggles()`.

The gate check and timer-expiry person check are skipped (no `gate_check` / `get_extend_check`
on InsightFace's `FaceAnalysisChild`). Timer extension is **motion-only**: the session extends
as long as `active_occupancy` is non-empty (i.e. a `stop_detection` event has not arrived).

---

## Flow 1 — No Face Detected

```mermaid
sequenceDiagram
    participant IoT
    participant py_handler
    participant GStreamer as gstreamer_threading
    participant Detector as FaceRecognitionBase / face_recognition.py
    participant MatchHandler as SecurityHandlerChain

    IoT->>py_handler: trigger_detection {cam_ip, lock_asset_id}
    py_handler->>py_handler: trigger_face_detection()<br>get_uc_toggles() → uc8_enabled=False<br>log: "UC8 toggle disabled, skipping gate check"
    py_handler->>GStreamer: feed_detecting(TIMER_DETECT)
    py_handler->>py_handler: store context_snapshot<br>{specific_locks, trigger_started_at}

    loop every frame (while is_feeding)
        GStreamer->>Detector: cam_queue.put(CMD, frame, cam_info)
        Detector->>Detector: process_frame()<br>face_app.get(img) → []<br>debug: "frame #N age:X face(s):0"<br>return []
        Note over Detector: matched_faces=[], unmatched_faces=[]<br>handler NOT called, identified stays False
    end

    loop every 10s (timer expiry)
        GStreamer->>py_handler: handle_timer_expiry()
        py_handler->>py_handler: uc8_enabled=False<br>→ person_check=PASS (motion-only)<br>active_occupancy non-empty → motion=PASS
        py_handler->>GStreamer: extend_timer(10s)
        GStreamer->>GStreamer: stop_feeding() blocked — timer extended
    end

    IoT->>py_handler: stop_detection {cam_ip, lock_asset_id}
    py_handler->>py_handler: handle_occupancy_false()<br>active_occupancy → empty
    py_handler->>GStreamer: stop_feeding()
    GStreamer->>Detector: cam_queue.put(SESSION_END)
    Detector->>Detector: log: "SESSION END frames:N identified:False"
    Detector->>MatchHandler: on_session_end()
    Note over MatchHandler: uc4_uc8_enabled=False → return immediately
    Note over py_handler: Nothing published to IoT
```

---

## Flow 2 — Face Detected, No Match

```mermaid
sequenceDiagram
    participant IoT
    participant py_handler
    participant GStreamer as gstreamer_threading
    participant Detector as FaceRecognitionBase / face_recognition.py
    participant MatchHandler as SecurityHandlerChain

    IoT->>py_handler: trigger_detection {cam_ip, lock_asset_id}
    py_handler->>GStreamer: feed_detecting(TIMER_DETECT)
    py_handler->>py_handler: store context_snapshot

    loop frames (identified still False)
        GStreamer->>Detector: cam_queue.put(CMD, frame, cam_info)
        Detector->>Detector: process_frame()<br>face_app.get(img) → [face_obj]<br>find_match(embedding, threshold)<br>→ (None, sim=0.31, best_name)<br>info: "detected:#N best_match:X best_sim:0.31 (no match)"<br>return []
        Note over Detector: matched_faces=[], unmatched_faces=[]<br>UC3 skipped: unmatched_faces=[] AND uc3_enabled=False<br>handler NOT called, identified stays False
    end

    Note over py_handler,MatchHandler: Session timer/stop flow identical to Flow 1.<br>Nothing published to IoT.
```

---

## Flow 3 — Face Detected and Recognized

```mermaid
sequenceDiagram
    participant IoT
    participant py_handler
    participant GStreamer as gstreamer_threading
    participant Detector as FaceRecognitionBase / face_recognition.py
    participant SecurityChain as SecurityHandlerChain
    participant DefaultMH as DefaultMatchHandler
    participant Queue as scanner_output_queue

    IoT->>py_handler: trigger_detection {cam_ip, lock_asset_id}
    py_handler->>py_handler: trigger_face_detection()<br>get_uc_toggles() → uc8_enabled=False<br>log: "UC8 toggle disabled, skipping gate check"
    py_handler->>GStreamer: feed_detecting(TIMER_DETECT)
    py_handler->>py_handler: store context_snapshot<br>{specific_locks={lock_asset_id}, trigger_started_at=T0}

    GStreamer->>Detector: cam_queue.put(CMD, frame, cam_info)
    Detector->>Detector: process_frame()<br>face_app.get(img) → [face_obj]<br>find_match(embedding, threshold)<br>→ (active_member, sim=0.52, name)<br>info: "detected:#N fullName:John sim:0.52 (MATCH)"<br>matched_faces = [(face, active_member, sim)]

    Detector->>Detector: identified = True<br>build MatchEvent(<br>  matched_faces=[(face, member, sim)],<br>  unmatched_faces=[],<br>  person_count=0, max_simultaneous=0<br>)

    Detector->>SecurityChain: on_match(MatchEvent)
    SecurityChain->>SecurityChain: uc1_enabled=True (P2) / False (P1)<br>uc3_enabled=False, uc5_enabled=False<br>category = member.get('category','ACTIVE') → 'ACTIVE'

    alt P2 camera (has locks) AND uc1_enabled
        SecurityChain->>SecurityChain: _handle_uc1_member()<br>session.unlocked = True<br>info: "UC1: First unlock triggered by ACTIVE John Smith"
    else P1 camera
        SecurityChain->>SecurityChain: _handle_uc1_member()<br>info: "UC1: John Smith detected (P1 - log only)"
    end

    SecurityChain->>DefaultMH: on_match(authorized_event)
    DefaultMH->>DefaultMH: draw bbox on snapshot<br>cv2.imwrite(local_file_path, img)
    DefaultMH->>DefaultMH: build member_payload + snapshot_payload
    DefaultMH->>Queue: put({<br>  type: "member_detected",<br>  members: [{payload, keyNotified, authorizedSpaces}],<br>  cam_ip, detecting_txn,<br>  local_file_path, first_frame_at<br>})

    Queue->>py_handler: fetch_scanner_output_queue dequeues message
    py_handler->>py_handler: lookup context_snapshot(cam_ip, detecting_txn)<br>→ clicked_locks = [lock_asset_id]<br>→ trigger_started_at = T0
    py_handler->>py_handler: log timing:<br>"MEMBER DETECTED trigger_to_identified: Xms"
    py_handler->>GStreamer: stop_feeding()

    py_handler->>py_handler: authorized_spaces = member_entry['authorizedSpaces']<br>member_clicked_locks = [l for l in clicked_locks<br>  if lock_items[l]['roomCode'] in authorized_spaces]<br>member_payload['clickedLocks'] = member_clicked_locks

    py_handler->>py_handler: uploader_app.put_object(snapshot → S3)
    py_handler->>IoT: publish gocheckin/{THING_NAME}/video_clipped (snapshot)

    loop for each member in members
        alt keyNotified == False
            py_handler->>py_handler: update_member(keyNotified=True)
            py_handler->>py_handler: fetch_members(forced=True)
            py_handler->>IoT: publish gocheckin/{THING_NAME}/member_detected<br>(clickedLocks=[lock_asset_id])
        end
        py_handler->>IoT: publish gocheckin/member_detected
    end
```
