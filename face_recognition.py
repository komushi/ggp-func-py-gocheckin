# import json
import logging
import time
from datetime import datetime, timezone
import sys
import os
import threading
import queue
import traceback
import numpy as np

import gstreamer_threading as gst
import cv2

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

class FaceRecognition(threading.Thread):
    def __init__(self, params, scanner_output_queue):

        super().__init__(name=f"Thread-FaceRecognition-{params['cam_ip']}")

        #Current Cam
        self.thread_gst = None
        self.cam_queue = queue.Queue(maxsize=100)
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()
        self.rtsp_src = params['rtsp_src']
        self.cam_ip = params['cam_ip']
        self.cam_uuid = params['cam_uuid']
        self.cam_name = params['cam_name']
        self.start_time = time.time()
        self.init_running_time = int(os.environ['INIT_RUNNING_TIME'])
        self.max_running_time = int(os.environ['MAX_RUNNING_TIME'])
        self.face_threshold = float(os.environ['FACE_THRESHOLD'])
        
        self.end_time = self.start_time + self.init_running_time

        if params['codec'] == 'h264':
            self.pipeline_str = f"""rtspsrc name=m_rtspsrc ! queue ! rtph264depay name=m_rtph264depay 
                ! queue ! h264parse ! tee name=t t. ! queue ! avdec_h264 name=m_avdec 
                ! queue ! videoconvert name=m_videoconvert 
                ! queue ! videorate name=m_videorate ! queue ! appsink name=m_appsink"""    
        elif params['codec'] == 'h265':
            self.pipeline_str = f"""rtspsrc name=m_rtspsrc ! queue ! rtph265depay name=m_rtph265depay 
                ! queue ! h265parse ! tee name=t t. ! queue ! avdec_h265 name=m_avdec 
                ! queue ! videoconvert name=m_videoconvert 
                ! queue ! videorate name=m_videorate ! queue ! appsink name=m_appsink"""
        elif params['codec'] == 'webcam':
            self.pipeline_str = """avfvideosrc device-index=0 ! videoscale
                ! videoconvert name=m_videoconvert ! video/x-raw,width=1280,height=720
                ! videorate name=m_videorate ! appsink name=m_appsink"""

        if params['framerate'] is not None:
            self.framerate = int(params['framerate'])
        else:
            self.framerate = 15

        self.inference_begins_at = 0
        self.face_app = params['face_app']

        self.active_members = params['active_members']

        self.captured_members = {}


    def run(self):

        logger.info(f"{self.name} started")

        #get all cams
        time.sleep(1)

        self.thread_gst = gst.StreamCapture(
            self.cam_ip,
            self.cam_uuid,
            self.cam_name,
            self.rtsp_src,
            self.pipeline_str,
            # self.stop_event,
            self.cam_queue,
            self.scanner_output_queue,
            self.framerate
        )
        self.thread_gst.start()

        try:
            while not self.stop_event.is_set():

                crt_time = time.time()


                if not self.cam_queue.empty():
                    # logger('Got frame')
                    cmd, val = self.cam_queue.get(False)

                    if crt_time >= self.end_time:
                        if not self.thread_gst.stop_event.is_set():
                            # logger.info(f"{self.name} reached maximum seconds limit of {self.end_time - self.start_time}")
                            self.thread_gst.pause_sampling()
                    else:
                        if cmd == gst.StreamCommands.FRAME:
                            if val is not None:

                                if self.active_members:

                                    if (crt_time - self.inference_begins_at) > 0.5:
                                    
                                        self.inference_begins_at = crt_time

                                        faces = self.face_app.get(val)
                                        
                                        if len(faces) > 0:
                                            for face in faces:
                                                for active_member in self.active_members:
                                                    sim = self.compute_sim(face.embedding, active_member['faceEmbedding'])
                                                    logger.info(f"fullName: {active_member['fullName']} sim: {str(sim)} duration: {time.time() - self.inference_begins_at} location: {self.rtsp_src}")

                                                    local_file_path = ''

                                                    if sim >= self.face_threshold:
                                                        memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                                                        if memberKey not in self.captured_members:

                                                            self.captured_members[memberKey] = {
                                                                "equipmentId": os.environ['AWS_IOT_THING_NAME'],
                                                                "cameraLink": self.rtsp_src,
                                                                "reservationCode": active_member['reservationCode'],
                                                                "listingId": active_member['listingId'],
                                                                "memberNo": int(str(active_member['memberNo'])),
                                                                "fullName": active_member['fullName'],
                                                                "similarity": sim,
                                                                "recordTime": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                                            }

                                                            checkedIn = False
                                                            if 'checkedIn' in active_member:
                                                                if active_member['checkedIn']:
                                                                    checkedIn = active_member['checkedIn']

                                                            # if not checkedIn:
                                                            now = datetime.now(timezone.utc)
                                                            date_folder = now.strftime("%Y-%m-%d")
                                                            time_filename = now.strftime("%H:%M:%S")
                                                            ext = ".jpg"

                                                            local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder, time_filename + ext)
                                                            if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder)):
                                                                os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder))

                                                            bbox = face.bbox.astype(int)
                                                            img = val.astype(np.uint8)
                                                            cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                                                            cv2.putText(img, f"{active_member['fullName']}:{sim}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36,255,12), 2)
                                                            cv2.imwrite(local_file_path, img)

                                                            logger.info(f"Newly checkIn snapshot taken at {local_file_path}")

                                                            if not self.scanner_output_queue.full():
                                                                checkin_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/listings/{active_member['listingId']}/{active_member['reservationCode']}/checkIn/{str(active_member['memberNo'])}{ext}"""
                                                                property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""
                                                                snapshot_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""

                                                                self.captured_members[memberKey]['checkInImgKey'] = checkin_object_key
                                                                self.captured_members[memberKey]['propertyImgKey'] = property_object_key

                                                                snapshot_payload = {
                                                                    "hostId": os.environ['HOST_ID'],
                                                                    "propertyCode": os.environ['PROPERTY_CODE'],
                                                                    "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                                                                    "coreName": os.environ['AWS_IOT_THING_NAME'],
                                                                    "equipmentId": self.cam_uuid,
                                                                    "equipmentName": self.cam_name,
                                                                    "cameraIp": self.cam_ip,
                                                                    "recordStart": self.captured_members[memberKey]['recordTime'],
                                                                    "recordEnd": self.captured_members[memberKey]['recordTime'],
                                                                    "identityId": os.environ['IDENTITY_ID'],
                                                                    "s3level": 'private',
                                                                    "videoKey": '',
                                                                    "snapshotKey": snapshot_key
                                                                }

                                                                self.scanner_output_queue.put({
                                                                    "type": "guest_detected",
                                                                    "checkedIn": checkedIn,
                                                                    "payload": self.captured_members[memberKey],
                                                                    "local_file_path": local_file_path,
                                                                    "snapshot_payload": snapshot_payload
                                                                }, block=False)

                                                        else:
                                                            if self.captured_members[memberKey]["similarity"] < sim:
                                                                self.captured_members[memberKey]["similarity"] = sim
                                        else:
                                            logger.info(f"after getting {len(faces)} face(s) with duration of {time.time() - self.inference_begins_at} at {self.rtsp_src}")
                else:
                    # logger.info(f"{self.thread_gst.name} is not sending FRAME")
                    time.sleep(0.1)
        except Exception as e:
            logger.info(f"Caught exception during running {self.name}")
            logger.info(e)
            traceback.print_exc()
        # finally:
        #     self.stop()


    def stop(self):
        logger.info(f"{self.name} stop in")
        try:
            
            if self.thread_gst:
                self.thread_gst.stop()
                self.thread_gst.join()

            self.stop_event.set()

            logger.info(f"{self.thread_gst.name} stopped")
        
            with self.cam_queue.mutex:
                self.cam_queue.queue.clear()

            logger.info(f"cam_queue cleared")
        except Exception as e:
            logger.info(f"Caught exception during stopping {self.name}")
            logger.info(e)
            traceback.print_exc()
        finally:
            logger.info(f"{self.name} stopped")

    def extend_detection_time(self):
        current_time = time.time()

        if current_time < self.end_time:
            additional_time = min(self.init_running_time, self.max_running_time - (self.end_time - self.start_time))
            self.end_time += additional_time
            logger.info(f"{self.name} detection time extended by {additional_time} seconds to total {self.end_time - self.start_time} seconds")
        else:
            self.thread_gst.restart_sampling()

            self.start_time = current_time
            self.end_time = self.start_time + self.init_running_time
            self.captured_members = {}

            logger.info(f"{self.name} has new start_time: {self.start_time} for {self.init_running_time} seconds")

    def compute_sim(self, feat1, feat2):
        # logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()

        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

        # logger.info('compute_sim out sim: %s', str(sim))
        return sim

