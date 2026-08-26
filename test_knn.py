#!/usr/bin/env python3
"""
Guru AI KNN Search Test Script
Tests the vector search functionality and RAG pipeline
"""

import sys
import os
from dotenv import load_dotenv

# Load environment
load_dotenv()

def test_imports():
    """Test all required imports"""
    print("🧪 Testing imports...")
    try:
        from utils import search_opensearch, embed_text, index_document
        from llm_providers import generate_answer_with_fallback
        from opensearchpy import OpenSearch
        print("✅ All imports successful")
        return True
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        return False

def test_opensearch_connection():
    """Test OpenSearch connection"""
    print("\n🔍 Testing OpenSearch connection...")
    try:
        from opensearchpy import OpenSearch
        client = OpenSearch(
            hosts=[{
                "host": os.getenv("OPENSEARCH_ENDPOINT"),
                "port": 443
            }],
            http_auth=("admin", "@Gur#Ur@g25"),
            use_ssl=True,
            verify_certs=True
        )

        info = client.info()
        print(f"✅ OpenSearch connected: {info['version']['number']}")

        # Check index exists
        if client.indices.exists("guru-rag"):
            count = client.count(index="guru-rag")
            print(f"✅ Index 'guru-rag' exists with {count['count']} documents")
        else:
            print("❌ Index 'guru-rag' does not exist")
            return False

        return True
    except Exception as e:
        print(f"❌ OpenSearch connection failed: {e}")
        return False

def test_embedding():
    """Test OpenAI embedding generation"""
    print("\n🧮 Testing embedding generation...")
    try:
        from utils import embed_text
        test_text = "LASIK surgery is a vision correction procedure."
        embedding = embed_text(test_text)
        print(f"✅ Embedding generated: {len(embedding)} dimensions")
        print(f"   Sample values: {embedding[:5]}")
        return True
    except Exception as e:
        print(f"❌ Embedding failed: {e}")
        return False

def test_knn_search():
    """Test KNN vector search"""
    print("\n🔎 Testing KNN vector search...")
    try:
        from utils import search_opensearch

        queries = [
            "What is LASIK surgery?",
            "vision correction procedures",
            "SMILE laser technology"
        ]

        for query in queries:
            print(f"\n   Query: '{query}'")
            hits = search_opensearch(query, top_k=3)

            if not hits:
                print("   ❌ No results found")
                continue

            print(f"   ✅ Found {len(hits)} results:")
            for i, hit in enumerate(hits):
                score = hit['score']
                text_preview = hit['text'][:80].replace('\n', ' ')
                print("2d")

        return True
    except Exception as e:
        print(f"❌ KNN search failed: {e}")
        return False

def test_rag_pipeline():
    """Test complete RAG pipeline"""
    print("\n🤖 Testing RAG pipeline...")
    try:
        from utils import search_opensearch
        from llm_providers import generate_answer_with_fallback

        test_query = "What are the benefits of LASIK surgery?"
        print(f"   Query: '{test_query}'")

        # Get search results
        hits = search_opensearch(test_query, top_k=3)
        if not hits:
            print("   ❌ No search results")
            return False

        print(f"   📄 Retrieved {len(hits)} documents")

        # Generate answer
        answer, model_used = generate_answer_with_fallback(test_query, hits)
        print(f"   🤖 Answer from {model_used}:")
        print(f"   {answer[:200]}...")

        return True
    except Exception as e:
        print(f"❌ RAG pipeline failed: {e}")
        return False

def test_api_endpoints():
    """Test FastAPI endpoints"""
    print("\n🌐 Testing API endpoints...")
    try:
        import requests

        base_url = "http://localhost:8000"

        # Test health
        response = requests.get(f"{base_url}/health")
        if response.status_code == 200:
            print("   ✅ Health endpoint working")
        else:
            print(f"   ❌ Health endpoint failed: {response.status_code}")
            return False

        # Test ask endpoint
        ask_data = {
            "query": "What is LASIK?",
            "top_k": 3
        }
        response = requests.post(f"{base_url}/ask", json=ask_data)
        if response.status_code == 200:
            data = response.json()
            if "answer" in data and "model_used" in data:
                print("   ✅ Ask endpoint working")
                print(f"   🤖 Response from {data['model_used']}")
            else:
                print("   ❌ Ask endpoint returned invalid response")
                return False
        else:
            print(f"   ❌ Ask endpoint failed: {response.status_code}")
            return False

        return True
    except requests.exceptions.ConnectionError:
        print("   ❌ API server not running (start with: uvicorn main:app --host 0.0.0.0 --port 8000)")
        return False
    except Exception as e:
        print(f"❌ API test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("🚀 Guru AI RAG System Test Suite")
    print("=" * 50)

    tests = [
        ("Imports", test_imports),
        ("OpenSearch Connection", test_opensearch_connection),
        ("Embedding Generation", test_embedding),
        ("KNN Search", test_knn_search),
        ("RAG Pipeline", test_rag_pipeline),
        ("API Endpoints", test_api_endpoints),
    ]

    passed = 0
    total = len(tests)

    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                print(f"   ❌ {test_name} failed")
        except Exception as e:
            print(f"   ❌ {test_name} crashed: {e}")

    print("\n" + "=" * 50)
    print(f"📊 Test Results: {passed}/{total} passed")

    if passed == total:
        print("🎉 All tests passed! Guru AI is ready for production.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the output above for details.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
