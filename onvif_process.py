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
        
def subscribe(camera_item, local_ip):
    server_ip = camera_item['localIp']
    server_port = camera_item['rtsp']['port']
    user = camera_item['username']
    password = camera_item['password']
    service_url = '%s:%s/onvif/Events' % \
                    (server_ip if (server_ip.startswith('http://') or server_ip.startswith('https://'))
                     else 'http://%s' % server_ip, server_port)
    
    wsdl_file = './wsdl/events.wsdl'

    notification_binding = '{http://www.onvif.org/ver10/events/wsdl}NotificationProducerBinding'
    subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'
    
    logger.info(f"service_url: {service_url}, wsdl_file: {wsdl_file}, subscription_binding: {subscription_binding}, notification_binding: {notification_binding}")

    # Create a session to handle authentication
    session = Session()
    session.auth = (user, password)

    wsse = UsernameToken(username=user, password=password, use_digest=True)

    # Create a Zeep client using the local WSDL file
    client = Client(wsdl_file, wsse=wsse, transport=Transport(session=session))

    notification_service = client.create_service(notification_binding, service_url)
    subscription_service = client.create_service(subscription_binding, service_url)

    # Get the EndpointReferenceType
    address_type = client.get_element('{http://www.w3.org/2005/08/addressing}EndpointReference')
    # print(f"address_type {address_type}")

    # Create the consumer reference
    consumer_reference = address_type(Address=f"http://{local_ip}:7788/onvif_notifications")
    # print(f"consumer_reference {consumer_reference}")

    subscription = notification_service.Subscribe(ConsumerReference=consumer_reference, InitialTerminationTime='PT1H')

    addressing_header_type = xsd.ComplexType(
        xsd.Sequence([
            xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
        ])
    )

    addressing_header = addressing_header_type(To=subscription.SubscriptionReference.Address._value_1)

class OnvifAdapter():
    def __init__(self):
        logger.info(f"OnvifAdapter init")


            