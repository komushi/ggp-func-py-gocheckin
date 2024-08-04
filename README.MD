# pylint
```
pyenv activate cv311
pylint <file> or <dir>
```

# cloudwatch log insights query 

## 1. To find if there are videos not uploaded
### 1-1. Query
```
fields @timestamp, @message
| filter @message like /New file/ or @message like /uploaded and removed successfully/ or @message like /stop in/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

### 1-2. Below is the expected result of correct sequence
```
[2024-07-24T21:16:46.229+09:00][INFO]-gstreamer_threading.py:292,New file being created: /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4
[2024-07-24T21:16:52.546+09:00][INFO]-face_recognition.py:217,Thread-FaceRecognition-192.168.11.202 stop in
[2024-07-24T21:16:52.549+09:00][INFO]-gstreamer_threading.py:296,New file created: /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4
[2024-07-24T21:16:53.044+09:00][INFO]-File /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4 uploaded and removed successfully 
```

```
[2024-07-24T21:14:21.047+09:00][INFO]-face_recognition.py:98,Thread-FaceRecognition-192.168.11.202 reached maximum seconds limit of 8.0
[2024-07-24T21:14:21.048+09:00][INFO]-face_recognition.py:217,Thread-FaceRecognition-192.168.11.202 stop in
[2024-07-24T21:14:21.048+09:00][INFO]-Stopping Thread-Gst-192.168.11.202
[2024-07-24T21:14:21.048+09:00][INFO]-gstreamer_threading.py:248,Stopping recording...
[2024-07-24T21:14:21.048+09:00][INFO]-gstreamer_threading.py:254,Before sending sink eos
[2024-07-24T21:14:21.049+09:00][INFO]-gstreamer_threading.py:260,End-Of-Stream sending...
[2024-07-24T21:14:21.049+09:00][INFO]-gstreamer_threading.py:267,Recording stopped
[2024-07-24T21:14:21.051+09:00][INFO]-gstreamer_threading.py:296,New file created: /etc/insightface/192.168.11.202/2024-07-24/12:14:13.mp4
[2024-07-24T21:14:21.068+09:00][INFO]-gstreamer_threading.py:237,Pipeline stopped and cleaned up.
[2024-07-24T21:14:21.069+09:00][INFO]-face_recognition.py:228,Thread-Gst-192.168.11.202 stopped
[2024-07-24T21:14:21.109+09:00][INFO]-face_recognition.py:233,cam_queue cleared
[2024-07-24T21:14:21.109+09:00][INFO]-face_recognition.py:239,Thread-FaceRecognition-192.168.11.202 stopped
[2024-07-24T21:14:21.736+09:00][INFO]-py_handler.py:544,Fetched from scanner_output_queue: {'type': 'video_clipped', 'payload': {'video_clipping_location': '/etc/insightface', 'cam_ip': '192.168.11.202', 'cam_uuid': 'b10b9ab2-facd-11b3-825f-94e1aca71c0c', 'cam_name': 'HIKVISION%20DS-2CV3Q01EFD-IW', 'date_folder': '2024-07-24', 'time_filename': '12:14:13', 'ext': '.mp4', 'local_file_path': '/etc/insightface/192.168.11.202/2024-07-24/12:14:13.mp4', 'start_datetime': '2024-07-24T12:14:13.000Z', 'end_datetime': '2024-07-24T12:14:20.000Z'}}
[2024-07-24T21:14:21.736+09:00][INFO]-Credentials will expire at 2024-07-24 12:59:00+00:00, Time remaining: 0:44:38.263682
[2024-07-24T21:14:21.905+09:00][INFO]-File /etc/insightface/192.168.11.202/2024-07-24/12:14:13.mp4 uploaded and removed successfully 
```

### 1-3. Upload not happening

```
[2024-07-25T01:45:33.146+09:00][INFO]-gstreamer_threading.py:292,New file being created: /etc/insightface/192.168.11.210/2024-07-24/16:45:32.mp4 
[2024-07-25T01:45:51.49+09:00][INFO]-face_recognition.py:217,Thread-FaceRecognition-192.168.11.210 stop in
[2024-07-25T01:45:51.493+09:00][INFO]-gstreamer_threading.py:296,New file created: /etc/insightface/192.168.11.210/2024-07-24/16:45:32.mp4
```

```
[2024-07-25T01:45:51.489+09:00][INFO]-face_recognition.py:98,Thread-FaceRecognition-192.168.11.210 reached maximum seconds limit of 20.0
[2024-07-25T01:45:51.49+09:00][INFO]-face_recognition.py:217,Thread-FaceRecognition-192.168.11.210 stop in
[2024-07-25T01:45:51.49+09:00][INFO]-Stopping Thread-Gst-192.168.11.210
[2024-07-25T01:45:51.49+09:00][INFO]-gstreamer_threading.py:248,Stopping recording...
[2024-07-25T01:45:51.491+09:00][INFO]-gstreamer_threading.py:254,Before sending sink eos
[2024-07-25T01:45:51.493+09:00][INFO]-gstreamer_threading.py:296,New file created: /etc/insightface/192.168.11.210/2024-07-24/16:45:32.mp4
[2024-07-25T01:45:51.494+09:00][INFO]-gstreamer_threading.py:260,End-Of-Stream sending...
[2024-07-25T01:45:51.494+09:00][INFO]-gstreamer_threading.py:267,Recording stopped
[2024-07-25T01:45:51.612+09:00][INFO]-gstreamer_threading.py:237,Pipeline stopped and cleaned up.
[2024-07-25T01:45:51.613+09:00][INFO]-face_recognition.py:228,Thread-Gst-192.168.11.210 stopped
[2024-07-25T01:45:51.614+09:00][INFO]-face_recognition.py:233,cam_queue cleared
[2024-07-25T01:45:51.614+09:00][INFO]-face_recognition.py:239,Thread-FaceRecognition-192.168.11.210 stopped 
```

## 2. To find if there are rtsp connectivity issues
### 2-1. Query
```
fields @timestamp, @message
| filter @message like /Pipeline state changed from paused to playing/ or @message like /Gst.MessageType.ERROR/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200

fields @timestamp, @message
| filter @message like /Monitor:/ or @message like /Gst.MessageType.ERROR/ or @message like /Pipeline state changed from paused to playing/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

## 3. To find if there are rtsp seek issues
### 3-1. Query
```
fields @timestamp, @message
| filter @message like /Downstream is not seekable/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```