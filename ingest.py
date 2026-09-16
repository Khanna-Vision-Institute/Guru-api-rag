import os
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import requests
import boto3
from requests_aws4auth import AWS4Auth
from opensearchpy import OpenSearch, RequestsHttpConnection
from openai import OpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

# -----------------------------------------------------------------------------
# LOAD ENV
# -----------------------------------------------------------------------------
load_dotenv()

AOSS_ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT")   # example: j3ee7drsp1f97dpotnq3.us-east-1.aoss.amazonaws.com
REGION = os.getenv("AWS_REGION", "us-east-1")
INDEX_NAME = os.getenv("OPENSEARCH_INDEX", "guru-rag-index")

print("Using endpoint:", AOSS_ENDPOINT)
print("Using region:", REGION)
print("Using index:", INDEX_NAME)

# -----------------------------------------------------------------------------
# AWS SIGV4 AUTH
# -----------------------------------------------------------------------------
session = boto3.Session()
creds = session.get_credentials().get_frozen_credentials()

auth = AWS4Auth(
    creds.access_key,
    creds.secret_key,
    REGION,
    "aoss",     # MUST BE aoss for OpenSearch Serverless
    session_token=creds.token
)

# -----------------------------------------------------------------------------
# AOSS CLIENT
# -----------------------------------------------------------------------------
client = OpenSearch(
    hosts=[{
        "host": "vpc-guru-rag-w7f4dc2djtwacgeelhfpd7u7li.us-east-1.es.amazonaws.com",
        "port": 443
    }],
    http_auth=(os.getenv("OPENSEARCH_USER", "admin"), os.environ["OPENSEARCH_PASSWORD"]),
    use_ssl=True,
    verify_certs=True
)


# -----------------------------------------------------------------------------
# CREATE INDEX
# -----------------------------------------------------------------------------
def create_index():
    print("\nCreating index:", INDEX_NAME)

    body = {
        "settings": { "index": { "knn": True }},
        "mappings": {
            "properties": {
                "text": {"type": "text"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 1536,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2"
                    }
                }
            }
        }
    }

    # if exists, do nothing
    if client.indices.exists(INDEX_NAME):
        print("Index already exists, skipping creation")
        return

    print(client.indices.create(index=INDEX_NAME, body=body))

# -----------------------------------------------------------------------------
# SCRAPE WEBSITE
# -----------------------------------------------------------------------------
def scrape():
    URL = "https://khannainstitute.com"
    print("\nScraping:", URL)

    html = requests.get(URL).text
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    cleaned = " ".join(text.split())

    return cleaned

# -----------------------------------------------------------------------------
# SPLIT TEXT INTO CHUNKS
# -----------------------------------------------------------------------------
def split(text):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200
    )
    chunks = splitter.split_text(text)
    print("Chunks:", len(chunks))
    return chunks

# -----------------------------------------------------------------------------
# EMBED + UPLOAD TO AOSS
# -----------------------------------------------------------------------------
def upload(chunks):
    print("\nUploading chunks...")
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    for i, chunk in enumerate(chunks):
        print(f"Uploading chunk {i}")

        emb = openai_client.embeddings.create(
            model="text-embedding-3-large",
            input=chunk
        ).data[0].embedding

        doc = {
            "text": chunk,
            "embedding": emb
        }

        # NO REFRESH — AOSS does not support it
        client.index(index=INDEX_NAME, body=doc)

    print("\nUpload complete. (No refresh needed for AOSS)")

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    create_index()
    text = scrape()
    chunks = split(text)
    upload(chunks)

if __name__ == "__main__":
    main()
