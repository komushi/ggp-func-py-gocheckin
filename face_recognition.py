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
# import PIL.Image

from insightface.app import FaceAnalysis

import gstreamer_threading as gst
# from libs import gstreamer_threading as gst

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)


def compute_sim(feat1, feat2):
    # logger.info('compute_sim in feat1:' + repr(feat1))
    # logger.info('compute_sim in feat2:' + repr(feat2))
    logger.info('compute_sim in feat1 type: %s, feat2 type: %s', type(feat1), type(feat2))
    feat1 = feat1.ravel()
    feat2 = feat2.ravel()

    sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))

    logger.info('compute_sim out sim: %s', str(sim))
    return sim

class FaceRecognition():
    def __init__(self, params):
        #Current Cam
        self.running = True
        self.cam_process = None
        self.cam_queue = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.camlink = params['rtsp_src']

        if params['codec'] == 'h264':
            self.pipeline_str = """rtspsrc name=m_rtspsrc ! rtph264depay name=m_rtph264depay ! avdec_h264 name=m_avdec 
                ! videoconvert name=m_videoconvert ! videorate name=m_videorate ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! appsink name=m_appsink"""
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


    def start_detector(self):

        #get all cams
        time.sleep(1)

        self.cam_process = gst.StreamCapture(self.camlink,
                            self.pipeline_str,
                            self.stop_event,
                            self.cam_queue,
                            self.framerate)
        self.cam_process.start()

        try:
            # while self.running:
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

                                        sim = compute_sim(face.embedding, self.active_members[0]['faceEmbedding'])

                                        # sim = str(compute_sim(face.embedding, np.array(self.active_members[0]['faceEmbedding'])))
                                        # print('compute_sim face similarity: %s', sim)
                                        logger.info('face sim: %s fullName: %s', str(sim), self.active_members[0]['fullName'])


        except KeyboardInterrupt:
            logger.info('Caught Keyboard interrupt')

        except:
            e = sys.exc_info()
            logger.info('Caught Main Exception')
            logger.info(e)

        self.stop_detector()
        logger.info('start_detector after destroyAllWindows')


    def stop_detector(self):
        logger.info('in stop_detector')
        try:
            if self.stop_event is not None:
                self.stop_event.set()
                while not self.cam_queue.empty():
                    try:
                        _ = self.cam_queue.get()
                        logger.info('in stop_detector cleanup')
                    except:
                        break
                    self.cam_queue.join()

                for thread in threading.enumerate(): 
                    print('afterA:' + thread.name)

                self.cam_process.join()

                for thread in threading.enumerate(): 
                    print('beforeA:' + thread.name)

                # self.camProcess.close()
                logger.info('After close')
        except queue.Empty as ex:
            logger.info('Caught stop_detctor Queue.Empty Exception')
            logger.info(ex)
            traceback.print_exc()
        except Exception as e:
            logger.info('Caught stop_detector Othert Exception')
            logger.info(e)
            traceback.print_exc()

def function_handler(event, context):
    logger.info('function_handler event: ' + repr(event))

    detector = FaceRecognition({"rtsp_src": event['rtsp_src'], "codec": event['codec'], "framerate": event['framerate'], "face_file": event['face_file']})

    if sys.platform == 'darwin' or sys.platform == 'win32':
        detector.start_detector()
    elif sys.platform == 'linux':
        t = threading.Thread(target=detector.start_detector)
        t.start()

if __name__ == "__main__":
    if len(sys.argv) == 7:
        function_handler(event={"method": sys.argv[1], "action": sys.argv[2], "rtsp_src": sys.argv[3], "codec": sys.argv[4], "framerate": sys.argv[5], "face_file": sys.argv[6]}, context=None)
    elif len(sys.argv) == 3:
        function_handler(event={"method": sys.argv[1], "action": sys.argv[2]}, context=None)
