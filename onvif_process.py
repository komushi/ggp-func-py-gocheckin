from urllib.parse import urlparse

import sys
import logging
import traceback
import threading

from zeep import Client, xsd
from zeep.helpers import serialize_object
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken

from requests import Session

import xml.etree.ElementTree as ET

# Setup logging to stdout
logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

thread_pullpoints = {}

class OnvifConnector():
    def __init__(self, camera_item):
        server_ip = camera_item['localIp']
        server_port = camera_item['onvif']['port']
        user = camera_item['username']
        password = camera_item['password']
        self.service_url = '%s:%s/onvif/Events' % \
                        (server_ip if (server_ip.startswith('http://') or server_ip.startswith('https://'))
                        else 'http://%s' % server_ip, server_port)
        
        wsdl_file = './wsdl/events.wsdl'

        # Create a session to handle authentication
        session = Session()
        session.auth = (user, password)

        wsse = UsernameToken(username=user, password=password, use_digest=True)
        # logger.info(f"onvif.subscribe wsse {wsse}")

        # Create a Zeep client using the local WSDL file
        self.client = Client(wsdl_file, wsse=wsse, transport=Transport(session=session))
        # logger.info(f"onvif.subscribe client {client}")

    @staticmethod
    def extract_notification(raw_payload):
        logger.debug(f"onvif.extract_notification in")

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

                logger.debug(f"onvif.extract_notification out ip_address {ip_address} is_motion_value {is_motion_value} utc_time {utc_time}")
                return ip_address, utc_time, is_motion_value
            
        logger.debug(f"onvif.extract_notification out None")
        return None, None, None
        
    def subscribe(self, camera_item, scanner_local_ip, http_port):

        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']
        else:
            onvif_sub_address = None

        logger.info(f"onvif.subscribe in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")

        try:

            notification_binding = '{http://www.onvif.org/ver10/events/wsdl}NotificationProducerBinding'

            notification_service = self.client.create_service(notification_binding, self.service_url)
            # logger.info(f"onvif.subscribe notification_service {notification_service}")

            # Get the EndpointReferenceType
            address_type = self.client.get_element('{http://www.w3.org/2005/08/addressing}EndpointReference')
            # logger.info(f"onvif.subscribe address_type {address_type}")

            # Create the consumer reference
            consumer_reference = address_type(Address=f"http://{scanner_local_ip}:{http_port}/onvif_notifications")
            logger.info(f"onvif.subscribe consumer_reference {consumer_reference}")

            subscription = notification_service.Subscribe(ConsumerReference=consumer_reference, InitialTerminationTime='PT1H')
            # logger.info(f"onvif.subscribe subscription {subscription}")

            onvif_sub_address = subscription.SubscriptionReference.Address._value_1

        except Exception as e:
            logger.error(f"onvif.subscribe, Exception during running, cam_ip: {camera_item['localIp']} Error: {e}")
            # traceback.print_exc()
        finally:
            logger.info(f"onvif.subscribe out cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")
            return onvif_sub_address


    def unsubscribe(self, camera_item):

        onvif_sub_address = None
        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']

        if onvif_sub_address is None:
            logger.info(f"onvif.unsubscribe in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")
            logger.info(f"onvif.unsubscribe out cam_ip: {camera_item['localIp']}")

            return

        logger.info(f"onvif.unsubscribe in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")

        try:
            subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'
            
            subscription_service = self.client.create_service(subscription_binding, self.service_url)

            addressing_header_type = xsd.ComplexType(
                xsd.Sequence([
                    xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
                ])
            )

            addressing_header = addressing_header_type(To=onvif_sub_address)

            logger.info(f"onvif.unsubscribe addressing_header: {addressing_header}")

            result = subscription_service.Unsubscribe(_soapheaders=[addressing_header])

            logger.info(f"onvif.unsubscribe cam_ip: {camera_item['localIp']} result: {result}")


        except Exception as e:
            logger.error(f"onvif.unsubscribe, Exception during running, cam_ip: {camera_item['localIp']} Error: {e}")
            traceback.print_exc()
            pass


    def renew(self, camera_item):

        result = None
        
        onvif_sub_address = None
        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']

        if onvif_sub_address is None:
            logger.info(f"onvif.renew in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")
            logger.info(f"onvif.renew out cam_ip: {camera_item['localIp']} result: {result}")

            return

        logger.info(f"onvif.renew in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")

        try:
            subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'

            subscription_service = self.client.create_service(subscription_binding, self.service_url)

            addressing_header_type = xsd.ComplexType(
                xsd.Sequence([
                    xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
                ])
            )

            addressing_header = addressing_header_type(To=onvif_sub_address)

            result = subscription_service.Renew(_soapheaders=[addressing_header], TerminationTime='PT1H')

        except Exception as e:
            logger.error(f"onvif.renew, Exception during running, cam_ip: {camera_item['localIp']} Error: {e}")
            traceback.print_exc()
        finally:
            logger.info(f"onvif.renew out cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")
            # logger.info(f"onvif.renew out cam_ip: {camera_item['localIp']} result: {result}")

            return result


    def start_pullpoint(self, camera_item, motion_detection_queue):

        def pull_messages(ip_address, motion_detection_queue):
        
            while True:
                try:
                    pullmess = pullpoint_service.PullMessages(Timeout='PT1M', MessageLimit=10)
                    for msg in pullmess.NotificationMessage:

                        message = serialize_object(msg)

                        message_element = message['Message']['_value_1']

                        utc_time = None
                        is_motion = None
                        for simple_item in message_element.findall(".//ns0:SimpleItem", namespaces={'ns0': 'http://www.onvif.org/ver10/schema'}):
                            if simple_item.attrib.get('Name') == "IsMotion":
                                is_motion = simple_item.attrib.get('Value')
                                utc_time = message_element.attrib.get('UtcTime')
                                motion_detection_queue.put((ip_address, is_motion == 'true', utc_time), block=False)
                                break

                        if utc_time is not None and is_motion is not None:
                            logger.info(f"onvif.start_pullpoint.pull_messages Motion detected: utc_time: {utc_time} is_motion: {is_motion}")

                except Exception as e:
                    pass

        global thread_pullpoints

        onvif_sub_address = None
        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']
        else:
            onvif_sub_address = None
            
        cam_ip = None
        if 'localIp' in camera_item:
            cam_ip = camera_item['localIp']
        else:
            cam_ip = None

        logger.info(f"onvif.start_pullpoint in cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

        try:

            pullpoint_subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}PullPointSubscriptionBinding'
            event_binding = '{http://www.onvif.org/ver10/events/wsdl}EventBinding'

            event_service = self.client.create_service(event_binding, self.service_url)

            subscription = event_service.CreatePullPointSubscription(InitialTerminationTime='PT24H')

            pullpoint_service = self.client.create_service(pullpoint_subscription_binding, subscription.SubscriptionReference.Address._value_1)

            onvif_sub_address = subscription.SubscriptionReference.Address._value_1


        except Exception as e:
            logger.error(f"onvif.start_pullpoint, Exception during running, cam_ip: {cam_ip} Error: {e}")
            # traceback.print_exc()
            onvif_sub_address = None

            return onvif_sub_address

        if cam_ip in thread_pullpoints:
            if thread_pullpoints[cam_ip] is not None:
                if thread_pullpoints[cam_ip].is_alive():
                    logger.error(f"onvif.start_pullpoint, out, cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

                    return onvif_sub_address

        thread_pullpoints[cam_ip] = threading.Thread(target=pull_messages, name=f"Thread-OnvifPull-{camera_item['localIp']}", args=(camera_item['localIp'], motion_detection_queue))
        thread_pullpoints[cam_ip].start()

        logger.error(f"onvif.start_pullpoint, out, cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

        return onvif_sub_address


    def unsubscribe_pullpoint(self, camera_item):
        onvif_sub_address = None
        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']

        if onvif_sub_address is None:
            logger.info(f"onvif.unsubscribe_pullpoint in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")
            logger.info(f"onvif.unsubscribe_pullpoint out cam_ip: {camera_item['localIp']}")

            return

        logger.info(f"onvif.unsubscribe_pullpoint in cam_ip: {camera_item['localIp']} onvif_sub_address: {onvif_sub_address}")

        try:

            subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'

            subscription_service = self.client.create_service(subscription_binding, self.service_url)

            addressing_header_type = xsd.ComplexType(
                xsd.Sequence([
                    xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
                ])
            )

            addressing_header = addressing_header_type(To=onvif_sub_address)

            response = subscription_service.Unsubscribe(_soapheaders=[addressing_header])

            logger.info(f"onvif.unsubscribe_pullpoint cam_ip: {camera_item['localIp']} response: {response}")

        except BrokenPipeError:
            logger.error("onvif.unsubscribe_pullpoint, Client disconnected before the response could be sent.")

        except Exception as e:
            logger.error(f"onvif.unsubscribe_pullpoint, Exception during running, cam_ip: {camera_item['localIp']} Error: {e}")
            traceback.print_exc()
            pass

    def stop_pullpoint(self, camera_item):  
        self.unsubscribe_pullpoint(camera_item)
        
        global thread_pullpoints
        thread_pullpoints[camera_item['localIp']].join()
        thread_pullpoints[camera_item['localIp']] = None
        del thread_pullpoints[camera_item['localIp']]

        logger.info(f"onvif.stop_pullpoint cam_ip: {camera_item['localIp']} out")
