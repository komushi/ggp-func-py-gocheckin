import re
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


Gst.debug_set_default_threshold(os.environ['GST_DEBUG_LEVEL'])

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

ext = ".mp4"

class StreamCapture(threading.Thread):

    def __init__(self, params, scanner_output_queue, cam_queue):
        super().__init__(name=f"Thread-Gst-{params['cam_ip']}")

        # params
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.framerate = params['framerate']
        self.cam_ip = params['cam_ip']
        self.cam_uuid = params['cam_uuid']
        self.cam_name = params['cam_name']
        self.codec = params['codec']

        pipeline_str = ''
        if params['codec'] == 'h264':
            pipeline_str = f"""rtspsrc name=m_rtspsrc location={params['rtsp_src']} protocols=tcp
                                    ! queue ! rtph264depay name=m_rtph264depay 
                                    ! queue ! h264parse ! appsink name=m_appsink"""
        elif params['codec'] == 'h265':
            pipeline_str = f"""rtspsrc name=m_rtspsrc location={params['rtsp_src']} protocols=tcp
                                    ! queue ! rtph265depay name=m_rtph265depay 
                                    ! queue ! h265parse ! appsink name=m_appsink"""

        # Create the empty pipeline
        self.pipeline = Gst.parse_launch(pipeline_str)

        # sink params
        appsink = self.pipeline.get_by_name('m_appsink')
        if  appsink is not None:
            
            logger.debug(f"{self.cam_ip} appsink not None")

            appsink.set_property('max-buffers', 100)
            appsink.set_property('drop', True)
            appsink.set_property('emit-signals', True)
            appsink.set_property('sync', False)
            appsink.connect("new-sample", self.on_new_sample, {})

        pipeline_str_decode = ''
        if self.codec == 'h264':
            pipeline_str_decode = f"""appsrc name=m_appsrc emit-signals=true is-live=true format=time
                ! queue ! h264parse ! queue ! avdec_h264 name=m_avdec
                ! queue ! videoconvert ! videorate drop-only=true ! video/x-raw,format=BGR,framerate={round(int(self.framerate) * float(os.environ['DETECTING_RATE_PERCENT']))}/1
                ! queue ! appsink name=m_appsink"""
        elif self.codec == 'h265':
            pipeline_str_decode = f"""appsrc name=m_appsrc emit-signals=true is-live=true format=time
                ! queue ! h265parse ! queue ! avdec_h265 name=m_avdec max-threads=2 output-corrupt=false
                ! queue ! videoconvert ! videorate drop-only=true ! video/x-raw,format=BGR,framerate={round(int(self.framerate) * float(os.environ['DETECTING_RATE_PERCENT']))}/1
                ! queue ! appsink name=m_appsink"""

        # Create the empty pipeline
        self.pipeline_decode = Gst.parse_launch(pipeline_str_decode)

        # source params
        self.decode_appsrc = self.pipeline_decode.get_by_name('m_appsrc')
        # if self.decode_appsrc is not None:
        #     self.decode_appsrc.connect('need-data', self.on_need_data, {})
        #     self.decode_appsrc.connect('push-sample', self.on_push_sample, {})

        # sink params
        appsink_decode = self.pipeline_decode.get_by_name('m_appsink')
        if  appsink_decode is not None:
            appsink_decode.set_property('max-buffers', 100)
            appsink_decode.set_property('drop', True)
            appsink_decode.set_property('emit-signals', True)
            appsink_decode.set_property('sync', False)
            appsink_decode.connect("new-sample", self.on_new_sample_decode, {})
        
        self.stop_event = threading.Event()
        self.buffer = deque()
        self.lock = threading.Lock()

        self.last_sampling_time = None
        self.is_playing = False
        self.is_feeding = False
        self.feeding_count = 0
        self.decoding_count = 0
        self.is_recording = False
        self.running_seconds = 10

        self.recordings = {}

        self.feeding_timer = None
        self.previous_pts = None

    # def on_need_data(self, appsrc, length, args):
    #     # This function gets triggered when appsrc needs data.
    #     # No direct sample pushing happens here, but you can inspect caps here too if needed
    #     logger.debug("appsrc needs data!")

    # def on_push_sample(self, appsrc, sample, args):
    #     caps = sample.get_caps()

    #     # Check the caps info here (this can be used to inspect the sample being pushed)
    #     logger.info(f"on_push_sample Pushing sample with caps: {caps.to_string()}")

    #     return Gst.FlowReturn.OK

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

    def add_frame(self, sample):
        with self.lock:
            current_time = time.time()
            self.buffer.append((current_time, sample))

            # Only discard frames if not recording
            if not self.is_recording:
                while self.buffer and current_time - self.buffer[0][0] > float(os.environ['PRE_RECORDING_SEC']):
                    self.buffer.popleft()

    def get_all_frames(self):
        with self.lock:
            return list(self.buffer)

    def clear_all_frames(self):
        with self.lock:
            self.buffer.clear()

    def on_new_sample(self, sink, _):
        sample = sink.emit('pull-sample')

        if not sample:
            return Gst.FlowReturn.ERROR

        self.add_frame(sample)

        if self.is_feeding:
            
            self.feeding_count += 1

            logger.debug(f"{self.cam_ip} on_new_sample feeding_count: {self.feeding_count}")

            if self.feeding_count > self.framerate * self.running_seconds:
                return Gst.FlowReturn.OK

            caps = sample.get_caps()
            structure = caps.get_structure(0)
            framerate_value = structure.get_fraction("framerate")
            
            if framerate_value[1] == 0:
                caps_string = caps.to_string()

                new_caps_string = re.sub(
                    r'framerate=\(fraction\)\d+/\d+',
                    f'framerate=(fraction){self.framerate}/1',
                    caps_string
                )

                new_caps = Gst.Caps.from_string(new_caps_string)
                buffer = sample.get_buffer()

                new_sample = Gst.Sample.new(buffer, new_caps, sample.get_segment(), sample.get_info())
            else:
                new_sample = sample

            
            logger.info(f"{self.cam_ip} on_new_sample new_caps: {new_sample.get_caps().to_string()}")

            # if framerate_value[1] == 0:
            #     new_structure = structure.copy()
            #     new_structure.set_value("framerate", int(self.framerate))

            #     new_caps = Gst.Caps.new_empty()
            #     new_caps.append_structure(new_structure)

            #     new_sample = Gst.Sample.new(buffer, new_caps, sample.get_segment(), sample.get_info())

            #     logger.debug(f"{self.cam_ip} on_new_sample caps: {caps.to_string()}")
            #     logger.info(f"{self.cam_ip} on_new_sample new_caps: {new_caps.to_string()}")
            # else:
            #     new_sample = sample

            ret = self.decode_appsrc.emit('push-sample', new_sample)
            if ret != Gst.FlowReturn.OK:
                logger.error(f"{self.cam_ip} on_new_sample, Error pushing sample to decode_appsrc: {ret}")

        sample = None
        return Gst.FlowReturn.OK

    def on_new_sample_decode(self, sink, _):
        if float(os.environ['DETECTING_RATE_PERCENT']) * self.feeding_count < self.decoding_count:
            logger.debug(f"on_new_sample_decode decoding_count:  {self.decoding_count}")
            # sample = sink.emit('pull-sample')
            return Gst.FlowReturn.OK

        sample = sink.emit('pull-sample')

        if sample:
            caps = sample.get_caps()
            logger.debug(f"{self.cam_ip} on_new_sample_decode caps: {caps.to_string()}")

            buffer = sample.get_buffer()
            if not buffer:
                logger.error("on_new_sample_decode: Received sample with no buffer")
                return Gst.FlowReturn.OK
            
            buffer_size = buffer.get_size()
            if buffer_size == 0:
                logger.error("on_new_sample_decode: Buffer is empty (size 0)")
                return Gst.FlowReturn.OK

            arr = self.gst_to_opencv(sample)

            if not self.cam_queue.full():
                self.decoding_count += 1
                self.cam_queue.put((StreamCommands.FRAME, arr, {"cam_ip": self.cam_ip, "cam_uuid": self.cam_uuid, "cam_name": self.cam_name}), block=False)

                logger.debug(f"{self.cam_ip} on_new_sample_decode decoding_count: {self.decoding_count}")

        sample = None
        return Gst.FlowReturn.OK

    def save_frames_as_video(self, utc_time_object):
        logger.debug(f"{self.cam_ip} save_frames_as_video in")

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

        logger.debug(f'Available threads after save_task: {", ".join(thread.name for thread in threading.enumerate())}')

        logger.debug(f"{self.cam_ip} save_frames_as_video out")

    def save_task(self, frames, utc_time_object):
        logger.debug(f"{self.cam_ip} save_task in date_folder with {len(frames)} frames.")

        try:

            end_datetime = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z'

            date_folder = utc_time_object.strftime("%Y-%m-%d")
            time_filename = utc_time_object.strftime("%H:%M:%S")

            local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder, time_filename + ext)

            save_pipeline_str = ''
            if self.codec == 'h264':
                save_pipeline_str = f"""appsrc name=m_appsrc emit-signals=true is-live=true format=time
                    ! h264parse ! mp4mux ! filesink name=m_sink location={local_file_path}"""
            elif self.codec == 'h265':
                save_pipeline_str = f"""appsrc name=m_appsrc emit-signals=true is-live=true format=time
                    ! h265parse ! mp4mux ! filesink name=m_sink location={local_file_path}"""

            save_pipeline = Gst.parse_launch(save_pipeline_str)

            appsrc = save_pipeline.get_by_name('m_appsrc')

            save_pipeline.set_state(Gst.State.PLAYING)

            # Push frames to appsrc
            for _, sample in frames:
                ret = appsrc.emit('push-sample', sample)
                if ret != Gst.FlowReturn.OK:
                    logger.error(f"Error pushing buffer to appsrc: {ret}")

            frames = None

            # Emit EOS to signal end of the stream
            appsrc.emit('end-of-stream')

            bus = save_pipeline.get_bus()
            bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS)

            if not self.scanner_output_queue.full():
                
                video_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""

                object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{ext}"""

                logger.info(f"New video file created at local_file_path {local_file_path} and will be uploaded as remote file /{self.cam_ip}/{date_folder}/{time_filename}{ext}")

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
                        "local_file_path": local_file_path,
                        "start_datetime": utc_time_object.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                        "end_datetime": end_datetime
                    }
                }, block=False)

        except Exception as e:
            logger.error(f"{self.cam_ip} save_task, Exception during running, Error: {e}")
            traceback.print_exc()
        finally:
            # Set pipeline to NULL state once processing is complete
            save_pipeline.set_state(Gst.State.NULL)

        logger.debug(f"{self.cam_ip} save_task out")


    def run(self):
        try:

            # Start playing
            if self.start_playing():
                logger.info(f"{self.cam_ip} StreamCapture run, start_playing result: {True}")

                self.pipeline_decode.set_state(Gst.State.PLAYING)

                bus = self.pipeline.get_bus()
                decode_bus = self.pipeline_decode.get_bus()

                while not self.stop_event.is_set():
                    message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                    if message:
                        self.on_message(bus, message)

                    msg_decode = decode_bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                    if msg_decode:
                        self.on_message_decode(bus, msg_decode)
            else:
                logger.error(f"{self.cam_ip} StreamCapture run, Not started as start_playing result: {False}")
                self.pipeline.set_state(Gst.State.NULL)
                self.stop_event.set()

        except Exception as e:
            logger.error(f"{self.cam_ip} StreamCapture run, Exception during running, Error: {e}")
            traceback.print_exc()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline_decode.set_state(Gst.State.NULL)
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
            logger.debug(f"{self.cam_ip} start_playing, set_state PLAYING state_change_return: {playing_state_change_return}")

            if playing_state_change_return != Gst.StateChangeReturn.SUCCESS:
                logger.warning(f"{self.cam_ip} start_playing, playing_state_change_return is NOT SUCCESS, sleeping for {interval} second...")
                time.sleep(interval)
                return self.start_playing(count)
            else:
                logger.debug(f"{self.cam_ip} start_playing, playing_state_change_return is SUCCESS, count: {count}")
                return True

        else:
            logger.warning(f"{self.cam_ip} start_playing, return with already playing, count: {count}")
            return True

        
    def stop(self):
        # self.stop_recording()

        self.stop_event.set()

        self.pipeline.set_state(Gst.State.NULL)


    def feed_detecting(self, running_seconds):
        logger.debug(f"{self.cam_ip} feed_detecting in")

        if self.is_feeding:
            logger.debug(f"{self.cam_ip} feed_detecting out, already feeding")
            return

        self.is_feeding = True
        self.running_seconds = running_seconds

        self.feeding_timer = threading.Timer(running_seconds, self.stop_feeding)
        self.feeding_timer.name = f"Thread-SamplingStopper-{self.cam_ip}"
        self.feeding_timer.start()

        logger.debug(f'Available threads after feed_detecting: {", ".join(thread.name for thread in threading.enumerate())}')

        logger.debug(f"{self.cam_ip} feed_detecting out")

        

    def stop_feeding(self):
        logger.debug(f"{self.cam_ip} stop_feeding in")

        self.is_feeding = False
        self.feeding_count = 0
        self.decoding_count = 0

        logger.debug(f'Available threads after stop_feeding: {", ".join(thread.name for thread in threading.enumerate())}')

        logger.debug(f"{self.cam_ip} stop_feeding out")

    def start_recording(self, utc_time):
        logger.debug(f"{self.cam_ip} start_recording in")

        if self.is_recording:
            logger.debug(f"{self.cam_ip} start_recording out, Recording already started")
            return False

        with self.lock:
            self.is_recording = True
            self.recordings[utc_time] = datetime.strptime(utc_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        logger.debug(f"{self.cam_ip} start_recording out")

        return True


    def stop_recording(self, utc_time):
        logger.debug(f"{self.cam_ip} stop_recording in")

        if not self.is_recording:
            logger.debug(f"{self.cam_ip} start_recording out, already stopped")
            return False
        
        with self.lock:
            self.is_recording = False

        self.save_frames_as_video(self.recordings[utc_time])
        del self.recordings[utc_time]

        logger.debug(f"{self.cam_ip} stop_recording out")

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
       
    def on_message_decode(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.warning("End-Of-Stream reached.")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # raise ValueError(f"{self.name} Gst.MessageType.ERROR: {err}, {debug}")
            logger.error(f"{self.cam_ip} on_message_decode Gst.MessageType.ERROR: {err}, {debug}")
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.debug(f"{self.cam_ip} Decode Pipeline state changed from {old_state.value_nick} to {new_state.value_nick} with pending_state {pending_state.value_nick}")
        elif message.type == Gst.MessageType.WARNING:
            logger.warning(f"Warning message {message.parse_warning()}： {message.type}")
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            logger.debug(f"New ELEMENT detected: {structure.get_name()}")