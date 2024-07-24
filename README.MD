## cloudwatch log insights query 

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
1 [2024-07-24T21:16:46.229+09:00][INFO]-gstreamer_threading.py:292,New file being created: /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4
2 [2024-07-24T21:16:52.546+09:00][INFO]-face_recognition.py:217,Thread-FaceRecognition-192.168.11.202 stop in
3 [2024-07-24T21:16:52.549+09:00][INFO]-gstreamer_threading.py:296,New file created: /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4
4 [2024-07-24T21:16:53.044+09:00][INFO]-File /etc/insightface/192.168.11.202/2024-07-24/12:16:45.mp4 uploaded and removed successfully 
```
