# import json
import logging
import time
from datetime import datetime, timezone, timedelta
import sys
import os
import threading
import traceback
import numpy as np

import gstreamer_threading as gst
import cv2

import gc

# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

class FaceRecognition(threading.Thread):
    def __init__(self, face_app, active_members, scanner_output_queue, cam_queue):
        super().__init__(name=f"Thread-Detector-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}")

        #Current Cam
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()

        self.face_app = face_app
        self.active_members = active_members

        self.captured_members = {}

        self.cam_detection_his = {}


    def run(self):

        logger.info(f"{self.name} started")

        #get all cams
        time.sleep(1)

        # while True:
        while not self.stop_event.is_set():
            try:

                if not self.cam_queue.empty():
                    cmd, raw_img, cam_info = self.cam_queue.get(False)

                    # Handle session end signal
                    if cmd == gst.StreamCommands.SESSION_END:
                        if cam_info['cam_ip'] in self.cam_detection_his:
                            prev = self.cam_detection_his[cam_info['cam_ip']]
                            if prev['detecting_txn'] == cam_info['detecting_txn']:
                                logger.info(f"{cam_info['cam_ip']} session ended - frames: {prev['fetched']}, identified: {prev['identified']}")
                        continue

                    if cam_info['cam_ip'] not in self.cam_detection_his:
                        self.cam_detection_his[cam_info['cam_ip']] = {}
                        self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                        self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                        self.cam_detection_his[cam_info['cam_ip']]['fetched'] = 1
                    else:
                        self.cam_detection_his[cam_info['cam_ip']]['fetched'] += 1

                        if self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] != cam_info['detecting_txn']:
                            self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                            self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                            self.cam_detection_his[cam_info['cam_ip']]['fetched'] = 1

                    if self.cam_detection_his[cam_info['cam_ip']]['identified']:
                        continue

                    current_time = time.time()
                    age = current_time - float(cam_info['frame_time'])
                    fetched = self.cam_detection_his[cam_info['cam_ip']]['fetched']

                    # logger.debug(f"{cam_info['cam_ip']} detecting_txn: {cam_info['detecting_txn']} fetched: {fetched} age: {age}")

                    if age > float(os.environ['AGE_DETECTING_SEC']):
                        logger.debug(f"{cam_info['cam_ip']} fetched: {fetched} age: {age}")
                        continue
                    else:
                        faces = self.face_app.get(raw_img)
                        duration = time.time() - current_time
                        # Log first frame for near-real-time feedback
                        if fetched == 1:
                            logger.info(f"{cam_info['cam_ip']} detection started - age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)}")

                    for face in faces:
                        for active_member in self.active_members:
                            sim = self.compute_sim(face.embedding, active_member['faceEmbedding'])
                            logger.info(f"{cam_info['cam_ip']} age: {age} fullName: {active_member['fullName']} sim: {str(sim)}")

                            local_file_path = ''

                            if sim >= float(os.environ['FACE_THRESHOLD']):
                            #   with self.captured_members_lock:

                                # Log frame where face is identified
                                logger.info(f"{cam_info['cam_ip']} fetched: {fetched} age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)}")
                                self.cam_detection_his[cam_info['cam_ip']]['identified'] = True
                                memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                                # if memberKey not in self.captured_members:

                                self.captured_members[memberKey] = {
                                    "hostId": os.environ['HOST_ID'],
                                    "propertyCode": os.environ['PROPERTY_CODE'],
                                    "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                                    "coreName": os.environ['AWS_IOT_THING_NAME'],
                                    "assetId": cam_info['cam_uuid'],
                                    "assetName": cam_info['cam_name'],
                                    "cameraIp": cam_info['cam_ip'],
                                    "reservationCode": active_member['reservationCode'],
                                    "listingId": active_member['listingId'],
                                    "memberNo": int(str(active_member['memberNo'])),
                                    "fullName": active_member['fullName'],
                                    "similarity": sim,
                                    "recordTime": datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                }

                                keyNotified = False
                                if 'keyNotified' in active_member:
                                    if active_member['keyNotified']:
                                        keyNotified = active_member['keyNotified']

                                date_folder = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
                                time_filename = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
                                ext = ".jpg"

                                local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder, time_filename + ext)
                                if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder)):
                                    os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder))

                                bbox = face.bbox.astype(int)
                                img = raw_img.astype(np.uint8)
                                cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                                cv2.putText(img, f"{active_member['fullName']}:{str(round(sim, 2))}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36,255,12), 2)
                                cv2.imwrite(local_file_path, img)

                                logger.info(f"Newly checkIn snapshot taken at {local_file_path}")

                                if not self.scanner_output_queue.full():
                                    checkin_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/listings/{active_member['listingId']}/{active_member['reservationCode']}/checkIn/{str(active_member['memberNo'])}{ext}"""
                                    property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""
                                    snapshot_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""

                                    self.captured_members[memberKey]['checkInImgKey'] = checkin_object_key
                                    self.captured_members[memberKey]['propertyImgKey'] = property_object_key
                                    self.captured_members[memberKey]['keyNotified'] = keyNotified

                                    snapshot_payload = {
                                        "hostId": os.environ['HOST_ID'],
                                        "propertyCode": os.environ['PROPERTY_CODE'],
                                        "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                                        "coreName": os.environ['AWS_IOT_THING_NAME'],
                                        "assetId": cam_info['cam_uuid'],
                                        "assetName": cam_info['cam_name'],
                                        "cameraIp": cam_info['cam_ip'],
                                        "recordStart": self.captured_members[memberKey]['recordTime'],
                                        "recordEnd": self.captured_members[memberKey]['recordTime'],
                                        "identityId": os.environ['IDENTITY_ID'],
                                        "s3level": 'private',
                                        "videoKey": '',
                                        "snapshotKey": snapshot_key
                                    }

                                    self.scanner_output_queue.put({
                                        "type": "member_detected",
                                        "keyNotified": keyNotified,
                                        "payload": self.captured_members[memberKey],
                                        "cam_ip": cam_info['cam_ip'],
                                        "local_file_path": local_file_path,
                                        "snapshot_payload": snapshot_payload
                                    }, block=False)

                                # else:
                                #     if self.captured_members[memberKey]['similarity'] < sim:
                                #         self.captured_members[memberKey]['similarity'] = sim
                                    
                else:
                    time.sleep(float(os.environ['DETECTING_SLEEP_SEC']))

            except Exception as e:
                logger.error(f"Caught {self.name} runtime exception!")
                logger.error(e)
                traceback.print_exc()
                self.stop_event.set()

        while not self.cam_queue.empty():
                _, _, _ = self.cam_queue.get(False)
    
    def stop_detection(self):
        logger.info(f"Stop face detector {self.name}")
        self.stop_event.set()
        if self.face_app is not None:
            del self.face_app  # Remove reference

    def compute_sim(self, feat1, feat2):
        # logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()

        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

        # logger.info('compute_sim out sim: %s', str(sim))
        return sim

