import os
from dotenv import load_dotenv

load_dotenv()

AOSS_ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT")  # like j3ee7...us-east-1.aoss.amazonaws.com
AOSS_REGION = os.getenv("AWS_REGION", "us-east-1")
AOSS_INDEX = os.getenv("OPENSEARCH_INDEX", "guru-rag-index")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# answer generation defaults
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "openai")  # openai or claude or gemini
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini")
DEFAULT_CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# retrieval
TOP_K_DEFAULT = int(os.getenv("TOP_K_DEFAULT", "5"))
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
EMBED_DIM = int(os.getenv("EMBED_DIM", "3072"))

# safety
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "12000"))

