import logging
import time
from datetime import datetime, timezone, timedelta
import sys
import os
import threading
import traceback
from abc import abstractmethod

import numpy as np

import gstreamer_threading as gst

from match_handler import MatchEvent

if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)


class FaceRecognitionBase(threading.Thread):
    """Base class for face recognition detector threads.

    Subclasses must set THREAD_NAME_PREFIX and implement process_frame().
    """

    THREAD_NAME_PREFIX = "Thread-Detector"

    def __init__(self, face_app, active_members, match_handler, cam_queue):
        super().__init__(name=f"{self.THREAD_NAME_PREFIX}-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}")

        self.cam_queue = cam_queue
        self.match_handler = match_handler
        self.stop_event = threading.Event()

        self.face_app = face_app
        self._active_members = None

        # Pre-compute embeddings matrix for vectorized comparison
        # (triggered automatically by the property setter)
        self.active_members = active_members

        self.cam_detection_his = {}

    def run(self):
        logger.info(f"{self.name} started")
        time.sleep(1)

        while not self.stop_event.is_set():
            try:
                if not self.cam_queue.empty():
                    cmd, raw_img, cam_info = self.cam_queue.get(False)

                    # Handle session end signal
                    if cmd == gst.StreamCommands.SESSION_END:
                        session_cam_ip = cam_info.get('cam_ip')
                        if session_cam_ip and session_cam_ip in self.cam_detection_his:
                            his = self.cam_detection_his[session_cam_ip]
                            detected = his.get('detected', 0)
                            identified = his.get('identified', False)
                            first_frame_at = his.get('first_frame_at', 0.0)
                            session_duration = (time.time() - first_frame_at) * 1000 if first_frame_at > 0 else 0
                            logger.info(f"{session_cam_ip} SESSION END - frames: {detected}, identified: {identified}, duration: {session_duration:.0f}ms")
                            del self.cam_detection_his[session_cam_ip]
                        continue

                    if cam_info['cam_ip'] not in self.cam_detection_his:
                        self.cam_detection_his[cam_info['cam_ip']] = {}
                        self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                        self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                        self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = 0.0
                    else:
                        if self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] != cam_info['detecting_txn']:
                            self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                            self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                            self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                            self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = 0.0

                    if self.cam_detection_his[cam_info['cam_ip']]['identified']:
                        continue

                    current_time = time.time()
                    age = current_time - float(cam_info['frame_time'])

                    if age > float(os.environ['AGE_DETECTING_SEC']):
                        logger.debug(f"{cam_info['cam_ip']} age: {age}")
                        continue
                    else:
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] += 1
                        detected = self.cam_detection_his[cam_info['cam_ip']]['detected']
                        if detected == 1:
                            self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = current_time

                    # Delegate to subclass for detection + matching
                    matched_faces = self.process_frame(raw_img, cam_info, detected, age)

                    if not matched_faces:
                        continue

                    # Phase 2: delegate to match handler
                    self.cam_detection_his[cam_info['cam_ip']]['identified'] = True
                    self.match_handler.on_match(MatchEvent(
                        cam_info=cam_info,
                        raw_img=raw_img,
                        matched_faces=matched_faces,
                        detected=detected,
                        first_frame_at=self.cam_detection_his[cam_info['cam_ip']].get('first_frame_at', 0.0),
                    ))

                else:
                    time.sleep(float(os.environ['DETECTING_SLEEP_SEC']))

            except Exception as e:
                logger.error(f"Caught {self.name} runtime exception!")
                logger.error(e)
                traceback.print_exc()
                self.stop_event.set()

        while not self.cam_queue.empty():
            _, _, _ = self.cam_queue.get(False)

    @abstractmethod
    def process_frame(self, raw_img, cam_info, detected, age):
        """Run detection and matching on a single frame.

        Args:
            raw_img: Raw image (numpy array)
            cam_info: Camera info dict
            detected: Frame counter for this session
            age: Frame age in seconds

        Returns:
            List of (face, active_member, sim) tuples, or empty list if no match.
        """
        ...

    def stop_detection(self):
        logger.info(f"Stop face detector {self.name}")
        self.stop_event.set()
        if self.face_app is not None:
            del self.face_app

    @property
    def active_members(self):
        return self._active_members

    @active_members.setter
    def active_members(self, value):
        def get_member_hash(members):
            """Create a hash of member IDs and their embedding checksums."""
            if not members:
                return set()
            result = set()
            for m in members:
                key = (m.get('memberNo'), m.get('reservationCode'))
                emb = m.get('faceEmbedding', [])
                emb_sig = tuple(emb[:4]) if len(emb) > 0 else ()
                result.add((key, emb_sig))
            return result

        old_hash = get_member_hash(self._active_members)
        new_hash = get_member_hash(value)

        self._active_members = value

        if old_hash != new_hash:
            logger.info(f"active_members changed: {len(old_hash)} -> {len(new_hash)} members, rebuilding embeddings")
            self._build_member_embeddings()
        else:
            logger.debug(f"active_members unchanged ({len(new_hash)} members), skipping embedding rebuild")

    def _build_member_embeddings(self):
        """Pre-compute embeddings matrix and norms for vectorized comparison."""
        if not self.active_members:
            self.member_embeddings = np.empty((0, 512), dtype=np.float32)
            self.member_norms = np.empty(0, dtype=np.float32)
            logger.info("No active members - embeddings matrix empty")
            return

        embeddings_list = []
        for member in self.active_members:
            emb = np.array(member['faceEmbedding'], dtype=np.float32).ravel()
            embeddings_list.append(emb)

        self.member_embeddings = np.array(embeddings_list, dtype=np.float32)
        self.member_norms = np.linalg.norm(self.member_embeddings, axis=1)

        logger.info(f"Built embeddings matrix: {self.member_embeddings.shape[0]} members, {self.member_embeddings.shape[1]} dimensions")

        for i, member in enumerate(self.active_members):
            emb = self.member_embeddings[i]
            logger.debug(f"Stored embedding [{member.get('fullName', '?')}]: norm={self.member_norms[i]:.4f}, mean={emb.mean():.4f}, std={emb.std():.4f}")

    def find_match(self, face_embedding, threshold):
        """Vectorized face matching - find best matching member above threshold.

        Returns:
            Tuple of (matched_member, similarity, best_name) or (None, 0.0, None)
        """
        if self.member_embeddings.shape[0] == 0:
            return None, 0.0, None

        face_emb = np.array(face_embedding, dtype=np.float32).ravel()
        face_norm = np.linalg.norm(face_emb)

        if face_norm == 0:
            return None, 0.0, None

        similarities = np.dot(self.member_embeddings, face_emb) / (self.member_norms * face_norm)

        max_idx = np.argmax(similarities)
        max_sim = similarities[max_idx]
        best_name = self.active_members[max_idx].get('fullName', '?')

        if max_sim >= threshold:
            return self.active_members[max_idx], float(max_sim), best_name

        return None, float(max_sim), best_name

    def compute_sim(self, feat1, feat2):
        """Legacy method for single pairwise comparison (kept for compatibility)."""
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()
        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))
        return sim
