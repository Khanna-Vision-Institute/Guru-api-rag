from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional

from rag_chain import answer_query
from utils import get_opensearch_client
from ingest import INDEX_NAME

app = FastAPI(title="Guru RAG API", version="1.0.0")

# ---------- Request Schema ----------
class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = 5
    model: Optional[str] = "openai"  # openai, gemini, claude


# ---------- Health Check ----------
@app.get("/health")
def health():
    return {"ok": True}


# ---------- Main Search Endpoint ----------
@app.post("/search")
def search(req: SearchRequest):
    try:
        print(f"🔥 Incoming search: {req.query} | model={req.model}")

        data = answer_query(
            query=req.query,
            model=req.model or "openai",
            top_k=req.top_k or 5
        )

        return data

    except Exception as e:
        print("❌ Search Error:", e)
        return {
            "error": "Search failed",
            "details": str(e)
        }

