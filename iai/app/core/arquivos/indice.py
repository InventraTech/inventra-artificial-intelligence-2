"""Índice vetorial dos documentos no Qdrant.

Organização: UMA coleção (`QDRANT_COLLECTION`) com o campo `tipo` (pdf, docx, xlsx, pptx, csv, texto,
imagem) no payload. Uma coleção só permite buscar em todos os tipos de uma vez e filtrar por tipo quando
útil; coleções separadas por tipo duplicariam config/índices sem ganho (todas usam o mesmo embedding).

Isolamento: todo ponto leva `kitchen_id` e `user_id`; toda consulta, listagem e remoção filtra por eles.
Um documento é visível para o dono e, se o supervisor o marcou como `compartilhado`, para a cozinha dele.
"""

import hashlib
import logging
import math
import re
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from iai.app.core.arquivos.extracao import Secao
from iai.app.core.assistant.guardrail import normalizar_texto
from iai.app.core.config import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    GEMINI_API_KEY,
    LLM_MODE,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_URL,
)
from iai.app.core.infra.security import Identity

logger = logging.getLogger("iai.arquivos")

TAMANHO_TRECHO = 1000
SOBREPOSICAO = 150
LOTE_UPSERT = 64
LIMITE_BUSCA_MAX = 10
_NAMESPACE = uuid.UUID("6f1c2b0e-4e4a-4a57-9a53-2b7a6d0c9e11")


class IndiceIndisponivel(Exception):
    """Qdrant não configurado ou inacessível."""


# --- chunking -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Trecho:
    rotulo: str
    texto: str
    ordem: int = 0


def _quebrar_linha_longa(linha: str, tamanho: int) -> list[str]:
    if len(linha) <= tamanho:
        return [linha]
    frases = re.split(r"(?<=[.!?;])\s+", linha)
    partes: list[str] = []
    atual = ""
    for frase in frases:
        while len(frase) > tamanho:  # sem pontuação: corta duro
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(frase[:tamanho])
            frase = frase[tamanho:]
        if len(atual) + len(frase) + 1 > tamanho and atual:
            partes.append(atual)
            atual = frase
        else:
            atual = f"{atual} {frase}".strip()
    if atual:
        partes.append(atual)
    return partes


def dividir_em_trechos(
    secoes: Sequence[Secao],
    tamanho: int = TAMANHO_TRECHO,
    sobreposicao: int = SOBREPOSICAO,
) -> list[Trecho]:
    """Divide por linhas/parágrafos (nunca no meio de uma linha curta), com sobreposição entre trechos.
    Em tabelas, o cabeçalho é repetido no começo de cada trecho."""
    trechos: list[Trecho] = []
    for secao in secoes:
        cabecalho = f"{secao.cabecalho}\n" if secao.cabecalho else ""
        espaco = max(tamanho - len(cabecalho), 200)
        unidades: list[str] = []
        for linha in secao.texto.split("\n"):
            if linha.strip():
                unidades.extend(_quebrar_linha_longa(linha.strip(), espaco))
        atual: list[str] = []
        tamanho_atual = 0
        for unidade in unidades:
            if atual and tamanho_atual + len(unidade) + 1 > espaco:
                trechos.append(Trecho(secao.rotulo, cabecalho + "\n".join(atual)))
                cauda: list[str] = []
                acumulado = 0
                for anterior in reversed(atual):
                    if acumulado + len(anterior) > sobreposicao:
                        break
                    cauda.insert(0, anterior)
                    acumulado += len(anterior) + 1
                atual, tamanho_atual = cauda, acumulado
            atual.append(unidade)
            tamanho_atual += len(unidade) + 1
        if atual:
            trechos.append(Trecho(secao.rotulo, cabecalho + "\n".join(atual)))
    return [Trecho(t.rotulo, t.texto, ordem) for ordem, t in enumerate(trechos)]


# --- embeddings ---------------------------------------------------------------------------------
class Embedder(Protocol):
    def documentos(self, textos: list[str]) -> list[list[float]]: ...
    def consulta(self, texto: str) -> list[float]: ...


class EmbedderGemini:
    def __init__(self) -> None:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        self._cliente = GoogleGenerativeAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=GEMINI_API_KEY,
            output_dimensionality=EMBEDDING_DIM,
        )

    def documentos(self, textos: list[str]) -> list[list[float]]:
        return self._cliente.embed_documents(
            textos, task_type="RETRIEVAL_DOCUMENT", output_dimensionality=EMBEDDING_DIM
        )

    def consulta(self, texto: str) -> list[float]:
        return self._cliente.embed_query(
            texto, task_type="RETRIEVAL_QUERY", output_dimensionality=EMBEDDING_DIM
        )


class EmbedderLocal:
    """Determinístico (hash de palavras). Só para modo demo e testes: sem rede, sem custo."""

    def _vetor(self, texto: str) -> list[float]:
        vetor = [0.0] * EMBEDDING_DIM
        for palavra in re.findall(r"[a-z0-9]{2,}", normalizar_texto(texto)):
            resumo = hashlib.blake2b(palavra.encode(), digest_size=8).digest()
            posicao = int.from_bytes(resumo[:4], "big") % EMBEDDING_DIM
            vetor[posicao] += 1.0
        norma = math.sqrt(sum(v * v for v in vetor)) or 1.0
        return [v / norma for v in vetor]

    def documentos(self, textos: list[str]) -> list[list[float]]:
        return [self._vetor(t) for t in textos]

    def consulta(self, texto: str) -> list[float]:
        return self._vetor(texto)


_trava = threading.Lock()
_embedder: Embedder | None = None
_cliente: QdrantClient | None = None
_colecao_pronta = False


def definir_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def definir_cliente(cliente: QdrantClient | None) -> None:
    """Gancho de teste (`QdrantClient(":memory:")`) e de troca a quente."""
    global _cliente, _colecao_pronta
    with _trava:
        _cliente = cliente
        _colecao_pronta = False


def configurado() -> bool:
    return _cliente is not None or bool(
        QDRANT_URL and QDRANT_API_KEY.get_secret_value()
    )


def _obter_embedder() -> Embedder:
    global _embedder
    with _trava:
        if _embedder is None:
            _embedder = EmbedderLocal() if LLM_MODE == "demo" else EmbedderGemini()
        return _embedder


def _obter_cliente() -> QdrantClient:
    global _cliente
    with _trava:
        if _cliente is None:
            if not configurado():
                raise IndiceIndisponivel(
                    "Qdrant não configurado (QDRANT_URL / QDRANT_API_KEY)."
                )
            _cliente = QdrantClient(
                url=QDRANT_URL, api_key=QDRANT_API_KEY.get_secret_value(), timeout=30
            )
        return _cliente


def _garantir_colecao(cliente: QdrantClient) -> None:
    global _colecao_pronta
    with _trava:
        if _colecao_pronta:
            return
        if not cliente.collection_exists(QDRANT_COLLECTION):
            cliente.create_collection(
                QDRANT_COLLECTION,
                vectors_config=models.VectorParams(
                    size=EMBEDDING_DIM, distance=models.Distance.COSINE
                ),
            )
        else:
            info = cliente.get_collection(QDRANT_COLLECTION)
            vetores: Any = info.config.params.vectors
            tamanho = getattr(vetores, "size", None)
            if tamanho is not None and tamanho != EMBEDDING_DIM:
                raise IndiceIndisponivel(
                    f"A coleção '{QDRANT_COLLECTION}' usa vetores de {tamanho} dimensões, mas "
                    f"EMBEDDING_DIM={EMBEDDING_DIM}. Use outra QDRANT_COLLECTION ou ajuste o valor."
                )
        for campo, esquema in (
            ("kitchen_id", models.PayloadSchemaType.INTEGER),
            ("user_id", models.PayloadSchemaType.KEYWORD),
            ("arquivo_id", models.PayloadSchemaType.KEYWORD),
            ("tipo", models.PayloadSchemaType.KEYWORD),
            ("compartilhado", models.PayloadSchemaType.BOOL),
            ("ordem", models.PayloadSchemaType.INTEGER),
        ):
            cliente.create_payload_index(QDRANT_COLLECTION, campo, esquema)
        _colecao_pronta = True


def _preparar() -> QdrantClient:
    cliente = _obter_cliente()
    try:
        _garantir_colecao(cliente)
    except IndiceIndisponivel:
        raise
    except Exception as erro:
        logger.exception("qdrant_colecao_falhou")
        raise IndiceIndisponivel(
            "Não consegui preparar o índice de documentos."
        ) from erro
    return cliente


# --- filtros ------------------------------------------------------------------------------------
def _igual(campo: str, valor: Any) -> models.FieldCondition:
    return models.FieldCondition(key=campo, match=models.MatchValue(value=valor))


def _filtro_visivel(
    identity: Identity, extra: list[models.Condition] | None = None
) -> models.Filter:
    """Cozinha do usuário E (é dono OU está compartilhado). Nunca depende de dado vindo do cliente."""
    return models.Filter(
        must=[
            _igual("kitchen_id", identity.kitchen_id),
            models.Filter(
                should=[
                    _igual("user_id", identity.user_id),
                    _igual("compartilhado", True),
                ]
            ),
            *(extra or []),
        ],
    )


def _filtro_gerenciavel(identity: Identity, arquivo_id: str) -> models.Filter:
    condicoes: list[models.Condition] = [
        _igual("kitchen_id", identity.kitchen_id),
        _igual("arquivo_id", arquivo_id),
    ]
    if identity.role != "SUPERVISOR":
        condicoes.append(_igual("user_id", identity.user_id))
    return models.Filter(must=condicoes)


# --- operações ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class DocumentoIndexado:
    arquivo_id: str
    nome: str
    tipo: str
    pasta: str
    trechos: int
    compartilhado: bool
    enviado_em: str
    dono: bool = True


@dataclass(frozen=True)
class Achado:
    arquivo_id: str
    nome: str
    tipo: str
    rotulo: str
    pasta: str
    texto: str
    score: float


def calcular_arquivo_id(identity: Identity, conteudo_sha256: str) -> str:
    # Mesmo arquivo do mesmo usuário => mesmo id => reenviar substitui em vez de duplicar.
    return uuid.uuid5(
        _NAMESPACE, f"{identity.kitchen_id}:{identity.user_id}:{conteudo_sha256}"
    ).hex


def contar_documentos(identity: Identity) -> int:
    cliente = _preparar()
    try:
        return cliente.count(
            QDRANT_COLLECTION,
            count_filter=models.Filter(
                must=[
                    _igual("kitchen_id", identity.kitchen_id),
                    _igual("user_id", identity.user_id),
                    _igual("ordem", 0),
                ]
            ),
            exact=True,
        ).count
    except Exception as erro:
        raise IndiceIndisponivel("Falha ao consultar o índice de documentos.") from erro


def documento_existe(identity: Identity, arquivo_id: str) -> bool:
    cliente = _preparar()
    try:
        return (
            cliente.count(
                QDRANT_COLLECTION,
                count_filter=models.Filter(
                    must=[
                        _igual("kitchen_id", identity.kitchen_id),
                        _igual("user_id", identity.user_id),
                        _igual("arquivo_id", arquivo_id),
                        _igual("ordem", 0),
                    ]
                ),
                exact=True,
            ).count
            > 0
        )
    except Exception as erro:
        raise IndiceIndisponivel("Falha ao consultar o índice de documentos.") from erro


def indexar(
    identity: Identity,
    *,
    arquivo_id: str,
    nome: str,
    tipo: str,
    extensao: str,
    pasta: str,
    sha256: str,
    tamanho: int,
    trechos: Sequence[Trecho],
    compartilhado: bool,
) -> int:
    """Troca o conteúdo do arquivo no índice (apaga o anterior com o mesmo id e grava os trechos)."""
    if not trechos:
        return 0
    cliente = _preparar()
    cabecalho = f"Documento: {nome}" + (f" (pasta {pasta})" if pasta else "")
    textos_embedding = [f"{cabecalho} · {t.rotulo}\n{t.texto}" for t in trechos]
    try:
        embedder = _obter_embedder()
        vetores: list[list[float]] = []
        for i in range(0, len(textos_embedding), 50):
            vetores.extend(embedder.documentos(textos_embedding[i : i + 50]))
    except Exception as erro:
        logger.exception("embedding_falhou")
        raise IndiceIndisponivel(
            "Não consegui gerar os embeddings do arquivo."
        ) from erro
    if len(vetores) != len(trechos) or any(len(v) != EMBEDDING_DIM for v in vetores):
        raise IndiceIndisponivel(
            "O modelo de embedding devolveu vetores com tamanho inesperado."
        )
    enviado_em = datetime.now(UTC).isoformat(timespec="seconds")
    pontos = [
        models.PointStruct(
            id=str(uuid.uuid5(_NAMESPACE, f"{arquivo_id}:{t.ordem}")),
            vector=vetor,
            payload={
                "kitchen_id": identity.kitchen_id,
                "user_id": identity.user_id,
                "arquivo_id": arquivo_id,
                "nome": nome,
                "tipo": tipo,
                "extensao": extensao,
                "pasta": pasta,
                "rotulo": t.rotulo,
                "ordem": t.ordem,
                "total_trechos": len(trechos),
                "texto": t.texto,
                "compartilhado": compartilhado,
                "sha256": sha256,
                "tamanho": tamanho,
                "enviado_em": enviado_em,
            },
        )
        for t, vetor in zip(trechos, vetores, strict=True)
    ]
    try:
        cliente.delete(
            QDRANT_COLLECTION,
            points_selector=models.FilterSelector(
                filter=_filtro_gerenciavel(identity, arquivo_id)
            ),
        )
        for i in range(0, len(pontos), LOTE_UPSERT):
            cliente.upsert(QDRANT_COLLECTION, points=pontos[i : i + LOTE_UPSERT])
    except Exception as erro:
        logger.exception("qdrant_upsert_falhou")
        try:  # não deixa um arquivo pela metade
            cliente.delete(
                QDRANT_COLLECTION,
                points_selector=models.FilterSelector(
                    filter=_filtro_gerenciavel(identity, arquivo_id)
                ),
            )
        except Exception:
            logger.exception("qdrant_limpeza_falhou")
        raise IndiceIndisponivel("Não consegui gravar o arquivo no índice.") from erro
    return len(pontos)


def buscar(
    identity: Identity,
    consulta: str,
    *,
    limite: int = 5,
    tipo: str | None = None,
    arquivo_id: str | None = None,
) -> list[Achado]:
    cliente = _preparar()
    extras: list[models.Condition] = []
    if tipo:
        extras.append(_igual("tipo", tipo))
    if arquivo_id:
        extras.append(_igual("arquivo_id", arquivo_id))
    try:
        vetor = _obter_embedder().consulta(consulta)
        resposta = cliente.query_points(
            QDRANT_COLLECTION,
            query=vetor,
            query_filter=_filtro_visivel(identity, extras),
            limit=max(1, min(limite, LIMITE_BUSCA_MAX)),
            with_payload=True,
        )
    except IndiceIndisponivel:
        raise
    except Exception as erro:
        logger.exception("qdrant_busca_falhou")
        raise IndiceIndisponivel(
            "Não consegui consultar o índice de documentos."
        ) from erro
    achados = []
    for ponto in resposta.points:
        p = ponto.payload or {}
        achados.append(
            Achado(
                arquivo_id=str(p.get("arquivo_id", "")),
                nome=str(p.get("nome", "")),
                tipo=str(p.get("tipo", "")),
                rotulo=str(p.get("rotulo", "")),
                pasta=str(p.get("pasta", "")),
                texto=str(p.get("texto", "")),
                score=float(ponto.score),
            )
        )
    return achados


def listar(identity: Identity) -> list[DocumentoIndexado]:
    cliente = _preparar()
    filtro = _filtro_visivel(identity, [_igual("ordem", 0)])
    itens: list[DocumentoIndexado] = []
    proximo = None
    try:
        while True:
            pontos, proximo = cliente.scroll(
                QDRANT_COLLECTION,
                scroll_filter=filtro,
                limit=100,
                offset=proximo,
                with_payload=True,
                with_vectors=False,
            )
            for ponto in pontos:
                p = ponto.payload or {}
                itens.append(
                    DocumentoIndexado(
                        arquivo_id=str(p.get("arquivo_id", "")),
                        nome=str(p.get("nome", "")),
                        tipo=str(p.get("tipo", "")),
                        pasta=str(p.get("pasta", "")),
                        trechos=int(p.get("total_trechos", 0)),
                        compartilhado=bool(p.get("compartilhado", False)),
                        enviado_em=str(p.get("enviado_em", "")),
                        dono=p.get("user_id") == identity.user_id,
                    )
                )
            if proximo is None:
                break
    except Exception as erro:
        logger.exception("qdrant_listagem_falhou")
        raise IndiceIndisponivel("Não consegui listar os documentos.") from erro
    return sorted(itens, key=lambda d: d.enviado_em, reverse=True)


def remover(identity: Identity, arquivo_id: str) -> bool:
    """Dono apaga o próprio arquivo; supervisor apaga qualquer um da própria cozinha."""
    cliente = _preparar()
    filtro = _filtro_gerenciavel(identity, arquivo_id)
    try:
        if cliente.count(QDRANT_COLLECTION, count_filter=filtro, exact=True).count == 0:
            return False
        cliente.delete(
            QDRANT_COLLECTION, points_selector=models.FilterSelector(filter=filtro)
        )
    except Exception as erro:
        logger.exception("qdrant_remocao_falhou")
        raise IndiceIndisponivel("Não consegui remover o documento.") from erro
    return True
