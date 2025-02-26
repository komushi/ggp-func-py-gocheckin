from urllib.parse import urlparse

import sys
import os
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
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

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

        self.stop_events = {}
        self.thread_pullpoints = {}

    @staticmethod
    def extract_notification(raw_payload, client_ip):
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
                if not ip_address:
                    ip_address = client_ip

                logger.debug(f"{ip_address} onvif.extract_notification out is_motion_value {is_motion_value} utc_time {utc_time}")
                return ip_address, utc_time, is_motion_value
            
        logger.debug(f"onvif.extract_notification out None")
        return None, None, None
        


    def unsubscribe(self, camera_item):

        onvif_sub_address = None
        if 'onvifSubAddress' in camera_item:
            onvif_sub_address = camera_item['onvifSubAddress']

        if onvif_sub_address is None:
            logger.debug(f"{camera_item['localIp']} onvif.unsubscribe in onvif_sub_address: {onvif_sub_address}")
            logger.debug(f"{camera_item['localIp']} onvif.unsubscribe out")

            return

        logger.info(f"{camera_item['localIp']} onvif.unsubscribe in onvif_sub_address: {onvif_sub_address}")

        try:
            subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'
            
            subscription_service = self.client.create_service(subscription_binding, self.service_url)

            addressing_header_type = xsd.ComplexType(
                xsd.Sequence([
                    xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
                ])
            )

            addressing_header = addressing_header_type(To=onvif_sub_address)

            logger.debug(f"onvif.unsubscribe addressing_header: {addressing_header}")

            result = subscription_service.Unsubscribe(_soapheaders=[addressing_header])

            logger.debug(f"{camera_item['localIp']} onvif.unsubscribe result: {result}")

            logger.info(f"{camera_item['localIp']} onvif.unsubscribe out")

        except Exception as e:
            logger.error(f"{camera_item['localIp']} onvif.unsubscribe, Exception during running, Error: {e}")
            traceback.print_exc()
            pass

    def _renew(self, cam_ip, onvif_sub_address):

        logger.debug(f"{cam_ip} onvif._renew in onvif_sub_address: {onvif_sub_address}")

        try:
            result = onvif_sub_address

            subscription_binding = '{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding'

            subscription_service = self.client.create_service(subscription_binding, self.service_url)

            addressing_header_type = xsd.ComplexType(
                xsd.Sequence([
                    xsd.Element('{http://www.w3.org/2005/08/addressing}To', xsd.String())
                ])
            )

            addressing_header = addressing_header_type(To=onvif_sub_address)

            subscription_service.Renew(_soapheaders=[addressing_header], TerminationTime='PT1H')

        except Exception as e:
            logger.error(f"{cam_ip} onvif._renew, Exception during running, Error: {e}")
            result = None
            traceback.print_exc()
        finally:
            logger.debug(f"{cam_ip} onvif._renew out onvif_sub_address: {result}")
            return result
        
    def _subscribe(self, cam_ip, scanner_local_ip, http_port):
        logger.debug(f"{cam_ip} onvif._subscribe in  scanner_local_ip: {scanner_local_ip} http_port:{http_port}")

        try:
            notification_binding = '{http://www.onvif.org/ver10/events/wsdl}NotificationProducerBinding'

            notification_service = self.client.create_service(notification_binding, self.service_url)
            logger.debug(f"onvif._subscribe notification_service {notification_service}")

            # Get the EndpointReferenceType
            address_type = self.client.get_element('{http://www.w3.org/2005/08/addressing}EndpointReference')
            logger.debug(f"onvif._subscribe address_type {address_type}")

            # Create the consumer reference
            consumer_reference = address_type(Address=f"http://{scanner_local_ip}:{http_port}/onvif_notifications")
            logger.debug(f"onvif._subscribe consumer_reference {consumer_reference}")

            subscription = notification_service.Subscribe(ConsumerReference=consumer_reference, InitialTerminationTime=os.environ['ONVIF_EXPIRATION'])
            logger.debug(f"onvif._subscribe subscription {subscription}")

            result = subscription.SubscriptionReference.Address._value_1

        except Exception as e:
            logger.error(f"{cam_ip} onvif._subscribe, Exception during running, Error: {e}")
            result = None
        finally:
            logger.debug(f"{cam_ip} onvif._subscribe out {result}")
            return result


    def subscribe(self, cam_ip, old_onvif_sub_address, scanner_local_ip, http_port):

        logger.info(f"{cam_ip} onvif.subscribe in old_onvif_sub_address: {old_onvif_sub_address}")

        if old_onvif_sub_address:
            onvif_sub_address = self._renew(cam_ip, old_onvif_sub_address)
        else:
            onvif_sub_address = self._subscribe(cam_ip, scanner_local_ip, http_port)

        logger.info(f"{cam_ip} onvif.subscribe out onvif_sub_address: {onvif_sub_address}")

        return onvif_sub_address


    def start_pullpoint(self, camera_item, motion_detection_queue):

        def pull_messages(ip_address, motion_detection_queue, stop_event):
        
            while not stop_event.is_set():
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
                            logger.debug(f"onvif.start_pullpoint.pull_messages Motion detected: cam_ip: {ip_address} utc_time: {utc_time} is_motion: {is_motion}")

                except Exception as e:
                    pass

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

        if cam_ip in self.thread_pullpoints:
            self.clear_pullmessage(camera_item)
            # logger.info(f"onvif.start_pullpoint, out, thread_pullpoints already running, cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")
            # return onvif_sub_address

        self.stop_events[cam_ip] = threading.Event()

        self.thread_pullpoints[cam_ip] = threading.Thread(target=pull_messages, name=f"Thread-OnvifPull-{cam_ip}", args=(cam_ip, motion_detection_queue, self.stop_events[cam_ip]))
        self.thread_pullpoints[cam_ip].start()

        logger.info(f"onvif.start_pullpoint, out, cam_ip: {cam_ip} onvif_sub_address: {onvif_sub_address}")

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
        logger.info(f"onvif.stop_pullpoint in cam_ip: {camera_item['localIp']}")

        self.clear_pullmessage(camera_item)

        self.unsubscribe_pullpoint(camera_item)

        logger.info(f"onvif.stop_pullpoint out cam_ip: {camera_item['localIp']}")

    def clear_pullmessage(self, camera_item):
        logger.info(f"onvif.clear_pullmessage in cam_ip: {camera_item['localIp']}")
        
        self.stop_events[camera_item['localIp']].set()
        self.thread_pullpoints[camera_item['localIp']].join()
        self.thread_pullpoints[camera_item['localIp']] = None
        del self.thread_pullpoints[camera_item['localIp']]

        self.stop_events[camera_item['localIp']] = None
        del self.stop_events[camera_item['localIp']]

        logger.info(f"onvif.clear_pullmessage out cam_ip: {camera_item['localIp']}")