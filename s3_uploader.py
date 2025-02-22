import hashlib
import hmac
import urllib.parse
import os
from datetime import datetime, timezone
import dateutil.parser
import requests
import http.client
import ssl
import json
import logging
import sys
import traceback

# Setup logging to stdout
if 'LOG_LEVEL' in os.environ:
    logging.basicConfig(stream=sys.stdout, level=os.environ['LOG_LEVEL'])
else:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

class S3Uploader():
    def __init__(self, cred_provider_host, cred_provider_path, bucket_name, expires_in=3600):
        logger.info(f"S3Uploader init, cred_provider_host: {cred_provider_host} cred_provider_path: {cred_provider_path} bucket_name: {bucket_name}")

        self.cred_provider_host = cred_provider_host
        self.cred_provider_path = cred_provider_path
        self.expires_in = expires_in
        self.bucket_name = bucket_name
        self.credentials = None
        self.get_temporary_credentials()

    def get_temporary_credentials(self):
        if not self.credentials:
            certificate_file = os.path.join(os.environ['GGC_CERT_PATH'], "core.crt")
            key_file = os.path.join(os.environ['GGC_CERT_PATH'], "core.key")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS)
            context.load_cert_chain(certfile=certificate_file, keyfile=key_file)
            connection = http.client.HTTPSConnection(self.cred_provider_host, port=443, context=context)
            headers = {'x-amzn-iot-thingname': os.environ["AWS_IOT_THING_NAME"]}
            connection.request(method="GET", url=self.cred_provider_path, headers=headers)

            response = connection.getresponse()
            if response.status == 200:
                self.credentials = json.loads(response.read().decode())['credentials']
                logger.debug(f"Credentials retrieved")
            else:
                raise Exception(f"Failed to get credentials: {response.status}, {repr(response)}")
        else:
            expiration = dateutil.parser.isoparse(self.credentials['expiration'])
            time_remaining = expiration - datetime.now(timezone.utc)

            logger.debug(f"Credentials will expire at {expiration}, Time remaining: {time_remaining}")

            if time_remaining.total_seconds() < 60:
                self.credentials = None
                self.get_temporary_credentials()

    def sign(self, key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


    def get_signature_key(self, key, dateStamp, regionName, serviceName):
        kDate = self.sign(("AWS4" + key).encode("utf-8"), dateStamp)
        kRegion = self.sign(kDate, regionName)
        kService = self.sign(kRegion, serviceName)
        kSigning = self.sign(kService, "aws4_request")
        return kSigning

    def generate_presigned_url(self, object_key, httpMethod='PUT'):
        # Define variables
        # HTTP verb "PUT"
        
        # Region ap-northeast-1
        region = os.environ['REGION']
        # sevice s3
        service = "s3"
        # Host
        host = "s3.amazonaws.com"
        # Service streaming endpoint
        endpoint = "https://" + self.bucket_name + "." + host
        
        timestamp = datetime.now(timezone.utc)
        # timestamp = datetime.utcnow()
        # timestamp = datetime.now(datetime.UTC)
        # Date and time of request
        amzDatetime = timestamp.strftime("%Y%m%dT%H%M%SZ")
        # Date without time for credential scope
        datetimeStr = timestamp.strftime("%Y%m%d")
        
        accessKey = self.credentials["accessKeyId"]
        secretKey = self.credentials["secretAccessKey"]
        securityToken = self.credentials["sessionToken"]

        # 1. Combine all of the elements to create the canonical request
        # Create a canonical URI (uniform resource identifier). The encoded URI in the following example, /example/photo.jpg, is the absolute path and you don't encode the "/" in the absolute path.
        # canonicalURI = urllib.parse.quote("/" + object_key, safe='/:')
        canonicalURI = urllib.parse.quote("/" + object_key)

        # Create the canonical query string. Query string values must be URI-encoded and sorted by name.
        # Match the algorithm to the hashing algorithm. You must use SHA-256.
        algorithmStr = "AWS4-HMAC-SHA256"

        # Create the credential scope, which scopes the derived key to the date, Region, and service to which the request is made.
        credentialScope = f'{datetimeStr}/{region}/{service}/aws4_request'

        # Create the canonical headers and signed headers. Note the trailing \n in the canonical headers.
        signedHeaders = "host"
        canonicalQueryParams = {
            'X-Amz-Algorithm': algorithmStr,
            'X-Amz-Credential': f'{accessKey}/{credentialScope}',
            'X-Amz-Date': amzDatetime,
            'X-Amz-Expires': self.expires_in,
            'X-Amz-Security-Token': securityToken,
            'X-Amz-SignedHeaders': signedHeaders
        }
        # print(f"canonicalQueryParams: {canonicalQueryParams}")

        canonicalHeaders = f'host:{self.bucket_name}.{host}\n'

        # Create a hash of the payload. For a GET request, the payload is an empty string.
        # You don't include a payload hash in the Canonical Request, because when you create a presigned URL, you don't know the payload content
        # because the URL is used to upload an arbitrary payload. Instead, you use a constant string UNSIGNED-PAYLOAD.
        payloadHash = "UNSIGNED-PAYLOAD"

        canonicalRequest = f'{httpMethod}\n{canonicalURI}\n{urllib.parse.urlencode(canonicalQueryParams)}\n{canonicalHeaders}\n{signedHeaders}\n{payloadHash}'

        # 2. Create the string to sign
        # algorithmStr:OK, amzDatetime:OK, credentialScope:OK
        stringToSign = f'{algorithmStr}\n{amzDatetime}\n{credentialScope}\n{hashlib.sha256(canonicalRequest.encode("utf-8")).hexdigest()}'
        # print(f"stringToSign: {stringToSign}")

        # 3. Calculate the signature
        # calculate the signature by using a signing key that"s obtained
        signingKey = self.get_signature_key(secretKey, datetimeStr, region, service)

        # Sign the stringToSign using the signing key
        signatureStr = hmac.new(signingKey, (stringToSign).encode("utf-8"), hashlib.sha256).hexdigest()

        # 4. Add signing information to request and create request URL
        # Add the authentication information to the query string
        canonicalQueryParams['X-Amz-Signature'] = signatureStr

        presignedRequestURL = f'{endpoint}{canonicalURI}?'
        presignedRequestURL += urllib.parse.urlencode(canonicalQueryParams)

        return presignedRequestURL
    
    # def boto3_gen_presigned_url(self, object_key, method='put_object'):
    #     import boto3

    #     aws_access_key_id = self.credentials["accessKeyId"]
    #     aws_secret_access_key = self.credentials["secretAccessKey"]
    #     aws_session_token = self.credentials["sessionToken"]

    #     session = boto3.session.Session(
    #         aws_access_key_id = aws_access_key_id,
    #         aws_secret_access_key = aws_secret_access_key,
    #         aws_session_token = aws_session_token
    #     )

    #     # get s3 presign
    #     url = session.client('s3').generate_presigned_url(
    #         ClientMethod=method,
    #         Params={'Bucket': self.bucket_name, 'Key': object_key },
    #         ExpiresIn=self.expires_in)

    #     return url

    # def boto3_gen_kms_presigned_url(self, object_key, method='put_object'):
    #     import boto3

    #     aws_access_key_id = self.credentials["accessKeyId"]
    #     aws_secret_access_key = self.credentials["secretAccessKey"]
    #     aws_session_token = self.credentials["sessionToken"]

    #     session = boto3.session.Session(
    #         aws_access_key_id = aws_access_key_id,
    #         aws_secret_access_key = aws_secret_access_key,
    #         aws_session_token = aws_session_token
    #     )

    #     # get s3 presign
    #     url = session.client('s3').generate_presigned_url(
    #         ClientMethod=method,
    #         Params={
    #             'Bucket': self.bucket_name,
    #             'Key': object_key,
    #             'ServerSideEncryption': 'aws:kms',
    #             'SSEKMSKeyId': self.kms_key_id
    #         },
    #         ExpiresIn=self.expires_in)

    #     return url

    # def generate_kms_presigned_url(self, object_key, http_method='PUT'):

    #     region = os.environ['REGION']
    #     service = "s3"
    #     host = "s3.amazonaws.com"
    #     endpoint = f"https://{os.environ['BUCKET_NAME']}.{host}"

    #     timestamp = datetime.now(timezone.utc)
    #     amz_datetime = timestamp.strftime("%Y%m%dT%H%M%SZ")
    #     datetime_str = timestamp.strftime("%Y%m%d")

    #     access_key = self.credentials["accessKeyId"]
    #     secret_key = self.credentials["secretAccessKey"]
    #     security_token = self.credentials["sessionToken"]

    #     # Canonical request components
    #     canonical_uri = urllib.parse.quote("/" + object_key)
    #     algorithm_str = "AWS4-HMAC-SHA256"
    #     credential_scope = f'{datetime_str}/{region}/{service}/aws4_request'

    #     signed_headers = "host"
    #     canonical_headers = f'host:{os.environ["BUCKET_NAME"]}.{host}\n'
    #     payload_hash = "UNSIGNED-PAYLOAD"

    #     # Query parameters with SSE-KMS support
    #     canonical_query_params = {
    #         'X-Amz-Algorithm': algorithm_str,
    #         'X-Amz-Credential': f'{access_key}/{credential_scope}',
    #         'X-Amz-Date': amz_datetime,
    #         'X-Amz-Expires': self.expires_in,
    #         'X-Amz-Security-Token': security_token,
    #         'X-Amz-SignedHeaders': signed_headers,
    #         'x-amz-server-side-encryption': 'aws:kms',
    #         'x-amz-server-side-encryption-aws-kms-key-id': os.environ['KMS_KEY_ID']  # KMS Key ID
    #     }

    #     # Canonical request and string to sign
    #     canonical_request = (
    #         f'{http_method}\n{canonical_uri}\n'
    #         f'{urllib.parse.urlencode(canonical_query_params)}\n'
    #         f'{canonical_headers}\n{signed_headers}\n{payload_hash}'
    #     )

    #     string_to_sign = (
    #         f'{algorithm_str}\n{amz_datetime}\n'
    #         f'{credential_scope}\n'
    #         f'{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'
    #     )

    #     signing_key = self.get_signature_key(secret_key, datetime_str, region, service)
    #     signature_str = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    #     # Generate the presigned URL
    #     canonical_query_params['X-Amz-Signature'] = signature_str
    #     presigned_request_url = f'{endpoint}{canonical_uri}?{urllib.parse.urlencode(canonical_query_params)}'

    #     return presigned_request_url

    
    def put_object(self, object_key, local_file_path):
        try:
            logger.debug(f"put_object, in local_file_path: {local_file_path}, object_key: {object_key}")
            
            self.get_temporary_credentials()

            presigned_url = self.generate_presigned_url(object_key)

            with open(local_file_path, 'rb') as file:
                response = requests.put(presigned_url, data=file)
            
            if response.status_code == 200:
                os.remove(local_file_path)
                logger.info(f"put_object, File uploaded as {object_key}.")
            else:
                logger.error(f"put_object, Failed to upload object_key: {object_key}, {local_file_path}, status: {response.status_code}")
        except Exception as e:
            logger.error(f"put_object, Exception: {e}")
            traceback.print_exc()
    
