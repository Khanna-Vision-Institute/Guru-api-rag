from utils import search_opensearch
from llm_providers import generate_answer

def answer_query(query, model, top_k=5):
    hits = search_opensearch(query, top_k)

    if not hits:
        return {
            "answer": "No matching information found in the indexed data.",
            "hits": []
        }

    answer = generate_answer(model, query, hits)

    return {
        "answer": answer,
        "hits": hits
    }

