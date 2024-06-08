# import json
import logging
import time
# import datetime
import sys
# import os
import threading
import queue
import traceback
import numpy as np

import gstreamer_threading as gst


# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

class FaceRecognition(threading.Thread):
    def __init__(self, params):

        super().__init__(name="Thread-FaceRecognition")

        #Current Cam
        self.thread_gst = None
        self.cam_queue = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.camlink = params['rtsp_src']

        if params['codec'] == 'h264':
            self.pipeline_str = """rtspsrc name=m_rtspsrc ! rtph264depay name=m_rtph264depay ! avdec_h264 name=m_avdec 
                ! videoconvert name=m_videoconvert ! videorate name=m_videorate ! queue ! appsink name=m_appsink"""
        elif params['codec'] == 'h265':
            self.pipeline_str = """rtspsrc name=m_rtspsrc ! rtph265depay name=m_rtph265depay ! avdec_h265 name=m_avdec 
                ! videoconvert name=m_videoconvert ! videorate name=m_videorate ! appsink name=m_appsink"""
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


    def run(self):

        logger.info(f"{self.name} started")

        #get all cams
        time.sleep(1)

        self.thread_gst = gst.StreamCapture(self.camlink,
                            self.pipeline_str,
                            self.stop_event,
                            self.cam_queue,
                            self.framerate)
        self.thread_gst.start()

        try:
            while not self.stop_event.is_set():

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
                                    logger.info('after getting %s face(s) at %s with duration of %s' % (len(faces), self.inference_begins_at, time.time() - self.inference_begins_at))
                                    for face in faces:
                                        for active_member in self.active_members:
                                            sim = self.compute_sim(face.embedding, active_member['faceEmbedding'])
                                            logger.info('face sim: %s fullName: %s', str(sim), active_member['fullName'])


        except Exception as e:
            logger.info(f"Caught Exception during running {self.name}")
            logger.info(e)
            traceback.print_exc()


    def stop(self):
        logger.info(f"{self.name} stop in")
        try:

            self.stop_event.set()
            if self.thread_gst:
                self.thread_gst.stop()
                self.thread_gst.join()
        
            with self.cam_queue.mutex:
                self.cam_queue.queue.clear()
            logger.ingo(f"{self.name} stopped and cam_queue cleared")

        except Exception as e:
            logger.info(f"Caught Exception during stopping {self.name}")
            logger.info(e)
            traceback.print_exc()

    def compute_sim(self, feat1, feat2):
        logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()

        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

        logger.info('compute_sim out sim: %s', str(sim))
        return sim

