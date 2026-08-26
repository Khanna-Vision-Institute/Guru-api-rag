from opensearchpy import OpenSearch, RequestsHttpConnection
import boto3
from requests_aws4auth import AWS4Auth

region = "us-east-1"
service = "aoss"

session = boto3.Session()
credentials = session.get_credentials()
aws_auth = AWS4Auth(
    credentials.access_key,
    credentials.secret_key,
    session.get_credentials().token,
    region,
    service,
)

client = OpenSearch(
    hosts=[{'host': endpoint, 'port': 443}],
    http_auth=aws_auth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection
)
