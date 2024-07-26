import os
import sys
import logging
import threading
from enum import Enum
import numpy as np
import time
from datetime import datetime, timedelta, timezone

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')
from gi.repository import Gst
from gi.repository import GstPbutils
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



class StreamCapture(threading.Thread):

    def __init__(self, cam_ip, cam_uuid, cam_name, rtsp_src, pipeline_str, cam_queue, scanner_output_queue, framerate):
        """
        Initialize the stream capturing process
        rtsp_src - rstp link of stream
        stop_event - to send commands to this thread
        outPipe - this process can send commands outside
        """

        super().__init__(name=f"Thread-Gst-{cam_ip}")

        self.rtsp_src = rtsp_src
        self.stop_event = threading.Event()
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.framerate = framerate
        self.currentState = StreamMode.INIT_STREAM
        self.pipeline_str = pipeline_str
        self.pipeline = None
        self.source = None
        self.decode = None
        self.convert = None
        self.sink = None
        self.image_arr = None
        self.newImage = False
        self.record_valve = None
        self.splitmuxsink = None
        # self.motioncells = None
        self.num_unexpected_tot = 1000
        self.unexpected_cnt = 0
        # self.eos_received = False
        self.cam_ip = cam_ip
        self.cam_uuid = cam_uuid
        self.cam_name = cam_name

        # Create the empty pipeline
        self.pipeline = Gst.parse_launch(self.pipeline_str)

        # Get the tee element
        tee = self.pipeline.get_by_name("t")

        # source params
        self.source = self.pipeline.get_by_name('m_rtspsrc')
        if  self.source is not None:
            self.source.set_property('latency', 0)
            self.source.set_property('location', self.rtsp_src)
            self.source.set_property('protocols', 'udp')
            self.source.set_property('retry', 50)
            self.source.set_property('timeout', 2000000)
            self.source.set_property('tcp-timeout', 2000000)
            self.source.set_property('drop-on-latency', 'true')
            self.source.set_property('ntp-time-source', 0)
            if float(f"{GstPbutils.plugins_base_version().major}.{GstPbutils.plugins_base_version().minor}") >= 1.18:
                self.source.set_property('is-live', 'true')
            self.source.set_property('buffer-mode', 0)
            self.source.set_property('ntp-sync', 'true')

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
        self.framerate_ctr.set_property('drop-only', 'true')

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
            self.stop_event.set()

        self.sink.connect("new-sample", self.new_buffer, self.sink)

        # # record_valve params
        # self.record_valve = self.pipeline.get_by_name('m_record_valve')

        # # splitmuxsink params
        # self.splitmuxsink = self.pipeline.get_by_name('m_splitmuxsink')

        # now = datetime.now(timezone.utc)
        # self.date_folder = now.strftime("%Y-%m-%d")
        # self.time_filename = now.strftime("%H:%M:%S")
        # self.ext = ".mp4"

        # self.splitmuxsink.set_property('location', os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder, self.time_filename + self.ext))
        # if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder)):
        #     os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder))


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
        if self.stop_event.is_set():
            self.image_arr = None
            self.newImage = False
            return Gst.FlowReturn.OK
        else:    
            sample = sink.emit("pull-sample")
            arr = self.gst_to_opencv(sample)
            self.image_arr = arr
            self.newImage = True
            return Gst.FlowReturn.OK

    def run(self):
        # Start playing
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Unable to set the pipeline to the playing state.")
            self.stop_event.set()

        # Wait until error or EOS
        bus = self.pipeline.get_bus()
        # bus.add_signal_watch()
        # bus.connect("message", self.on_message)

        try:
            while True:

                if self.stop_event.is_set():
                    time.sleep(0.1)
                    # logger.info(f"{self.name} is in paused mode")
                else:
                    if self.image_arr is not None and self.newImage is True:

                        if not self.cam_queue.full():
                            # print("\r adding to queue of size{}".format(self.cam_queue.qsize()), end='\r')
                            # print("adding to queue of size")
                            self.cam_queue.put((StreamCommands.FRAME, self.image_arr), block=False)

                        self.image_arr = None
                        self.unexpected_cnt = 0

                    message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                    if message:
                        self.on_message(bus, message)
                    
                



        except Exception as e:
            logger.info(f"Caught exception during running {self.name}")
            logger.error(e)
            traceback.print_exc()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            logger.info("Pipeline stopped and cleaned up.")
          

    def stop(self):
        print(f"Stopping {self.name}")

        self.stop_recording()

        self.stop_event.set()

    def pause_sampling(self):
        logger.info(f"pause_sampling")
        self.stop_event.set()

    def restart_sampling(self):
        logger.info(f"restart_sampling")
        self.stop_event.clear()

    def stop_recording(self):
        logger.info("Stopping recording...")
        # self.eos_received = False

        # self.record_valve.set_property('drop', True)
        
        # Send EOS to the recording branch
        logger.info("Before sending sink eos")
        # self.splitmuxsink.send_event(Gst.Event.new_eos())

        # logger.info("Before sending pipeline eos")
        # self.pipeline.send_event(Gst.Event.new_eos())

        logger.info("End-Of-Stream sending...")

        # Wait for the EOS event to be processed
        # while not self.eos_received:
        #     time.sleep(0.1)
        #     logger.info("waiting for eos_received")

        logger.info("Recording stopped")

    def on_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.info("End-Of-Stream reached.")
            # self.eos_received = True
            self.stop_event.set()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.info(f"Error: {err}, {debug}")
            self.stop_event.set()
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.info(f"Pipeline state changed from {old_state.value_nick} to {new_state.value_nick}.")
        # elif message.type == Gst.MessageType.WARNING:
        #     logger.info(f"Warning message {message.parse_warning()}： {message.type}.")
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            # logger.info(f"New ELEMENT detected: {structure.get_name()}")
            if structure and structure.get_name().startswith("splitmuxsink-"):
                action = structure.get_name()
                # logger.info(f"New action detected: {action}")
                if action == "splitmuxsink-fragment-opened":
                    location = structure.get_string("location")
                    logger.info(f"New file being created: {location}")
                elif action == "splitmuxsink-fragment-closed":
                    location = structure.get_string("location")
                    
                    logger.info(f"New file created: {location}")

                    duration_timedelta = timedelta(microseconds=int(structure.get_value("running-time")) / 1000)
                    start_datetime_utc = datetime.strptime(f"{self.date_folder} {self.time_filename}", "%Y-%m-%d %H:%M:%S")
                    end_datetime_utc = start_datetime_utc + duration_timedelta

                    if not self.scanner_output_queue.full():
                        self.scanner_output_queue.put({
                            "type": "video_clipped",
                            "payload": {
                                "video_clipping_location": os.environ['VIDEO_CLIPPING_LOCATION'],
                                "cam_ip": self.cam_ip,
                                "cam_uuid": self.cam_uuid,
                                "cam_name": self.cam_name,
                                "date_folder": self.date_folder,
                                "time_filename": self.time_filename,
                                "ext": self.ext,
                                "local_file_path": location,
                                "start_datetime": start_datetime_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                                "end_datetime": end_datetime_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z',
                            }
                        }, block=False)
                        logger.info(f"Sending video_clipped for video file: {self.time_filename}")
                    
