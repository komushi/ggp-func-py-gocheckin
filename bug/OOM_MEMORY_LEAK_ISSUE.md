# OOM (Out of Memory) / Memory Leak Issue

**Date Discovered**: 2026-01-11
**Status**: Open - Investigation Required
**Priority**: High

## 1. Context

### Environment
- **Platform**: Raspberry Pi 4 (2GB RAM) running Bookworm OS
- **Runtime**: AWS Greengrass v1 Core
- **Lambda**: `demo-py_handler` (Python 3.7, NoContainer isolation mode)
- **Uptime before crash**: ~4 hours (12:31 to 16:37)

### What Happened
On 2026-01-11, the Python Lambda was unexpectedly killed by the Linux OOM (Out of Memory) killer after running for approximately 4 hours. This caused:
1. The HTTP server on port 7777 to become unavailable
2. The TypeScript Lambda's `/recognise` call to fail with `ECONNREFUSED`
3. Face embedding computation to fail during shadow deployment
4. Members saved to DynamoDB without `faceEmbedding` field

### Impact
- Face recognition matching fails (members have no embeddings)
- System requires manual intervention to recompute embeddings
- Unpredictable Lambda restarts during normal operation

## 2. How to Reproduce

### Prerequisites
- SSH access to the Raspberry Pi (`ssh demo`)
- Greengrass deployment with Python Lambda running

### Steps to Reproduce
1. Deploy the Python Lambda and let it run normally
2. Ensure cameras are connected and streaming (3 cameras in test environment)
3. Wait 4-6 hours (or until memory exhaustion)
4. Trigger a shadow deployment with reservation/member data

### Monitoring Commands

Check current memory usage:
```bash
ssh demo "free -h"
ssh demo "ps aux --sort=-%mem | head -10"
```

Check Python Lambda memory specifically:
```bash
ssh demo "ps aux | grep python3.7 | grep -v grep"
```

Monitor memory over time:
```bash
ssh demo "watch -n 5 'ps aux --sort=-%mem | head -5'"
```

Check for OOM events:
```bash
ssh demo "sudo dmesg -T | grep -i oom"
```

Check Greengrass runtime logs for worker kills:
```bash
ssh demo "sudo strings /greengrass/ggc/var/log/system/runtime.log | grep -E 'killed|memory' | tail -20"
```

## 3. Evidence from Incident

### Timeline (2026-01-11)

| Time | Event |
|------|-------|
| 12:31:37 | Python Lambda started (HTTP server initialized) |
| 16:37:19 | Linux OOM killer invoked |
| 16:37:20.033 | Greengrass detects Python Lambda invocation |
| 16:37:20.060 | Worker killed - Memory: 1,564,912 KB (1.5GB) |
| 16:37:20.077 | New Python worker created (pid 92414) |
| 16:37:20.499 | TS Lambda calls `/recognise` - ECONNREFUSED |
| 16:37:23.706 | Python `init_env_var` starts |
| 16:37:24.613 | Python HTTP server ready |

### Kernel OOM Log
```
[Sun Jan 11 16:37:19 2026] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),
  cpuset=/,mems_allowed=0,global_oom,
  task_memcg=/user.slice/user-1000.slice/session-7.scope,
  task=python3.7,pid=82400,uid=1001

Out of memory: Killed process 82400 (python3.7)
  total-vm:4294112kB (4.3GB virtual)
  anon-rss:1557744kB (1.5GB RSS)
  file-rss:512kB
  shmem-rss:0kB
```

### Greengrass Runtime Log
```
[2026-01-11T16:37:20.06+09:00][INFO]-Worker Max Memory Usage (KB): 1564912
[2026-01-11T16:37:20.06+09:00][ERROR]-Worker is ungracefully killed.
  {"workerId": "11875582-c6c1-4b90-5b8d-79947e96b6e6",
   "funcArn": "arn:aws:lambda:ap-northeast-1:769412733712:function:demo-py_handler:16",
   "state": "signal: killed"}
```

## 4. Potential Memory Leak Sources

### 4.1 GStreamer Threads (`gstreamer_threading.py`)
**Suspect Level**: HIGH

- Multiple GStreamer pipelines running concurrently (one per camera)
- Video buffers may not be properly released
- Frame extraction for face detection creates numpy arrays
- Pre-recording circular buffers accumulate frames

Areas to investigate:
- `appsink` buffer handling in `on_new_sample()`
- Circular buffer management in pre-recording
- Pipeline cleanup on camera disconnect/reconnect

### 4.2 Face Detection (`face_recognition.py`)
**Suspect Level**: MEDIUM-HIGH

- InsightFace model holds GPU/CPU memory
- Numpy arrays from face embeddings (512-dim float arrays)
- Image frames passed for detection may not be garbage collected
- `active_members` list refreshed periodically but old references may persist

Areas to investigate:
- Frame queue (`cam_queue`) size limits
- Embedding array lifecycle
- PIL/OpenCV image object cleanup

### 4.3 Queue Management (`py_handler.py`)
**Suspect Level**: MEDIUM

- `scanner_output_queue` - Queue for face detection results
- `cam_queue` - Queue for camera frames

```python
# Lines 73-74
scanner_output_queue = Queue()
cam_queue = Queue()
```

If consumers are slower than producers, queues grow unbounded.

### 4.4 Thread Accumulation
**Suspect Level**: MEDIUM

Multiple thread types are created dynamically:
- `Thread-Gst-{cam_ip}` - GStreamer threads per camera
- `Thread-GstMonitor-{cam_ip}` - Monitor threads per camera
- `Thread-Detector` - Face detection thread
- `Thread-DetectorMonitor` - Detection monitor thread
- `Thread-FetchMembers` - Timer threads for member refresh
- `Thread-InitCameras-Timer` - Timer for camera initialization
- HTTP request handler threads (with `ThreadedTCPServer`)

If threads are not properly joined/terminated on camera disconnect, they may accumulate.

### 4.5 HTTP Server Request Handlers
**Suspect Level**: LOW-MEDIUM

Recent change (commit `ca306ea`) switched to `ThreadedTCPServer`:
```python
class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True  # Threads die when main thread exits
```

Each HTTP request spawns a new thread. If requests come frequently (ONVIF notifications), threads may accumulate.

### 4.6 ONVIF Subscriptions
**Suspect Level**: LOW

- ONVIF event subscriptions per camera
- Notification data parsing and processing

### 4.7 S3 Uploader (`s3_uploader.py`)
**Suspect Level**: LOW

- Video file buffers during upload
- Retry queues for failed uploads

## 5. Investigation Steps

### Step 1: Add Memory Profiling
Add memory tracking to identify growth patterns:

```python
import tracemalloc
import psutil
import os

def log_memory_usage(context=""):
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    logger.info(f"MEMORY [{context}] RSS: {mem_info.rss / 1024 / 1024:.1f}MB, VMS: {mem_info.vms / 1024 / 1024:.1f}MB")

# Call periodically or at key points:
# - After each face detection cycle
# - After camera connect/disconnect
# - After HTTP request handling
# - Every N minutes via timer
```

### Step 2: Track Object Counts
```python
import gc

def log_object_counts():
    gc.collect()
    counts = {}
    for obj in gc.get_objects():
        obj_type = type(obj).__name__
        counts[obj_type] = counts.get(obj_type, 0) + 1

    # Log top memory consumers
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:20]
    logger.info(f"TOP OBJECTS: {sorted_counts}")
```

### Step 3: Monitor Queue Sizes
```python
def log_queue_sizes():
    logger.info(f"QUEUES - scanner_output_queue: {scanner_output_queue.qsize()}, cam_queue: {cam_queue.qsize()}")
```

### Step 4: Monitor Thread Counts
```python
def log_thread_counts():
    threads = threading.enumerate()
    logger.info(f"THREADS ({len(threads)}): {[t.name for t in threads]}")
```

## 6. Potential Solutions / Workarounds

### Immediate Workarounds

#### 6.1 Scheduled Lambda Restart
Add a cron job to restart the Python Lambda every 2-3 hours before OOM:
```bash
# Add to crontab
0 */3 * * * sudo /greengrass/ggc/core/greengrassd restart
```

#### 6.2 Memory Limit with Earlier Kill
Configure systemd to kill the process at a lower threshold (e.g., 1GB) to allow cleaner restart:
```bash
# /etc/systemd/system/greengrass.service.d/memory.conf
[Service]
MemoryMax=1G
```

#### 6.3 Add Swap Space
Add swap to delay OOM (not a fix, just buys time):
```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Long-term Fixes

#### 6.4 Implement Queue Size Limits
```python
from queue import Queue

# Limit queue sizes
scanner_output_queue = Queue(maxsize=100)
cam_queue = Queue(maxsize=50)
```

#### 6.5 Fix GStreamer Buffer Management
Ensure proper cleanup in GStreamer pipeline:
```python
def on_new_sample(self, sink):
    sample = sink.emit('pull-sample')
    if sample:
        buffer = sample.get_buffer()
        # Process buffer
        # Explicitly unref if needed
        del buffer
        del sample
    return Gst.FlowReturn.OK
```

#### 6.6 Periodic Garbage Collection
Force garbage collection periodically:
```python
import gc

def periodic_gc():
    gc.collect()
    logger.info(f"GC complete - collected {gc.get_count()}")

# Run every 10 minutes
gc_timer = threading.Timer(600, periodic_gc)
gc_timer.start()
```

#### 6.7 Limit HTTP Handler Threads
```python
class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Limit concurrent connections
    request_queue_size = 10
```

## 7. Related Files

- `py_handler.py` - Main orchestrator, HTTP server, thread management
- `gstreamer_threading.py` - GStreamer pipeline, video buffer handling
- `face_recognition.py` - Face detection, numpy arrays, queue consumer
- `s3_uploader.py` - Video upload, file buffers

## 8. Related Commits

- `ca306ea` (2026-01-10) - Changed to ThreadedTCPServer
- Previous commits may have introduced memory issues

## 9. References

- [Python Memory Profiling](https://docs.python.org/3/library/tracemalloc.html)
- [GStreamer Memory Management](https://gstreamer.freedesktop.org/documentation/application-development/advanced/memory.html)
- [Linux OOM Killer](https://www.kernel.org/doc/gorman/html/understand/understand016.html)

---

**Next Steps**:
1. Add memory profiling to identify the specific leak source
2. Monitor queue sizes and thread counts over time
3. Implement immediate workaround (scheduled restart or swap)
4. Fix identified leak source
5. Test with extended runtime (8+ hours)
