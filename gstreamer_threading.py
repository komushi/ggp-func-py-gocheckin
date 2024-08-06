import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')

from gi.repository import Gst
from gi.repository import GstPbutils

import uuid

import os
import sys
import logging
import threading
from enum import Enum
import numpy as np
import time
from datetime import datetime, timedelta, timezone

import traceback

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

Gst.init(None)

'''Konwn issues

* if format changes at run time system hangs
'''

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
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! rtph264depay name=m_rtph264depay 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! capsfilter caps=video/x-h264 ! h264parse ! tee name=t t. 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! avdec_h264 name=m_avdec 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! videoconvert name=m_videoconvert 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! videorate name=m_videorate 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! appsink name=m_appsink"""    

pipeline_str_h265 = f"""rtspsrc name=m_rtspsrc 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! rtph265depay name=m_rtph265depay 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! capsfilter caps=video/x-h265 ! h265parse ! tee name=t t. 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! avdec_h265 name=m_avdec 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! videoconvert name=m_videoconvert 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! videorate name=m_videorate 
    ! queue max-size-buffers=0 max-size-time=0 max-size-bytes=10485760 ! appsink name=m_appsink"""
# pipeline_str_h264 = f"""rtspsrc name=m_rtspsrc 
#     ! queue ! rtph264depay name=m_rtph264depay 
#     ! queue ! h264parse ! tee name=t t. 
#     ! queue ! avdec_h264 name=m_avdec 
#     ! queue ! videoconvert name=m_videoconvert 
#     ! queue ! videorate name=m_videorate 
#     ! queue ! appsink name=m_appsink"""    

# pipeline_str_h265 = f"""rtspsrc name=m_rtspsrc 
#     ! queue ! rtph265depay name=m_rtph265depay 
#     ! queue ! h265parse ! tee name=t t. 
#     ! queue ! avdec_h265 name=m_avdec 
#     ! queue ! videoconvert name=m_videoconvert 
#     ! queue ! videorate name=m_videorate 
#     ! queue ! appsink name=m_appsink"""

class StreamCapture(threading.Thread):

    def __init__(self, params, scanner_output_queue, cam_queue):
        """
        Initialize the stream capturing process
        rtsp_src - rstp link of stream
        stop_event - to show this thread is stopped
        outPipe - this process can send commands outside
        """

        super().__init__(name=f"Thread-Gst-{params['cam_ip']}")

        self.stop_event = threading.Event()

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
            self.source.set_property('latency', 1000)
            self.source.set_property('location', self.rtsp_src)
            self.source.set_property('protocols', 'tcp')
            self.source.set_property('retry', 1)
            self.source.set_property('timeout', 2000000)
            self.source.set_property('tcp-timeout', 2000000)
            self.source.set_property('buffer-mode', 1)            

        if float(f"{GstPbutils.plugins_base_version().major}.{GstPbutils.plugins_base_version().minor}") >= 1.18:
            self.source.set_property("onvif-mode", True)
            self.source.set_property("onvif-rate-control", False)
            self.source.set_property('is-live', True)

        # rtph264depay
        self.rtph264depay = self.pipeline.get_by_name('m_rtph264depay')
        self.rtph265depay = self.pipeline.get_by_name('m_rtph265depay')

        # decode params
        self.decode = self.pipeline.get_by_name('m_avdec')
        if  self.decode is not None:
            self.decode.set_property('max-threads', 2)
            self.decode.set_property('output-corrupt', 'false')
        
        # convert params
        self.convert = self.pipeline.get_by_name('m_videoconvert')

        #framerate parameters
        self.framerate_ctr = self.pipeline.get_by_name('m_videorate')
        self.framerate_ctr.set_property('max-rate', self.framerate/1)
        # self.framerate_ctr.set_property('drop-only', 'true')

        # sink params
        self.sink = self.pipeline.get_by_name('m_appsink')

        # Maximum number of nanoseconds that a buffer can be late before it is dropped (-1 unlimited)
        # flags: readable, writable
        # Integer64. Range: -1 - 9223372036854775807 Default: -1
        self.sink.set_property('max-lateness', 500000000)

        # The maximum number of buffers to queue internally (0 = unlimited)
        # flags: readable, writable
        # Unsigned Integer. Range: 0 - 4294967295 Default: 0
        self.sink.set_property('max-buffers', 5)

        # Drop old buffers when the buffer queue is filled
        # flags: readable, writable
        # Boolean. Default: false
        self.sink.set_property('drop', 'true')

        # Emit new-preroll and new-sample signals
        # flags: readable, writable
        # Boolean. Default: false
        self.sink.set_property('emit-signals', True)

        # # sink.set_property('drop', True)
        # # sink.set_property('sync', False)

        # The allowed caps for the sink pad
        # flags: readable, writable
        caps = Gst.caps_from_string(
            'video/x-raw, format=(string){BGR, GRAY8}; video/x-bayer,format=(string){rggb,bggr,grbg,gbrg}')
        self.sink.set_property('caps', caps)

        # if not self.source or not self.sink or not self.pipeline or not self.decode or not self.convert:
        if not self.sink or not self.pipeline or not self.convert:
            print("Not all elements could be created.")
            # self.stop_event.set()

        # Get the tee element
        self.tee = self.pipeline.get_by_name("t")
        self.queue = None
        self.record_valve = None
        self.splitmuxsink = None
        self.h264h265_parser = None

        self.last_sampling_time = None
        self.handler_id = None
        self.start_datetime_utc = None
        self.ext = ".mp4"

        self.is_playing = False


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

    def new_buffer(self, sink, _):
        crt_time = time.time()
        # self.image_arr = None
        # self.newImage = False

        # if not self.stop_event.is_set():
        if self.last_sampling_time is None or crt_time - self.last_sampling_time > 0.75:
            self.last_sampling_time = crt_time
            sample = sink.emit("pull-sample")
            arr = self.gst_to_opencv(sample)

            if not self.cam_queue.full():
                self.cam_queue.put((StreamCommands.FRAME, arr, {"cam_ip": self.cam_ip, "cam_uuid": self.cam_uuid, "cam_name": self.cam_name}), block=False)


        return Gst.FlowReturn.OK

    def run(self):
        try:

            # Start playing
            if not self.start_playing():
                # logger.info(f"run start_playing result: {False}")
                self.stop_event.set()

            # Wait until error or EOS
            bus = self.pipeline.get_bus()
            # bus.add_signal_watch()
            # bus.connect("message", self.on_message)

            while not self.stop_event.is_set():
                message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                if message:
                    self.on_message(bus, message)

        except Exception as e:
            logger.error(f"Caught exception during running {self.name}")
            logger.error(e)
            traceback.print_exc()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            self.stop_event.set()
            logger.info("Pipeline stopped and cleaned up.")

    def start_playing(self, count = 0, playing = False):
        logger.info(f"start_playing, {self.name} count: {count} playing: {playing}")

        if count > 5:
            logger.warning(f"start_playing, {self.name} count ended with result playing: {playing}, count: {count}")
            return playing
        else:
            if playing:
                return playing

        count += 1
        
        try:
            if not self.is_playing:

                playing_state_change_return = self.pipeline.set_state(Gst.State.PLAYING)
                # logger.info(f"start_playing, {self.name} set_state PLAYING state_change_return: {playing_state_change_return}")

                if playing_state_change_return != Gst.StateChangeReturn.SUCCESS:
                    interval = 1
                    logger.warning(f"start_playing, {self.name} playing_state_change_return is not SUCCESS, sleeping for {interval} second...")
                    time.sleep(interval)
                    return self.start_playing(count)
                else:
                    logger.info(f"start_playing, {self.name} return with playing: {True}, count: {count}")
                    return True

            else:
                logger.warning(f"start_playing, {self.name} return with not playing, count: {count}")
                return True
    
        except Exception as e:
            logger.error(f"start_playing, {self.name} exception")
            logger.error(e)
            traceback.print_exc()
            return False
        # finally:
        #     logger.info(f"start_playing, {self.name} return with final result: {False}, count: {count}")
        #     return False


        
    def stop(self):
        self.stop_recording()

        self.stop_sampling()

        self.stop_event.set()

    def stop_sampling(self):
        logger.info(f"stop_sampling, Stop sampling with {self.name}")

        # self.stop_event.set()

        if self.handler_id is not None:
            self.sink.disconnect(self.handler_id)
            self.handler_id = None

    def start_sampling(self):
        logger.info(f"start_sampling, {self.name} Start sampling...")

        if self.is_playing:

            if self.handler_id is None:
                # logger.info(f"start_sampling, connect new buffer callback")
                self.handler_id = self.sink.connect("new-sample", self.new_buffer, self.sink)
            else:
                logger.warning(f"start_sampling, Sampling already started with {self.name}")

            # self.stop_event.clear()

        else:
            logger.info(f"start_sampling, Sampling not started as {self.name} is not playing.")


    def start_recording(self):
        logger.info(f"start_recording, {self.name} Start recording...")

        if self.is_playing:

            if self.create_and_link_splitmuxsink():

                self.record_valve.set_property('drop', False)
                # logger.info(f"start_recording, {self.name} Start New Recording...")

                return True;
            else:
                logger.warning(f"start_recording, Recording already started with {self.name}")
                return False;
        else:
            logger.info(f"start_recording, Recording not started as {self.name} is not playing")
            return False;

    def stop_recording(self):
        logger.info(f"stop_recording, Stop recording with {self.name}")

        if self.record_valve is not None:
            self.record_valve.set_property('drop', True)
            # logger.info(f"stop_recording, {self.name} Dropping record_valve...")
        
        # Send EOS to the recording branch
        if self.splitmuxsink is not None:
            self.splitmuxsink.send_event(Gst.Event.new_eos())
            logger.info(f"stop_recording, {self.name} End-Of-Stream sent...")

        time.sleep(1)

        self.unlink_and_remove_splitmuxsink()

    def on_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.warning("End-Of-Stream reached.")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            raise ValueError(f"{self.name} Gst.MessageType.ERROR: {err}, {debug}")
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()

                if new_state == Gst.State.PLAYING:
                    self.is_playing = True
                else:
                    self.is_playing = False

                logger.info(f"{self.cam_ip} Pipeline state changed from {old_state.value_nick} to {new_state.value_nick}.")
        elif message.type == Gst.MessageType.WARNING:
            logger.warning(f"Warning message {message.parse_warning()}： {message.type} at {self.cam_ip}.")
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            # logger.info(f"New ELEMENT detected: {structure.get_name()}")
            if structure and structure.get_name().startswith("splitmuxsink-"):
                action = structure.get_name()
                # logger.info(f"New action detected: {action}")
                if action == "splitmuxsink-fragment-opened":
                    
                    self.start_datetime_utc = datetime.now(timezone.utc)

                    location = structure.get_string("location")
                    logger.info(f"splitmuxsink-fragment-opened, New video file being created at local_file_path {location}")

                elif action == "splitmuxsink-fragment-closed":
                    location = structure.get_string("location")

                    if not self.scanner_output_queue.full():
                        date_folder = self.start_datetime_utc.strftime("%Y-%m-%d")
                        time_filename = self.start_datetime_utc.strftime("%H:%M:%S")
                        
                        video_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{self.ext}"""

                        object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{self.cam_ip}/{date_folder}/{time_filename}{self.ext}"""

                        logger.info(f"splitmuxsink-fragment-closed, New video file created at local_file_path {location} and will be uploaded as remote file /{self.cam_ip}/{date_folder}/{time_filename}{self.ext}")

                        self.scanner_output_queue.put({
                            "type": "video_clipped",
                            "payload": {
                                "video_clipping_location": os.environ['VIDEO_CLIPPING_LOCATION'],
                                "cam_ip": self.cam_ip,
                                "cam_uuid": self.cam_uuid,
                                "cam_name": self.cam_name,
                                "video_key": video_key,
                                "object_key": object_key,
                                "ext": self.ext,
                                "local_file_path": location,
                                "start_datetime": self.start_datetime_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                                "end_datetime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                            }
                        }, block=False)
                        # logger.info(f"Sending video_clipped for video file: {location}")
                    

    def create_and_link_splitmuxsink(self):

        if self.pipeline.get_by_name("splitmuxsink"):
            logger.warning("create_and_link_splitmuxsink, Splitmuxsink branch is already linked...")
            return False

        date_folder = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_name = str(uuid.uuid4())[:8]

        # logger.info(f"create_and_link_splitmuxsink for date_folder: {date_folder}, file_name: {file_name + self.ext} ")
            
        # Create elements for the splitmuxsink branch
        self.queue = Gst.ElementFactory.make("queue", "record_queue")
        self.queue.set_property("max-size-buffers", 0)
        self.queue.set_property("max-size-time", 0)
        self.queue.set_property("max-size-bytes", 10485760)  # 1 MB buffer size

        self.record_valve = Gst.ElementFactory.make("valve", "record_valve")

        if self.rtph265depay is not None:
            self.h264h265_parser = Gst.ElementFactory.make("h265parse", "record_h265parse")
        elif self.rtph264depay is not None:
            self.h264h265_parser = Gst.ElementFactory.make("h264parse", "record_h264parse")
        self.splitmuxsink = Gst.ElementFactory.make("splitmuxsink", "splitmuxsink")


        if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder)):
            os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder))

        # Set properties
        self.splitmuxsink.set_property("location", os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, date_folder, file_name + self.ext))
        self.splitmuxsink.set_property("max-size-time", 20000000000)  # 20 seconds
        if float(f"{GstPbutils.plugins_base_version().major}.{GstPbutils.plugins_base_version().minor}") >= 1.18:
            self.splitmuxsink.set_property("async-finalize", True)

        # Add elements to the pipeline
        self.pipeline.add(self.queue)
        self.pipeline.add(self.record_valve)
        self.pipeline.add(self.h264h265_parser)
        self.pipeline.add(self.splitmuxsink)

        self.queue.sync_state_with_parent()
        self.record_valve.sync_state_with_parent()
        self.h264h265_parser.sync_state_with_parent()
        self.splitmuxsink.sync_state_with_parent()

        # Link the tee to the queue
        tee_pad = self.tee.get_request_pad("src_%u")
        queue_pad = self.queue.get_static_pad("sink")
        tee_pad.link(queue_pad)

        # Link the elements together
        self.queue.link(self.record_valve)
        self.record_valve.link(self.h264h265_parser)
        self.h264h265_parser.link(self.splitmuxsink)

        # self.pipeline.set_state(Gst.State.READY)
        self.pipeline.set_state(Gst.State.PLAYING)

        self.send_keyframe_request()

        logging.info("create_and_link_splitmuxsink, Splitmuxsink branch created and linked")

        return True

    def send_keyframe_request(self):
        event = Gst.Event.new_custom(Gst.EventType.CUSTOM_DOWNSTREAM, Gst.Structure.new_empty("GstForceKeyUnit"))
        if self.rtph265depay is not None:
            self.rtph265depay.send_event(event)
        elif self.rtph264depay is not None:
            self.rtph264depay.send_event(event)

    # Function to unlink and remove the splitmuxsink branch
    def unlink_and_remove_splitmuxsink(self):
        if self.splitmuxsink is None:
            logging.warning("unlink_and_remove_splitmuxsink, No splitmuxsink branch to unlink")
            return

        # Set elements to NULL state before unlinking
        self.splitmuxsink.set_state(Gst.State.NULL)
        self.h264h265_parser.set_state(Gst.State.NULL)
        self.record_valve.set_state(Gst.State.NULL)
        self.queue.set_state(Gst.State.NULL)

        # Unlink the tee from the queue
        self.h264h265_parser.unlink(self.splitmuxsink)
        self.record_valve.unlink(self.h264h265_parser)
        self.queue.unlink(self.record_valve)
        tee_pad = self.tee.get_request_pad("src_%u")
        tee_pad.unlink(self.queue.get_static_pad("sink"))

        # Release the tee pad
        self.tee.release_request_pad(tee_pad)

        # Remove the elements from the pipeline
        self.pipeline.remove(self.splitmuxsink)
        self.pipeline.remove(self.h264h265_parser)
        self.pipeline.remove(self.record_valve)
        self.pipeline.remove(self.queue)

        self.splitmuxsink = None
        self.h264h265_parser = None
        self.record_valve = None
        self.queue = None
        
        logging.info("unlink_and_remove_splitmuxsink, Splitmuxsink branch unlinked and removed")