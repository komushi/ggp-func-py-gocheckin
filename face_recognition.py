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

import cv2

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

class FaceRecognition(threading.Thread):
    def __init__(self, params, scanner_output_queue, cam_queue):

        super().__init__(name=f"Thread-FaceRecognition")

        #Current Cam
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()

        self.inference_begins_at = 0
        self.face_app = params['face_app']

        if params['active_members'] is not None:
            self.active_members = params['active_members']
        else:
            self.active_members = None

        self.captured_members = {}


    def run(self):

        logger.info(f"{self.name} started")

        #get all cams
        time.sleep(1)

        # self.stop_event.set()

        try:
            while True:

                if self.stop_event.is_set():
                    if self.cam_queue.empty():
                        time.sleep(0.1)
                    else:
                        cmd, _, _ = self.cam_queue.get(False)
                else:

                    if not self.cam_queue.empty():
                        cmd, raw_img, cam_info = self.cam_queue.get(False)
                    
                        crt_time = time.time()


                        if cmd == gst.StreamCommands.FRAME:
                            if raw_img is not None and self.active_members:
                                if (crt_time - self.inference_begins_at) > 0.5:
                                    
                                    self.inference_begins_at = crt_time
                                    faces = self.face_app.get(raw_img)

                                    if len(faces) == 0:
                                        logger.info(f"after getting {len(faces)} face(s) with duration of {time.time() - self.inference_begins_at} at {cam_info.cam_ip}")
                                    

                                    for face in faces:
                                        for active_member in self.active_members:
                                            sim = self.compute_sim(face.embedding, active_member['faceEmbedding'])
                                            logger.info(f"fullName: {active_member['fullName']} sim: {str(sim)} duration: {time.time() - self.inference_begins_at} location: {cam_info.cam_ip}")

                                            local_file_path = ''

                                            if sim >= self.face_threshold:
                                                memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                                                if memberKey not in self.captured_members:

                                                    self.captured_members[memberKey] = {
                                                        "equipmentId": os.environ['AWS_IOT_THING_NAME'],
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

                                                    local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info.cam_ip, date_folder, time_filename + ext)
                                                    if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info.cam_ip, date_folder)):
                                                        os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info.cam_ip, date_folder))

                                                    bbox = face.bbox.astype(int)
                                                    img = raw_img.astype(np.uint8)
                                                    cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                                                    cv2.putText(img, f"{active_member['fullName']}:{str(round(sim, 2))}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36,255,12), 2)
                                                    cv2.imwrite(local_file_path, img)

                                                    logger.info(f"Newly checkIn snapshot taken at {local_file_path}")

                                                    if not self.scanner_output_queue.full():
                                                        checkin_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/listings/{active_member['listingId']}/{active_member['reservationCode']}/checkIn/{str(active_member['memberNo'])}{ext}"""
                                                        property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info.cam_ip}/{date_folder}/{time_filename}{ext}"""
                                                        snapshot_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info.cam_ip}/{date_folder}/{time_filename}{ext}"""

                                                        self.captured_members[memberKey]['checkInImgKey'] = checkin_object_key
                                                        self.captured_members[memberKey]['propertyImgKey'] = property_object_key

                                                        snapshot_payload = {
                                                            "hostId": os.environ['HOST_ID'],
                                                            "propertyCode": os.environ['PROPERTY_CODE'],
                                                            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                                                            "coreName": os.environ['AWS_IOT_THING_NAME'],
                                                            "equipmentId": cam_info.cam_uuid,
                                                            "equipmentName": cam_info.cam_name,
                                                            "cameraIp": cam_info.cam_ip,
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
                        time.sleep(0.1)

                
        except Exception as e:
            logger.info(f"Caught exception during running {self.name}")
            logger.info(e)
            traceback.print_exc()
    
    def pause_detection(self):
        self.stop_event.set()

    def start_detection(self):
        self.stop_event.clear()

    # def extend_detection_time(self):
    #     current_time = time.time()

    #     if current_time < self.end_time:
    #         additional_time = min(self.init_running_time, self.max_running_time - (self.end_time - self.start_time))
    #         self.end_time += additional_time
    #         logger.info(f"{self.name} detection time extended by {additional_time} seconds to total {self.end_time - self.start_time} seconds")
    #     else:
    #         self.thread_gst.start_sampling()
    #         # self.thread_gst.start_recording()

    #         self.start_time = current_time
    #         self.end_time = self.start_time + self.init_running_time
    #         self.captured_members = {}

    #         logger.info(f"{self.name} has new start_time: {self.start_time} for {self.init_running_time} seconds")

    def compute_sim(self, feat1, feat2):
        # logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()

        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

        # logger.info('compute_sim out sim: %s', str(sim))
        return sim

