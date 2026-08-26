from typing import List
from config import OPENAI_API_KEY, EMBED_MODEL
from openai import OpenAI

_client = None

def _ensure():
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client

def embed_texts(texts: List[str]) -> List[List[float]]:
    client = _ensure()
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]

