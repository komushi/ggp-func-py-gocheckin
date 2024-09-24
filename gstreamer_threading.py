import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')

from gi.repository import Gst
from gi.repository import GstPbutils

import uuid

import os
import sys
import gc
import logging
import threading
from enum import Enum
import numpy as np
import time
from datetime import datetime, timedelta, timezone
from collections import deque

import traceback

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

Gst.init(None)
# Gst.debug_set_default_threshold(Gst.DebugLevel.ERROR)

class StreamMode(Enum):
    INIT_STREAM = 1
    SETUP_STREAM = 1
    READ_STREAM = 2


class StreamCommands(Enum):
    FRAME = 1
    ERROR = 2
    HEARTBEAT = 3
    RESOLUTION = 4
    MOTION_BEGIN = 5
    MOTION_END = 6
    VIDEO_CLIPPED = 7
    STOP = 8


# h264 or h265
pipeline_str_h264 = f"""rtspsrc name=m_rtspsrc 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! rtph264depay name=m_rtphdepay
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! h264parse
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! avdec_h264 name=m_avdec 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! videoconvert name=m_videoconvert 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! videorate name=m_videorate 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! appsink name=m_appsink"""

pipeline_str_h265 = f"""rtspsrc name=m_rtspsrc 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! rtph265depay name=m_rtphdepay 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! h265parse
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! avdec_h265 name=m_avdec
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! videoconvert name=m_videoconvert 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! videorate name=m_videorate 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=0 ! appsink name=m_appsink"""


ext = ".mp4"
max_seconds = 2

class StreamCapture(threading.Thread):

    def __init__(self, params, scanner_output_queue, cam_queue):
        super().__init__(name=f"Thread-Gst-{params['cam_ip']}")

        # params
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.rtsp_src = params['rtsp_src']
        self.framerate = params['framerate']
        self.cam_ip = params['cam_ip']
        self.cam_uuid = params['cam_uuid']
        self.cam_name = params['cam_name']
        self.codec = params['codec']
        if params['codec'] == 'h264':
            self.pipeline_str = pipeline_str_h264
        elif params['codec'] == 'h265':
            self.pipeline_str = pipeline_str_h265

        # Create the empty pipeline
        self.pipeline = Gst.parse_launch(self.pipeline_str)

        # source params
        self.source = self.pipeline.get_by_name('m_rtspsrc')
        if  self.source is not None:
            # self.source.set_property('latency', 2000)
            self.source.set_property('location', self.rtsp_src)
            # self.source.set_property('protocols', 'tcp')
            # self.source.set_property('retry', 1)
            # self.source.set_property('timeout', 5000000)
            # self.source.set_property('tcp-timeout', 20000000)
            self.source.set_property('buffer-mode', 3)
            # self.source.set_property("onvif-mode", True)
            # self.source.set_property("onvif-rate-control", False)
            self.source.set_property('is-live', True)    


        # rtphdepay
        self.rtphdepay = self.pipeline.get_by_name('m_rtphdepay')

        # decode params
        self.decode = self.pipeline.get_by_name('m_avdec')
        if  self.decode is not None:
            self.decode.set_property('max-threads', 2)
            self.decode.set_property('output-corrupt', 'false')
        
        # convert params
        self.convert = self.pipeline.get_by_name('m_videoconvert')

        #framerate parameters
        self.framerate_ctr = self.pipeline.get_by_name('m_videorate')
        if  self.framerate_ctr is not None:
            self.framerate_ctr.set_property('max-rate', self.framerate/1)
            self.framerate_ctr.set_property('drop-only', 'true')

        # sink params
        self.appsink = self.pipeline.get_by_name('m_appsink')
        if  self.appsink is not None:
            self.appsink.set_property('max-buffers', 5)
            self.appsink.set_property('drop', True)
            self.appsink.set_property('emit-signals', True)
            self.appsink.set_property('sync', False)
            # caps = Gst.caps_from_string(
            #     'video/x-raw, format=(string){BGR, GRAY8}; video/x-bayer,format=(string){rggb,bggr,grbg,gbrg}')
            caps = Gst.caps_from_string('video/x-raw, format=(string){BGR, GRAY8}')
            self.appsink.set_property('caps', caps)
            self.appsink.connect("new-sample", self.on_new_sample, {})

        self.stop_event = threading.Event()
        self.buffer = deque()
        self.lock = threading.Lock()

        self.last_sampling_time = None
        self.is_playing = False
        self.is_feeding = False
        self.is_recording = False

        self.recordings = {}

    def gst_to_opencv(self, sample):
        buf = sample.get_buffer()
        caps = sample.get_caps()

        arr = np.ndarray(
            (caps.get_structure(0).get_value('height'),
             caps.get_structure(0).get_value('width'),
             3),
            buffer=buf.extract_dup(0, buf.get_size()),
            dtype=np.uint8)
        return arr

    # def gst_to_opencv(self, buf, caps):
    #     arr = np.ndarray(
    #         (caps.get_structure(0).get_value('height'),
    #          caps.get_structure(0).get_value('width'),
    #          3),
    #         buffer=buf.extract_dup(0, buf.get_size()),
    #         dtype=np.uint8)
    #     return arr

    def add_frame(self, sample):
        with self.lock:
            current_time = time.time()
            self.buffer.append((current_time, sample))

            # Only discard frames if not recording
            if not self.is_recording:
                while self.buffer and current_time - self.buffer[0][0] > max_seconds:
                    self.buffer.popleft()

    def get_all_frames(self):
        with self.lock:
            return list(self.buffer)

    def clear_all_frames(self):
        with self.lock:
            self.buffer.clear()
            
    def on_new_sample(self, sink, _):
        crt_time = time.time()

        sample = sink.emit('pull-sample')

        if sample:
            self.add_frame(sample)

            if self.is_feeding:
                if self.last_sampling_time is None or crt_time - self.last_sampling_time > 0.75:
                    self.last_sampling_time = crt_time
                    arr = self.gst_to_opencv(sample)

                    if not self.cam_queue.full():
                        self.cam_queue.put((StreamCommands.FRAME, arr, {"cam_ip": self.cam_ip, "cam_uuid": self.cam_uuid, "cam_name": self.cam_name}), block=False)

        sample = None
        return Gst.FlowReturn.OK

    def save_frames_as_video(self, utc_time_object):
        logger.info(f"{self.cam_ip} save_frames_as_video in")

        date_folder = utc_time_object.strftime("%Y-%m-%d")
        time_filename = utc_time_object.strftime("%H:%M:%S")
        # local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder, time_filename + ext)

        if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder)):
            os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder))

        # Get frames from buffer
        frames = self.get_all_frames()

        # Start the save task in a new thread
        save_thread = threading.Thread(target=self.save_task, args=(frames, utc_time_object), name=f"save_task_{time_filename}")
        save_thread.start()

        # Clear the frame buffer
        self.clear_all_frames()
        frames = None

        logger.info(f'Available threads after save_task: {", ".join(thread.name for thread in threading.enumerate())}')

        logger.info(f"{self.cam_ip} save_frames_as_video out")

    def save_task(self, frames, utc_time_object):
        logger.info(f"{self.cam_ip} save_task in date_folder with {len(frames)} frames.")

        try:

            end_datetime = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z'

            date_folder = utc_time_object.strftime("%Y-%m-%d")
            time_filename = utc_time_object.strftime("%H:%M:%S")

            local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder, time_filename + ext)

            save_pipeline = Gst.parse_launch(f'''
                appsrc name=m_appsrc emit-signals=true is-live=true format=time
                ! videoconvert ! video/x-raw, format=I420
                ! x265enc bitrate=100 ! video/x-h265 ! h265parse ! splitmuxsink name=m_sink location={local_file_path} max-size-time=10000000000
            ''')

            # save_pipeline = Gst.parse_launch(f'''
            #     appsrc name=m_appsrc emit-signals=true is-live=true format=time
            #     ! videoconvert ! video/x-raw, format=I420
            #     ! x265enc bitrate=100 ! video/x-h265 ! h265parse ! mp4mux ! filesink name=m_sink location={local_file_path}
            # ''')

            save_pipeline.set_state(Gst.State.PLAYING)

            # Push frames to appsrc
            with self.lock:
                for _, sample in frames:
                    ret = appsrc.emit('push-sample', sample)
                    if ret != Gst.FlowReturn.OK:
                        logger.error(f"Error pushing buffer to appsrc: {ret}")

            frames = None

            # Emit EOS to signal end of the stream
            appsrc.emit('end-of-stream')

            # bus = save_pipeline.get_bus()
            # bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS)

            # Wait for EOS message or error on the bus
            bus = save_pipeline.get_bus()
            while True:
                # msg = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ANY)
                msg = bus.timed_pop_filtered(500 * Gst.MSECOND, Gst.MessageType.ANY)
                if msg:
                    if msg.type == Gst.MessageType.EOS:
                        logger.info(f"EOS received")
                        break
                    elif msg.type == Gst.MessageType.ERROR:
                        err, debug = msg.parse_error()
                        logger.error(f"Error received: {err}, {debug}")
                        break
                    elif msg.type == Gst.MessageType.ELEMENT:
                        structure = msg.get_structure()
                        if structure and structure.get_name().startswith("splitmuxsink-"):
                            action = structure.get_name()
                            # logger.info(f"New action detected: {action}")
                            if action == "splitmuxsink-fragment-opened":
                                location = structure.get_string("location")
                                logger.info(f"New file being created: {location}")
                            elif action == "splitmuxsink-fragment-closed":
                                location = structure.get_string("location")
                                logger.info(f"New file created: {location}")

                                if not self.scanner_output_queue.full():
                                    
                                    video_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""

                                    object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""

                                    logger.info(f"splitmuxsink-fragment-closed, New video file created at local_file_path {location} and will be uploaded as remote file /{self.cam_ip}/{date_folder}/{time_filename}{ext}")

                                    self.scanner_output_queue.put({
                                        "type": "video_clipped",
                                        "payload": {
                                            "video_clipping_location": os.environ['VIDEO_CLIPPING_LOCATION'],
                                            "cam_ip": self.cam_ip,
                                            "cam_uuid": self.cam_uuid,
                                            "cam_name": self.cam_name,
                                            "video_key": video_key,
                                            "object_key": object_key,
                                            "ext": ext,
                                            "local_file_path": location,
                                            "start_datetime": utc_time_object.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                                            "end_datetime": end_datetime
                                        }
                                    }, block=False)

                                break
                                
            # Set pipeline to NULL state once processing is complete
            save_pipeline.set_state(Gst.State.NULL)
            appsrc.set_state(Gst.State.NULL)

        except Exception as e:
            logger.error(f"{self.cam_ip} save_task, Exception during running, Error: {e}")
            traceback.print_exc()
        finally:
            appsrc = None
            save_pipeline = None

            gc.collect()

        logger.info(f"{self.cam_ip} save_task out")


    def run(self):
        try:

            # Start playing
            if self.start_playing():
                logger.info(f"{self.cam_ip} StreamCapture run, start_playing result: {True}")

                # Wait until error or EOS
                bus = self.pipeline.get_bus()
                # bus.add_signal_watch()
                # bus.connect("message", self.on_message)

                while not self.stop_event.is_set():
                    message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                    if message:
                        self.on_message(bus, message)
            else:
                logger.error(f"{self.cam_ip} StreamCapture run, Not started as start_playing result: {False}")
                self.pipeline.set_state(Gst.State.NULL)
                self.stop_event.set()

        except Exception as e:
            logger.error(f"{self.cam_ip} StreamCapture run, Exception during running, Error: {e}")
            traceback.print_exc()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            self.stop_event.set()
            self.is_playing = False
            logger.info(f"{self.cam_ip} StreamCapture run, Pipeline stopped and cleaned up")

    def start_playing(self, count = 0, playing = False):
        logger.info(f"{self.cam_ip} start_playing, count: {count} playing: {playing}")
        interval = 10

        if count > 3:
            logger.warning(f"{self.cam_ip} start_playing, count ended with result playing: {playing}, count: {count}")
            return playing
        else:
            if playing:
                return playing

        count += 1
        
        if not self.is_playing:

            playing_state_change_return = self.pipeline.set_state(Gst.State.PLAYING)
            logger.info(f"{self.cam_ip} start_playing, set_state PLAYING state_change_return: {playing_state_change_return}")

            if playing_state_change_return != Gst.StateChangeReturn.SUCCESS:
                logger.warning(f"{self.cam_ip} start_playing, playing_state_change_return is NOT SUCCESS, sleeping for {interval} second...")
                time.sleep(interval)
                return self.start_playing(count)
            else:
                logger.info(f"{self.cam_ip} start_playing, playing_state_change_return is SUCCESS, count: {count}")
                return True

        else:
            logger.warning(f"{self.cam_ip} start_playing, return with already playing, count: {count}")
            return True

        
    def stop(self):
        # self.stop_recording()

        self.stop_event.set()

        self.pipeline.set_state(Gst.State.NULL)

    def feed_detecting(self):
        logger.info(f"{self.cam_ip} feed_detecting in")

        self.is_feeding = True

        logger.info(f"{self.cam_ip} feed_detecting out")

    def stop_feeding(self):
        logger.info(f"{self.cam_ip} stop_feeding in")

        self.is_feeding = False    

        logger.info(f"{self.cam_ip} stop_feeding out")

    def start_recording(self, utc_time):
        logger.info(f"{self.cam_ip} start_recording in")

        if self.is_recording:
            logger.warning(f"{self.cam_ip} start_recording out, Recording already started")
            return False

        with self.lock:
            self.is_recording = True
            self.recordings[utc_time] = datetime.strptime(utc_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        logger.info(f"{self.cam_ip} start_recording out")

        return True


    def stop_recording(self, utc_time):
        logger.info(f"{self.cam_ip} stop_recording in")

        if not self.is_recording:
            logger.warning(f"{self.cam_ip} start_recording out, already stopped")
            return False
        
        with self.lock:
            self.is_recording = False

        self.save_frames_as_video(self.recordings[utc_time])
        del self.recordings[utc_time]

        logger.info(f"{self.cam_ip} stop_recording out")

        return True


    def on_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.warning("End-Of-Stream reached.")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            raise ValueError(f"{self.name} Gst.MessageType.ERROR: {err}, {debug}")
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()

                logger.info(f"{self.cam_ip} Pipeline state changed from {old_state.value_nick} to {new_state.value_nick} with pending_state {pending_state.value_nick}")

                if new_state == Gst.State.PLAYING:
                    self.is_playing = True
                    return
                
                if new_state == old_state:
                    if new_state == Gst.State.PAUSED:
                        return
                    
                self.is_playing = False
                
        elif message.type == Gst.MessageType.WARNING:
            gerror, debug = message.parse_warning()
            warning_message = gerror.message
            
            logger.warning(f"Warning message {message.parse_warning()}： {message.type} at {self.cam_ip}.")
            
            if "Could not read from resource." in warning_message:
                raise ValueError(f"{self.name} Gst.MessageType.ERROR: {gerror}, {debug}")
            
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            logger.debug(f"New ELEMENT detected: {structure.get_name()}")
       
