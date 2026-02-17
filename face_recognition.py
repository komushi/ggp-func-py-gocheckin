# import json
import logging
import time
import sys
import os

import numpy as np

# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

from face_recognition_base import FaceRecognitionBase


class FaceRecognition(FaceRecognitionBase):
    THREAD_NAME_PREFIX = "Thread-Detector"

    def process_frame(self, raw_img, cam_info, detected, age):
        current_time = time.time()
        faces = self.face_app.get(raw_img)
        duration = time.time() - current_time
        logger.debug(f"{cam_info['cam_ip']} detection frame #{detected} - age: {age:.3f} duration: {duration:.3f} face(s): {len(faces)}")

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

        return matched_faces
