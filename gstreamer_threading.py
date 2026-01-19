import resource  # NEW: Required for managing file descriptor limits

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

from typing import Dict, Any

ext = ".mp4"

# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

if 'GST_DEBUG' in os.environ:
    Gst.debug_set_default_threshold(os.environ['GST_DEBUG'])

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
        super().__init__(name=f"Thread-Gst-{params['cam_ip']}-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}")

        Gst.init(None)

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
                ! queue name=queue_after_appsrc ! h264parse ! queue ! avdec_h264 name=m_avdec
                ! queue ! videoconvert ! videorate drop-only=true ! video/x-raw,format=BGR,framerate={round(int(self.framerate) * float(os.environ['DETECTING_RATE_PERCENT']))}/1
                ! queue ! appsink name=m_appsink"""
        elif self.codec == 'h265':
            pipeline_str_decode = f"""appsrc name=m_appsrc emit-signals=true is-live=true format=time
                ! queue name=queue_after_appsrc ! h265parse ! queue ! avdec_h265 name=m_avdec max-threads=2 output-corrupt=false
                ! queue ! videoconvert ! videorate drop-only=true ! video/x-raw,format=BGR,framerate={round(int(self.framerate) * float(os.environ['DETECTING_RATE_PERCENT']))}/1
                ! queue ! appsink name=m_appsink"""

        # Create the empty pipeline
        self.pipeline_decode = Gst.parse_launch(pipeline_str_decode)

        # source params
        self.decode_appsrc = self.pipeline_decode.get_by_name('m_appsrc')
        # if self.decode_appsrc is not None:
        #     self.decode_appsrc.connect('need-data', self.on_need_data, {})
        #     self.decode_appsrc.connect('push-sample', self.on_push_sample, {})

        queue_after_appsrc = self.pipeline_decode.get_by_name('queue_after_appsrc')
        if queue_after_appsrc:
            sink_pad = queue_after_appsrc.get_static_pad('sink')
            if sink_pad:
                sink_pad.add_probe(
                    Gst.PadProbeType.BUFFER,  # We only need buffer probes for samples
                    self.probe_callback
                )

        # sink params
        appsink_decode = self.pipeline_decode.get_by_name('m_appsink')
        if  appsink_decode is not None:
            appsink_decode.set_property('max-buffers', 100)
            appsink_decode.set_property('drop', True)
            appsink_decode.set_property('emit-signals', True)
            appsink_decode.set_property('sync', False)
            appsink_decode.connect("new-sample", self.on_new_sample_decode, {})
        
        self.stop_event = threading.Event()
        self.force_stop = threading.Event()
        self.recording_buffer = deque()
        self.detecting_buffer = deque()
        self.recording_lock = threading.Lock()
        self.detecting_lock = threading.Lock()

        self.last_sampling_time = None
        self.is_playing = False
        self.is_feeding = False
        self.feeding_count = 0
        self.decoding_count = 0
        self.is_recording = False
        self.running_seconds = 10

        self.recordings = {}

        self.feeding_timer = None

        # Dictionary to store metadata for each buffer
        self.metadata_store: Dict[int, Any] = {}
        self.metadata_lock = threading.Lock()

        self.detecting_txn = None


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

    def add_recording_frame(self, sample, current_time):
        with self.recording_lock:
            self.recording_buffer.append((current_time, sample))

            # Only discard frames if not recording
            if not self.is_recording:
                while self.recording_buffer and current_time - self.recording_buffer[0][0] > float(os.environ['PRE_RECORDING_SEC']):
                    self.recording_buffer.popleft()

    def get_all_frames(self):
        with self.recording_lock:
            return list(self.recording_buffer)

    def clear_all_frames(self):
        with self.recording_lock:
            self.recording_buffer.clear()
    
    def push_detecting_buffer(self):
        logger.debug(f"{self.cam_ip} push_detecting_buffer, detecting_buffer length: {len(self.recording_buffer)}")

        with self.detecting_lock:
            for single_buffer in self.detecting_buffer:
                ret = self.decode_appsrc.emit('push-sample', single_buffer[1])

                if ret != Gst.FlowReturn.OK:
                    logger.error(f"{self.cam_ip} push_detecting_buffer, Error pushing sample to decode_appsrc: {ret}")

                self.feeding_count += 1

            self.detecting_buffer.clear()

    def add_detecting_frame(self, sample, current_time):
        with self.detecting_lock:
            self.detecting_buffer.append((current_time, sample))

            # discard frames
            while self.detecting_buffer and current_time - self.detecting_buffer[0][0] > float(os.environ['PRE_DETECTING_SEC']):
                self.detecting_buffer.popleft()
    
    def edit_sample_caption(self, sample, current_time):

        sample_caps = sample.get_caps()
        caps_string = sample_caps.to_string()
        structure = sample_caps.get_structure(0)
        sample_framerate = (structure.get_fraction("framerate"))[1]
        sample_info = sample.get_info()
        sample_buffer = sample.get_buffer()
        sample_segment = sample.get_segment()
        
        if sample_framerate == 0:
            caps_string = re.sub(
                r'framerate=\(fraction\)\d+/\d+',
                f'framerate=(fraction){self.framerate}/1',
                caps_string
            )

        caps_string += ", x-current-time=(string)"
        caps_string += str(current_time)

        new_caps = Gst.Caps.from_string(caps_string)
        
        new_sample = Gst.Sample.new(sample_buffer, new_caps, sample_segment, sample_info)

        return new_sample


    def on_new_sample(self, sink, _):
        sample = sink.emit('pull-sample')

        if not sample:
            return Gst.FlowReturn.ERROR
        
        current_time = time.time()

        self.add_recording_frame(sample, current_time)

        if not self.is_feeding:
            self.add_detecting_frame(self.edit_sample_caption(sample, current_time), current_time)

        else:
            self.push_detecting_buffer()

            logger.debug(f"{self.cam_ip} on_new_sample feeding_count: {self.feeding_count}")

            if self.feeding_count > self.framerate * self.running_seconds:
                return Gst.FlowReturn.OK

            ret = self.decode_appsrc.emit('push-sample', self.edit_sample_caption(sample, current_time))
            if ret != Gst.FlowReturn.OK:
                logger.error(f"{self.cam_ip} on_new_sample, Error pushing sample to decode_appsrc: {ret}")

            self.feeding_count += 1

        sample = None
        return Gst.FlowReturn.OK

    def probe_callback(self, pad, info):
        if info.type & Gst.PadProbeType.BUFFER:

            buffer = info.get_buffer()
            pts = buffer.pts  # Use PTS as unique identifier
            
            structure = pad.get_current_caps().get_structure(0)

            with self.metadata_lock:
                self.metadata_store[pts] = structure.get_value("x-current-time")
                # Clean up old metadata (keep last 100 entries)
                if len(self.metadata_store) > 100:
                    oldest_pts = min(self.metadata_store.keys())
                    self.metadata_store.pop(oldest_pts, None)
            
        return Gst.PadProbeReturn.OK

    def on_new_sample_decode(self, sink, _):

        sample = sink.emit('pull-sample')

        # if float(os.environ['DETECTING_RATE_PERCENT']) * self.feeding_count < self.decoding_count:
        #     logger.debug(f"on_new_sample_decode decoding_count:  {self.decoding_count}")
        #     sample = None
        #     return Gst.FlowReturn.OK

        if not self.is_feeding:
            sample = None
            return Gst.FlowReturn.OK

        if sample:
            logger.debug(f"{self.cam_ip} on_new_sample_decode is_feeding: {self.is_feeding}")

            caps = sample.get_caps()
            logger.debug(f"{self.cam_ip} on_new_sample_decode caps: {caps.to_string()}")

            buffer = sample.get_buffer()
            if not buffer:
                logger.error("on_new_sample_decode: Received sample with no buffer")
                return Gst.FlowReturn.OK
            
            pts = buffer.pts
            
            frame_time = None
            with self.metadata_lock:
                frame_time = self.metadata_store.get(pts)
                logger.debug(f"{self.cam_ip} on_new_sample_decode frame_time: {frame_time}")
                # Clean up used metadata
                self.metadata_store.pop(pts, None)

            buffer_size = buffer.get_size()
            if buffer_size == 0:
                logger.error("on_new_sample_decode: Buffer is empty (size 0)")
                return Gst.FlowReturn.OK

            arr = self.gst_to_opencv(sample)

            if not self.cam_queue.full():
                if frame_time is not None:
                    self.decoding_count += 1
                    self.cam_queue.put((StreamCommands.FRAME, arr, {"cam_ip": self.cam_ip, "cam_uuid": self.cam_uuid, "cam_name": self.cam_name, "frame_time": frame_time, "pts": pts, "detecting_txn": self.detecting_txn}), block=False)

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

                
                logger.debug(f"""!!!scanner_output_queue is NOT FULL!!! {self.scanner_output_queue.qsize()}""")
                
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
            else:
                logger.error(f"""!!!scanner_output_queue is FULL!!! {self.scanner_output_queue.qsize()}""")

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
                logger.info(f"{self.cam_ip} StreamCapture run, start_playing result: {True} {self.name}")

                decode_state_ret = self.pipeline_decode.set_state(Gst.State.PLAYING)
                logger.info(f"{self.cam_ip} Decode pipeline set_state(PLAYING) returned: {decode_state_ret}")

                bus = self.pipeline.get_bus()
                decode_bus = self.pipeline_decode.get_bus()

                while not self.stop_event.is_set():
                    try:
                        message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                        if message:
                            self.on_message(bus, message)

                        msg_decode = decode_bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ANY)

                        if msg_decode:
                            self.on_message_decode(bus, msg_decode)

                    except Exception as loop_error:
                        # NEW: Add specific error handling for message loop
                        logger.error(f"{self.cam_ip} Error in message loop: {loop_error}")
                        break
            else:
                logger.error(f"{self.cam_ip} StreamCapture run, Not started as start_playing result: {False}")
                self.pipeline.set_state(Gst.State.NULL)
                self.stop_event.set()

        except Exception as e:
            logger.error(f"{self.cam_ip} StreamCapture run, Exception during running, Error: {e}")
            traceback.print_exc()
        finally:
            try:
                logger.debug(f"{self.cam_ip} StreamCapture run, starting cleanup")
                
                # NEW: Set stop event first to ensure all operations begin stopping
                self.stop_event.set()
                
                # NEW: Stop any ongoing timer operations
                if self.feeding_timer:
                    self.feeding_timer.cancel()
                    self.feeding_timer = None

                # NEW: Clear all buffers to free memory
                with self.detecting_lock:
                    self.detecting_buffer.clear()
                with self.recording_lock:
                    self.recording_buffer.clear()
                with self.metadata_lock:
                    self.metadata_store.clear()

                # MODIFIED: More careful pipeline state changes with verification
                if self.pipeline_decode:
                    logger.debug(f"{self.cam_ip} Setting decode pipeline to NULL")
                    self.pipeline_decode.set_state(Gst.State.NULL)
                    # NEW: Wait for state change to complete and log result
                    state_change = self.pipeline_decode.get_state(Gst.CLOCK_TIME_NONE)
                    logger.debug(f"{self.cam_ip} Decode pipeline state change result: {state_change[0]}")
                    
                if self.pipeline:
                    logger.debug(f"{self.cam_ip} Setting main pipeline to NULL")
                    self.pipeline.set_state(Gst.State.NULL)
                    # NEW: Wait for state change to complete and log result
                    state_change = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
                    logger.debug(f"{self.cam_ip} Main pipeline state change result: {state_change[0]}")

                # NEW: Force garbage collection to clean up any remaining references
                gc.collect()
                
                self.is_playing = False
                logger.info(f"{self.cam_ip} StreamCapture run, Pipeline stopped and cleaned up {self.name}")
                
            except Exception as cleanup_error:
                # NEW: Specific error handling for cleanup process
                logger.error(f"{self.cam_ip} Error during pipeline cleanup: {cleanup_error}")

    def start_playing(self, count = 0, playing = False):
        logger.info(f"{self.cam_ip} start_playing, count: {count} playing: {playing}")
        interval = 10

        if count > 1:
            logger.warning(f"{self.cam_ip} start_playing, count ended with result playing: {playing}, count: {count}")
            return playing
        else:
            if playing:
                return playing

        count += 1
        
        if not self.is_playing:
            try:
                if not self.pipeline:
                    logger.error(f"{self.cam_ip} start_playing, pipeline is NULL")
                    return False

                current_state = self.pipeline.get_state(0)[1]
                if current_state == Gst.State.NULL:
                    time.sleep(0.1)

                playing_state_change_return = self.pipeline.set_state(Gst.State.PLAYING)
                logger.debug(f"{self.cam_ip} start_playing, set_state PLAYING state_change_return: {playing_state_change_return}")

                if playing_state_change_return == Gst.StateChangeReturn.FAILURE:
                    logger.error(f"{self.cam_ip} start_playing, state change failed completely")
                    return False
                elif playing_state_change_return != Gst.StateChangeReturn.SUCCESS:
                    logger.warning(f"{self.cam_ip} start_playing, playing_state_change_return is NOT SUCCESS, sleeping for {interval} second...")
                    time.sleep(interval)
                    return self.start_playing(count)
                else:
                    logger.debug(f"{self.cam_ip} start_playing, playing_state_change_return is SUCCESS, count: {count}")
                    return True

            except Exception as e:
                logger.error(f"{self.cam_ip} start_playing error: {str(e)}")
                return False

        else:
            logger.warning(f"{self.cam_ip} start_playing, return with already playing, count: {count}")
            return True

        
    def stop(self, force=False):
        try:
            if force:
                self.force_stop.set()
            
            # Set the stop event first
            self.stop_event.set()
            
            # Add a small delay to allow pending operations to complete
            time.sleep(0.1)

            # NEW: Stop any ongoing timer operations
            if self.feeding_timer:
                self.feeding_timer.cancel()
                self.feeding_timer = None

            # NEW: Clear all buffers to free memory
            with self.detecting_lock:
                self.detecting_buffer.clear()
            with self.recording_lock:
                self.recording_buffer.clear()
            with self.metadata_lock:
                self.metadata_store.clear()

            # MODIFIED: More careful pipeline state changes with verification
            if self.pipeline_decode:
                logger.debug(f"{self.cam_ip} Setting decode pipeline to NULL")
                self.pipeline_decode.set_state(Gst.State.NULL)
                # NEW: Wait for state change to complete and log result
                state_change = self.pipeline_decode.get_state(Gst.CLOCK_TIME_NONE)
                logger.debug(f"{self.cam_ip} Decode pipeline state change result: {state_change[0]}")
                
            if self.pipeline:
                logger.debug(f"{self.cam_ip} Setting main pipeline to NULL")
                self.pipeline.set_state(Gst.State.NULL)
                # NEW: Wait for state change to complete and log result
                state_change = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
                logger.debug(f"{self.cam_ip} Main pipeline state change result: {state_change[0]}")
        except Exception as e:
            # NEW: Specific error handling for stop process
            logger.error(f"{self.cam_ip} Error during pipeline stop: {e}")

    def feed_detecting(self, running_seconds):
        logger.info(f"{self.cam_ip} feed_detecting in")

        if self.is_feeding:
            logger.info(f"{self.cam_ip} feed_detecting out, already feeding")
            return

        # Cancel any existing timer if it exists
        if self.feeding_timer is not None:
            logger.info(f"{self.cam_ip} feed_detecting before cancel feeding_timer")
            self.feeding_timer.cancel()
            self.feeding_timer = None
            logger.info(f"{self.cam_ip} feed_detecting after cancel feeding_timer")

        with self.detecting_lock:
            # Clear any stale buffered frames before starting
            self.detecting_buffer.clear()
            self.is_feeding = True
            self.running_seconds = running_seconds
            self.detecting_txn = str(uuid.uuid4())

        # Create a new timer
        self.feeding_timer = threading.Timer(running_seconds, self.stop_feeding)
        self.feeding_timer.name = f"Thread-SamplingStopper-{self.cam_ip}"
        self.feeding_timer.start()

        logger.info(f'Available threads after feed_detecting: {", ".join(thread.name for thread in threading.enumerate())}')
        logger.info(f"{self.cam_ip} feed_detecting out")


    def extend_timer(self, running_seconds):
        """Extend the detection timer without resetting detection state.

        Called when a new trigger arrives while detection is already running.
        Only resets the timer, does not affect is_feeding or other state.
        """
        logger.info(f"{self.cam_ip} extend_timer in, extending to {running_seconds}s")

        if not self.is_feeding:
            logger.warning(f"{self.cam_ip} extend_timer out - not currently feeding, ignoring")
            return

        # Cancel existing timer
        if self.feeding_timer is not None:
            self.feeding_timer.cancel()
            self.feeding_timer = None

        # Create new timer with fresh duration
        self.feeding_timer = threading.Timer(running_seconds, self.stop_feeding)
        self.feeding_timer.name = f"Thread-SamplingStopper-{self.cam_ip}"
        self.feeding_timer.start()

        logger.info(f"{self.cam_ip} extend_timer out - timer extended to {running_seconds}s")


    def stop_feeding(self):
        logger.debug(f"{self.cam_ip} stop_feeding in")
        # Check if the timer exists before trying to cancel
        if self.feeding_timer is not None:
            logger.debug(f"{self.cam_ip} stop_feeding before cancel feeding_timer")
            self.feeding_timer.cancel()
            self.feeding_timer = None
            logger.debug(f"{self.cam_ip} stop_feeding after cancel feeding_timer")

        with self.detecting_lock:
            self.is_feeding = False
            self.feeding_count = 0
            self.decoding_count = 0

        # FIX: Flush decode pipeline on STOP (not on resume)
        # This resets decoder state while no frames are being pushed.
        # Then re-assert PLAYING state so pipeline is ready for next detection.
        logger.info(f"{self.cam_ip} stop_feeding flushing decode pipeline")
        self.decode_appsrc.send_event(Gst.Event.new_flush_start())
        self.decode_appsrc.send_event(Gst.Event.new_flush_stop(True))

        # Re-assert PLAYING state after flush to ensure pipeline is ready
        # Without this, pipeline stays in PAUSED and may have issues after long idle
        self.pipeline_decode.set_state(Gst.State.PLAYING)
        logger.info(f"{self.cam_ip} stop_feeding set decode pipeline to PLAYING")

        with self.metadata_lock:
            self.metadata_store.clear()

        logger.debug(f'Available threads after stop_feeding: {", ".join(thread.name for thread in threading.enumerate())}')
        logger.debug(f"{self.cam_ip} stop_feeding out")


    def start_recording(self, utc_time):
        logger.debug(f"{self.cam_ip} start_recording in")

        if self.is_recording:
            logger.debug(f"{self.cam_ip} start_recording out, Recording already started")
            return False

        with self.recording_lock:
            self.is_recording = True
            # Try parsing with milliseconds, fallback to seconds
            try:
                self.recordings[utc_time] = datetime.strptime(utc_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            except ValueError:
                self.recordings[utc_time] = datetime.strptime(utc_time, "%Y-%m-%dT%H:%M:%SZ")
            self.recordings[utc_time] = self.recordings[utc_time].replace(tzinfo=timezone.utc)

        logger.debug(f"{self.cam_ip} start_recording out")

        return True


    def stop_recording(self, utc_time):
        logger.debug(f"{self.cam_ip} stop_recording in")

        if not self.is_recording:
            logger.debug(f"{self.cam_ip} start_recording out, already stopped")
            return False
        
        with self.recording_lock:
            self.is_recording = False

        self.save_frames_as_video(self.recordings[utc_time])
        del self.recordings[utc_time]

        logger.debug(f"{self.cam_ip} stop_recording out")

        return True


    def on_message(self, bus, message):
        if message.type == Gst.MessageType.EOS:
            logger.error(f"{self.cam_ip} on_message End-Of-Stream reached.")
            raise ValueError(f"{self.cam_ip} on_message Gst.MessageType.EOS")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"{self.cam_ip} on_message Gst.MessageType.ERROR: {err}, {debug}")
            raise ValueError(f"{self.cam_ip} on_message Gst.MessageType.ERROR: {err}, {debug}")
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
            logger.error(f"{self.cam_ip} on_message_decode End-Of-Stream reached.")
            raise ValueError(f"{self.cam_ip} on_message_decode Gst.MessageType.EOS")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()

            # Get pipeline states for diagnosis
            _, main_state, _ = self.pipeline.get_state(0)
            _, decode_state, _ = self.pipeline_decode.get_state(0)

            # Log error with full context
            logger.error(f"{self.cam_ip} on_message_decode Gst.MessageType.ERROR: {err}, {debug}")
            logger.error(f"{self.cam_ip} ERROR CONTEXT: "
                         f"is_feeding={self.is_feeding}, "
                         f"is_playing={self.is_playing}, "
                         f"feeding_count={self.feeding_count}, "
                         f"decoding_count={self.decoding_count}, "
                         f"detecting_buffer_len={len(self.detecting_buffer)}, "
                         f"main_pipeline_state={main_state.value_nick}, "
                         f"decode_pipeline_state={decode_state.value_nick}, "
                         f"thread={threading.current_thread().name}")

            raise ValueError(f"{self.cam_ip} on_message_decode Gst.MessageType.ERROR: {err}, {debug}")
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.info(f"{self.cam_ip} Decode Pipeline state changed from {old_state.value_nick} to {new_state.value_nick} with pending_state {pending_state.value_nick}")
        elif message.type == Gst.MessageType.WARNING:
            logger.warning(f"Warning message {message.parse_warning()}： {message.type}")
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            logger.debug(f"New ELEMENT detected: {structure.get_name()}")

