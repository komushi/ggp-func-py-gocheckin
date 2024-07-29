import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')

from gi.repository import Gst
from gi.repository import GstPbutils



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



class StreamCapture(threading.Thread):

    def __init__(self, params, scanner_output_queue, cam_queue):
        """
        Initialize the stream capturing process
        rtsp_src - rstp link of stream
        stop_event - to send commands to this thread
        outPipe - this process can send commands outside
        """

        super().__init__(name=f"Thread-Gst-{params['cam_ip']}")

        self.stop_event = threading.Event()

        # params
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.rtsp_src = params['rtsp_src']
        self.framerate = params['framerate']
        self.pipeline_str = params['pipeline_str']
        self.cam_ip = params['cam_ip']
        self.cam_uuid = params['cam_uuid']
        self.cam_name = params['cam_name']
        self.codec = params['codec']

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
            
            # self.source.set_property("onvif-mode", True)
            # self.source.set_property("onvif-rate-control", True)
            # if float(f"{GstPbutils.plugins_base_version().major}.{GstPbutils.plugins_base_version().minor}") >= 1.18:
            #     self.source.set_property('is-live', True)

            # self.source.set_property('drop-on-latency', 'true')
            # self.source.set_property('ntp-time-source', 0)
            # self.source.set_property('ntp-sync', 'true')

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

        # Get the tee element
        self.tee = self.pipeline.get_by_name("t")
        self.tee_pad = None
        self.queue = None
        self.record_valve = None
        self.splitmuxsink = None
        self.h264h265_parser = None

        self.image_arr = None
        self.newImage = False
        self.handler_id = None
        self.date_folder = None
        self.time_filename = None
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
        sample = sink.emit("pull-sample")
        arr = self.gst_to_opencv(sample)
        self.image_arr = arr
        self.newImage = True
        return Gst.FlowReturn.OK

    def run(self):
        # Start playing
        # ret = self.pipeline.set_state(Gst.State.PLAYING)
        # if ret == Gst.StateChangeReturn.FAILURE:
        #     logger.info("Unable to set the pipeline to the playing state.")
        #     self.stop_event.set()

        self.start_playing()

        # Wait until error or EOS
        bus = self.pipeline.get_bus()
        # bus.add_signal_watch()
        # bus.connect("message", self.on_message)

        try:
            while True:

                message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                if message:
                    self.on_message(bus, message)

                if self.stop_event.is_set():
                    # logger.info(f"{self.name} stop_event.is_set()")
                    time.sleep(0.5)
                else:
                    if self.image_arr is not None and self.newImage:

                        if not self.cam_queue.full():
                            self.cam_queue.put((StreamCommands.FRAME, self.image_arr, {"cam_ip": self.cam_ip, "cam_uuid": self.cam_uuid, "cam_name": self.cam_name}), block=False)
                            time.sleep(1)
                        else:
                            logger.info(f"!! gstreamer cam_queue is full !!")

                        self.image_arr = None
                        self.newImage = False

                        



        except Exception as e:
            logger.info(f"Caught exception during running {self.name}")
            logger.error(e)
            traceback.print_exc()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            logger.info("Pipeline stopped and cleaned up.")

    # def pipeline_is_playing(self):

    #     logger.info(f"pipeline_is_playing, {self.name} before get_state")
    #     state_change_return, current_state, pending_state = self.pipeline.get_state(Gst.SECOND)
    #     logger.info(f"pipeline_is_playing, {self.name} get_state state_change_return: {state_change_return}, current_state: {current_state}, pending_state: {pending_state}")

    #     if state_change_return == Gst.StateChangeReturn.SUCCESS:
    #         logger.info(f"Current state: {current_state.value_name}")
    #         logger.info(f"Pending state: {pending_state.value_name}")
    #         if current_state == Gst.State.PLAYING:
    #             return True
    #     elif state_change_return == Gst.StateChangeReturn.ASYNC:
    #         logger.info("State change is still in progress.")
    #     elif state_change_return == Gst.StateChangeReturn.NO_PREROLL:
    #         logger.info("Pipeline is in a state where preroll is not possible.")
    #     else:
    #         logger.info(f"State change failed or returned unexpected value: {state_change_return.value_name}")

    #     return False

    def start_playing(self, count = 0):
        logger.info(f"start_playing, {self.name} count: {count}")

        if count > 5:
            logger.info(f"start_playing, {self.name} ended with is_playing: {self.is_playing}, count: {count}")
            return

        count += 1
        
        try:
            if not self.is_playing:

                playing_state_change_return = self.pipeline.set_state(Gst.State.PLAYING)
                logger.info(f"start_playing, {self.name} set_state PLAYING state_change_return: {playing_state_change_return}")

                if playing_state_change_return != Gst.StateChangeReturn.SUCCESS:
                    logger.info(f"start_playing, {self.name} playing_state_change_return is not {Gst.StateChangeReturn.SUCCESS}")
                    time.sleep(5)
                    self.start_playing(count)

                logger.info(f"start_playing, {self.name} return with is_playing: {self.is_playing}, count: {count}")
                return
    
        except Exception as e:
            logger.info(f"start_playing, {self.name} exception")
            logger.error(e)
            traceback.print_exc()
            self.pipeline.set_state(Gst.State.NULL)

        


    def stop_sampling(self):
        logger.info(f"stop_sampling, {self.name} Stop sampling...")

        self.stop_event.set()

        if self.handler_id is not None:
            self.sink.disconnect(self.handler_id)
            self.handler_id = None

    def start_sampling(self):
        logger.info(f"start_sampling, {self.name} Start sampling...")

        if self.is_playing:

            if self.handler_id is None:
                logger.info(f"start_sampling, connect new buffer callback")
                self.handler_id = self.sink.connect("new-sample", self.new_buffer, self.sink)

            self.stop_event.clear()

            logger.info(f"start_sampling, {self.name} Sampling started...")

        else:
            logger.info(f"start_sampling, {self.name} Sampling not started...")


    def start_recording(self):
        logger.info(f"start_recording, {self.name} Start recording...")

        if self.is_playing:

            if self.create_and_link_splitmuxsink():

                self.record_valve.set_property('drop', False)
                logger.info(f"start_recording, {self.name} Start New Recording...")

                return True;
            else:
                logger.info(f"start_recording, {self.name} Recording already started!!!")
                return False;
        else:
            logger.info(f"start_recording, {self.name} Recording not started!!!")
            return False;

    def stop_recording(self):
        logger.info(f"stop_recording, {self.name} Stop recording...")

        if self.record_valve is not None:
            self.record_valve.set_property('drop', True)
            logger.info(f"stop_recording, {self.name} Dropping record_valve...")
        
        # Send EOS to the recording branch
        if self.splitmuxsink is not None:
            self.splitmuxsink.send_event(Gst.Event.new_eos())
            logger.info(f"stop_recording, {self.name} End-Of-Stream sent...")

        time.sleep(1)

        self.unlink_and_remove_splitmuxsink()

    def on_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.info("End-Of-Stream reached.")
            # self.stop_event.set()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.info(f"Error: {err}, {debug}")
            self.stop_event.set()
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()

                if new_state == Gst.State.PLAYING:
                    self.is_playing = True
                else:
                    self.is_playing = False

                logger.info(f"{self.cam_ip} Pipeline state changed from {old_state.value_nick} to {new_state.value_nick}.")
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
                    logger.info(f"New file created running-time: {structure.get_value('running-time')}")
                    logger.info(f"New file created duration_timedelta: {duration_timedelta}")

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
                    

    def create_and_link_splitmuxsink(self):

        if self.pipeline.get_by_name("splitmuxsink"):
            logger.info("Splitmuxsink branch is already linked...")
            return False

            
        # Create elements for the splitmuxsink branch
        self.queue = Gst.ElementFactory.make("queue", "record_queue")
        self.record_valve = Gst.ElementFactory.make("valve", "record_valve")
        if self.rtph265depay is not None:
            self.h264h265_parser = Gst.ElementFactory.make("h265parse", "record_h265parse")
        elif self.rtph264depay is not None:
            self.h264h265_parser = Gst.ElementFactory.make("h264parse", "record_h264parse")
        self.splitmuxsink = Gst.ElementFactory.make("splitmuxsink", "splitmuxsink")
        
        now = datetime.now(timezone.utc)
        self.date_folder = now.strftime("%Y-%m-%d")
        self.time_filename = now.strftime("%H:%M:%S")

        if not os.path.exists(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder)):
            os.makedirs(os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder))

        # Set properties
        self.splitmuxsink.set_property("location", os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], self.cam_ip, self.date_folder, self.time_filename + self.ext))
        self.splitmuxsink.set_property("max-size-time", 20000000000)  # 20 seconds

        # Add elements to the pipeline
        self.pipeline.add(self.queue)
        self.pipeline.add(self.record_valve)
        self.pipeline.add(self.h264h265_parser)
        self.pipeline.add(self.splitmuxsink)

        # Link the elements together
        self.queue.link(self.record_valve)
        self.record_valve.link(self.h264h265_parser)
        self.h264h265_parser.link(self.splitmuxsink)

        # Link the tee to the queue
        self.tee_pad = self.tee.get_request_pad("src_%u")
        queue_pad = self.queue.get_static_pad("sink")
        self.tee_pad.link(queue_pad)

        self.pipeline.set_state(Gst.State.PLAYING)

        logging.info("Splitmuxsink branch created and linked")

        return True

    # Function to unlink and remove the splitmuxsink branch
    def unlink_and_remove_splitmuxsink(self):
        if not self.tee_pad:
            logging.info("No splitmuxsink branch to unlink")
            return

        # Unlink the tee from the queue
        self.tee_pad.unlink(self.queue.get_static_pad("sink"))

        self.queue.set_state(Gst.State.NULL)
        self.record_valve.set_state(Gst.State.NULL)
        self.h264h265_parser.set_state(Gst.State.NULL)
        self.splitmuxsink.set_state(Gst.State.NULL)

        # Remove the elements from the pipeline
        self.pipeline.remove(self.queue)
        self.pipeline.remove(self.record_valve)
        self.pipeline.remove(self.h264h265_parser)
        self.pipeline.remove(self.splitmuxsink)

        # Release the tee pad
        self.tee.release_request_pad(self.tee_pad)
        self.tee_pad = None

        logging.info("Splitmuxsink branch unlinked and removed")