# GoCheckin Face Recognition and Video Surveillance System

A modular video surveillance system designed for hospitality properties, running on Raspberry Pi (Bookworm OS) with AWS IoT Greengrass v1. The system provides motion-triggered recording with pluggable face recognition capabilities for guest identification.

## Overview

This system integrates with IP cameras using ONVIF protocol to provide:
- Real-time motion detection and video recording
- Pluggable face recognition architecture (supports multiple face_app implementations)
- Cloud storage integration via AWS S3
- Local data management with DynamoDB Local
- Remote monitoring through AWS IoT Core

## System Architecture

### Core Components

- **py_handler.py** - Main orchestrator handling AWS IoT messages, camera management, and HTTP server
- **face_recognition.py** - Face detection/recognition module with pluggable face_app support
- **gstreamer_threading.py** - RTSP video stream processing with GStreamer
- **onvif_process.py** - ONVIF protocol integration for IP camera events
- **s3_uploader.py** - AWS S3 integration for video/snapshot uploads
- **web_image_process.py** - Image processing utilities for face recognition API

### Operating Modes

1. **Face Recognition Mode** (when face_app is specified)
   - Motion detection triggers recording
   - Face recognition identifies guests using the configured face_app
   - Extended recording for recognized individuals
   - Currently supports InsightFace (USE_INSIGHTFACE=true)
   - Extensible to support other face recognition engines
   
2. **Recording-Only Mode** (when no face_app is specified)
   - Motion detection triggers recording
   - No face recognition processing
   - Reduced CPU usage on Raspberry Pi
   - Default mode when USE_INSIGHTFACE=false

## Deployment Environment

- **Hardware**: Raspberry Pi with Bookworm OS
- **Runtime**: AWS Greengrass v1 Core (Local Lambda)
- **Video Processing**: GStreamer with H264/H265 support
- **Camera Protocol**: ONVIF-compliant IP cameras
- **Local Storage**: DynamoDB Local
- **Cloud Services**: AWS S3, AWS IoT Core

## Key Features

### Video Processing
- RTSP stream capture from multiple IP cameras
- Pre-recording buffer (configurable, default 3s)
- Motion-triggered recording with post-motion duration
- Automatic video clip generation and upload

### Face Recognition (Pluggable Architecture)
- Modular design supporting multiple face recognition engines
- Currently implemented: InsightFace
- Guest database comparison
- Configurable recognition thresholds
- Recognition cooldown to prevent duplicate detections
- Easy to extend with new face_app implementations

### Camera Integration
- ONVIF event subscription for motion detection
- Automatic camera discovery and management
- Camera heartbeat monitoring
- Multi-camera support with threading

### Data Management
- Local DynamoDB tables for guest/camera data
- S3 upload with lifecycle policies
- AWS IoT Core integration for remote monitoring
- HTTP API for external face recognition requests

## Configuration

Key environment variables in `function.conf`:

### Face Recognition Settings
- `USE_INSIGHTFACE`: Enable InsightFace recognition engine (true/false)
- `FACE_RECOG_THRESHOLD`: Recognition confidence threshold (0.45)
- `FACE_RECOG_TIMER_SECOND`: Cooldown between recognitions (600s)

### Recording Settings
- `RECORD_BEFORE_MOTION_SECOND`: Pre-recording buffer (3s)
- `RECORD_AFTER_MOTION_SECOND`: Post-motion recording (10s)
- `ONVIF_MOTION_IDLE_SECOND`: Motion event timeout (10s)

### AWS Settings
- DynamoDB table names for host, reservation, member, and asset data
- S3 bucket configuration
- IoT topic subscriptions and publications

## Development

### Prerequisites
- Python 3.11 with pyenv
- GStreamer with RTSP support
- AWS Greengrass v1 Core
- DynamoDB Local

### Linting
```bash
pyenv activate cv311
pylint <file> or <dir>
```

### Testing RTSP Streams
```bash
gst-launch-1.0 -v rtspsrc location=<rtsp_url> ! fakesink
```

### Adding New Face Recognition Engines

To implement a new face recognition engine:
1. Create a new initialization function in `py_handler.py` (similar to `init_insightface_app()`)
2. Add corresponding environment variable flag (e.g., `USE_YOUR_ENGINE`)
3. Ensure your face_app implements the required interface:
   - `get()` method for face detection
   - Returns face objects with `embedding` attribute
4. Update the initialization logic in `init_face_detector()`

## Monitoring

CloudWatch Insights queries for troubleshooting:
- Video upload failures
- RTSP stream connectivity issues
- Pipeline state changes
- Camera heartbeat status
- Face recognition performance (when enabled)

## Data Flow

1. IP cameras send ONVIF motion events to the HTTP server
2. Motion events trigger GStreamer to save buffered video
3. If face_app is configured: Frames are extracted for face recognition
4. Recognized guests trigger extended recording duration
5. Video clips and snapshots upload to S3
6. Recognition results publish to AWS IoT topics (if face recognition enabled)
7. Local DynamoDB tracks all events and metadata

## Architecture Benefits

- **Modularity**: Easy to swap or add face recognition engines
- **Flexibility**: Can run with or without face recognition
- **Performance**: Recording-only mode reduces Raspberry Pi CPU load
- **Scalability**: Multi-camera support with efficient threading
- **Reliability**: Local DynamoDB ensures data persistence
- **Maintainability**: Clear separation between video processing and face recognition

## Future Extensibility

The modular architecture supports:
- Additional face recognition engines beyond InsightFace
- Custom video analytics capabilities
- Alternative motion detection algorithms
- Extended camera protocol support
- Enhanced cloud integration features
- Real-time alerting systems