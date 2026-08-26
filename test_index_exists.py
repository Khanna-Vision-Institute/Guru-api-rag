import os
from dotenv import load_dotenv
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
import boto3

load_dotenv()

endpoint = os.getenv("OPENSEARCH_ENDPOINT")
region = os.getenv("AWS_REGION")
index_name = "guru-rag-vector"

session = boto3.Session()
creds = session.get_credentials().get_frozen_credentials()

auth = AWS4Auth(
    creds.access_key,
    creds.secret_key,
    region,
    "aoss",
    session_token=creds.token
)

client = OpenSearch(
    hosts=[{"host": endpoint.replace("https://", ""), "port": 443}],
    http_auth=auth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection,
)

print("Checking index:", index_name)

try:
    exists = client.indices.exists(index=index_name)
    print("Index exists?", exists)
except Exception as e:
    print("Error:", e)

