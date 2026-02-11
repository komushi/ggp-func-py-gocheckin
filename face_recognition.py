# import json
import logging
import time
from datetime import datetime, timezone, timedelta
import sys
import os
import threading
import traceback
import numpy as np

import gstreamer_threading as gst
import cv2

import gc

# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

class FaceRecognition(threading.Thread):
    def __init__(self, face_app, active_members, scanner_output_queue, cam_queue):
        super().__init__(name=f"Thread-Detector-{datetime.now(timezone(timedelta(hours=9))).strftime('%H:%M:%S.%f')}")

        #Current Cam
        self.cam_queue = cam_queue
        self.scanner_output_queue = scanner_output_queue
        self.stop_event = threading.Event()

        self.face_app = face_app
        self._active_members = None

        # Pre-compute embeddings matrix for vectorized comparison
        # (triggered automatically by the property setter)
        self.active_members = active_members

        self.captured_members = {}

        self.cam_detection_his = {}


    def run(self):

        logger.info(f"{self.name} started")

        #get all cams
        time.sleep(1)

        # while True:
        while not self.stop_event.is_set():
            try:

                if not self.cam_queue.empty():
                    cmd, raw_img, cam_info = self.cam_queue.get(False)

                    # Handle session end signal
                    if cmd == gst.StreamCommands.SESSION_END:
                        continue

                    if cam_info['cam_ip'] not in self.cam_detection_his:
                        self.cam_detection_his[cam_info['cam_ip']] = {}
                        self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                        self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                        self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = 0.0  # T1: timestamp of first frame processed
                    else:
                        if self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] != cam_info['detecting_txn']:
                            self.cam_detection_his[cam_info['cam_ip']]['detecting_txn'] = cam_info['detecting_txn']
                            self.cam_detection_his[cam_info['cam_ip']]['identified'] = False
                            self.cam_detection_his[cam_info['cam_ip']]['detected'] = 0
                            self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = 0.0  # T1: timestamp of first frame processed

                    # TEMP: Disabled for testing - continue detecting after recognition
                    # if self.cam_detection_his[cam_info['cam_ip']]['identified']:
                    #     continue

                    current_time = time.time()
                    age = current_time - float(cam_info['frame_time'])

                    if age > float(os.environ['AGE_DETECTING_SEC']):
                        logger.debug(f"{cam_info['cam_ip']} age: {age}")
                        continue
                    else:
                        self.cam_detection_his[cam_info['cam_ip']]['detected'] += 1
                        detected = self.cam_detection_his[cam_info['cam_ip']]['detected']
                        if detected == 1:
                            self.cam_detection_his[cam_info['cam_ip']]['first_frame_at'] = current_time  # T1: first frame processed
                        faces = self.face_app.get(raw_img)
                        duration = time.time() - current_time
                        # TODO: Temporarily log every frame for debugging, revert to "if detected == 1:" later
                        logger.debug(f"{cam_info['cam_ip']} detection frame #{detected} - age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)}")

                    # Phase 1: Match all faces, collect results
                    matched_faces = []
                    for face in faces:
                        # Log embedding stats for comparison with Hailo
                        emb = face.embedding
                        emb_norm = np.linalg.norm(emb)
                        logger.debug(f"InsightFace embedding: pre_norm={emb_norm:.4f}, mean={emb.mean():.4f}, std={emb.std():.4f}")

                        threshold = float(os.environ['FACE_THRESHOLD_INSIGHTFACE'])
                        active_member, sim, best_name = self.find_match(face.embedding, threshold)

                        if active_member is None:
                            logger.info(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} best_match: {best_name} best_sim: {sim:.4f} (no match)")
                            continue

                        logger.info(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} fullName: {active_member['fullName']} sim: {sim:.4f} (MATCH)")
                        matched_faces.append((face, active_member, sim))

                    if not matched_faces:
                        logger.debug(f"{cam_info['cam_ip']} detected: {detected} age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)} matched: {len(matched_faces)}")
                        continue  # back to outer while loop — no matches this frame

                    # Phase 2: Build composite snapshot + single queue entry
                    self.cam_detection_his[cam_info['cam_ip']]['identified'] = True

                    date_folder = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
                    time_filename = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
                    ext = ".jpg"

                    local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder, time_filename + ext)
                    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

                    # Draw ALL bounding boxes on one image
                    img = raw_img.astype(np.uint8)
                    for face, active_member, sim in matched_faces:
                        bbox = face.bbox.astype(int)
                        cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                        cv2.putText(img, f"{active_member['fullName']}:{str(round(sim, 2))}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36,255,12), 2)
                    cv2.imwrite(local_file_path, img)
                    logger.debug(f"Snapshot taken at {local_file_path} with {len(matched_faces)} face(s)")

                    # Build per-member payloads
                    members_data = []
                    for face, active_member, sim in matched_faces:
                        memberKey = f"{active_member['reservationCode']}-{active_member['memberNo']}"
                        keyNotified = active_member.get('keyNotified', False)
                        checkin_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/listings/{active_member['listingId']}/{active_member['reservationCode']}/checkIn/{str(active_member['memberNo'])}{ext}"""
                        property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""

                        member_payload = {
                            "hostId": os.environ['HOST_ID'],
                            "propertyCode": os.environ['PROPERTY_CODE'],
                            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                            "coreName": os.environ['AWS_IOT_THING_NAME'],
                            "assetId": cam_info['cam_uuid'],
                            "assetName": cam_info['cam_name'],
                            "cameraIp": cam_info['cam_ip'],
                            "reservationCode": active_member['reservationCode'],
                            "listingId": active_member['listingId'],
                            "memberNo": int(str(active_member['memberNo'])),
                            "fullName": active_member['fullName'],
                            "similarity": sim,
                            "recordTime": datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                            "checkInImgKey": checkin_object_key,
                            "propertyImgKey": property_object_key,
                            "keyNotified": keyNotified,
                        }

                        self.captured_members[memberKey] = member_payload
                        members_data.append({"memberKey": memberKey, "payload": member_payload, "keyNotified": keyNotified})

                    # Single queue entry with all members
                    snapshot_key = f"""{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_info['cam_ip']}/{date_folder}/{time_filename}{ext}"""
                    snapshot_payload = {
                        "hostId": os.environ['HOST_ID'],
                        "propertyCode": os.environ['PROPERTY_CODE'],
                        "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                        "coreName": os.environ['AWS_IOT_THING_NAME'],
                        "assetId": cam_info['cam_uuid'],
                        "assetName": cam_info['cam_name'],
                        "cameraIp": cam_info['cam_ip'],
                        "recordStart": members_data[0]['payload']['recordTime'],
                        "recordEnd": members_data[0]['payload']['recordTime'],
                        "identityId": os.environ['IDENTITY_ID'],
                        "s3level": 'private',
                        "videoKey": '',
                        "snapshotKey": snapshot_key
                    }

                    if not self.scanner_output_queue.full():
                        self.scanner_output_queue.put({
                            "type": "member_detected",
                            "members": members_data,
                            "cam_ip": cam_info['cam_ip'],
                            "detecting_txn": cam_info['detecting_txn'],
                            "local_file_path": local_file_path,
                            "property_object_key": members_data[0]['payload']['propertyImgKey'],
                            "snapshot_payload": snapshot_payload,
                            "first_frame_at": self.cam_detection_his[cam_info['cam_ip']].get('first_frame_at', 0.0),  # T1: for timing measurement
                        }, block=False)

                else:
                    time.sleep(float(os.environ['DETECTING_SLEEP_SEC']))

            except Exception as e:
                logger.error(f"Caught {self.name} runtime exception!")
                logger.error(e)
                traceback.print_exc()
                self.stop_event.set()

        while not self.cam_queue.empty():
                _, _, _ = self.cam_queue.get(False)
    
    def stop_detection(self):
        logger.info(f"Stop face detector {self.name}")
        self.stop_event.set()
        if self.face_app is not None:
            del self.face_app  # Remove reference

    @property
    def active_members(self):
        return self._active_members

    @active_members.setter
    def active_members(self, value):
        # Only rebuild embeddings if members or their embeddings actually changed
        def get_member_hash(members):
            """Create a hash of member IDs and their embedding checksums."""
            if not members:
                return set()
            result = set()
            for m in members:
                key = (m.get('memberNo'), m.get('reservationCode'))
                # Include first few embedding values as a quick change detector
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

        # Stack all embeddings into a matrix (N x 512)
        embeddings_list = []
        for member in self.active_members:
            emb = np.array(member['faceEmbedding'], dtype=np.float32).ravel()
            embeddings_list.append(emb)

        self.member_embeddings = np.array(embeddings_list, dtype=np.float32)
        self.member_norms = np.linalg.norm(self.member_embeddings, axis=1)

        logger.info(f"Built embeddings matrix: {self.member_embeddings.shape[0]} members, {self.member_embeddings.shape[1]} dimensions")

    def find_match(self, face_embedding, threshold):
        """
        Vectorized face matching - find best matching member above threshold.

        Args:
            face_embedding: Face embedding vector (512-dim)
            threshold: Minimum similarity threshold

        Returns:
            Tuple of (matched_member, similarity, best_name) or (None, 0.0, None) if no match
        """
        if self.member_embeddings.shape[0] == 0:
            return None, 0.0, None

        # Normalize face embedding
        face_emb = np.array(face_embedding, dtype=np.float32).ravel()
        face_norm = np.linalg.norm(face_emb)

        if face_norm == 0:
            return None, 0.0, None

        # Vectorized cosine similarity: dot(embeddings, face) / (norms * face_norm)
        similarities = np.dot(self.member_embeddings, face_emb) / (self.member_norms * face_norm)

        # Find best match
        max_idx = np.argmax(similarities)
        max_sim = similarities[max_idx]
        best_name = self.active_members[max_idx]['fullName']

        if max_sim >= threshold:
            return self.active_members[max_idx], float(max_sim), best_name

        return None, float(max_sim), best_name

    def compute_sim(self, feat1, feat2):
        """Legacy method for single pairwise comparison (kept for compatibility)."""
        feat1 = feat1.ravel()
        feat2 = feat2.ravel()
        sim = np.dot(feat1, feat2) / (np.linalg.norm(feat1) * np.linalg.norm(feat2))
        return sim

