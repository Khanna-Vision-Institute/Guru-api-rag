import os
from dotenv import load_dotenv
import requests
from requests_aws4auth import AWS4Auth
import boto3

load_dotenv()

ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT")
REGION = os.getenv("AWS_REGION")

session = boto3.Session()
creds = session.get_credentials().get_frozen_credentials()

auth = AWS4Auth(
    creds.access_key,
    creds.secret_key,
    REGION,
    "aoss",
    session_token=creds.token
)

url = f"https://{ENDPOINT}/v1/collections"

print("Request:", url)
r = requests.get(url, auth=auth)

print("Status:", r.status_code)
print("Body:", r.text)

