"""Vocabulário de eventos do /chat/stream: o que o grafo faz vira mensagens pequenas e tipadas.

O app decide como desenhar cada tipo; nenhum evento traz rótulo do tipo ("pensamento", "resposta").

- etapa      id, status (inicio|fim), texto      nós do grafo e tools começando/terminando
- pensamento id, delta                           resumo de raciocínio do modelo, ao vivo (não conferido pelo juiz)
- artefato   artifact                            gráfico/tabela pronto (mesmo formato do /chat)
- cartao     approval_id, pending_action         proposta aguardando aprovação (mesmo formato do /chat)
- texto      delta                               resposta já aprovada pelo juiz, em pedaços
- fim        status (aprovado|segura)            último evento
- erro       mensagem                            falha depois de o stream abrir (o HTTP já foi 200)
"""

import re
import time
from collections.abc import Iterator
from contextlib import suppress
from threading import Event

from langchain_core.messages import AIMessageChunk
from langgraph.config import get_stream_writer

from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.schemas import ChatResponse

# Nó -> (texto ao começar, texto ao terminar). Toda mensagem passa pelos guardrails, então sempre há fluxo.
PASSOS_DO_GRAFO: dict[str, tuple[str, str]] = {
    "guardrail_insulto": ("Verificando a mensagem…", "Mensagem verificada"),
    "guardrail_escopo": ("Checando o assunto…", "Assunto liberado"),
    "roteador": ("Entendendo o pedido…", "Entendi o pedido"),
    "estoquista": ("Analisando o estoque…", "Estoque analisado"),
    "comprador": ("Analisando as compras…", "Compras analisadas"),
    "supervisor": ("Montando a visão geral…", "Visão geral pronta"),
    "faq": ("Consultando a documentação…", "Documentação consultada"),
    "orquestrador": ("Organizando a resposta…", "Resposta organizada"),
    "guardrail_saida": ("Conferindo a resposta…", "Resposta conferida"),
}
PASSOS_DAS_TOOLS: dict[str, tuple[str, str]] = {
    "consultar_inventra": ("Consultando o estoque…", "Estoque consultado"),
    "planejar_baixa": ("Planejando a baixa por lote…", "Baixa planejada"),
    "consultar_solicitacoes_iai": (
        "Consultando suas solicitações…",
        "Solicitações consultadas",
    ),
    "faq_retriever": ("Consultando a documentação…", "Documentação consultada"),
    "buscar_historico": (
        "Lembrando conversas anteriores…",
        "Conversas anteriores consultadas",
    ),
    "buscar_documentos": ("Lendo seus documentos…", "Documentos lidos"),
}
PASSO_PROPOSTA = ("Preparando a proposta…", "Proposta preparada")
BLOCOS_DE_PENSAMENTO = {"thinking": "thinking", "reasoning": "reasoning"}

TAMANHO_PEDACO = 12
INTERVALO_ENTRE_PEDACOS = 0.02


def passo_da_tool(nome: str) -> tuple[str, str] | None:
    if nome.startswith("propor_"):
        return PASSO_PROPOSTA
    return PASSOS_DAS_TOOLS.get(nome)


def avisar(evento: dict) -> None:
    """Manda um evento ao stream; fora de um stream (invoke, REST, MCP) não faz nada."""
    with suppress(Exception):  # progresso nunca pode derrubar uma tool
        get_stream_writer()(evento)


def etapa(
    id_: str,
    status: str,
    texto: str | None = None,
    pai: str | None = None,
    detalhe: str | None = None,
) -> dict:
    """`pai`: id do passo em que este roda (a tool dentro do nó); `detalhe`: o comando executado."""
    evento = {"type": "etapa", "id": id_, "status": status}
    if texto:
        evento["texto"] = texto
    if pai:
        evento["pai"] = pai
    if detalhe:
        evento["detalhe"] = detalhe
    return evento


def comando(nome: str, argumentos: dict) -> str:
    """`nome(chave='valor', ...)`: o que a tool vai rodar, para o app mostrar sob o passo."""
    partes = []
    for chave, valor in argumentos.items():
        texto = repr(valor) if isinstance(valor, str) else str(valor)
        partes.append(f"{chave}={texto[:80]}")
    return remove_secrets(f"{nome}({', '.join(partes)})")[:400]


def _etapa_de_tarefa(tarefa: dict) -> dict | None:
    passo = PASSOS_DO_GRAFO.get(tarefa.get("name", ""))
    if passo is None or "id" not in tarefa:
        return None
    if "result" in tarefa:
        return etapa(tarefa["id"], "fim", passo[1])
    return etapa(tarefa["id"], "inicio", passo[0])


def _pensamento(chunk, ns: tuple) -> dict | None:
    if not isinstance(chunk, AIMessageChunk) or not isinstance(chunk.content, list):
        return None
    conteudo = chunk.content
    texto = "".join(
        str(bloco.get(BLOCOS_DE_PENSAMENTO[bloco["type"]]) or "")
        for bloco in conteudo
        if isinstance(bloco, dict) and bloco.get("type") in BLOCOS_DE_PENSAMENTO
    )
    if not texto:
        return None
    return {
        "type": "pensamento",
        # Especialistas em paralelo pensam ao mesmo tempo: o id deixa o app separá-los.
        "id": ns[0].split(":")[0] if ns else "modelo",
        "delta": remove_secrets(texto),
    }


def traduzir(ns: tuple, modo: str, parte) -> dict | None:
    """Um item de `fluxo.stream(stream_mode=[...], subgraphs=True)` vira um evento do app.

    `ns` vazio é o grafo principal; os agentes dos especialistas são subgrafos, e só com
    `subgraphs=True` o pensamento do modelo e o progresso das tools chegam até aqui.
    """
    if modo == "tasks" and isinstance(parte, dict) and not ns:
        return _etapa_de_tarefa(parte)
    if modo == "custom" and isinstance(parte, dict) and parte.get("type") == "etapa":
        if ns and ":" in ns[0] and "pai" not in parte:
            # O nome do subgrafo é "<nó>:<id da tarefa>": o id da tarefa é o do passo do nó.
            return {**parte, "pai": ns[0].split(":", 1)[1]}
        return parte
    if modo == "messages" and isinstance(parte, tuple) and len(parte) == 2:
        return _pensamento(parte[0], ns)
    return None


class RegistroDoStream:
    """O que o app mostrou além do texto (passos e raciocínio), para a conversa reabrir igual."""

    def __init__(self):
        self.passos: dict[str, dict] = {}
        self.pensamento = ""
        self._ultimo_pensador: str | None = None

    def aplicar(self, evento: dict) -> None:
        if evento["type"] == "etapa":
            atual = self.passos.setdefault(
                evento["id"], {"id": evento["id"], "texto": "", "concluido": False}
            )
            atual["texto"] = evento.get("texto") or atual["texto"]
            atual["concluido"] = evento["status"] == "fim"
            for chave in ("pai", "detalhe"):
                if evento.get(chave):
                    atual[chave] = evento[chave]
        elif evento["type"] == "pensamento":
            if self.pensamento and self._ultimo_pensador != evento["id"]:
                self.pensamento += "\n\n"
            self._ultimo_pensador = evento["id"]
            self.pensamento += evento["delta"]

    def extras(self) -> dict:
        extras: dict = {}
        if self.passos:
            # Passo que ainda girava quando a resposta saiu deixou de ser relevante.
            extras["passos"] = [{**p, "concluido": True} for p in self.passos.values()]
        if self.pensamento:
            extras["pensamento"] = self.pensamento
        return extras


def fatiar(texto: str, tamanho: int = TAMANHO_PEDACO) -> Iterator[str]:
    """Pedaços que terminam em fim de palavra, para o efeito de digitação não quebrar palavras."""
    atual = ""
    for palavra in re.findall(r"\S+\s*", texto):
        atual += palavra
        if len(atual) >= tamanho:
            yield atual
            atual = ""
    if atual:
        yield atual


def eventos_finais(
    resposta: ChatResponse, interrompida: bool, cancelado: Event | None = None
) -> Iterator[dict]:
    """Depois do grafo e do juiz: resultados prontos inteiros, texto aprovado em pedaços, fim."""
    for artefato in resposta.artifacts:
        yield {"type": "artefato", "artifact": artefato.model_dump(mode="json")}
    for acao in resposta.pending_actions:
        yield {
            "type": "cartao",
            "approval_id": acao.id,
            "pending_action": acao.model_dump(mode="json"),
        }
    for pedaco in fatiar(resposta.resposta):
        if cancelado is not None and cancelado.is_set():
            return
        yield {"type": "texto", "delta": pedaco}
        time.sleep(INTERVALO_ENTRE_PEDACOS)
    yield {"type": "fim", "status": "segura" if interrompida else "aprovado"}
