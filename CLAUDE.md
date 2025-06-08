# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a GoCheckin Face Recognition and Video Surveillance System designed to run on **Raspberry Pi (Bookworm OS)** as a local Lambda function via **AWS IoT Greengrass v1 Core**. It integrates IP cameras for face recognition of hotel/property guests, motion detection, and video recording.

## Deployment Environment

- **Platform**: Raspberry Pi running Bookworm OS
- **Runtime**: AWS Greengrass v1 Core (local Lambda)
- **Local Services**: DynamoDB Local for data storage
- **Cloud Services**: AWS S3 (video storage) and AWS IoT Core (messaging)

## Key Architecture

### Core Components
- **py_handler.py**: Main orchestrator that handles AWS IoT messages, manages camera connections, and runs HTTP server for ONVIF notifications and face recognition API
- **face_recognition.py**: Face detection/recognition using InsightFace, compares against active guest database
- **gstreamer_threading.py**: Video stream processing using GStreamer for RTSP capture and recording
- **onvif_process.py**: ONVIF protocol integration for IP camera events and motion detection
- **s3_uploader.py**: Handles video/snapshot uploads to AWS S3

### Data Flow
1. ONVIF cameras send motion detection events to HTTP server
2. GStreamer captures RTSP video streams with pre-recording buffer
3. Face recognition runs on extracted frames
4. Recognized guests trigger extended recording
5. Video clips and snapshots upload to S3
6. Results publish to AWS IoT topics

## Development Commands

```bash
# Linting
pyenv activate cv311
pylint <file> or <dir>

# Test RTSP stream connectivity
gst-launch-1.0 -v rtspsrc location=<rtsp_url> ! fakesink
```

## Environment Configuration

Key environment variables (set in function.conf):
- `FACE_RECOG_THRESHOLD`: Face recognition confidence threshold (default: 0.45)
- `FACE_RECOG_TIMER_SECOND`: Cooldown between recognitions (default: 600s)
- `RECORD_BEFORE_MOTION_SECOND`: Pre-recording buffer (default: 3s)
- `RECORD_AFTER_MOTION_SECOND`: Post-motion recording (default: 10s)
- `ONVIF_MOTION_IDLE_SECOND`: Motion event timeout (default: 10s)

## AWS Integration

### Local DynamoDB Tables
- `iot-ggv2-component-host`: Camera/host information
- `iot-ggv2-component-reservation`: Guest reservation data
- `iot-ggv2-component-member`: Member face embeddings
- `iot-ggv2-component-asset`: Video/image metadata

### AWS IoT Topics (Cloud)
- Subscribe: `ggp/face/request`, `dt/status/+/cameraStatus`
- Publish: `ggp/face/result`, `dt/face/recognize`, `dt/video/upload`

## Important Notes

- Uses InsightFace model stored at `/greengrass/v2/face_model/`
- Requires GStreamer with H264/H265 support on Raspberry Pi
- ONVIF cameras must support event subscriptions
- Face images expire from S3 after 7 days
- Video recordings have 30-day retention
- Greengrass v1 manages the Lambda lifecycle and permissions