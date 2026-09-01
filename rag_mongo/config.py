import os

from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")  # validated lazily on first connection, not at import
MONGODB_DB = os.getenv("MONGODB_DB", "rag_db")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "documents")
VECTOR_INDEX_NAME = os.getenv("VECTOR_INDEX_NAME", "vector_index")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))
TOP_K = int(os.getenv("TOP_K", "5"))
GENERATION_MODEL = os.getenv("RAG_GENERATION_MODEL", "sonnet")
