import os
from opensearchpy import OpenSearch
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

INDEX_NAME = os.getenv("OPENSEARCH_INDEX", "guru-rag")

def get_opensearch_client():
    client = OpenSearch(timeout=30, max_retries=3, retry_on_timeout=True,
        hosts=[{
            "host": os.getenv("OPENSEARCH_ENDPOINT"),
            "port": 443
        }],
        http_auth=("admin", "@Gur#Ur@g25"),
        use_ssl=True,
        verify_certs=True
    )
    return client

def embed_text(text):
    """Generate embedding for text using OpenAI"""
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.embeddings.create(
        model="text-embedding-3-large",
        input=text
    )
    return response.data[0].embedding

def search_opensearch(query, top_k=5):
    """Perform KNN vector search in OpenSearch"""
    client = get_opensearch_client()

    # Generate embedding for the query
    query_embedding = embed_text(query)

    body = {
        "size": top_k,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": top_k
                }
            }
        }
    }

    res = client.search(index=INDEX_NAME, body=body)

    hits = []
    for h in res["hits"]["hits"]:
        hits.append({
            "id": h["_id"],
            "score": h["_score"],
            "text": h["_source"].get("text", ""),
            "content": h["_source"].get("text", "")  # for backward compatibility
        })

    return hits

def index_document(text, embedding=None):
    """Index a document with its embedding"""
    client = get_opensearch_client()

    if embedding is None:
        embedding = embed_text(text)

    doc = {
        "text": text,
        "embedding": embedding
    }

    response = client.index(index=INDEX_NAME, body=doc)
    return response
