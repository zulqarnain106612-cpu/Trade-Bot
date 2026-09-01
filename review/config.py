import os

from dotenv import load_dotenv

load_dotenv()

FULLTEXT_INDEX_NAME = os.getenv("FULLTEXT_INDEX_NAME", "fulltext_index")
REVIEW_MAX_CONTEXT_ITEMS = int(os.getenv("REVIEW_MAX_CONTEXT_ITEMS", "15"))
