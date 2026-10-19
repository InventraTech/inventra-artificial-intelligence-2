"""Busca textual explicável, sem embeddings ou alegação de RAG semântico."""

import math
import re
import unicodedata
from collections import Counter

STOP = {
    "o",
    "a",
    "os",
    "as",
    "de",
    "do",
    "da",
    "dos",
    "das",
    "um",
    "uma",
    "em",
    "no",
    "na",
    "que",
    "e",
    "eu",
    "me",
    "como",
    "qual",
    "quais",
    "para",
    "por",
    "com",
    "sobre",
    "voce",
    "meu",
    "minha",
}
SYNONYMS = {
    "baixa": "consumo",
    "usei": "consumo",
    "gastamos": "consumo",
    "gastei": "consumo",
    "contato": "suporte",
    "email": "suporte",
    "vencer": "validade",
    "vencem": "validade",
    "vencimento": "validade",
    "vencimentos": "validade",
    "pendentes": "requisicao",
    "pedidos": "requisicao",
    "requisicoes": "requisicao",
    "aprovar": "aprovacao",
    "aprova": "aprovacao",
    "cadastrar": "cadastro",
    "criar": "cadastro",
    "lotes": "lote",
    "produtos": "produto",
    "fornecedores": "fornecedor",
}


def normalize(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def tokens(text):
    return [
        SYNONYMS.get(word, word)
        for word in re.findall(r"[a-z0-9]+", normalize(text))
        if word not in STOP
    ]


def rank(query, texts, limit=4):
    words = set(tokens(query))
    documents = [Counter(tokens(text)) for text in texts]
    scores = []
    for index, document in enumerate(documents):
        score = 0.0
        for word in words:
            frequency = document[word]
            if frequency:
                df = sum(word in doc for doc in documents)
                score += (
                    math.log(1 + (len(documents) + 1) / (df + 1))
                    * frequency
                    / (frequency + 1)
                )
        if score:
            scores.append((index, score))
    return sorted(scores, key=lambda pair: (-pair[1], pair[0]))[:limit]
