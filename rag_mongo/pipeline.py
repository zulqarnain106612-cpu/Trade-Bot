from .retrieve import vector_search
from .generate import generate


def answer_query(query: str) -> dict:
    docs = vector_search(query)
    return generate(query, docs)
