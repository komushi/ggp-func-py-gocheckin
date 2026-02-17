# /onvif_notifications Endpoint

## Overview

The `/onvif_notifications` endpoint is a local HTTP listener (port **7777**) that receives ONVIF motion detection events from IP cameras. It is the primary trigger for video recording workflows.

> **Note:** Face detection is no longer triggered by ONVIF motion events. See [Lock-Triggered Face Detection](./lock_triggered_detection.md) for the new face detection trigger mechanism using Zigbee lock occupancy events.

## Endpoint Details

| Property | Value |
|----------|-------|
| Path | `/onvif_notifications` |
| Method | `POST` |
| Port | `7777` (configured via `http_port`) |
| Content-Type | XML (ONVIF WS-Notification) |

## Request Flow

### 1. XML Payload Parsing

The incoming ONVIF notification XML is parsed by `OnvifConnector.extract_notification()`:

```python
cam_ip, utc_time, is_motion_value = OnvifConnector.extract_notification(post_data, client_address[0])
```

**Extracted Fields:**
- `cam_ip` - Camera IP address (from XML `Address` element or fallback to `client_address[0]`)
- `utc_time` - Event timestamp (from `Message/@UtcTime` attribute)
- `is_motion_value` - Boolean indicating motion state (from `SimpleItem[@Name='IsMotion']/@Value`)

**Topic Filter:**
Only events with topic `tns1:RuleEngine/CellMotionDetector/Motion` are processed. Other topics return `None` values.

### 2. Async Processing

The endpoint immediately returns HTTP 200 and spawns a daemon thread for processing:

```python
thread_name = f"Thread-ONVIF-Notification-{cam_ip}-{utc_time}"
t = threading.Thread(target=async_handle, name=thread_name, daemon=True)
t.start()
```

This prevents blocking the camera's notification delivery.

### 3. Notification Handler

The `handle_notification()` function performs validation and triggers actions:

**Validation Checks:**
1. `is_motion_value` must be `True`
2. `cam_ip` must exist in `camera_items` dictionary
3. `thread_gstreamers[cam_ip]` must exist
4. GStreamer thread must be alive (`is_alive()`)
5. GStreamer must be playing (`is_playing`)

**Actions Based on Camera Configuration:**

| Camera Setting | Action |
|----------------|--------|
| `isRecording: true` | Triggers video recording workflow |

> **Deprecated:** The `isDetecting` setting no longer triggers face detection from ONVIF motion events. Face detection is now triggered by Zigbee lock occupancy events via the `trigger_detection` MQTT topic.

---

## Video Recording Workflow

When `camera_item['isRecording']` is `True`:

```
Motion Event
    │
    ▼
thread_gstreamer.start_recording(utc_time)
    │
    ├── Sets is_recording = True
    └── Stores recording start timestamp
    │
    ▼
set_recording_time(cam_ip, TIMER_RECORD, utc_time)
    │
    └── Schedules stop_recording after TIMER_RECORD seconds
    │
    ▼ (after timer fires)
stop_recording()
    │
    ▼
save_frames_as_video()
    │
    ├── Creates MP4 from buffered frames
    └── Puts "video_clipped" message to scanner_output_queue
    │
    ▼
Upload video to S3
Publish to IoT topic: gocheckin/{thing}/video_clipped
```

### Key Parameters

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `TIMER_RECORD` | `10` | Seconds to record after motion |
| `PRE_RECORDING_SEC` | `2.0` | Pre-buffered video before motion |
| `VIDEO_CLIPPING_LOCATION` | `/etc/insightface` | Local storage path |

---

## Face Detection Workflow (DEPRECATED for ONVIF)

> **Important:** This workflow is no longer triggered by ONVIF motion events. Face detection is now triggered by Zigbee lock occupancy events. See [Lock-Triggered Face Detection](./lock_triggered_detection.md).

The face detection workflow is triggered via the `gocheckin/trigger_detection` MQTT topic:

```
Lock Occupancy Event (zigbee2mqtt/{lockName}/occupancy)
    │
    ▼
TS Component → trigger_detection MQTT
    │
    ▼
trigger_face_detection(cam_ip)
    │
    ▼
fetch_members()                    # Refresh active guest list from DynamoDB
    │
    ▼
thread_gstreamer.feed_detecting()  # Start feeding frames for TIMER_DETECT seconds
    │
    ▼
Frames → decode pipeline → cam_queue
    │
    ▼
FaceRecognition thread consumes frames
    │
    ▼
face_app.get(raw_img)              # InsightFace detection
    │
    ▼
Compare embeddings vs active_members
    │
    ▼ (if similarity >= FACE_THRESHOLD)
Save annotated snapshot
    │
    ▼
scanner_output_queue.put("member_detected")
    │
    ▼
Upload snapshot to S3
Publish to IoT topics
Stop feeding (early termination)
```

### Key Parameters

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `TIMER_DETECT` | `10` | Seconds to run face detection after trigger |
| `FACE_THRESHOLD` | `0.30` | Minimum cosine similarity for face match |
| `AGE_DETECTING_SEC` | `4.0` | Max frame age (seconds) for processing |
| `DETECTING_SLEEP_SEC` | `0.1` | Sleep interval when queue is empty |
| `PRE_DETECTING_SEC` | `1.0` | Pre-buffered frames before trigger |

---

## Output Messages

### member_detected

Published when a guest face is recognized:

```json
{
  "type": "member_detected",
  "keyNotified": false,
  "cam_ip": "192.168.1.100",
  "local_file_path": "/etc/insightface/192.168.1.100/2024-01-15/10:30:45.jpg",
  "payload": {
    "hostId": "...",
    "propertyCode": "...",
    "coreName": "gg-core-001",
    "assetId": "camera-uuid",
    "assetName": "Front Door Camera",
    "cameraIp": "192.168.1.100",
    "reservationCode": "RES123",
    "listingId": "LISTING456",
    "memberNo": 1,
    "fullName": "John Doe",
    "similarity": 0.85,
    "recordTime": "2024-01-15T10:30:45.123Z",
    "checkInImgKey": "private/.../checkIn/1.jpg",
    "propertyImgKey": "private/.../properties/.../10:30:45.jpg"
  },
  "snapshot_payload": {
    "hostId": "...",
    "snapshotKey": "..."
  }
}
```

**IoT Topics:**
- `gocheckin/{AWS_IOT_THING_NAME}/member_detected` - Core-specific topic
- `gocheckin/member_detected` - Global topic (triggers lock unlock in TS component)

### video_clipped

Published when a video recording is complete:

```json
{
  "type": "video_clipped",
  "payload": {
    "cam_ip": "192.168.1.100",
    "cam_uuid": "camera-uuid",
    "cam_name": "Front Door Camera",
    "video_key": "{hostId}/properties/.../2024-01-15/10:30:45.mp4",
    "object_key": "private/{identityId}/.../10:30:45.mp4",
    "local_file_path": "/etc/insightface/.../10:30:45.mp4",
    "start_datetime": "2024-01-15T10:30:43.000Z",
    "end_datetime": "2024-01-15T10:30:55.000Z"
  }
}
```

**IoT Topic:** `gocheckin/{AWS_IOT_THING_NAME}/video_clipped`

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Camera disconnects before response | Logs warning, continues processing |
| Invalid XML payload | Returns 500, logs error |
| Camera not in `camera_items` | Silently exits handler |
| GStreamer not running | Silently exits handler |
| Face detection exception | Logs error, sets `stop_event` to restart thread |

---

## Thread Architecture

```
Main Thread
    │
    ├── Thread-HttpServer (runs start_http_server)
    │       │
    │       └── Thread-ONVIF-Notification-{cam_ip}-{time} (per request, daemon)
    │
    ├── Thread-Gst-{cam_ip} (GStreamer capture per camera)
    │
    ├── Thread-Detector (FaceRecognition, single instance)
    │
    └── Thread-FaceQueue (fetch_scanner_output_queue, uploads & publishes)
```

---

## ONVIF Subscription Setup

Cameras are subscribed to send notifications to this endpoint via `subscribe_onvif()`:

```python
onvif_sub_address = onvif_connectors[cam_ip].subscribe(
    cam_ip,
    old_onvif_sub_address,
    scanner_local_ip,  # Local IP of this Greengrass device
    http_port          # 7777
)
```

Subscription is created when:
- `camera_item['isDetecting']` is `True`, OR
- `camera_item['isRecording']` is `True`

> **Note:** Even though `isDetecting` no longer triggers face detection from ONVIF motion, it is still used to determine whether to subscribe to ONVIF events. This allows the camera to be ready to receive `trigger_detection` MQTT messages from the lock occupancy handler.

Subscription expiration: `ONVIF_EXPIRATION` (default: `PT1H` = 1 hour)
Renewal interval: `TIMER_CAM_RENEW` (default: 600 seconds)

---

## Related Documentation

### ggp-func-py-gocheckin
- [Lock-Triggered Face Detection](./lock_triggered_detection.md) - Main feature documentation and Python changes

### ggp-func-ts-gocheckin
- [Bidirectional Lock-Camera Reference](../../ggp-func-ts-gocheckin/doc/bidirectional_lock_camera.md) - Data model for lock→camera lookup (✅ DONE)
- [Lock Occupancy Handler](../../ggp-func-ts-gocheckin/doc/lock_occupancy_handler.md) - Event handler implementation (✅ DONE)
