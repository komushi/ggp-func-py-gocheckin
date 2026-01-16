import signal
import json
import logging
from datetime import datetime, timezone, timedelta
import sys
import os

import io
import base64

from queue import Queue

import traceback

import http.server
import socketserver

import socket

import threading
import time

import gc

# import requests

# import PIL.Image
import numpy as np

import boto3
from boto3.dynamodb.conditions import Attr, Key

import s3_uploader as uploader

from insightface.app import FaceAnalysis
import face_recognition as fdm

import gstreamer_threading as gst

# import onvif_process as onvif
from onvif_process import OnvifConnector

import web_image_process as web_img

import greengrasssdk
iotClient = greengrasssdk.client("iot-data")

def get_local_ip():

    # Connect to an external host, in this case, Google's DNS server
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    return local_ip


logging.getLogger('ipc_client').setLevel(logging.ERROR)
# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

shutting_down = False

# Initialize the http server
server_thread = None
httpd = None
thread_lock = threading.Lock()
http_port = 7777
scanner_local_ip = get_local_ip()

# Initialize the scheduler
# scheduler_thread = None
# scheduler = sched.scheduler(time.time, time.sleep)

# Initialize the active_members and the last_fetch_time
last_fetch_time = None
active_members = None

# Initialize the face_app, uploader_app
face_app = None
uploader_app = None
onvif_connectors = {}

# Initialize the detectors
thread_detector = None
detection_timer = None
# recording_timer = None
recording_timers = {}

# Initialize the gstreamers
thread_gstreamers = {}

# Initialize the camera_items
camera_items = {}

# Initialize the trigger_lock_context for tracking lock triggers per camera
# Structure: { cam_ip: { 'onvif_triggered': bool, 'specific_locks': set(), 'active_occupancy': set() } }
trigger_lock_context = {}

# Initialize the thread_monitors
thread_monitors = {}

# Initialize the scanner_output_queue
scanner_output_queue = Queue(maxsize=50)
cam_queue = Queue(maxsize=500)
# motion_detection_queue = Queue(maxsize=500)

# Initialize the DynamoDB resource
dynamodb = boto3.resource(
    'dynamodb',
    endpoint_url=os.environ['DDB_ENDPOINT'],
    region_name='us-west-1',
    aws_access_key_id='fakeMyKeyId',
    aws_secret_access_key='fakeSecretAccessKey'
)

def function_handler(event, context):
    context_vars = vars(context)
    topic = context_vars['client_context'].custom['subject']

    logger.debug('function_handler topic: %s', str(topic))

    if topic == f"gocheckin/reset_camera":
        logger.info('function_handler reset_camera')

        # cameras_to_update, cameras_to_remove = fetch_camera_items()

        # for cam_ip in cameras_to_update:
        #     try:
        #         init_gst_app(cam_ip, True)
        #     except Exception as e:
        #         logger.error(f"Error updating camera {cam_ip}: {e}")

        # for cam_ip, onvif_sub_address in cameras_to_remove.items():
        #     try:
        #         force_stop_camera(cam_ip)
        #         if cam_ip in onvif_connectors:
        #             onvif_connectors[cam_ip].unsubscribe(cam_ip, onvif_sub_address)
        #             del onvif_connectors[cam_ip]
        #     except Exception as e:
        #         logger.error(f"Error removing camera {cam_ip}: {e}")

        init_cameras()

    elif topic == f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/force_detect":
        logger.info('function_handler force_detect')

        if 'cam_ip' in event:
            handle_notification(event['cam_ip'], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z', True)
    elif topic == f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/change_var":
        logger.info(f"function_handler change_var event: ${event}")
        for key, value in event.items():
            os.environ[key] = value
            logger.info(f"change_var: ${key}: ${value}")
            if key == "TIMER_CAM_RENEW" or key == "ONVIF_EXPIRATION":
                init_cameras()
                break
            elif key == "TIMER_INIT_ENV_VAR":
                init_env_var()
                break
    elif topic == "gocheckin/trigger_detection":
        logger.info('function_handler trigger_detection event: %s', json.dumps(event))
        cam_ip = event.get('cam_ip')
        lock_asset_id = event.get('lock_asset_id')  # May be None for ONVIF trigger
        if cam_ip:
            trigger_face_detection(cam_ip, lock_asset_id)
    elif topic == "gocheckin/stop_detection":
        logger.info('function_handler stop_detection event: %s', json.dumps(event))
        cam_ip = event.get('cam_ip')
        lock_asset_id = event.get('lock_asset_id')
        if cam_ip and lock_asset_id:
            handle_occupancy_false(cam_ip, lock_asset_id)


def has_config_changed(current_item, new_item):
    """Compare camera configurations to determine if update is needed"""
    critical_fields = [
        'username', 'password', 'isRecording', 'isDetecting',
        'onvif', 'rtsp', 'uuid', 'assetName', 'localIp'
    ]
    
    for field in critical_fields:
        if current_item.get(field) != new_item.get(field):
            logger.info(f"Configuration change detected in {field}")
            return True
    return False

def fetch_camera_items():
    logger.info(f"fetch_camera_items in")

    global camera_items

    try:
        current_cameras = {}
        cameras_to_update = []
        cameras_to_remove = {}
        camera_item_list = query_camera_items(os.environ['HOST_ID'])
        
        for camera_item in camera_item_list:
            onvif_sub_address = None
            cam_ip = camera_item['localIp']
            current_cameras[cam_ip] = True
            
            if cam_ip not in camera_items:
                cameras_to_update.append(cam_ip)
                camera_items[cam_ip] = camera_item
            else:
                current_item = camera_items[cam_ip]

                if 'onvifSubAddress' in current_item:
                    onvif_sub_address = current_item.get('onvifSubAddress')

                if has_config_changed(current_item, camera_item):
                    cameras_to_update.append(cam_ip)
                    camera_items[cam_ip] = camera_item
                    if onvif_sub_address:
                        camera_items[cam_ip]['onvifSubAddress'] = onvif_sub_address
        
        for cam_ip in camera_items:
            if cam_ip not in current_cameras:
                cameras_to_remove[cam_ip] = camera_items[cam_ip].get('onvifSubAddress')
                # del camera_items[cam_ip]

        for cam_ip in cameras_to_remove.keys():
            if cam_ip in camera_items:
                del camera_items[cam_ip]

        logger.info(f"fetch_camera_items out, cameras_to_update: {cameras_to_update}, cameras_to_remove: {cameras_to_remove}")

        return cameras_to_update, cameras_to_remove
            
    except Exception as e:
        logger.error(f"Error handling fetch_camera_items: {e}")
        return [], {}


def init_uploader_app():
    logger.debug(f"init_uploader_app in")

    try:

        global uploader_app
        if uploader_app is None:
            if 'CRED_PROVIDER_HOST' in os.environ:
                uploader_app = uploader.S3Uploader(
                    cred_provider_host=os.environ['CRED_PROVIDER_HOST'],
                    cred_provider_path=f"/role-aliases/{os.environ['AWS_ROLE_ALIAS']}/credentials",
                    bucket_name=os.environ['VIDEO_BUCKET']
                )
        
        logger.debug(f"init_uploader_app out")
    except Exception as e:
        logger.error(f"Error handling init_uploader_app: {e}")
        uploader_app = None

def init_face_detector():
    global thread_detector

    for thread in threading.enumerate():
        logger.info(f"init_face_detector in thread.name {thread.name}")

    if os.environ['USE_INSIGHTFACE'] == 'true':
        init_insightface_app()

    if face_app is None:
        logger.info('init_face_detector out, face_app is None')
        return
    
    fetch_members()

    thread_detector = fdm.FaceRecognition(face_app, active_members, scanner_output_queue, cam_queue)
    thread_detector.start()

    if thread_detector is not None:
        if thread_detector.is_alive():

            thread_monitor_detector = threading.Thread(target=monitor_detector, name=f"Thread-DetectorMonitor-init_face_detector-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}", args=())
            thread_monitor_detector.start()

    for thread in threading.enumerate():
        logger.info(f"init_face_detector out thread.name {thread.name}")

def monitor_detector():
    logger.info(f"monitor_detector in")

    global thread_detector

    thread_detector.stop_event.wait()  # Wait indefinitely for the event to be set
    thread_detector.join()  # Join the stopped thread

    if shutting_down:
        logger.info(f"shutting down, not restarting.")
        return

    if thread_detector.is_alive():
        logger.info(f"thread_detector {thread_detector.name} still alive unexpectedly, not restarting.")
        return

    logger.info(f"monitor_detector: {thread_detector.name} has stopped, restarting detector by {threading.current_thread().name}...")

    # Clear previous references before restarting
    thread_detector = None
    thread_monitor_detector = None

    thread_detector = fdm.FaceRecognition(face_app, active_members, scanner_output_queue, cam_queue)
    thread_detector.start()

    if thread_detector is not None:
        if thread_detector.is_alive():

            thread_monitor_detector = threading.Thread(target=monitor_detector, name=f"Thread-DetectorMonitor-monitor_detector-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}", args=())
            thread_monitor_detector.start()


def init_insightface_app(model='buffalo_sc'):
    class FaceAnalysisChild(FaceAnalysis):
        def get(self, img, max_num=0, det_size=(640, 640)):
            if det_size is not None:
                self.det_model.input_size = det_size

            return super().get(img, max_num)

    global face_app

    if face_app is None:
        logger.info(f"Initializing face_app with Model: {model}")
        face_app = FaceAnalysisChild(name=model, allowed_modules=['detection', 'recognition'], providers=['CPUExecutionProvider'], root=os.environ['INSIGHTFACE_LOCATION'])
        face_app.prepare(ctx_id=0, det_size=(640, 640))#ctx_id=0 CPU

def init_cameras():
    logger.info(f"init_cameras in")

    name = "Thread-InitCameras-Timer"

    for thread in threading.enumerate():
        logger.info(f"init_cameras thread.name {thread.name}")
        if isinstance(thread, threading.Timer) and thread.name == name:
            thread.cancel()

    cameras_to_update, cameras_to_remove = fetch_camera_items()

    for cam_ip in cameras_to_update:
        try:
            init_gst_app(cam_ip)
        except Exception as e:
            logger.error(f"Error updating camera {cam_ip}: {e}")

    for cam_ip, onvif_sub_address in cameras_to_remove.items():
        try:
            stop_gstreamer_thread(cam_ip)
            if cam_ip in onvif_connectors:
                onvif_connectors[cam_ip].unsubscribe(cam_ip, onvif_sub_address)
                del onvif_connectors[cam_ip]
        except Exception as e:
            logger.error(f"Error removing camera {cam_ip}: {e}")

    for cam_ip in camera_items:
        if cam_ip not in cameras_to_update:
            try:
                claim_camera(cam_ip)

                subscribe_onvif(cam_ip)

            except Exception as e:
                logger.error(f"Error handling init_cameras: {e}")
                traceback.print_exc()
            pass

    timer = threading.Timer(int(os.environ['TIMER_CAM_RENEW']), init_cameras)
    timer.name = name
    timer.start()

    for thread in threading.enumerate():
        logger.info(f"init_cameras out thread.name {thread.name}")

    logger.info(f"init_cameras out")


def init_gst_app(cam_ip):
    logger.info(f"{cam_ip} init_gst_app in")

    host_id = os.environ['HOST_ID']
    if host_id is None:
        logger.info(f"{cam_ip} init_gst_app out no HOST_ID")
        return

    global thread_monitors


    # for thread_name in thread_monitors:
    #     logger.info(f"init_gst_app thread_monitors: {thread_name}")

    thread_gstreamer = None
    stop_gstreamer_thread(cam_ip)

    if cam_ip not in thread_monitors:
        logger.info(f"init_gst_app cam_ip {cam_ip} not in thread_monitors")
    # else:
        thread_gstreamer, is_new_gst_thread = start_gstreamer_thread(host_id=host_id, cam_ip=cam_ip)

        logger.info(f"init_gst_app thread_gstreamer: {thread_gstreamer}, is_new_gst_thread: {is_new_gst_thread}")

        if thread_gstreamer is not None:
            if is_new_gst_thread:

                if cam_ip in thread_monitors:
                    if thread_monitors[cam_ip] is not None:
                        logger.info(f"{cam_ip} init_gst_app before monitor.join()")
                        thread_monitors[cam_ip].join()
                        thread_monitors[cam_ip] = None
                        del thread_monitors[cam_ip]
                        logger.info(f"{cam_ip} init_gst_app after monitor.join()")
                        
                thread_monitors[cam_ip] = threading.Thread(target=monitor_stop_event, name=f"Thread-GstMonitor-{cam_ip}-init_gst_app-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}", args=(thread_gstreamer,))
                thread_monitors[cam_ip].start()

    subscribe_onvif(cam_ip)

    logger.info(f"{cam_ip} init_gst_app out")

    return thread_gstreamer


def stop_http_server():
    global httpd
    if httpd:
        logger.info("Shutting down HTTP server")
        httpd.shutdown()
        httpd.server_close()
        httpd = None
        logger.info("HTTP server shut down")

def start_http_server():

    global httpd
    global thread_monitors

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True  # Threads die when main thread exits

    class NewHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            # Override this method to suppress logging
            return
        
        def do_POST(self):
            # global thread_detector

            try:

                if self.path == '/recognise':
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()

                    # Process the POST data
                    if not self.headers['Content-Length']:

                        logger.debug('/recognise POST finished with fetch_members only')

                        timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                        timer.name = "Thread-FetchMembers"
                        timer.start()
                        # timer.join()

                        return

                    content_length = int(self.headers['Content-Length'])

                    logger.debug(f"/recognise POST {content_length}")
                    if content_length <= 0:

                        logger.debug('/recognise POST finished with fetch_members only')

                        timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                        timer.name = "Thread-FetchMembers"
                        timer.start()
                        # timer.join()

                        return

                    post_data = self.rfile.read(content_length)
                    event = json.loads(post_data)

                    logger.info('/recognise POST %s', json.dumps(event))

                    image_bgr, org_image = web_img.read_picture_from_url(event['faceImgUrl'])

                    # reference_faces = face_app.get(image_bgr)
                    reference_faces = self.analyze_faces(image_bgr)

                    if len(reference_faces) > 0:                    
                        event['faceEmbedding'] = reference_faces[0].embedding.tolist()

                        bbox = reference_faces[0].bbox.astype(int).flatten()
                        cropped_face = org_image.crop((bbox[0], bbox[1], bbox[2], bbox[3]))

                        # Convert the image to bytes
                        buffered = io.BytesIO()
                        cropped_face.save(buffered, format="JPEG")
                        cropped_face_bytes = buffered.getvalue()

                        event['faceImgBase64'] = base64.b64encode(cropped_face_bytes).decode('utf-8')

                    # Send the response
                    self.wfile.write(json.dumps(event).encode())

                    logger.info('/recognise POST finished')

                    timer = threading.Timer(10.0, fetch_members, kwargs={'forced': True})
                    timer.name = "Thread-FetchMembers"
                    timer.start()
                    # timer.join()

                elif self.path == '/onvif_notifications':
                    try:
                        # 1. Read and parse the incoming request
                        content_length = int(self.headers['Content-Length'])
                        post_data = self.rfile.read(content_length)

                        # 2. Extract notification data before any processing
                        cam_ip, utc_time, is_motion_value = OnvifConnector.extract_notification(post_data, self.client_address[0])

                        # 3. Define the async handler function that will process the notification
                        def async_handle():
                            try:
                                if is_motion_value:
                                    local_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z'
                                    logger.info(f"ONVIF Motion detected: is_motion_value={is_motion_value}, cam_ip={cam_ip}, camera_utc_time={utc_time}, using local_now={local_now}")
                                    handle_notification(cam_ip, local_now, is_motion_value)
                            except Exception as e:
                                # Log any errors in the async processing
                                logger.error(f"Exception in async_handle for ONVIF notification: {e}")
                                import traceback
                                traceback.print_exc()
                            finally:
                                # Always log completion, even if there was an error
                                logger.info(f"Async ONVIF notification thread for {cam_ip} finished and will be cleaned up.")

                        # 4. Start the async processing thread
                        thread_name = f"Thread-ONVIF-Notification-{cam_ip}-{utc_time}"
                        t = threading.Thread(target=async_handle, name=thread_name, daemon=True)
                        t.start()

                        # 5. Try to send response to client, but don't fail if client disconnected
                        try:
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b'Notification handled')
                        except (ConnectionResetError, BrokenPipeError) as e:
                            # Client disconnected before we could send response - this is okay
                            logger.warning(f"Client disconnected while sending response: {e}")
                            return
                    except Exception as e:
                        # 6. Handle any other errors in the main request processing
                        logger.error(f"Error in ONVIF notification handler: {e}")
                        traceback.print_exc()
                        try:
                            self.send_response(500)
                            self.end_headers()
                            self.wfile.write(b'Internal Server Error')
                        except (ConnectionResetError, BrokenPipeError):
                            # Client disconnected while sending error response - this is okay
                            logger.warning("Client disconnected while sending error response")
                            return

                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'Not Found')
            except BrokenPipeError:
                logger.error("Client disconnected before the response could be sent.")
            except Exception as e:
                logger.error(f"Error handling POST request: {e}")
                traceback.print_exc()

                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'Internal Server Error')



        def address_string(self):  # Limit access to local network requests
            host, _ = self.client_address[:2]
            return host


        def analyze_faces(self, img_data: np.ndarray, det_size=(640, 640)):

            if face_app is None:
                logger.info('analyze_faces out, face_app is None')
                return []

            # NOTE: try detect faces, if no faces detected, lower det_size until it does
            detection_sizes = [None] + [(size, size) for size in range(640, 256, -64)] + [(256, 256)]

            for size in detection_sizes:
                faces = face_app.get(img_data, det_size=size)
                if len(faces) > 0:
                    logger.info(f'analyze_faces out with {len(faces)} faces')
                    return faces

            logger.info('analyze_faces out, no faces detected')
            return []

    try:
        # Define the server address and port
        server_address = ('', http_port)

        httpd = ThreadedTCPServer(server_address, NewHandler)
        
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Error starting HTTP server: {e}")


def get_host_item():
    logger.debug('get_host_item in')

    # Specify the table name
    tbl_host = os.environ['TBL_HOST']

    # Get the table
    table = dynamodb.Table(tbl_host)

    # Scan the table with the filter expression
    response = table.scan()

    # Get the items from the response
    items = response.get('Items', [])

    if len(items) > 0:
        return items[0]
    else:
        return None
    
def get_property_item(host_id):
    logger.debug('get_property_item in')

    # Specify the table name
    tbl_asset = os.environ['TBL_ASSET']

    # Get the table
    table = dynamodb.Table(tbl_asset)

    # Retrieve item from the table
    response = table.query(
        KeyConditionExpression=Key('hostId').eq(host_id),
        FilterExpression=Attr('category').eq("PROPERTY")
    )

    # Get the items from the response
    items = response.get('Items', [])

    if len(items) > 0:
        return items[0]
    else:
        return None

def query_camera_item(host_id, cam_ip):

    logger.info(f"query_camera_item, in with {host_id} {cam_ip} ...")

    # Specify the table name
    tbl_asset = os.environ['TBL_ASSET']

    # Get the table
    table = dynamodb.Table(tbl_asset)

    # Retrieve item from the table
    response = table.query(
        KeyConditionExpression=Key('hostId').eq(host_id),
        FilterExpression=Attr('category').eq('CAMERA') & Attr('localIp').eq(cam_ip)
    )
    
    # Print the items returned by the query
    camera_item = response.get('Items', [None])[0]

    logger.info(f'query_camera_item out {camera_item}')

    return camera_item

def query_camera_items(host_id):

    logger.debug(f"query_camera_items, in with {host_id} ...")

    # Specify the table name
    tbl_asset = os.environ['TBL_ASSET']

    # Get the table
    table = dynamodb.Table(tbl_asset)

    # Retrieve item from the table
    response = table.query(
        KeyConditionExpression=Key('hostId').eq(host_id),
        FilterExpression=Attr('category').eq('CAMERA')
    )
    
    # Print the items returned by the query
    camera_item_list = response.get('Items', [None])

    logger.debug(f'query_camera_items out {camera_item_list}')

    return camera_item_list


def get_active_reservations():
    logger.debug('get_active_reservations in')

    # Specify the table name
    tbl_reservation = os.environ['TBL_RESERVATION']

    # Get the table
    table = dynamodb.Table(tbl_reservation)

    # Get the current date in 'YYYY-MM-DD' format
    current_date = datetime.now().strftime('%Y-%m-%d')

    # Create the filter expression
    filter_expression = Attr('checkInDate').lte(current_date) \
        & Attr('checkOutDate').gte(current_date)

    # Define the list of attributes to retrieve
    attributes_to_get = ['reservationCode', 'listingId']

    # Scan the table with the filter expression
    response = table.scan(
        # FilterExpression=filter_expression,
        ProjectionExpression=', '.join(attributes_to_get)
    )

    # Get the items from the response
    items = response.get('Items', [])

    for item in items:
        logger.debug(item)

    logger.debug('get_active_reservations out')

    return items

def update_member(reservationCode, memberNo, keyNotified=True):
    logger.debug('update_member in')

    # Specify the table name
    tbl_member = os.environ['TBL_MEMBER']

    table = dynamodb.Table(tbl_member)

    member_key = {
        'reservationCode': reservationCode,
        'memberNo': memberNo
    }

    response = table.update_item(
        Key=member_key,
        UpdateExpression=f'SET #kn = :kn',
        ExpressionAttributeNames={
            '#kn': 'keyNotified'
        },
        ExpressionAttributeValues={
            ':kn': keyNotified
        },
        ReturnValues='UPDATED_NEW'
    )
    logger.debug(f"update_member update_item: {repr(response)}")

    logger.debug('update_member out')

    return

def get_active_members():
    logger.debug('get_active_members in')

    # Specify the table name
    tbl_member = os.environ['TBL_MEMBER']

    # List of reservation codes to query
    active_reservations = get_active_reservations()

    # Define the list of attributes to retrieve
    attributes_to_get = ['reservationCode', 'memberNo', 'faceEmbedding', 'fullName', 'keyNotified']

    # Initialize an empty list to store the results
    results = []

    # Iterate over each reservation code and query DynamoDB
    for active_reservation in active_reservations:
        table = dynamodb.Table(tbl_member)
        
        # Query DynamoDB using the partition key (reservationCode)
        response = table.query(
            KeyConditionExpression='reservationCode = :code',
            ProjectionExpression=', '.join(attributes_to_get),
            ExpressionAttributeValues={
                ':code': active_reservation['reservationCode']
            }
        )

        for active_member in response['Items']:
            active_member['listingId'] = active_reservation['listingId']
        
        # Add the query results to the results list
        results.extend(response['Items'])

    filtered_results = []

    for item in results:
        if 'faceEmbedding' in item:
            item['faceEmbedding'] = np.array([float(value) for value in item['faceEmbedding']])
            filtered_results.append(item)
        else:
            logger.debug(f"get_active_members, member {item['reservationCode']}-{item['memberNo']} filtered out with no faceEmbedding")
    
    results = filtered_results

    for item in results:
        logger.debug(f"get_active_members out, reservationCode: {item['reservationCode']}, memberNo: {item['memberNo']}, fullName: {item['fullName']}")

    return results

def fetch_members(forced=False):
    logger.debug('fetch_members in')

    global last_fetch_time
    global active_members

    current_date = datetime.now().date()

    if forced is True:
        # logger.info('fetch_members init')
        active_members = get_active_members()
        last_fetch_time = current_date
        # logger.info('fetch_members done')
    else:
        if not active_members:
            # logger.info('fetch_members init')
            active_members = get_active_members()
            last_fetch_time = current_date
            # logger.info('fetch_members done')
        else:
            if last_fetch_time is None or last_fetch_time < current_date:
                # logger.info('fetch_members update')
                active_members = get_active_members()
                last_fetch_time = current_date
            # else:
            #     logger.info(f"fetch_members skip as last_fetch_time:{str(last_fetch_time)} >= current_date:{str(current_date)}")

    if thread_detector != None:
        logger.debug(f"fetch_members, Set active_members to thread_detector")
        thread_detector.active_members = active_members


def init_env_var():
    logger.debug('init_env_var in')

    try:
        name = "Thread-InitEnvVar-Timer"

        for thread in threading.enumerate():
            logger.info(f"init_env_var thread.name {thread.name}")
            if isinstance(thread, threading.Timer) and thread.name == name:
                thread.cancel()

        host_item = get_host_item()

        if host_item is not None:
            property_item = get_property_item(host_item['hostId'])

            if property_item is not None:
                os.environ['HOST_ID'] = host_item['hostId']
                os.environ['IDENTITY_ID'] = host_item['identityId']
                os.environ['PROPERTY_CODE'] = property_item['propertyCode']
                os.environ['CRED_PROVIDER_HOST'] = host_item['credProviderHost']
            else:
                raise ValueError("property_item is None")
        else:
            raise ValueError("host_item is None")
                
        logger.debug(f"init_env_var out HOST_ID:{os.environ['HOST_ID']} IDENTITY_ID:{os.environ['IDENTITY_ID']} PROPERTY_CODE{os.environ['PROPERTY_CODE']} CRED_PROVIDER_HOST{os.environ['CRED_PROVIDER_HOST']}")
    except Exception as e:
        # Log the exception
        logger.error(f"init_env_var error: {e}", exc_info=True)
    finally:
        # Reschedule the initialization function
        timer = threading.Timer(int(os.environ['TIMER_INIT_ENV_VAR']), init_env_var)
        timer.name = name
        timer.start()
    

def claim_camera(cam_ip):
    logger.info(f"{cam_ip} claim_cameras in")
    if cam_ip in thread_gstreamers:
        thread_gstreamer = thread_gstreamers[cam_ip]
        if thread_gstreamer is not None:
            if thread_gstreamer.is_playing:
                data = {
                    "uuid": thread_gstreamer.cam_uuid,
                    "hostId": os.environ['HOST_ID'],
                    "lastUpdateOn": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                    "isPlaying": True
                }

                iotClient.publish(
                    topic=f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/camera_heartbeat",
                    payload=json.dumps(data)
                )
                
                logger.debug(f"{cam_ip} claim_cameras out published: {data}")

                return
    data = {
        "uuid": camera_items[cam_ip]['uuid'],
        "hostId": os.environ['HOST_ID'],
        "lastUpdateOn": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "isPlaying": False
    }

    iotClient.publish(
        topic=f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/camera_heartbeat",
        payload=json.dumps(data)
    )

    logger.debug(f"{cam_ip} claim_cameras out published: {data}")

def claim_scanner():
    data = {
        "assetId": os.environ['AWS_IOT_THING_NAME'],
        "assetName": os.environ['AWS_IOT_THING_NAME'],
        "localIp": scanner_local_ip
    }
    
    iotClient.publish(
        topic="gocheckin/scanner_detected",
        payload=json.dumps(data)
    )

def fetch_scanner_output_queue():
    def upload_video_clip(message):
        if uploader_app is not None:

            local_file_path = message['payload']['local_file_path']
            video_key = message['payload']['video_key']
            object_key = message['payload']['object_key']

            logger.debug(f"fetch_scanner_output_queue, video_clipped received: {local_file_path}")

            uploader_app.put_object(object_key=object_key, local_file_path=local_file_path)

            # Keep milliseconds for unique DynamoDB keys (avoids collision when multiple cameras record at same second)
            record_start = message['payload']['start_datetime']
            record_end = message['payload']['end_datetime']

            logger.info(f"{message['payload']['cam_ip']} video_clipped with ms precision: record_start={record_start}, record_end={record_end}")

            payload = {
                "hostId": os.environ['HOST_ID'],
                "propertyCode": os.environ['PROPERTY_CODE'],
                "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                "coreName": os.environ['AWS_IOT_THING_NAME'],
                "assetId": message['payload']['cam_uuid'],
                "assetName": message['payload']['cam_name'],
                "cameraIp": message['payload']['cam_ip'],
                "recordStart": record_start,
                "recordEnd": record_end,
                "identityId": os.environ['IDENTITY_ID'],
                "s3level": 'private',
                "videoKey": video_key,
                "snapshotKey": ''
            }

            logger.debug(f"fetch_scanner_output_queue, video_clipped with IoT Publish payload: {payload}")

            iotClient.publish(
                topic=f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
                payload=json.dumps(payload)
            )

    while True:
        try:
            message = None
            if not scanner_output_queue.empty():
                message = scanner_output_queue.get_nowait()    
            
            if not message is None and 'type' in message:
                if message['type'] == 'member_detected':
                    cam_ip = message.get('cam_ip')

                    # Add trigger context to payload before publishing
                    if 'payload' in message and cam_ip:
                        context = trigger_lock_context.get(cam_ip, {
                            'onvif_triggered': False,
                            'specific_locks': set()
                        })
                        message['payload']['onvifTriggered'] = context.get('onvif_triggered', False)
                        message['payload']['occupancyTriggeredLocks'] = list(context.get('specific_locks', set()))

                        logger.info(f"fetch_scanner_output_queue, member_detected with trigger context: onvifTriggered={message['payload']['onvifTriggered']}, occupancyTriggeredLocks={message['payload']['occupancyTriggeredLocks']}")

                        # Clear context after adding to payload
                        if cam_ip in trigger_lock_context:
                            del trigger_lock_context[cam_ip]

                    if cam_ip:
                        logger.info(f"fetch_scanner_output_queue, member_detected WANT TO stop_feeding NOW")
                        thread_gstreamers[cam_ip].stop_feeding()

                    if ('payload' in message and 'local_file_path' in message and 'snapshot_payload' in message and uploader_app is not None):
                        local_file_path = message['local_file_path']
                        property_object_key = message['payload']['propertyImgKey']
                        snapshot_payload= message['snapshot_payload']

                        uploader_app.put_object(object_key=property_object_key, local_file_path=local_file_path)

                        logger.debug(f"fetch_scanner_output_queue, member_detected with IoT Publish snapshot_payload: {snapshot_payload}")

                        iotClient.publish(
                            topic=f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
                            payload=json.dumps(snapshot_payload)
                        )

                    if 'keyNotified' in message:
                        keyNotified = message['keyNotified']

                        if not keyNotified:
                            update_member(message['payload']['reservationCode'], message['payload']['memberNo'])

                            timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                            timer.name = "Thread-FetchMembers"
                            timer.start()
                            # timer.join()

                            logger.info(f"fetch_scanner_output_queue, member_detected with IoT Publish payload: {message['payload']}")

                            iotClient.publish(
                                topic=f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/member_detected",
                                payload=json.dumps(message['payload'])
                            )

                    if 'payload' in message:
                        iotClient.publish(
                            topic=f"gocheckin/member_detected",
                            payload=json.dumps(message['payload'])
                        )


                elif message['type'] == 'video_clipped':
                    thread_video_uploader = threading.Thread(target=upload_video_clip, name=f"Thread-VideoUploader-{message['payload']['start_datetime']}", args=(message,))
                    thread_video_uploader.start()

                    logger.debug(f'Available threads after thread_video_uploader: {", ".join(thread.name for thread in threading.enumerate())}')

        except Exception as e:
            logger.error(f"fetch_scanner_output_queue, Exception during running, Error: {e}")
            traceback.print_exc()
            pass
        time.sleep(0.1)


# http server
def start_server_thread():
    global server_thread
    with thread_lock:
        if server_thread is None or not server_thread.is_alive():
            stop_http_server()
            server_thread = threading.Thread(target=start_http_server, name="Thread-HttpServer" ,daemon=True)
            server_thread.start()
            logger.info("Server thread started")
        else:
            logger.info("Server thread is already running")

# scanner_output_queue
def start_scanner_output_queue_thread():
    scheduler_thread = threading.Thread(target=fetch_scanner_output_queue, name="Thread-FaceQueue")
    scheduler_thread.start()
    logger.info("Scanner Output Queue thread started")
    

# def stop_gstreamer_thread(thread_name):
#     logger.info(f"stop_gstreamer_thread, {thread_name} received, shutting down thread_gstreamer.")

#     if thread_name in thread_gstreamers:
#         if thread_gstreamers[thread_name] is not None:
#             thread_gstreamers[thread_name].stop()
#             thread_gstreamers[thread_name].join()
#             thread_gstreamers[thread_name] = None
#             logger.info(f"stop_gstreamer_thread, {thread_name} received, thread_gstreamer was just shut down.")

def start_gstreamer_thread(host_id, cam_ip):

    logger.debug(f"{cam_ip} start_gstreamer_thread in")

    global camera_items
    global thread_gstreamers
    camera_item = None

    if cam_ip in camera_items:
        camera_item = camera_items[cam_ip]

    if camera_item is None:
        camera_item = query_camera_item(host_id, cam_ip)        
        camera_items[cam_ip] = camera_item

    logger.debug(f"{cam_ip} start_gstreamer_thread camera_item {camera_item}")

    if camera_item is None:
        logger.debug(f"{cam_ip} start_gstreamer_thread, camera_item cannot be found")
        return None, False

    if not camera_item['isDetecting'] and not camera_item['isRecording']:
        logger.info(f"{cam_ip} start_gstreamer_thread not starting, camera_item is neither detecting nor recording")
        return None, False

    if cam_ip in thread_gstreamers:
        if thread_gstreamers[cam_ip] is not None:
            if thread_gstreamers[cam_ip].is_alive():
                logger.info(f"{cam_ip} start_gstreamer_thread not starting, already started")
                return thread_gstreamers[cam_ip], False

    params = {}
    params['rtsp_src'] = f"rtsp://{camera_item['username']}:{camera_item['password']}@{cam_ip}:{camera_item['rtsp']['port']}{camera_item['rtsp']['path']}"
    params['codec'] = camera_item['rtsp']['codec']
    params['framerate'] = camera_item['rtsp']['framerate']
    params['cam_ip'] = cam_ip
    params['cam_uuid'] = camera_item['uuid']
    params['cam_name'] = camera_item['assetName']

    thread_gstreamers[cam_ip] = gst.StreamCapture(params, scanner_output_queue, cam_queue)
    thread_gstreamers[cam_ip].start()

    logger.debug(f"{cam_ip} start_gstreamer_thread, starting...")

    return thread_gstreamers[cam_ip], True

def stop_gstreamer_thread(cam_ip):
    logger.info(f"{cam_ip} stop_gstreamer_thread in")

    if cam_ip in thread_gstreamers and thread_gstreamers[cam_ip] is not None:
        thread_gstreamers[cam_ip].stop(force=True)
        thread_gstreamers[cam_ip].join()
        thread_gstreamers[cam_ip] = None
        logger.info(f"{cam_ip} stop_gstreamer_thread out")


# Function to handle termination signals
def signal_handler(signum, frame):
    logger.info(f"Signal {signum} received, shutting down http server.")

    global shutting_down
    shutting_down = True

    for cam_ip in list(thread_gstreamers.keys()):
        try:
            camera_item = camera_items[cam_ip]

            if 'onvifSubAddress' in camera_item:
                if camera_item['onvifSubAddress'] is not None:
                    onvif_connectors[cam_ip].unsubscribe(cam_ip, camera_item['onvifSubAddress'])
                    camera_item['onvifSubAddress'] = None

        except Exception as e:
            logger.error(f"Error handling unsubscribe, cam_ip:{cam_ip} Error:{e}")
            pass

        stop_gstreamer_thread(cam_ip)


    global thread_monitors
    for thread in thread_monitors.values():
        thread.join()
        thread = None
    thread_monitors = {}

    clear_detector()

    global face_app
    face_app = None

    # global thread_gstreamers
    # for thread_name in thread_gstreamers:
    #     stop_gstreamer_thread(thread_name)
    # thread_gstreamers = {}


    global scanner_output_queue
    with scanner_output_queue.mutex:
        scanner_output_queue.queue.clear()

    global cam_queue
    with cam_queue.mutex:
        cam_queue.queue.clear()

    global server_thread    
    if server_thread is not None:
        stop_http_server()
        server_thread.join()  # Wait for the server thread to finish
        server_thread = None
    logger.info(f'Available threads after http server shutdown: {", ".join(thread.name for thread in threading.enumerate())}')

def clear_detector():
    global thread_detector
    if thread_detector is not None:
        thread_detector.stop_detection()
        thread_detector.join(timeout=1)
        thread_detector = None

    global active_members
    active_members = None

    global last_fetch_time
    last_fetch_time = None

def monitor_stop_event(thread_gstreamer):
    logger.info(f"{thread_gstreamer.cam_ip} monitor_stop_event")

    global thread_gstreamers
    global thread_monitors

    cam_ip = thread_gstreamer.cam_ip

    thread_gstreamer.stop_event.wait()  # Wait indefinitely for the event to be set
    thread_gstreamer.join()  # Join the stopped thread
    
    # Check for forced stop
    if thread_gstreamer.force_stop.is_set():
        logger.info(f"{cam_ip} Force stop detected, exiting monitor")
        thread_monitors[cam_ip] = None
        del thread_monitors[cam_ip]
        return

    # if shutting_down:
    #     logger.info(f"{cam_ip} shutting down, not restarting.")
    #     return

    if thread_gstreamer.is_alive():
        logger.info(f"{cam_ip} thread_gstreamer still alive unexpectedly, not restarting.")
        return
    
    logger.info(f"{cam_ip} monitor_stop_event: {thread_gstreamer.name} has stopped, restarting gstreamer by {threading.current_thread().name}...")

    # Check if a new thread already exists
    if cam_ip in thread_gstreamers and thread_gstreamers[cam_ip] is not None and thread_gstreamers[cam_ip].is_alive():
        logger.info(f"{cam_ip} A new GStreamer thread is already running, skipping restart.")
        return

    # Clear previous references before restarting
    thread_gstreamers[cam_ip] = None
    thread_monitors[cam_ip] = None
    del thread_monitors[cam_ip]
    new_thread_gstreamer, _ = start_gstreamer_thread(host_id=os.environ['HOST_ID'], cam_ip=cam_ip)

    if new_thread_gstreamer is not None:
        thread_gstreamers[cam_ip] = new_thread_gstreamer

        # Ensure only one monitor thread is created
        if cam_ip in thread_monitors and thread_monitors[cam_ip] is not None and thread_monitors[cam_ip].is_alive():
            logger.warning(f"{cam_ip} Monitor thread already running, skipping duplicate.")
            return

        thread_monitors[cam_ip] = threading.Thread(
            target=monitor_stop_event,
            name=f"Thread-GstMonitor-{cam_ip}-monitor_stop_event-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}",
            args=(thread_gstreamers[cam_ip],),
            daemon=True
        )
        thread_monitors[cam_ip].start()

    subscribe_onvif(cam_ip)
            
def set_recording_time(cam_ip, delay, utc_time):
    logger.debug(f'set_recording_time, cam_ip: {cam_ip} utc_time: {utc_time}')
    global recording_timers

    if cam_ip in recording_timers:
        if recording_timers[cam_ip]:
            recording_timers[cam_ip].cancel()

    recording_timers[cam_ip] = threading.Timer(delay, thread_gstreamers[cam_ip].stop_recording, [utc_time])
    recording_timers[cam_ip].name = f"Thread-RecordingStopper-{cam_ip}"
    recording_timers[cam_ip].start()
    # recording_timers[cam_ip].join()

def handle_notification(cam_ip, utc_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z', is_motion_value=False):
    logger.info(f"{cam_ip} handle_notification in is_motion_value: {is_motion_value}, utc_time: {utc_time}")

    global thread_detector

    if not is_motion_value:
        logger.info(f"{cam_ip} handle_notification out not motion detected event")
        return

    if cam_ip is None:
        logger.info(f"{cam_ip} handle_notification out no cam_ip")
        return
    else:
        if cam_ip not in camera_items:
            logger.info(f"{cam_ip} handle_notification out no cam_ip")
            return
        else:
            camera_item = camera_items[cam_ip]
            if camera_item is None:
                logger.info(f"{cam_ip} handle_notification out no camera_item")
                return

    # thread_gstreamer = init_gst_app(cam_ip)
    thread_gstreamer = thread_gstreamers[cam_ip]
    if thread_gstreamer is None:
        logger.info(f"{cam_ip} handle_notification out no thread_gstreamer")
        return
    elif not thread_gstreamer.is_alive():
        logger.info(f"{cam_ip} handle_notification out thread_gstreamer not started")
        return
    elif not thread_gstreamer.is_playing:
        logger.info(f"{cam_ip} handle_notification out thread_gstreamer not playing")
        return


    # record
    if camera_item['isRecording']:
        if thread_gstreamer.start_recording(utc_time):
            set_recording_time(cam_ip, int(os.environ['TIMER_RECORD']), utc_time)

    # detect - ONVIF motion triggers detection with lock_asset_id=None
    # The selective unlock logic is handled by trigger_face_detection() and TypeScript
    if camera_item['isDetecting']:
        trigger_face_detection(cam_ip, None)  # None = ONVIF trigger (unlock legacy locks only)

    logger.info(f"{cam_ip} handle_notification out")


def trigger_face_detection(cam_ip, lock_asset_id=None):
    """Trigger face detection for a camera

    Args:
        cam_ip: Camera IP address
        lock_asset_id: Optional lock assetId that triggered detection
                       None = ONVIF motion (unlock legacy locks)
                       string = specific lock occupancy (unlock that lock)
    """
    global camera_items, thread_gstreamers, thread_detector, trigger_lock_context

    logger.info('trigger_face_detection in cam_ip: %s, lock_asset_id: %s', cam_ip, lock_asset_id)

    # Validate camera exists
    if cam_ip not in camera_items:
        logger.warning('trigger_face_detection - camera not found: %s', cam_ip)
        return

    camera_item = camera_items[cam_ip]

    # Skip ONVIF trigger if no legacy locks (all locks have sensors)
    if lock_asset_id is None:
        camera_locks = camera_item.get('locks', {})
        # withKeypad=true means has occupancy sensor, withKeypad=false/missing means legacy
        has_legacy = any(not lock.get('withKeypad', False) for lock in camera_locks.values())
        if not has_legacy:
            logger.info('trigger_face_detection - skipping ONVIF trigger, no legacy locks: %s', cam_ip)
            return

    # Check if this is a new detection or extending existing one
    is_new_detection = cam_ip not in trigger_lock_context

    # Initialize context for new detection
    if is_new_detection:
        trigger_lock_context[cam_ip] = {
            'started_by_onvif': (lock_asset_id is None),  # Set once, never changes
            'onvif_triggered': False,
            'specific_locks': set(),
            'active_occupancy': set()
        }

    context = trigger_lock_context[cam_ip]

    # Merge trigger info
    if lock_asset_id is None:
        context['onvif_triggered'] = True
    else:
        context['specific_locks'].add(lock_asset_id)
        context['active_occupancy'].add(lock_asset_id)

    # Check if detection is enabled for this camera
    if not camera_item.get('isDetecting', False):
        logger.info('trigger_face_detection - detection not enabled for camera: %s', cam_ip)
        return

    # Validate GStreamer thread exists and is ready
    if cam_ip not in thread_gstreamers:
        logger.warning('trigger_face_detection - gstreamer not found: %s', cam_ip)
        return

    thread_gstreamer = thread_gstreamers[cam_ip]

    if not thread_gstreamer.is_alive():
        logger.warning('trigger_face_detection - gstreamer not alive: %s', cam_ip)
        return

    if not thread_gstreamer.is_playing:
        logger.warning('trigger_face_detection - gstreamer not playing: %s', cam_ip)
        return

    # Handle timer extension if already detecting
    if thread_gstreamer.is_feeding:
        should_extend = False

        if lock_asset_id is not None:
            # Occupancy trigger - ALWAYS extend timer
            should_extend = True
            logger.info('trigger_face_detection - occupancy trigger, will extend timer')
        elif context.get('started_by_onvif', False):
            # ONVIF trigger - only extend if detection was started by ONVIF
            should_extend = True
            logger.info('trigger_face_detection - ONVIF trigger, started_by_onvif=True, will extend timer')
        else:
            logger.info('trigger_face_detection - ONVIF trigger, started_by_onvif=False, timer NOT extended')

        if should_extend:
            thread_gstreamer.extend_timer(int(os.environ['TIMER_DETECT']))
            logger.info('trigger_face_detection - timer extended for camera: %s', cam_ip)

        logger.info('trigger_face_detection out - context merged, detection continues')
        return

    # Start new face detection
    fetch_members()
    if thread_detector is not None:
        thread_gstreamer.feed_detecting(int(os.environ['TIMER_DETECT']))
        logger.info('trigger_face_detection - started for camera: %s', cam_ip)
    else:
        logger.warning('trigger_face_detection - detector thread not available')

    logger.info('trigger_face_detection out')


def handle_occupancy_false(cam_ip, lock_asset_id):
    """Handle occupancy:false - remove lock from context and possibly stop detection early"""
    global trigger_lock_context, thread_gstreamers, camera_items

    logger.info('handle_occupancy_false in cam_ip: %s, lock_asset_id: %s', cam_ip, lock_asset_id)

    if cam_ip not in trigger_lock_context:
        logger.info('handle_occupancy_false - no context for camera: %s', cam_ip)
        return

    context = trigger_lock_context[cam_ip]

    # Remove from both sets
    context['specific_locks'].discard(lock_asset_id)
    context['active_occupancy'].discard(lock_asset_id)

    logger.info('handle_occupancy_false - context after removal: %s', context)

    # Check if should stop detection early
    if cam_ip in thread_gstreamers:
        thread_gstreamer = thread_gstreamers[cam_ip]
        if thread_gstreamer is not None and thread_gstreamer.is_feeding:
            # Stop if: active_occupancy empty AND no ONVIF triggered AND no legacy locks
            if len(context['active_occupancy']) == 0 and not context['onvif_triggered']:
                camera_locks = camera_items.get(cam_ip, {}).get('locks', {})
                # withKeypad=true means has occupancy sensor, withKeypad=false/missing means legacy
                has_legacy = any(not lock.get('withKeypad', False) for lock in camera_locks.values())
                if not has_legacy:
                    logger.info('handle_occupancy_false - stopping detection early for: %s', cam_ip)
                    thread_gstreamer.stop_feeding()
                    del trigger_lock_context[cam_ip]
                else:
                    logger.info('handle_occupancy_false - has legacy locks, continuing detection: %s', cam_ip)
            else:
                logger.info('handle_occupancy_false - other triggers active, continuing detection: %s', cam_ip)

    logger.info('handle_occupancy_false out')


def subscribe_onvif(cam_ip):
    logger.info(f"{cam_ip} subscribe_onvif in")
    
    global camera_items
    global onvif_connectors

    if cam_ip not in camera_items:
        logger.info(f"{cam_ip} subscribe_onvif out cam_ip not in camera_items")
        return
    else:
        camera_item = camera_items[cam_ip]

    if 'onvifSubAddress' in camera_item:
        old_onvif_sub_address = camera_item['onvifSubAddress']
    else:
        old_onvif_sub_address = None
    
    if cam_ip not in onvif_connectors:
        logger.info(f"{cam_ip} subscribe_onvif cam_ip not in onvif_connectors")

        try:
            onvif_connectors[cam_ip] = OnvifConnector(camera_item)
        except Exception as e:
            logger.error(f"{cam_ip} Error handling OnvifConnector init: {e}")
            onvif_connectors[cam_ip] = None

    if onvif_connectors[cam_ip] is None:
        logger.info(f"{cam_ip} subscribe_onvif out OnvifConnector　cannot be created")
        return

    # Check if ONVIF subscription is enabled in camera settings
    onvif_settings = camera_item.get('onvif', {})
    is_subscription_enabled = onvif_settings.get('isSubscription', False)

    logger.info(f"{cam_ip} subscribe_onvif camera_item: {camera_item} old_onvif_sub_address: {old_onvif_sub_address} is_subscription_enabled: {is_subscription_enabled}")

    # Only subscribe if isSubscription is enabled AND (isDetecting OR isRecording)
    if is_subscription_enabled and (camera_item['isDetecting'] or camera_item['isRecording']):

        onvif_sub_address = onvif_connectors[cam_ip].subscribe(cam_ip, old_onvif_sub_address, scanner_local_ip, http_port)

        logger.info(f"subscribe_onvif subscribe cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

        camera_item['onvifSubAddress'] = onvif_sub_address

    else:
        # Unsubscribe if isSubscription is disabled or both isDetecting and isRecording are false
        if old_onvif_sub_address is not None:
            onvif_connectors[cam_ip].unsubscribe(cam_ip, old_onvif_sub_address)
            camera_item['onvifSubAddress'] = None

        onvif_connectors[cam_ip] = None
        del onvif_connectors[cam_ip]

    logger.info(f"{cam_ip} subscribe_onvif out")

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Start the scheduler threads
# start_init_processes()

# Claim scanner
claim_scanner()

# init env var
init_env_var()

# Init face_app
init_face_detector()

# Init uploader_app
init_uploader_app()

# Start the HTTP server thread
start_server_thread()

# Start scanner_output_queue thread
start_scanner_output_queue_thread()

# init cameras
init_cameras()

