import os
from dotenv import load_dotenv

load_dotenv()

KG_NODES_COLLECTION = os.getenv("KG_NODES_COLLECTION", "kg_nodes")
KG_EDGES_COLLECTION = os.getenv("KG_EDGES_COLLECTION", "kg_edges")
KG_EXTRACT_MODEL = os.getenv("KG_EXTRACT_MODEL", "haiku")
KG_RESOLVE_MODEL = os.getenv("KG_RESOLVE_MODEL", "sonnet")
KG_QUERY_MODEL = os.getenv("KG_QUERY_MODEL", "sonnet")
KG_MAX_SUBGRAPH_TRIPLES = int(os.getenv("KG_MAX_SUBGRAPH_TRIPLES", "40"))
