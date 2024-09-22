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
| filter @message like /HTTPSConnectionPool/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

### 1-2. Query
```
fields @timestamp, @message
| filter @message like /put_object, Exception/
| display @timestamp, @logStream, @message
| sort @timestamp asc
| limit 200
```

### 1-3. Query
```
fields @timestamp, @message
| filter @message like /Name or service not known/
| display @timestamp, @logStream, @message
| sort @timestamp desc
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

## 4. Check pipeline PLAYING state
### 4-1. Query Check when state changes recently
```
fields @timestamp, @message
| filter @message like /192.168.11.206 Pipeline state changed from/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```


### 4-2. Query Check when start_playing happened
```
fields @timestamp, @message
| filter @message like /start_playing, Thread-Gst-192.168.11.206 return with playing: True/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```

### 4-3. Query Check when start_playing execution history
```
fields @timestamp, @message
| filter @message like /start_playing, Thread-Gst-192.168.11.206 count ended with result playing:/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```

### 4-4. Query Check state changes and hearbeat
```
fields @timestamp, @message
| filter @message like /Pipeline state changed from/ or @message like /claim_cameras published/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```

## 5. To find if splitmuxsink not unlink issues
### 5-1. Query
```
fields @timestamp, @message
| filter @message like /unlink_and_remove_splitmuxsink/ or @message like /stop_recording/
| display @timestamp, @logStream, @message
| sort @timestamp desc
| limit 200
```

# gst query to check IPC issues
## 1. Check if a IPC has issues with pulling RTSP streams
If there is no-standard media type or application type data, it might come together with the pulling issue.
```
gst-launch-1.0 -v rtspsrc location=<rtsp_url> ! fakesink
> caps = application/x-rtp, media=(string)application/tp-link, payload=(int)0,
```
