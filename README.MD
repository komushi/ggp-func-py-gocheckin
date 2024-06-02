## Use serverless cli 3.38.0 to start from boilerplate

```
$ sls create -t aws-python  -p ggp-func-py-edgecv
```

## Local testing on macOS or windows
```
$ python3 py_handler.py motion start <RTSP_URL> <h264_h265_webcam> <framerate>
$ python3 py_handler.py motion stop

$ python3 py_handler.py face start <RTSP_URL> <h264_h265_webcam> <framerate>
$ python3 py_handler.py face stop

$ python3 py_handler.py recognition start <RTSP_URL> <h264_h265_webcam> <framerate> <face_file>
$ python3 py_handler.py recognition stop
```

### Local testing macOS requirements.txt
```
albumentations==1.4.1
certifi==2024.2.2
charset-normalizer==3.3.2
coloredlogs==15.0.1
contourpy==1.2.0
cycler==0.12.1
Cython==3.0.9
easydict==1.13
flatbuffers==24.3.7
fonttools==4.50.0
humanfriendly==10.0
idna==3.6
imageio==2.34.0
insightface==0.7.3
joblib==1.3.2
kiwisolver==1.4.5
lazy_loader==0.3
matplotlib==3.8.3
mpmath==1.3.0
networkx==3.2.1
numpy==1.26.4
onnx==1.15.0
onnxruntime==1.17.1
opencv-contrib-python==4.9.0.80
opencv-python-headless==4.9.0.80
packaging==24.0
pillow==10.2.0
prettytable==3.10.0
protobuf==5.26.0
pycairo==1.26.0
PyGObject==3.48.1
pyparsing==3.1.2
python-dateutil==2.9.0.post0
PyYAML==6.0.1
requests==2.31.0
scikit-image==0.22.0
scikit-learn==1.4.1.post1
scipy==1.12.0
six==1.16.0
sympy==1.12
threadpoolctl==3.3.0
tifffile==2024.2.12
tqdm==4.66.2
typing_extensions==4.10.0
urllib3==2.2.1
wcwidth==0.2.13
```

## Individual testing on macOS or windows
```
$ python3 motion_detector.py motion start <RTSP_URL> <h264_h265_webcam> <framerate>

$ python3 face_detector.py face start <RTSP_URL> <h264_h265_webcam> <framerate>

$ python3 recognition_detector.py recognition start <RTSP_URL> <h264_h265_webcam> <framerate> <face_file>
```

## Greengrass testing
Topic: 
```
gocheckin/"${AWS_IOT_THING_NAME}"/detect
```

Payload:
```
{
	"method": "face",
	"action": "start",
	"rtsp_src": "<RTSP_URL>",
	"codec": "h264",
	"framerate": "10"
}

{
	"method": "motion",
	"action": "start",
	"rtsp_src": "<RTSP_URL>",
	"codec": "h264",
	"framerate": "10"
}

{
	"method": "face",
	"action": "stop"
}

{
	"method": "motion",
	"action": "stop"
}
```


