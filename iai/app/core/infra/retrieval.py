"""Busca textual explicável, sem embeddings ou alegação de RAG semântico."""

import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache

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


# --- Busca de produto por nome (filtro local das tools de consulta) -------------------------
# Regras de `nome_confere`:
# - normaliza (minúscula, sem acento) e separa em palavras: sequências de letras ou de dígitos
#   ("5kg" vira "5" e "kg", igual ao nome "5 kg");
# - descarta as stopwords de STOP ("de", "da", "com", "e"...);
# - a busca casa quando TODAS as palavras dela aparecem no nome, em qualquer ordem;
# - cada palavra casa por igualdade (incluindo o plural simples) ou por prefixo, e o prefixo só vale
#   com 3 ou mais letras: "toma" casa "tomate", mas "to" não casa nada. Números casam por igualdade;
# - busca vazia, só com espaços ou só com stopwords NÃO casa nada (nunca vira "casa tudo");
#   quem quer listar tudo simplesmente não filtra por nome.
PREFIXO_MINIMO = 3
_PALAVRA = re.compile(r"[a-z]+|[0-9]+")


@lru_cache(maxsize=4096)
def _palavras_do_nome(texto: str) -> tuple[str, ...]:
    return tuple(p for p in _PALAVRA.findall(normalize(texto)) if p not in STOP)


@lru_cache(maxsize=4096)
def _formas(palavra: str) -> frozenset[str]:
    """A própria palavra e as formas singulares plausíveis dela.

    Conservador: a palavra original sempre fica no conjunto, então o que terminava em "s" de
    nascença ("gas", "lapis", "ananas") continua casando consigo mesmo. Só palavras com 4 ou
    mais letras são singularizadas, e terminações em "ss", "us" e "is" não perdem o "s"."""
    formas = {palavra}
    if len(palavra) < 4 or not palavra.endswith("s"):
        return frozenset(formas)
    raiz = palavra[:-1]
    if palavra.endswith(("oes", "aes")):  # limoes -> limao, paes -> pao
        formas.add(palavra[:-3] + "ao")
    elif palavra.endswith(
        ("ais", "eis", "ois", "uis")
    ):  # animais -> animal, papeis -> papel
        formas.add(
            palavra[:-3] + {"a": "al", "e": "el", "o": "ol", "u": "ul"}[palavra[-3]]
        )
    elif palavra.endswith("ns"):  # itens -> item, bombons -> bombom
        formas.add(palavra[:-2] + "m")
        formas.add(raiz)
    elif not palavra.endswith(("ss", "us", "is")):  # tomates -> tomate, ovos -> ovo
        formas.add(raiz)
        if palavra.endswith("es") and len(palavra) >= 5:  # colheres -> colher
            formas.add(palavra[:-2])
    return frozenset(formas)


def _palavra_confere(palavra: str, alvo: tuple[str, ...]) -> bool:
    formas = _formas(palavra)
    por_prefixo = palavra.isalpha() and len(palavra) >= PREFIXO_MINIMO
    for candidata in alvo:
        if formas & _formas(candidata):
            return True
        if por_prefixo and candidata.startswith(palavra):
            return True
    return False


def nome_confere(busca: str | None, alvo: str | None) -> bool:
    """True quando todas as palavras relevantes de `busca` aparecem em `alvo` (qualquer ordem)."""
    palavras = _palavras_do_nome(busca or "")
    candidatas = _palavras_do_nome(alvo or "")
    if not palavras or not candidatas:
        return False
    return all(_palavra_confere(palavra, candidatas) for palavra in palavras)


def termos_de_busca_alternativos(busca: str | None, maximo: int = 2) -> list[str]:
    """Termos curtos para uma segunda consulta ao servidor quando o texto inteiro não achou nada.

    O servidor filtra por trecho contíguo (LIKE) e não tira acento; por isso tentamos a palavra mais
    longa da busca e a forma singular dela (de "tomates" sai "tomate"). O resultado SEMPRE passa
    de novo por `nome_confere` com a busca completa."""
    palavras = [p for p in _palavras_do_nome(busca or "") if p.isalpha()]
    if not palavras:
        return []
    maior = max(palavras, key=len)
    termos = sorted(
        (f for f in _formas(maior) if len(f) >= PREFIXO_MINIMO),
        key=lambda f: (-len(f), f),
    )
    original = normalize((busca or "").strip())
    return [t for t in termos if t != original][:maximo]
