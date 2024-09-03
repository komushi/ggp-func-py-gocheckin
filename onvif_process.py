from urllib.parse import urlparse

import sys
import logging
import traceback

from zeep import Client, xsd
from zeep.transports import Transport
import xml.etree.ElementTree as ET

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

def extract_notification(raw_payload):
    # Parse the XML content
    root = ET.fromstring(raw_payload)
    
    # Extract the topic
    topic_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}Topic")
    if topic_element is not None:
        topic = topic_element.text
        if topic == "tns1:RuleEngine/CellMotionDetector/Motion":

            # Extract SubscriptionReference Address and get the host/IP
            address_element = root.find(".//{http://www.w3.org/2005/08/addressing}Address")
            address = address_element.text if address_element is not None else None
            
            # Extract IsMotion as a boolean
            is_motion_element = root.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='IsMotion']")
            is_motion_value = is_motion_element.get('Value').lower() == 'true' if is_motion_element is not None else None
            
            # Extract UtcTime
            utc_time_element = root.find(".//{http://www.onvif.org/ver10/schema}Message")
            utc_time = utc_time_element.get('UtcTime') if utc_time_element is not None else None
            
            # Log the extracted values

            ip_address = urlparse(address).hostname

            return ip_address, utc_time, is_motion_value

class OnvifAdapter():
    def __init__(self):
        logger.info(f"OnvifAdapter init")


            