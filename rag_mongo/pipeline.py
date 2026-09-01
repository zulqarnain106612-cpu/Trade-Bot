from .generate import generate
from .retrieve import vector_search


def answer_query(query: str) -> dict:
    docs = vector_search(query)
    return generate(query, docs)
