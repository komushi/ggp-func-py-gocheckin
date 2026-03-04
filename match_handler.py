import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Tuple, Any, Dict, Optional, Set
import threading

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
    unmatched_faces: List[Tuple[Any, str, float]]  # list of (face, best_name, best_sim) - for UC3
    detected: int
    first_frame_at: float
    person_count: int = 0  # UC8 continuous person count
    max_simultaneous_persons: int = 0  # UC8 max persons in any single frame


@dataclass
class SessionState:
    """Per-camera session state for UC2 (tailgating) and UC4 (group size)."""
    cam_ip: str
    detecting_txn: str
    unlocked: bool = False  # UC2: True after first UC1 unlock
    unlock_count: int = 0  # UC2: Number of unlock events
    first_unlock_at: float = 0.0  # UC2: Timestamp of first unlock
    distinct_members: Set[str] = field(default_factory=set)  # UC4: Unique member keys seen
    person_count_history: List[int] = field(default_factory=list)  # UC8: Person counts per frame
    max_simultaneous_persons: int = 0  # UC4/UC8: Peak person count in any single frame
    block_further_unlocks: bool = False  # UC5: Set True when blocklist member detected
    session_started_at: float = field(default_factory=time.time)


# Global session state cache
_session_states: Dict[str, SessionState] = {}
_session_lock = threading.Lock()


def get_session_state(cam_ip: str, detecting_txn: str) -> SessionState:
    """Get or create session state for a camera session."""
    key = f"{cam_ip}:{detecting_txn}"
    with _session_lock:
        if key not in _session_states or _session_states[key].detecting_txn != detecting_txn:
            _session_states[key] = SessionState(cam_ip=cam_ip, detecting_txn=detecting_txn)
        return _session_states[key]


def clear_session_state(cam_ip: str, detecting_txn: str):
    """Clear session state at session end."""
    key = f"{cam_ip}:{detecting_txn}"
    with _session_lock:
        if key in _session_states:
            del _session_states[key]


class MatchHandler:
    """Base class for match handlers with UC extension points."""

    def on_match(self, event: MatchEvent):
        """Called when face(s) match active member(s)."""
        pass

    def on_no_match(self, event: MatchEvent):
        """Called when face(s) detected but no match found (UC3 - unknown faces)."""
        pass

    def on_session_end(self, cam_ip: str, detecting_txn: str, final_state: SessionState):
        """Called at session end with accumulated state (UC4 - group size check)."""
        pass


class DefaultMatchHandler(MatchHandler):
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


class SecurityHandlerChain(MatchHandler):
    """UC1-UC5 handler chain with priority-based execution.

    Priority order (lower = higher priority):
    1. UC1: Member identification + unlock (priority 10)
    2. UC5: Blocklist check (priority 5) - checked before UC1
    3. UC2: Tailgating detection (priority 30)
    4. UC5: Non-active member alert (priority 20)
    5. UC3: Unknown face logging (priority 40)

    Session-level handlers (on_session_end):
    - UC4: Group size validation
    """

    def __init__(self, scanner_output_queue, get_uc_toggles_fn=None):
        """
        Args:
            scanner_output_queue: Queue for publishing IoT events
            get_uc_toggles_fn: Function(cam_ip) -> dict of UC toggles
        """
        self.scanner_output_queue = scanner_output_queue
        self.get_uc_toggles_fn = get_uc_toggles_fn
        self.default_match_handler = DefaultMatchHandler(scanner_output_queue)

    def _is_uc_enabled(self, cam_ip: str, uc_field: str) -> bool:
        """Check if a UC is enabled for a camera."""
        if not self.get_uc_toggles_fn:
            return True  # Default to enabled if no toggle function
        toggles = self.get_uc_toggles_fn(cam_ip)
        return toggles.get(uc_field, True)

    def on_match(self, event: MatchEvent):
        """Process matched faces through UC1-UC5 handler chain."""
        cam_ip = event.cam_info.get('cam_ip')
        detecting_txn = event.cam_info.get('detecting_txn')

        # Get session state for UC2/UC4/UC5
        session = get_session_state(cam_ip, detecting_txn)

        # Get UC toggles
        uc1_uc2_enabled = self._is_uc_enabled(cam_ip, 'uc1_uc2_enabled')
        uc3_enabled = self._is_uc_enabled(cam_ip, 'uc3_enabled')
        uc5_enabled = self._is_uc_enabled(cam_ip, 'uc5_enabled')

        # Track person count for UC4/UC8
        if event.person_count > 0:
            session.person_count_history.append(event.person_count)
            if event.max_simultaneous_persons > session.max_simultaneous_persons:
                session.max_simultaneous_persons = event.max_simultaneous_persons

        # Process each matched face through handlers
        for face, active_member, sim in event.matched_faces:
            member_key = f"{active_member['reservationCode']}-{active_member['memberNo']}"

            # UC5: Check blocklist first (highest priority)
            if uc5_enabled:
                member_type = active_member.get('memberType', 'active')
                if member_type == 'blocklist':
                    self._handle_uc5_blocklist(event, active_member, session)
                    continue  # Skip further processing for blocklist

            # UC1: Member identification + unlock
            if uc1_uc2_enabled:
                self._handle_uc1_member(event, active_member, sim, session)

            # UC2: Tailgating detection (after unlock)
            if uc1_uc2_enabled and session.unlocked:
                self._handle_uc2_tailgating(event, active_member, session)

            # UC5: Non-active member alert
            if uc5_enabled:
                self._handle_uc5_non_active(event, active_member, session)

            # Track distinct members for UC4
            session.distinct_members.add(member_key)

        # UC3: Handle unmatched faces (unknown faces)
        if uc3_enabled and event.unmatched_faces:
            self._handle_uc3_unknown(event)

        # Default handler: snapshot and queue
        if event.matched_faces:
            self.default_match_handler.on_match(event)

    def _handle_uc1_member(self, event: MatchEvent, active_member: dict, sim: float, session: SessionState):
        """UC1: Member identification - trigger unlock on P2 cameras."""
        cam_ip = event.cam_info.get('cam_ip')
        member_key = f"{active_member['reservationCode']}-{active_member['memberNo']}"

        # Check if this is a P2 camera (has locks)
        camera_locks = event.cam_info.get('locks', {})
        is_p2_camera = len(camera_locks) > 0

        if not is_p2_camera:
            # P1 camera: log only, no unlock
            logger.info(f"{cam_ip} UC1: Member {active_member['fullName']} detected (P1 - log only)")
            return

        # P2 camera: trigger unlock if clicked lock exists
        # The unlock logic is handled by the TypeScript side via member_detected event
        if not session.unlocked:
            session.unlocked = True
            session.unlock_count = 1
            session.first_unlock_at = event.first_frame_at
            logger.info(f"{cam_ip} UC1: First unlock triggered by {active_member['fullName']} (sim: {sim:.4f})")
        else:
            logger.debug(f"{cam_ip} UC1: Additional match for {active_member['fullName']} (already unlocked)")

        # Publish member_detected event (handled by DefaultMatchHandler)

    def _handle_uc2_tailgating(self, event: MatchEvent, active_member: dict, session: SessionState):
        """UC2: Tailgating detection - alert if multiple members after unlock."""
        cam_ip = event.cam_info.get('cam_ip')

        # Tailgating: multiple distinct members detected after unlock
        # This is a simple version - can be enhanced with person count from UC8
        if session.unlock_count >= 1 and len(session.distinct_members) > 1:
            # Check if this is a new member (not the one who unlocked)
            member_key = f"{active_member['reservationCode']}-{active_member['memberNo']}"

            # Alert if different member detected after unlock
            self._publish_tailgating_alert(cam_ip, active_member, session)

    def _publish_tailgating_alert(self, cam_ip: str, active_member: dict, session: SessionState):
        """Publish tailgating alert to IoT."""
        alert_payload = {
            "hostId": os.environ['HOST_ID'],
            "propertyCode": os.environ['PROPERTY_CODE'],
            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
            "coreName": os.environ['AWS_IOT_THING_NAME'],
            "assetId": session.cam_ip,
            "cameraIp": cam_ip,
            "alertType": "tailgating",
            "memberName": active_member.get('fullName', 'Unknown'),
            "reservationCode": active_member.get('reservationCode', ''),
            "unlockCount": session.unlock_count,
            "distinctMembers": len(session.distinct_members),
            "recordTime": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        }

        logger.warning(f"{cam_ip} UC2: TAILGATING ALERT - {alert_payload['memberName']} detected after unlock")

        # Publish to IoT queue
        if not self.scanner_output_queue.full():
            self.scanner_output_queue.put({
                "type": "tailgating_alert",
                "cam_ip": cam_ip,
                "payload": alert_payload,
            }, block=False)

    def _handle_uc5_blocklist(self, event: MatchEvent, active_member: dict, session: SessionState):
        """UC5: Blocklist member detected - prevent further unlocks."""
        cam_ip = event.cam_info.get('cam_ip')

        session.block_further_unlocks = True

        alert_payload = {
            "hostId": os.environ['HOST_ID'],
            "propertyCode": os.environ['PROPERTY_CODE'],
            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
            "coreName": os.environ['AWS_IOT_THING_NAME'],
            "assetId": session.cam_ip,
            "cameraIp": cam_ip,
            "alertType": "blocklist_member",
            "memberName": active_member.get('fullName', 'Unknown'),
            "reservationCode": active_member.get('reservationCode', ''),
            "memberNo": active_member.get('memberNo', ''),
            "recordTime": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        }

        logger.warning(f"{cam_ip} UC5: BLOCKLIST member {active_member['fullName']} detected - blocking further unlocks")

        if not self.scanner_output_queue.full():
            self.scanner_output_queue.put({
                "type": "blocklist_alert",
                "cam_ip": cam_ip,
                "payload": alert_payload,
            }, block=False)

    def _handle_uc5_non_active(self, event: MatchEvent, active_member: dict, session: SessionState):
        """UC5: Non-active member (expired reservation) alert."""
        cam_ip = event.cam_info.get('cam_ip')

        # Check if member is non-active (expired)
        # This would be set by the fetch_members logic
        is_active = active_member.get('isActive', True)

        if not is_active:
            alert_payload = {
                "hostId": os.environ['HOST_ID'],
                "propertyCode": os.environ['PROPERTY_CODE'],
                "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
                "coreName": os.environ['AWS_IOT_THING_NAME'],
                "assetId": session.cam_ip,
                "cameraIp": cam_ip,
                "alertType": "non_active_member",
                "memberName": active_member.get('fullName', 'Unknown'),
                "reservationCode": active_member.get('reservationCode', ''),
                "memberNo": active_member.get('memberNo', ''),
                "recordTime": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            }

            logger.warning(f"{cam_ip} UC5: NON-ACTIVE member {active_member['fullName']} detected")

            if not self.scanner_output_queue.full():
                self.scanner_output_queue.put({
                    "type": "non_active_alert",
                    "cam_ip": cam_ip,
                    "payload": alert_payload,
                }, block=False)

    def _handle_uc3_unknown(self, event: MatchEvent):
        """UC3: Unknown face logging - save to S3 and publish alert."""
        cam_ip = event.cam_info.get('cam_ip')
        detecting_txn = event.cam_info.get('detecting_txn')

        date_folder = datetime.fromtimestamp(float(event.cam_info['frame_time']), timezone.utc).strftime("%Y-%m-%d")
        time_filename = datetime.fromtimestamp(float(event.cam_info['frame_time']), timezone.utc).strftime("%H:%M:%S")
        ext = ".jpg"

        local_file_path = os.path.join(os.environ['VIDEO_CLIPPING_LOCATION'], cam_ip, date_folder, time_filename + ext)
        os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

        # Draw bounding boxes on unmatched faces
        img = event.raw_img.astype(np.uint8)
        for face, best_name, best_sim in event.unmatched_faces:
            bbox = face.bbox.astype(int)
            cv2.rectangle(img, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 0, 255), 2)  # Red for unknown
            cv2.putText(img, f"Unknown:{best_sim:.2f}", (bbox[0], bbox[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        cv2.imwrite(local_file_path, img)
        logger.debug(f"UC3: Unknown face snapshot at {local_file_path}")

        # Build unknown face payload
        property_object_key = f"""private/{os.environ['IDENTITY_ID']}/{os.environ['HOST_ID']}/properties/{os.environ['PROPERTY_CODE']}/{os.environ['AWS_IOT_THING_NAME']}/{cam_ip}/{date_folder}/{time_filename}{ext}"""

        alert_payload = {
            "hostId": os.environ['HOST_ID'],
            "propertyCode": os.environ['PROPERTY_CODE'],
            "hostPropertyCode": f"{os.environ['HOST_ID']}-{os.environ['PROPERTY_CODE']}",
            "coreName": os.environ['AWS_IOT_THING_NAME'],
            "assetId": event.cam_info['cam_uuid'],
            "assetName": event.cam_info['cam_name'],
            "cameraIp": cam_ip,
            "alertType": "unknown_face",
            "unknownFaceCount": len(event.unmatched_faces),
            "bestMatch": event.unmatched_faces[0][1] if event.unmatched_faces else None,
            "bestSimilarity": float(event.unmatched_faces[0][2]) if event.unmatched_faces else 0,
            "recordTime": datetime.fromtimestamp(float(event.cam_info['frame_time']), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            "unknownFaceImgKey": property_object_key,
            "detecting_txn": detecting_txn,
        }

        logger.info(f"{cam_ip} UC3: Unknown face detected (best: {alert_payload['bestMatch']}, sim: {alert_payload['bestSimilarity']:.4f})")

        if not self.scanner_output_queue.full():
            self.scanner_output_queue.put({
                "type": "unknown_face",
                "cam_ip": cam_ip,
                "payload": alert_payload,
                "local_file_path": local_file_path,
                "property_object_key": property_object_key,
            }, block=False)

    def on_no_match(self, event: MatchEvent):
        """Called when no faces match - delegates to UC3 handler."""
        if event.unmatched_faces:
            self._handle_uc3_unknown(event)

    def on_session_end(self, cam_ip: str, detecting_txn: str, final_state: SessionState):
        """UC4: Group size validation at session end."""
        # Get UC4 toggle
        uc4_uc8_enabled = self._is_uc_enabled(cam_ip, 'uc4_uc8_enabled')

        if not uc4_uc8_enabled:
            return

        # Get member count from reservation (would need to be passed in)
        # For now, we'll just log the session stats
        session_duration = (time.time() - final_state.session_started_at) * 1000 if final_state.session_started_at > 0 else 0

        logger.info(f"{cam_ip} UC4 Session End - distinct_members: {len(final_state.distinct_members)}, "
                    f"max_simultaneous_persons: {final_state.max_simultaneous_persons}, "
                    f"unlock_count: {final_state.unlock_count}, duration: {session_duration:.0f}ms")

        # UC4: Check group size mismatch (would compare against memberCount from reservation)
        # This requires access to reservation data - implement when needed


def on_session_end(cam_ip: str, detecting_txn: str, match_handler: MatchHandler):
    """Convenience function to handle session end."""
    session = get_session_state(cam_ip, detecting_txn)
    if isinstance(match_handler, MatchHandler):
        match_handler.on_session_end(cam_ip, detecting_txn, session)
    clear_session_state(cam_ip, detecting_txn)
