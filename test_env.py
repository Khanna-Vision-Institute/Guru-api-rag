import os
from dotenv import load_dotenv

load_dotenv()

print("OPENSEARCH_ENDPOINT:", os.getenv("OPENSEARCH_ENDPOINT"))
print("AWS_REGION:", os.getenv("AWS_REGION"))
print("OPENAI_API_KEY:", os.getenv("OPENAI_API_KEY"))
