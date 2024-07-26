import signal
import json
import logging
from datetime import datetime
import sys
import os

import io
import base64

from queue import Queue, Empty

import traceback

import http.server
import socketserver

import socket

import threading
import sched
import time

import random

import requests

import PIL.Image
import numpy as np

import boto3
from boto3.dynamodb.conditions import  Attr

from insightface.app import FaceAnalysis

import greengrasssdk
iotClient = greengrasssdk.client("iot-data")

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

# Initialize the http server
server_thread = None
httpd = None
thread_lock = threading.Lock()
http_port = 7777

# Initialize the scheduler
scheduler_thread = None
scheduler = sched.scheduler(time.time, time.sleep)

# Initialize the active_members and the last_fetch_time
active_members = []
last_fetch_time = None

# Initialize the face_app, uploader_app
face_app = None
uploader_app = None

# Initialize the detector
thread_detectors = {}

# Initialize the scanner_output_queue
scanner_output_queue = Queue(maxsize=50)


class FaceAnalysisChild(FaceAnalysis):
    # NOTE: allows setting det_size for each detection call.
    # the model allows it but the wrapping code from insightface
    # doesn't show it, and people end up loading duplicate models
    # for different sizes where there is absolutely no need to
    def get(self, img, max_num=0, det_size=(640, 640)):
        if det_size is not None:
            self.det_model.input_size = det_size

        return super().get(img, max_num)

def function_handler(event, context):

    context_vars = vars(context)
    topic = context_vars['client_context'].custom['subject']

    logger.info('function_handler topic: %s', str(topic))

    if topic == f"gocheckin/{os.environ['AWS_IOT_THING_NAME']}/init_scanner":
        logger.info('function_handler init_scanner')

        if 'model' in event:
            logger.info(f"function_handler init_scanner changing model to {str(topic)}")
            init_face_app(event['model'])

def get_local_ip():

    # Connect to an external host, in this case, Google's DNS server
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    return local_ip

def init_uploader_app():
    import s3_uploader as uploader

    global uploader_app
    if uploader_app is None:
        if 'CRED_PROVIDER_HOST' in os.environ:
            uploader_app = uploader.S3Uploader(
                cred_provider_host=os.environ['CRED_PROVIDER_HOST'],
                cred_provider_path=f"/role-aliases/{os.environ['AWS_ROLE_ALIAS']}/credentials",
                bucket_name=os.environ['VIDEO_BUCKET']
            )

def init_face_app(model='buffalo_sc'):
    global face_app

    logger.info(f"Initializing with Model Name: {model}")
    face_app = FaceAnalysisChild(name=model, allowed_modules=['detection', 'recognition'], providers=['CUDAExecutionProvider', 'CPUExecutionProvider'], root=os.environ['INSIGHTFACE_LOCATION'])
    face_app.prepare(ctx_id=0, det_size=(640, 640))#ctx_id=0 CPU

def read_picture_from_url(url):

    # Download the image
    response = requests.get(url)
    response.raise_for_status()  # Ensure the request was successful
    
    # Open the image from the downloaded content    
    image = PIL.Image.open(io.BytesIO(response.content)).convert("RGB")
    
    # Convert the image to a numpy array
    image_array = np.array(image)
    
    # Rearrange the channels from RGB to BGR
    image_bgr = image_array[:, :, [2, 1, 0]]
    
    return image_bgr, image

def set_host_info_to_env(host_info):
    os.environ['HOST_ID'] = host_info['hostId']
    os.environ['IDENTITY_ID'] = host_info['identityId']
    os.environ['PROPERTY_CODE'] = host_info['propertyCode']
    os.environ['CRED_PROVIDER_HOST'] = host_info['credProviderHost']
    

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

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    class NewHandler(http.server.SimpleHTTPRequestHandler):
        def do_POST(self):
            try:

                if self.client_address[0] != '127.0.0.1':
                    self.send_error(403, "Forbidden: Only localhost allowed")
                    return

                if self.path == '/recognise':
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()

                    # Process the POST data
                    if not self.headers['Content-Length']:

                        logger.info('/recognise POST finished with fetch_members only')

                        timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                        timer.start()

                        return

                    content_length = int(self.headers['Content-Length'])

                    logger.info(f"/recognise POST {content_length}")
                    if content_length <= 0:

                        logger.info('/recognise POST finished with fetch_members only')

                        timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                        timer.start()

                        return

                    post_data = self.rfile.read(content_length)
                    event = json.loads(post_data)

                    logger.info('/recognise POST %s', json.dumps(event))

                    image_bgr, org_image = read_picture_from_url(event['faceImgUrl'])

                    # reference_faces = face_app.get(image_bgr)
                    reference_faces = self.analyze_faces(image_bgr)
                    

                    # print('reference_faces[0].embedding:')
                    # print(type(reference_faces[0].embedding))

                    event['faceEmbedding'] = reference_faces[0].embedding.tolist()

                    # print('event[faceEmbedding]:')
                    # print(type(event['faceEmbedding']))

                    bbox = reference_faces[0].bbox.astype(np.int).flatten()
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
                    timer.start()

                elif self.path == '/detect':

                    # Process the POST data
                    content_length = int(self.headers['Content-Length'])
                    post_data = self.rfile.read(content_length)
                    event = json.loads(post_data)

                    if 'hostInfo' in event:
                        set_host_info_to_env(event['hostInfo'])

                    # init_uploader_app
                    init_uploader_app()

                    logger.info(f"/detect POST host: {format(event['cameraItem']['localIp'])}")

                    global thread_detectors

                    if event['cameraItem']['localIp'] not in thread_detectors or thread_detectors[event['cameraItem']['localIp']] is None or not thread_detectors[event['cameraItem']['localIp']].is_alive():

                        logger.info(f"Starting detector thread for : {event['cameraItem']['localIp']}")

                        # logger.info(f'Available threads before starting: {", ".join(thread.name for thread in threading.enumerate())}')

                        import face_recognition as fdm

                        fetch_members()

                        # if active_members:
                        params = {}
                        params['rtsp_src'] = f"rtsp://{event['cameraItem']['username']}:{event['cameraItem']['password']}@{event['cameraItem']['localIp']}:{event['cameraItem']['rtsp']['port']}{event['cameraItem']['rtsp']['path']}"
                        params['codec'] = event['cameraItem']['rtsp']['codec']
                        params['framerate'] = event['cameraItem']['rtsp']['framerate']
                        params['cam_ip'] = event['cameraItem']['localIp']
                        params['cam_uuid'] = event['cameraItem']['uuid']
                        params['cam_name'] = event['cameraItem']['equipmentName']
                        params['active_members'] = active_members
                        params['face_app'] = face_app
                        # params['max_running_time'] = int(os.environ['MAX_RUNNING_TIME'])
                        # params['init_running_time'] = int(os.environ['INIT_RUNNING_TIME'])
                        # params['face_threshold'] = float(os.environ['FACE_THRESHOLD'])

                        thread_detectors[event['cameraItem']['localIp']] = fdm.FaceRecognition(params, scanner_output_queue)
                        thread_detectors[event['cameraItem']['localIp']].start()

                        self.send_response(200)
                        self.send_header('Content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"message": "Started Thread FaceRecognition " + event['cameraItem']['localIp']}).encode())

                        logger.info(f'Available threads after starting: {", ".join(thread.name for thread in threading.enumerate())}')
                        # else:
                        #     self.send_response(400)
                        #     self.end_headers()
                        #     self.wfile.write(json.dumps({"message": f'No active_members: {repr(active_members)} to start Thread FaceRecognition'}).encode())

                        #     logger.info(f'No active_members: {repr(active_members)} to start Thread FaceRecognition')

                
                    elif thread_detectors[event['cameraItem']['localIp']].is_alive():

                        logger.info(f"Extending detector thread for : {event['cameraItem']['localIp']}")

                        # logger.info(f'Available threads before extending: {", ".join(thread.name for thread in threading.enumerate())}')

                        thread_detectors[event['cameraItem']['localIp']].extend_detection_time()
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(json.dumps({"message": "Thread" + thread_detectors[event['cameraItem']['localIp']].name + " is already running"}).encode())

                        # logger.info(f'Available threads after extending: {", ".join(thread.name for thread in threading.enumerate())}')

                    else:                        
                        # logger.info(f"Detector thread for : {event['cameraItem']['localIp']} is not running properly")  

                        # logger.info(f'Available threads: {", ".join(thread.name for thread in threading.enumerate())}')

                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(json.dumps({"message": "Thread" + thread_detectors[event['cameraItem']['localIp']].name + " is not running properly"}).encode())

                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'Not Found')

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

def get_active_reservations():
    logger.info('get_active_reservations in')

    # Initialize the DynamoDB resource
    dynamodb = boto3.resource(
        'dynamodb',
        endpoint_url=os.environ['DDB_ENDPOINT'],
        region_name='us-west-1',
        aws_access_key_id='fakeMyKeyId',
        aws_secret_access_key='fakeSecretAccessKey'
    )

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
        logger.info(item)

    logger.info('get_active_reservations out')

    return items

def update_member(reservationCode, memberNo):
    logger.info('update_member in')

    # Initialize the DynamoDB resource
    dynamodb = boto3.resource(
        'dynamodb',
        endpoint_url=os.environ['DDB_ENDPOINT'],
        region_name='us-west-1',
        aws_access_key_id='fakeMyKeyId',
        aws_secret_access_key='fakeSecretAccessKey'
    )
    # Specify the table name
    tbl_member = os.environ['TBL_MEMBER']

    table = dynamodb.Table(tbl_member)

    member_key = {
        'reservationCode': reservationCode,
        'memberNo': memberNo         # Replace with actual member number
    }

    attribute_name = 'checkedIn'

    response = table.update_item(
        Key=member_key,
        UpdateExpression=f'SET {attribute_name} = :val',
        ExpressionAttributeValues={
            ':val': True
        },
        ReturnValues='UPDATED_NEW'
    )
    logger.info(f"update_member update_item: {repr(response)}")

    logger.info('update_member out')

    return

def get_active_members():
    logger.info('get_active_members in')

    # Initialize the DynamoDB resource
    dynamodb = boto3.resource(
        'dynamodb',
        endpoint_url=os.environ['DDB_ENDPOINT'],
        region_name='us-west-1',
        aws_access_key_id='fakeMyKeyId',
        aws_secret_access_key='fakeSecretAccessKey'
    )
    # Specify the table name
    tbl_member = os.environ['TBL_MEMBER']

    # List of reservation codes to query
    active_reservations = get_active_reservations()

    # Define the list of attributes to retrieve
    attributes_to_get = ['reservationCode', 'memberNo', 'faceEmbedding', 'fullName', 'checkedIn']

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

    # logger.info(f'active_member: {results}')

    filtered_results = []

    for item in results:
        if 'faceEmbedding' in item:
            item['faceEmbedding'] = np.array([float(value) for value in item['faceEmbedding']])
            filtered_results.append(item)
        else:
            logger.info(f"get_active_members, member {item.reservationCode}-{item.memberNo} filtered out with no faceEmbedding")
    
    results = filtered_results

    for item in results:
        # logger.info(f"get_active_members out, reservationCode: {item.reservationCode}, memberNo: {item.memberNo}, fullName: {item.fullName}, checkedIn: {item.checkedIn}")
        logger.info(f"get_active_members out item: {item}")

    return results

def fetch_members(forced=False):
    logger.info('fetch_members in')

    current_date = datetime.now().date()

    global active_members
    global last_fetch_time

    # logger.info('fetch_members last_fetch_time: %s', str(last_fetch_time))
    # logger.info('fetch_members current_date: %s', str(current_date))

    if forced is True:
        logger.info('fetch_members init')
        active_members = get_active_members()
        last_fetch_time = current_date
        logger.info('fetch_members done')
    else:
        if not active_members:
            logger.info('fetch_members init')
            active_members = get_active_members()
            last_fetch_time = current_date
            logger.info('fetch_members done')
        else:
            if last_fetch_time is None or last_fetch_time < current_date:
                logger.info('fetch_members update')
                active_members = get_active_members()
                last_fetch_time = current_date
            else:
                logger.info(f"fetch_members skip as last_fetch_time:{str(last_fetch_time)} >= current_date:{str(current_date)}")
        
def claim_scanner_once():
    data = {
        "equipmentId": os.environ['AWS_IOT_THING_NAME'],
        "equipmentName": os.environ['AWS_IOT_THING_NAME'],
        "localIp": get_local_ip()
    }
    
    iotClient.publish(
        topic="gocheckin/scanner_detected",
        payload=json.dumps(data)
    )


def claim_scanner():
    claim_scanner_once()

    # Reschedule the function
    scheduler.enter(1800, 1, claim_scanner)

# Function to start the scheduler
def start_scheduler():

    claim_scanner_once()
    # Schedule the first call to my_function
    scheduler.enter(1800, 1, claim_scanner)
    # Start the scheduler
    scheduler.run()

def fetch_scanner_output_queue():
    while True:
        try:
            message = scanner_output_queue.get_nowait()
            logger.info(f"Fetched from scanner_output_queue: {repr(message)}")
            
            if 'type' in message:
                if message['type'] == 'guest_detected':
                    if ('payload' in message and 'local_file_path' in message and 'snapshot_payload' in message):
                        local_file_path = message['local_file_path']
                        property_object_key = message['payload']['propertyImgKey']
                        snapshot_payload= message['snapshot_payload']

                        uploader_app.put_object(object_key=property_object_key, local_file_path=local_file_path)

                        iotClient.publish(
                            topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
                            payload=json.dumps(snapshot_payload)
                        )

                    if 'checkedIn' in message:
                        checkedIn = message['checkedIn']

                        if not checkedIn:    
                            update_member(message['payload']['reservationCode'], message['payload']['memberNo'])

                            timer = threading.Timer(0.1, fetch_members, kwargs={'forced': True})
                            timer.start()

                            iotClient.publish(
                                topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/member_detected",
                                payload=json.dumps(message['payload'])
                            )

                elif message['type'] == 'video_clipped':
                    local_file_path = message['payload']['local_file_path']

                    video_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{message['payload']['cam_ip']}/{message['payload']['date_folder']}/{message['payload']['time_filename']}{message['payload']['ext']}"""
                    object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{message['payload']['cam_ip']}/{message['payload']['date_folder']}/{message['payload']['time_filename']}{message['payload']['ext']}"""

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

                    iotClient.publish(
                        topic=f"gocheckin/{os.environ['STAGE']}/{os.environ['AWS_IOT_THING_NAME']}/video_clipped",
                        payload=json.dumps(payload)
                    )

        except Empty:
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

# scheduler
def start_scheduler_thread():
    scheduler_thread = threading.Thread(target=start_scheduler, name="Thread-Scheduler")
    scheduler_thread.start()
    logger.info("Scheduler thread started")

# Function to handle termination signals
def signal_handler(signum, frame):
    logger.info(f"Signal {signum} received, shutting down http server.")
    # logger.info(f'Available threads before shutting down server: {", ".join(thread.name for thread in threading.enumerate())}')

    global thread_detectors
    for thread_name in thread_detectors:
        if thread_detectors[thread_name] is not None:
            thread_detectors[thread_name].stop()
            thread_detectors[thread_name].join()
            thread_detectors[thread_name] = None
    thread_detectors = {}

    global scanner_output_queue
    with scanner_output_queue.mutex:
        scanner_output_queue.queue.clear()
    logger.info("Stopped and scanner_output_queue cleared")


    global server_thread    
    if server_thread is not None:
        stop_http_server()
        server_thread.join()  # Wait for the server thread to finish
        server_thread = None
    logger.info(f'Available threads after http server shutdown: {", ".join(thread.name for thread in threading.enumerate())}')


# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Init face_app
init_face_app()

# Start the HTTP server thread
start_server_thread()

# Start the scheduler thread
start_scheduler_thread()

# Start scanner_output_queue thread
start_scanner_output_queue_thread()




