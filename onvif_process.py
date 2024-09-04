from urllib.parse import urlparse

import sys
import logging
import traceback

from zeep import Client, xsd
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

from requests import Session

import xml.etree.ElementTree as ET

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

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
        
def subscribe(camera_item, scanner_local_ip, http_port):
    logger.info(f"onvif.subscribe in camera_item {camera_item}, scanner_local_ip {scanner_local_ip}")

    try:
        server_ip = camera_item['localIp']
        server_port = camera_item['onvif']['port']
        user = camera_item['username']
        password = camera_item['password']
        service_url = '%s:%s/onvif/Events' % \
                        (server_ip if (server_ip.startswith('http://') or server_ip.startswith('https://'))
                        else 'http://%s' % server_ip, server_port)
        
        wsdl_file = './wsdl/events.wsdl'

        notification_binding = '{http://www.onvif.org/ver10/events/wsdl}NotificationProducerBinding'
        
        logger.info(f"service_url: {service_url}, wsdl_file: {wsdl_file}, notification_binding: {notification_binding}")

        # Create a session to handle authentication
        session = Session()
        session.auth = (user, password)

        wsse = UsernameToken(username=user, password=password, use_digest=True)
        # logger.info(f"onvif.subscribe wsse {wsse}")

        # Create a Zeep client using the local WSDL file
        client = Client(wsdl_file, wsse=wsse, transport=Transport(session=session))
        # logger.info(f"onvif.subscribe client {client}")

        notification_service = client.create_service(notification_binding, service_url)
        # logger.info(f"onvif.subscribe notification_service {notification_service}")

        # Get the EndpointReferenceType
        address_type = client.get_element('{http://www.w3.org/2005/08/addressing}EndpointReference')
        # logger.info(f"onvif.subscribe address_type {address_type}")

        # Create the consumer reference
        consumer_reference = address_type(Address=f"http://{scanner_local_ip}:{http_port}/onvif_notifications")
        # logger.info(f"onvif.subscribe consumer_reference {consumer_reference}")

        subscription = notification_service.Subscribe(ConsumerReference=consumer_reference, InitialTerminationTime='PT1D')
        # logger.info(f"onvif.subscribe subscription {subscription}")

        logger.info(f"onvif.subscribe out onvif_sub_address {subscription.SubscriptionReference.Address._value_1}")

    except Exception as e:
        logger.error(f"onvif.subscribe, Exception during running, Error: {e}")
        traceback.print_exc()

    return subscription.SubscriptionReference.Address._value_1

def unsubscribe(camera_item):
    logger.info(f"onvif.unsubscribe in camera_item {camera_item}")

    try:
        server_ip = camera_item['localIp']
        server_port = camera_item['onvif']['port']
        user = camera_item['username']
        password = camera_item['password']
        onvif_sub_address = camera_item['onvifSubAddress']
        service_url = '%s:%s/onvif/Events' % \
                        (server_ip if (server_ip.startswith('http://') or server_ip.startswith('https://'))
                        else 'http://%s' % server_ip, server_port)
        
        wsdl_file = './wsdl/events.wsdl'

        subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'
        
        logger.info(f"service_url: {service_url}, wsdl_file: {wsdl_file}, subscription_binding: {subscription_binding}")

        # Create a session to handle authentication
        session = Session()
        session.auth = (user, password)

        wsse = UsernameToken(username=user, password=password, use_digest=True)
        # logger.info(f"onvif.subscribe wsse {wsse}")

        # Create a Zeep client using the local WSDL file
        client = Client(wsdl_file, wsse=wsse, transport=Transport(session=session))
        # logger.info(f"onvif.subscribe client {client}")

        subscription_service = client.create_service(subscription_binding, service_url)
        # logger.info(f"onvif.subscribe subscription_service {subscription_service}")

        addressing_header_type = xsd.ComplexType(
            xsd.Sequence([
                xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
            ])
        )

        addressing_header = addressing_header_type(To=onvif_sub_address)

        result = subscription_service.Unsubscribe(_soapheaders=[addressing_header])

        logger.info(f"onvif.unsubscribe result {result}")


    except Exception as e:
        logger.error(f"onvif.unsubscribe, Exception during running, Error: {e}")
        traceback.print_exc()
        pass
