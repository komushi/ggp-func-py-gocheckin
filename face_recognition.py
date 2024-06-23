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
        self.camlink = params['rtsp_src']
        self.cam_ip = params['cam_ip']
        self.start_time = time.time()
        self.init_running_time = int(os.environ['INIT_RUNNING_TIME'])
        self.max_running_time = int(os.environ['MAX_RUNNING_TIME'])
        self.face_threshold = float(os.environ['FACE_THRESHOLD'])
        
        self.end_time = self.start_time + self.init_running_time

        if params['codec'] == 'h264':
            self.pipeline_str = f"""rtspsrc name=m_rtspsrc ! queue ! rtph264depay name=m_rtph264depay 
                ! queue ! h264parse ! tee name=t t. ! queue ! avdec_h264 name=m_avdec 
                ! queue ! videoconvert name=m_videoconvert 
                ! queue ! videorate name=m_videorate ! queue ! appsink name=m_appsink 
                t. ! queue ! valve name=m_record_valve ! h264parse 
                ! splitmuxsink name=m_splitmuxsink max-size-time={self.init_running_time * 1000000000}"""    
        elif params['codec'] == 'h265':
            self.pipeline_str = f"""rtspsrc name=m_rtspsrc ! queue ! rtph265depay name=m_rtph265depay 
                ! queue ! h265parse ! tee name=t t. ! queue ! avdec_h265 name=m_avdec 
                ! queue ! videoconvert name=m_videoconvert 
                ! queue ! videorate name=m_videorate ! queue ! appsink name=m_appsink 
                t. ! queue ! valve name=m_record_valve　! h265parse 
                ! splitmuxsink name=m_splitmuxsink max-size-time={self.init_running_time * 1000000000}"""
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

        self.thread_gst = gst.StreamCapture(self.cam_ip,
                            self.camlink,
                            self.pipeline_str,
                            # self.stop_event,
                            self.cam_queue,
                            self.framerate)
        self.thread_gst.start()

        try:
            while not self.stop_event.is_set():

                current_time = time.time()

                if current_time >= self.end_time:
                    logger.info(f"{self.name} reached maximum seconds limit of {self.end_time - self.start_time}")
                    self.stop()
                    break

                if not self.cam_queue.empty():
                    # logger('Got frame')
                    cmd, val = self.cam_queue.get(False)

                    if cmd == gst.StreamCommands.FRAME:
                        if val is not None:

                            crt_time = time.time()

                            if (crt_time - self.inference_begins_at) > 1.0:
                            
                                self.inference_begins_at = crt_time

                                faces = self.face_app.get(val)
                                
                                if len(faces) > 0:
                                    for face in faces:
                                        for active_member in self.active_members:
                                            sim = self.compute_sim(face.embedding, active_member['faceEmbedding'])
                                            logger.info(f"fullName: {active_member['fullName']} sim: {str(sim)} duration: {time.time() - self.inference_begins_at} location: {self.camlink}")

                                            if sim >= self.face_threshold:
                                                memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                                                if memberKey not in self.captured_members:
                                                    # logger.info(f"New member recognized fullName: {active_member['fullName']} similarity: {str(sim)}")
                                                    # logger.info(f"Captured members: {repr(self.captured_members)}")

                                                    self.captured_members[memberKey] = {
                                                        "equipmentId": os.environ['AWS_IOT_THING_NAME'],
                                                        "cameraLink": self.camlink,
                                                        "reservationCode": active_member['reservationCode'],
                                                        "listingId": active_member['listingId'],
                                                        "memberNo": int(str(active_member['memberNo'])),
                                                        "fullName": active_member['fullName'],
                                                        "keyInfo": active_member['memberKeyItem']['keyInfo'],
                                                        "roomCode": active_member['memberKeyItem']['roomCode'],
                                                        "similarity": sim,
                                                        "recordTime": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                                                    }
                                                    if not self.scanner_output_queue.full():
                                                        self.scanner_output_queue.put({
                                                            "type": "guest_detected",
                                                            "payload": self.captured_members[memberKey]
                                                        }, block=False)
                                                else:
                                                    # logger.info(f"Existing member recognized fullName: {active_member['fullName']} similarity: {str(sim)}")
                                                    # logger.info(f"Captured members: {repr(self.captured_members)}")
                                                    if self.captured_members[memberKey]["similarity"] < sim:
                                                        self.captured_members[memberKey]["similarity"] = sim
                                # else:
                                #     logger.info(f"after getting {len(faces)} face(s) with duration of {time.time() - self.inference_begins_at} at {self.camlink}")

                    elif cmd == gst.StreamCommands.VIDEO_CLIPPED:
                        if val is not None:
                            if not self.scanner_output_queue.full():
                                self.scanner_output_queue.put({
                                    "type": "video_clipped",
                                    "payload": val
                                }, block=False)


        except Exception as e:
            logger.info(f"Caught exception during running {self.name}")
            logger.info(e)
            traceback.print_exc()


    def stop(self):
        logger.info(f"{self.name} stop in")
        try:

            self.stop_event.set()
            if self.thread_gst:
                self.thread_gst.stop()
                self.thread_gst.join()

            logger.info(f"{self.thread_gst.name} stopped")
        
            with self.cam_queue.mutex:
                self.cam_queue.queue.clear()

            logger.info(f"cam_queue cleared")
        except Exception as e:
            logger.info(f"Caught Exception during stopping {self.name}")
            logger.info(e)
            traceback.print_exc()
        finally:
            logger.info(f"{self.name} stopped")

    def extend_runtime(self):
        current_time = time.time()
        if current_time < self.end_time:
            additional_time = min(self.init_running_time, self.max_running_time - (self.end_time - self.start_time))
            self.end_time += additional_time
            logger.info(f"{self.name} runtime extended by {additional_time} seconds")
        else:
            logger.info(f"{self.name} cannot be extended beyond {self.max_running_time} seconds")

    def compute_sim(self, feat1, feat2):
        # logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()

        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

        # logger.info('compute_sim out sim: %s', str(sim))
        return sim

