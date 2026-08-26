from typing import List, Dict
from opensearch_client import get_client
from config import AOSS_INDEX, TOP_K_DEFAULT

# cosine is not available in aoss knn by name
# we used l2 in index mapping convert dot products by normalizing embeddings first if needed

def knn_search(query_vector: List[float], top_k: int = TOP_K_DEFAULT) -> List[Dict]:
    client = get_client()
    body = {
        "size": top_k,
        "query": {
            "knn": {
                "vector": {
                    "vector": query_vector,
                    "k": top_k
                }
            }
        },
        "_source": ["text", "url", "title", "chunk_id"]
    }
    res = client.search(index=AOSS_INDEX, body=body)
    hits = []
    for h in res["hits"]["hits"]:
        src = h["_source"]
        hits.append({
            "id": h["_id"],
            "score": h["_score"],
            "text": src.get("text",""),
            "url": src.get("url"),
            "title": src.get("title"),
            "chunk_id": src.get("chunk_id"),
        })
    return hits

