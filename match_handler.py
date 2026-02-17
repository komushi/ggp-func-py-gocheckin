import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Tuple, Any

import cv2
import numpy as np

if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class MatchEvent:
    """Data passed from detection thread to match handler."""
    cam_info: dict
    raw_img: np.ndarray
    matched_faces: List[Tuple[Any, dict, float]]  # list of (face, active_member, sim)
    detected: int
    first_frame_at: float


class DefaultMatchHandler:
    """Phase 2 business logic: snapshot, S3 keys, queue entry."""

    def __init__(self, scanner_output_queue):
        self.scanner_output_queue = scanner_output_queue
        self.captured_members = {}

    def on_match(self, event: MatchEvent):
        """Process a match event: draw bboxes, build payloads, enqueue."""
        cam_info = event.cam_info
        matched_faces = event.matched_faces

        date_folder = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
        time_filename = datetime.fromtimestamp(float(cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
        ext = ".jpg"

        local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_info['cam_ip'], date_folder, time_filename + ext)
        os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

        # Draw ALL bounding boxes on one image
        img = event.raw_img.astype(np.uint8)
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
                "first_frame_at": event.first_frame_at,
            }, block=False)
