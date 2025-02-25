import signal
import json
import logging
from datetime import datetime, timezone
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

# Initialize the thread_monitors
thread_monitors = {}

# Initialize the scanner_output_queue
scanner_output_queue = Queue(maxsize=50)
cam_queue = Queue(maxsize=500)
motion_detection_queue = Queue(maxsize=500)

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

        if 'cam_ip' in event:
            init_gst_app(event['cam_ip'], os.environ['HOST_ID'], True)

    elif topic == f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/force_detect":
        logger.info('function_handler force_detect')

        if 'cam_ip' in event:
            handle_notification(event['cam_ip'], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + 'Z', True)
    elif topic == f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/change_var":
        logger.info(f"function_handler change_var event: ${event}")
        for key, value in event.items():
            os.environ[key] = value

        for key in event:
            logger.info(f"change_var: ${key}: ${os.environ[key]}")


def fetch_camera_items():
    logger.debug(f"fetch_camera_items in")

    global camera_items

    try:
        camera_item_list = query_camera_items(os.environ['HOST_ID'])
        
        for camera_item in camera_item_list:
            cam_ip = camera_item['localIp']
            if cam_ip not in camera_items:
                camera_items[cam_ip] = camera_item
            else:
                onvif_sub_address = None
                if 'onvifSubAddress' in camera_items[cam_ip]:
                    onvif_sub_address = camera_items[cam_ip]['onvifSubAddress']

                camera_items[cam_ip] = camera_item
                camera_items[cam_ip]['onvifSubAddress'] = onvif_sub_address
            
    except Exception as e:
        logger.error(f"Error handling fetch_camera_items: {e}")

    logger.debug(f"fetch_camera_items out")

def init_uploader_app():
    logger.debug(f"init_uploader_app in")

    global uploader_app
    if uploader_app is None:
        if 'CRED_PROVIDER_HOST' in os.environ:
            uploader_app = uploader.S3Uploader(
                cred_provider_host=os.environ['CRED_PROVIDER_HOST'],
                cred_provider_path=f"/role-aliases/{os.environ['AWS_ROLE_ALIAS']}/credentials",
                bucket_name=os.environ['VIDEO_BUCKET']
            )
    
    logger.debug(f"init_uploader_app out")

def init_face_app(model='buffalo_sc'):
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

    fetch_camera_items()

    for cam_ip in camera_items:
        try:
            claim_camera(cam_ip)

            init_gst_app(cam_ip)

            subscribe_onvif(cam_ip)


        except Exception as e:
            logger.error(f"Error handling init_cameras: {e}")
            traceback.print_exc()
            pass

    timer = threading.Timer(int(os.environ['TIMER_CAM_RENEW']), init_cameras)
    timer.name = "Thread-InitGst-Timer"
    timer.start()

    logger.info(f"init_cameras out")

def init_gst_apps():
    logger.info(f"init_gst_apps in")

    fetch_camera_items()

    for cam_ip in camera_items:
        try:
            init_gst_app(cam_ip)
        except Exception as e:
            logger.error(f"Error handling init_gst_apps: {e}")
            traceback.print_exc()
            pass

    timer = threading.Timer(int(os.environ['TIMER_CAM_RENEW']), init_gst_apps)
    timer.name = "Thread-InitGst-Timer"
    timer.start()

    logger.info(f"init_gst_apps out")


def init_gst_app(cam_ip, host_id, forced=False):
    logger.info(f"{cam_ip} init_gst_app in host_id: {host_id}, forced: {forced}")

    if host_id is None:
        host_id = os.environ['HOST_ID']

    global thread_monitors

    if forced:
        stop_gstreamer_thread(cam_ip)

    thread_gstreamer, is_new_gst_thread = start_gstreamer_thread(host_id=host_id, cam_ip=cam_ip, forced=forced)

    logger.info(f"init_gst_app thread_gstreamer: {thread_gstreamer}, is_new_gst_thread: {is_new_gst_thread}")

    if thread_gstreamer is not None:
        if is_new_gst_thread and not forced:

            if cam_ip in thread_monitors:
                if thread_monitors[cam_ip] is not None:
                    thread_monitors[cam_ip].join()

            thread_monitors[cam_ip] = threading.Thread(target=monitor_stop_event, name=f"Thread-GstMonitor-{cam_ip}", args=(thread_gstreamer,))
            thread_monitors[cam_ip].start()
            

    logger.info(f"{cam_ip} init_gst_app out forced: {forced}")

    return thread_gstreamer


# def read_picture_from_url(url):

#     # Download the image
#     response = requests.get(url)
#     response.raise_for_status()  # Ensure the request was successful
    
#     # Open the image from the downloaded content    
#     image = PIL.Image.open(io.BytesIO(response.content)).convert("RGB")
    
#     # Convert the image to a numpy array
#     image_array = np.array(image)
    
#     # Rearrange the channels from RGB to BGR
#     image_bgr = image_array[:, :, [2, 1, 0]]
    
#     return image_bgr, image

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

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    class NewHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            # Override this method to suppress logging
            return
        
        def do_POST(self):
            # global thread_detector

            try:

                # if self.client_address[0] != '127.0.0.1':
                #     self.send_error(403, "Forbidden: Only localhost allowed")
                #     return

                

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
                    

                    # print('reference_faces[0].embedding:')
                    # print(type(reference_faces[0].embedding))

                    event['faceEmbedding'] = reference_faces[0].embedding.tolist()

                    # print('event[faceEmbedding]:')
                    # print(type(event['faceEmbedding']))

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
                    content_length = int(self.headers['Content-Length'])
                    post_data = self.rfile.read(content_length)

                    cam_ip, utc_time, is_motion_value = OnvifConnector.extract_notification(post_data, self.client_address[0])
                    if is_motion_value:
                        logger.info(f"ONVIF Motion detected: is_motion_value={is_motion_value}, cam_ip={cam_ip}, utc_time={utc_time}")

                    if is_motion_value:
                        handle_notification(cam_ip, utc_time, is_motion_value)

                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'Notification handled')
  
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
            # NOTE: try detect faces, if no faces detected, lower det_size until it does
            detection_sizes = [None] + [(size, size) for size in range(640, 256, -64)] + [(256, 256)]

            for size in detection_sizes:
                faces = face_app.get(img_data, det_size=size)
                if len(faces) > 0:
                    return faces

            return []

    try:
        # Define the server address and port
        server_address = ('', http_port)

        httpd = ReusableTCPServer(server_address, NewHandler)
        
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

# def get_camera_item(host_id, cam_uuid):

#     # Specify the table name
#     tbl_asset = os.environ['TBL_ASSET']

#     # Get the table
#     table = dynamodb.Table(tbl_asset)

#     # Retrieve item from the table
#     response = table.get_item(
#         Key={
#             'hostId': host_id,
#             'uuid': cam_uuid
#         }
#     )
    
#     # Check if the item exists
#     item = response.get('Item')
#     if item:
#         return item
#     else:
#         return None


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
        
        
def initialize_env_var():
    logger.debug('initialize_env_var in')

    try:
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
                
        # Reschedule the initialization function for every 30 minutes (1800 seconds)
        timer = threading.Timer(int(os.environ['TIMER_INIT_ENV_VAR']), initialize_env_var)
        timer.name = "Thread-Initializer-Timer"
        timer.start()
        # timer.join()
        
        logger.debug(f"initialize_env_var out HOST_ID:{os.environ['HOST_ID']} IDENTITY_ID:{os.environ['IDENTITY_ID']} PROPERTY_CODE{os.environ['PROPERTY_CODE']} CRED_PROVIDER_HOST{os.environ['CRED_PROVIDER_HOST']}")
    except Exception as e:
        # Log the exception
        logger.error(f"initialize_env_var error: {e}", exc_info=True)
        
        # Exit the script
        sys.exit(1)

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
                    topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/camera_heartbeat",
                    payload=json.dumps(data)
                )
                
                logger.info(f"{cam_ip} claim_cameras out published: {data}")

                return
    data = {
        "uuid": camera_items[cam_ip]['uuid'],
        "hostId": os.environ['HOST_ID'],
        "lastUpdateOn": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "isPlaying": False
    }

    iotClient.publish(
        topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/camera_heartbeat",
        payload=json.dumps(data)
    )

    logger.info(f"{cam_ip} claim_cameras out published: {data}")

def claim_cameras():
    logger.debug(f"claim_cameras in")
    for cam_ip in camera_items:
        claim_camera(cam_ip)

    # Reschedule the claim cameras function for every 2 minutes (120 seconds)
    timer = threading.Timer(600, claim_cameras)
    timer.name = "Thread-ClaimCameras-Timer"
    timer.start()
    # timer.join()

def claim_scanner():
    data = {
        "equipmentId": os.environ['AWS_IOT_THING_NAME'],
        "equipmentName": os.environ['AWS_IOT_THING_NAME'],
        "localIp": scanner_local_ip
    }
    
    iotClient.publish(
        topic="gocheckin/scanner_detected",
        payload=json.dumps(data)
    )

def fetch_scanner_output_queue():
    def upload_video_clip(message):
        local_file_path = message['payload']['local_file_path']
        video_key = message['payload']['video_key']
        object_key = message['payload']['object_key']

        logger.debug(f"fetch_scanner_output_queue, video_clipped received: {local_file_path}")

        uploader_app.put_object(object_key=object_key, local_file_path=local_file_path)

        payload = {
            "hostId": os.environ['HOST_ID'],
            "propertyCode": os.environ['PROPERTY_CODE'],
            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
            "coreName": os.environ['AWS_IOT_THING_NAME'],
            "equipmentId": message['payload']['cam_uuid'],
            "equipmentName": message['payload']['cam_name'],
            "cameraIp": message['payload']['cam_ip'],
            "recordStart": message['payload']['start_datetime'],
            "recordEnd": message['payload']['end_datetime'],
            "identityId": os.environ['IDENTITY_ID'],
            "s3level": 'private',
            "videoKey": video_key,
            "snapshotKey": ''
        }

        logger.debug(f"fetch_scanner_output_queue, video_clipped with IoT Publish payload: {payload}")

        iotClient.publish(
            topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
            payload=json.dumps(payload)
        )

    while True:
        try:
            message = None
            if not scanner_output_queue.empty():
                message = scanner_output_queue.get_nowait()    
            
            if not message is None and 'type' in message:
                if message['type'] == 'member_detected':
                    if 'cam_ip' in message:
                        cam_ip = message['cam_ip']
                        logger.info(f"fetch_scanner_output_queue, member_detected WANT TO stop_feeding NOW")
                        thread_gstreamers[cam_ip].stop_feeding()
                    
                    if ('payload' in message and 'local_file_path' in message and 'snapshot_payload' in message):
                        local_file_path = message['local_file_path']
                        property_object_key = message['payload']['propertyImgKey']
                        snapshot_payload= message['snapshot_payload']

                        uploader_app.put_object(object_key=property_object_key, local_file_path=local_file_path)

                        logger.debug(f"fetch_scanner_output_queue, member_detected with IoT Publish snapshot_payload: {snapshot_payload}")

                        iotClient.publish(
                            topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
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
                                topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/member_detected",
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

def fetch_motion_detection_queue():
    while True:
        try:
            if not motion_detection_queue.empty():
                cam_ip, is_motion_value, utc_time = motion_detection_queue.get_nowait()    
                logger.debug(f"Fetched from motion_detection_queue: {is_motion_value}")

                if is_motion_value:
                    handle_notification(cam_ip, utc_time, is_motion_value)

        except Exception as e:
            logger.error(f"fetch_motion_detection_queue, Exception during running, Error: {e}")
            traceback.print_exc()
            pass
        
        time.sleep(1)



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

# motion_detection_queue
def start_motion_detection_queue_thread():
    scheduler_thread = threading.Thread(target=fetch_motion_detection_queue, name="Thread-MotionDetectionQueue")
    scheduler_thread.start()
    logger.info("Motion Detection Queue thread started")

# Function to start the init processes
def start_init_processes():
    # Start the claim scanner thread after the initialization
    claim_scanner_thread = threading.Thread(target=claim_scanner, name="Thread-ClaimScanner")
    claim_scanner_thread.start()
    logger.info("Claim scanner thread started")

    # Start the initialization thread first
    initialization_thread = threading.Thread(target=initialize_env_var, name="Thread-Initializer")
    initialization_thread.start()
    logger.info("Initialization thread started")

    time.sleep(2)

    init_cameras
    init_cameras_thread = threading.Thread(target=init_cameras, name="Thread-InitCam-Timer")
    init_cameras_thread.start()
    logger.info("InitCam thread started")


    # # Start the InitGst thread
    # init_gst_apps_thread = threading.Thread(target=init_gst_apps, name="Thread-InitGst-Timer")
    # init_gst_apps_thread.start()
    # logger.info("InitGst thread started")

    # # Start the SubscribeOnvif thread
    # subscribe_onvifs_thread = threading.Thread(target=subscribe_onvifs, name="Thread-SubscribeOnvifs")
    # subscribe_onvifs_thread.start()
    # logger.info("SubscribeOnvif thread started")


    # # Start the claim camera thread after the initialization
    # claim_cameras_thread = threading.Thread(target=claim_cameras, name="Thread-ClaimCameras")
    # claim_cameras_thread.start()
    # logger.info("Claim camera thread started")
    

def stop_gstreamer_thread(thread_name):
    logger.info(f"stop_gstreamer_thread, {thread_name} received, shutting down thread_gstreamer.")

    if thread_name in thread_gstreamers:
        if thread_gstreamers[thread_name] is not None:
            thread_gstreamers[thread_name].stop()
            thread_gstreamers[thread_name].join()
            thread_gstreamers[thread_name] = None
            logger.info(f"stop_gstreamer_thread, {thread_name} received, thread_gstreamer was just shut down.")

def start_gstreamer_thread(host_id, cam_ip, forced=False):

    logger.debug(f"{cam_ip} start_gstreamer_thread in")

    global camera_items
    global thread_gstreamers
    camera_item = None

    if cam_ip in camera_items:
        camera_item = camera_items[cam_ip]

    if camera_item is None or forced:
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
    params['cam_name'] = camera_item['equipmentName']

    thread_gstreamers[cam_ip] = gst.StreamCapture(params, scanner_output_queue, cam_queue)
    thread_gstreamers[cam_ip].start()

    logger.info(f"{cam_ip} start_gstreamer_thread, starting...")

    return thread_gstreamers[cam_ip], True

# Function to handle termination signals
def signal_handler(signum, frame):
    logger.info(f"Signal {signum} received, shutting down http server.")

    global shutting_down
    shutting_down = True

    for cam_ip in camera_items:
        try:
            camera_item = camera_items[cam_ip]

            if camera_item['onvif']['isSubscription']:
                if 'onvifSubAddress' in camera_item:
                    if camera_item['onvifSubAddress'] is not None:
                        onvif_connectors[cam_ip].unsubscribe(camera_item)
                        camera_item['onvifSubAddress'] = None
            elif camera_item['onvif']['isPullpoint']:
                onvif_connectors[cam_ip].stop_pullpoint(camera_item)

        except Exception as e:
            logger.error(f"Error handling unsubscribe, cam_ip:{cam_ip} Error:{e}")
            pass

    clear_detector()

    global thread_gstreamers
    for thread_name in thread_gstreamers:
        stop_gstreamer_thread(thread_name)
    thread_gstreamers = {}

    global thread_monitors
    for thread in thread_monitors.values():
        thread.join()
        thread = None
    thread_monitors = {}

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
        thread_detector.join()
        thread_detector = None

    global face_app
    face_app = None

    global active_members
    active_members = None

    global last_fetch_time
    last_fetch_time = None

def monitor_stop_event(thread_gstreamer):
    logger.debug(f"{thread_gstreamer.cam_ip} monitor_stop_event in")
    
    global thread_gstreamers
    global thread_monitors

    cam_ip = thread_gstreamer.cam_ip

    thread_gstreamer.stop_event.wait()  # Wait indefinitely for the event to be set
    logger.debug(f"{cam_ip} monitor_stop_event: {thread_gstreamer.name} has stopped")
    thread_gstreamer.join()  # Join the stopped thread

    
    # Restart the thread
    if not thread_gstreamer.is_alive() and not shutting_down:
        thread_gstreamer = None
        thread_gstreamers[cam_ip] = None
        thread_gstreamers[cam_ip], _ = start_gstreamer_thread(host_id=os.environ['HOST_ID'], cam_ip=cam_ip)

        if thread_gstreamers[cam_ip] is not None:
            if thread_monitors[cam_ip] is not None:
                thread_monitors[cam_ip] = None
            logger.debug(f"{cam_ip} monitor_stop_event restarting")
            thread_monitors[cam_ip] = threading.Thread(target=monitor_stop_event, name=f"Thread-GstMonitor-{cam_ip}", args=(thread_gstreamers[cam_ip],))
            thread_monitors[cam_ip].start()

            


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

def handle_notification(cam_ip, utc_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + 'Z', is_motion_value=False):
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

    thread_gstreamer = init_gst_app(cam_ip)
    if thread_gstreamer is None:
        logger.info(f"{cam_ip} handle_notification out no thread_gstreamer")
        return
    elif not thread_gstreamer.is_playing:
        logger.info(f"{cam_ip} handle_notification out thread_gstreamer not playing")
        return

    # detect
    if camera_item['isDetecting']:
        
        init_face_app()
        fetch_members()

        if thread_detector is None:

            thread_detector = fdm.FaceRecognition(face_app, active_members, scanner_output_queue, cam_queue)
            thread_detector.start()

        else:
            if thread_detector.stop_event.is_set():
                logger.info(f"Clearing detector and initializing face_app")
                clear_detector()

                thread_detector = fdm.FaceRecognition(face_app, active_members, scanner_output_queue, cam_queue)

        thread_gstreamer.feed_detecting(int(os.environ['TIMER_DETECT']))


    # record 
    if camera_item['isRecording']:
        if thread_gstreamer.start_recording(utc_time):
            set_recording_time(cam_ip, int(os.environ['TIMER_RECORD']), utc_time)

    logger.info(f"{cam_ip} handle_notification out")

def subscribe_onvifs():
    logger.debug(f"subscribe_onvifs in")
        
    for cam_ip in camera_items:
        subscribe_onvif(cam_ip)
    
    timer = threading.Timer(1800, subscribe_onvifs)
    timer.name = "Thread-SubscribeOnvif-Timer"
    timer.start()

    logger.debug(f"subscribe_onvifs out")


def subscribe_onvif(cam_ip):
    logger.info(f"{cam_ip} subscribe_onvif in")
    
    global camera_items
    global onvif_connectors
        
    # if 'isDetecting' in camera_items[cam_ip] or 'isRecording' in camera_items[cam_ip]:
    if camera_items[cam_ip]['isDetecting'] or camera_items[cam_ip]['isRecording']:

        if not (cam_ip in onvif_connectors and onvif_connectors[cam_ip] is not None):
            onvif_connectors[cam_ip] = OnvifConnector(camera_items[cam_ip])

        if camera_items[cam_ip]['onvif']['isSubscription']:
            old_onvif_sub_address = None
            if 'onvifSubAddress' in camera_items[cam_ip]:
                old_onvif_sub_address = camera_items[cam_ip]['onvifSubAddress']
            
            onvif_sub_address = onvif_connectors[cam_ip].subscribe(cam_ip, old_onvif_sub_address, scanner_local_ip, http_port)

            logger.info(f"subscribe_onvif subscribe cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

            camera_items[cam_ip]['onvifSubAddress'] = onvif_sub_address

    else:
        if cam_ip in onvif_connectors and onvif_connectors[cam_ip] is not None:
            if camera_items[cam_ip]['onvif']['isSubscription']:
                if 'onvifSubAddress' in camera_items[cam_ip]:
                    if camera_items[cam_ip]['onvifSubAddress'] is not None:
                        onvif_connectors[cam_ip].unsubscribe(camera_items[cam_ip])
                        camera_items[cam_ip]['onvifSubAddress'] = None

            onvif_connectors[cam_ip] = None
            del onvif_connectors[cam_ip]

    logger.info(f"{cam_ip} subscribe_onvif out")

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Start the scheduler threads
start_init_processes()

# Init face_app
init_face_app()

# Init uploader_app
init_uploader_app()

# Start the HTTP server thread
start_server_thread()

# Start scanner_output_queue thread
start_scanner_output_queue_thread()

# Start motion_detection_queue thread
# start_motion_detection_queue_thread()