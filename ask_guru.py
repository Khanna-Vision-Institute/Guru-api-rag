import sys
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

INDEX_NAME = "guru-index"
HOST = "7ed065gyya3ayjsec508.us-east-1.aoss.amazonaws.com"
REGION = "us-east-1"

session = boto3.Session(profile_name="guru")
credentials = session.get_credentials()
auth = AWSV4SignerAuth(credentials, REGION, "aoss")

client = OpenSearch(
    hosts=[{"host": HOST, "port": 443}],
    http_auth=auth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection
)

embedder = OpenAIEmbeddings(model="text-embedding-3-large")
llm = ChatOpenAI(model="gpt-4o", temperature=0)

def retrieve(query):
    q_vec = embedder.embed_query(query)

    search = {
        "size": 5,
        "query": {
            "knn": {
                "vector": {
                    "vector": q_vec,
                    "k": 5
                }
            }
        }
    }

    res = client.search(index=INDEX_NAME, body=search)
    return [hit["_source"]["text"] for hit in res["hits"]["hits"]]

def answer(question):
    chunks = retrieve(question)
    context = "\n\n".join(chunks)

    prompt = f"""
    You are Guru, a refractive surgery specialist assistant.
    Use ONLY the context provided.

    Context:
    {context}

    Question:
    {question}

    If answer is not in context, say you don't have enough info.
    """

    reply = llm.invoke(prompt)
    return reply.content

if __name__ == "__main__":
    q = " ".join(sys.argv[1:])
    print(answer(q))
