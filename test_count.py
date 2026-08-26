import os
from dotenv import load_dotenv
import boto3
from requests_aws4auth import AWS4Auth
from opensearchpy import OpenSearch, RequestsHttpConnection

load_dotenv()
endpoint = os.getenv("OPENSEARCH_ENDPOINT")
region = os.getenv("AWS_REGION")
index = os.getenv("OPENSEARCH_INDEX")

creds = boto3.Session().get_credentials().get_frozen_credentials()

auth = AWS4Auth(
    creds.access_key,
    creds.secret_key,
    region,
    "aoss",
    session_token=creds.token
)

client = OpenSearch(
    hosts=[{"host": endpoint, "port": 443}],
    http_auth=auth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection,
)

print(client.count(index=index))

