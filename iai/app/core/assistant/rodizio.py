"""Rodízio de chaves e provedores de LLM.

Cada papel (roteador, guardrail, especialista) tem uma lista de candidatos em ordem de preferência: uma
entrada por chave de cada provedor. Quando uma chamada falha por limite (429), crédito (402), chave ou modelo
indisponível (401/403/404), erro do provedor (5xx) ou timeout, a MESMA requisição segue para o próximo
candidato e o usuário não percebe. A falha vai só para o log.

Quem falhou por motivo que se repete (limite, crédito, acesso) fica "em espera" por um tempo: as próximas
chamadas pulam esse candidato sem gastar uma ida à rede e voltam a ele quando a espera termina.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from pydantic import Field

from iai.app.core.assistant.responses import _cadeia, resumo_para_log

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

ESPERA_LIMITE = 60.0
ESPERA_LIMITE_DIARIO = 1800.0
ESPERA_CREDITO = 300.0
ESPERA_ACESSO = 1800.0
ESPERA_INDISPONIVEL = 20.0
ESPERA_MAXIMA = 3600.0
ESPERA_MINIMA = 5.0

_LIMITE_DIARIO = re.compile(
    r"per\s*day|daily|tokens per day|requests per day", re.IGNORECASE
)
_SEM_CREDITO = re.compile(
    r"insufficient[_ ]quota|insufficient credits|payment required", re.IGNORECASE
)
_CHAVE_INVALIDA = re.compile(
    r"api key not valid|invalid api key|api_key_invalid|incorrect api key", re.IGNORECASE
)
_ESPERA_NO_TEXTO = re.compile(
    r"(?:try again|retry)\w*\s+(?:in|after)\s+((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)\s*)+)",
    re.IGNORECASE,
)
_PARTE_DA_ESPERA = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", re.IGNORECASE)
_UNIDADES = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
_NOMES_DE_REDE = ("timeout", "connect", "unavailable", "internalserver", "overloaded")


@dataclass(frozen=True)
class Falha:
    motivo: str  # limite | credito | acesso | indisponivel | pedido
    status: int | None
    espera: float  # segundos em que o candidato não deve ser tentado; 0 = tentar de novo na próxima


def _status(exc: BaseException) -> int | None:
    for erro in _cadeia(exc):
        for atributo in ("status_code", "code"):
            valor = getattr(erro, atributo, None)
            if isinstance(valor, int) and not isinstance(valor, bool):
                return valor
    return None


def _espera_sugerida(exc: BaseException) -> float | None:
    """Retry-After do provedor (cabeçalho ou "try again in 7m12.5s" na mensagem), se houver."""
    for erro in _cadeia(exc):
        cabecalhos = getattr(getattr(erro, "response", None), "headers", None)
        try:
            bruto = cabecalhos.get("retry-after") if cabecalhos is not None else None
            if bruto is not None:
                return float(bruto)
        except (AttributeError, TypeError, ValueError):
            pass
        achado = _ESPERA_NO_TEXTO.search(str(erro))
        if achado:
            return sum(
                float(n) * _UNIDADES[u.lower()]
                for n, u in _PARTE_DA_ESPERA.findall(achado.group(1))
            )
    return None


def classificar_falha(exc: BaseException) -> Falha:
    """Diz se a falha é do candidato (e por quanto tempo evitá-lo) ou do pedido em si."""
    nomes = " ".join(type(e).__name__.lower() for e in _cadeia(exc))
    texto = " ".join(str(e) for e in _cadeia(exc))
    status = _status(exc)
    if status is None and ("ratelimit" in nomes or "resourceexhausted" in nomes):
        status = 429
    if status == 402 or (status == 429 and _SEM_CREDITO.search(texto)):
        return Falha("credito", status, ESPERA_CREDITO)
    if status == 429:
        espera = _espera_sugerida(exc) or ESPERA_LIMITE
        if _LIMITE_DIARIO.search(texto):
            espera = max(espera, ESPERA_LIMITE_DIARIO)
        return Falha("limite", status, min(max(espera, ESPERA_MINIMA), ESPERA_MAXIMA))
    if (
        status in {401, 403, 404}
        or "authentication" in nomes
        or "permissiondenied" in nomes
        or _CHAVE_INVALIDA.search(texto)
    ):
        return Falha("acesso", status, ESPERA_ACESSO)
    if (status is not None and status >= 500) or status == 408 or any(
        n in nomes for n in _NOMES_DE_REDE
    ):
        return Falha("indisponivel", status, ESPERA_INDISPONIVEL)
    return Falha("pedido", status, 0.0)


class Registro:
    """Quais candidatos estão em espera e até quando. Compartilhado entre as threads do grafo."""

    def __init__(self, relogio: Callable[[], float] = time.monotonic) -> None:
        self._relogio = relogio
        self._ate: dict[str, float] = {}
        self._voltas: dict[str, int] = {}
        self._trava = threading.Lock()

    def esfriar(self, rotulo: str, segundos: float) -> None:
        with self._trava:
            self._ate[rotulo] = max(
                self._ate.get(rotulo, 0.0), self._relogio() + segundos
            )

    def restante(self, rotulo: str) -> float:
        with self._trava:
            return max(0.0, self._ate.get(rotulo, 0.0) - self._relogio())

    def ordem(self, rotulos: Sequence[str]) -> list[int]:
        """Índices na ordem de tentativa. Os prontos seguem a preferência entre provedores, mas as
        chaves do mesmo grupo (ex.: as seis da Groq) se revezam, para dividir o limite de cada uma em
        vez de esgotar a primeira. Depois vêm, como último recurso, os em espera (o que volta antes
        primeiro): nada fica de fora, porque se todos estiverem em espera a estimativa pode estar errada."""
        esperas = [self.restante(r) for r in rotulos]
        prontos = [i for i, e in enumerate(esperas) if e == 0.0]
        em_espera = sorted(
            (i for i, e in enumerate(esperas) if e > 0.0), key=lambda i: esperas[i]
        )
        return self._revezar(rotulos, prontos) + em_espera

    def _revezar(self, rotulos: Sequence[str], indices: list[int]) -> list[int]:
        ordem: list[int] = []
        inicio = 0
        while inicio < len(indices):
            grupo = rotulos[indices[inicio]].partition("#")[0]
            fim = inicio
            while fim < len(indices) and rotulos[indices[fim]].partition("#")[0] == grupo:
                fim += 1
            bloco = indices[inicio:fim]
            with self._trava:
                volta = self._voltas.get(grupo, 0)
                self._voltas[grupo] = volta + 1
            deslocamento = volta % len(bloco)
            ordem += bloco[deslocamento:] + bloco[:deslocamento]
            inicio = fim
        return ordem

    def limpar(self) -> None:
        with self._trava:
            self._ate.clear()
            self._voltas.clear()


registro = Registro()


class Vigia(BaseCallbackHandler):
    """Fica preso ao modelo de UM candidato: se a chamada falha, põe o candidato em espera e loga."""

    raise_error = False

    def __init__(self, rotulo: str) -> None:
        self.rotulo = rotulo

    def on_llm_error(self, error: BaseException, **kwargs) -> None:
        falha = classificar_falha(error)
        if falha.espera:
            registro.esfriar(self.rotulo, falha.espera)
        logger.warning(
            "LLM %s falhou (%s, HTTP %s)%s: %s",
            self.rotulo,
            falha.motivo,
            falha.status or "-",
            f"; em espera por {falha.espera:.0f}s" if falha.espera else "",
            resumo_para_log(error),
        )


class ComRodizio(RunnableWithFallbacks):
    """`with_fallbacks` que pula candidatos em espera e tenta primeiro os que estão prontos.

    `bind_tools`, `with_structured_output` e afins continuam valendo: a classe base reaplica a chamada
    em cada candidato, e a ordem de `rotulos` acompanha a de `[runnable, *fallbacks]`.
    """

    rotulos: list[str] = Field(default_factory=list)

    @property
    def runnables(self) -> Iterator[Runnable]:
        todos = [self.runnable, *self.fallbacks]
        if len(self.rotulos) != len(todos):
            yield from todos
            return
        for i in registro.ordem(self.rotulos):
            yield todos[i]


def montar(candidatos: Sequence[tuple[str, Runnable]]) -> ComRodizio:
    """`candidatos`: (rótulo, modelo) em ordem de preferência. Cada modelo deve levar o `Vigia` do seu rótulo."""
    (_, primeiro), *resto = candidatos
    return ComRodizio(
        runnable=primeiro,
        fallbacks=[modelo for _, modelo in resto],
        rotulos=[rotulo for rotulo, _ in candidatos],
    )


def tentar_em_rodizio(candidatos: Sequence[tuple[str, T]], chamar: Callable[[T], R]) -> R:
    """O mesmo rodízio para quem chama o provedor direto (áudio, embeddings): tenta cada candidato na ordem
    do registro, põe em espera o que falhar e devolve o primeiro resultado. Se todos falham, sobe o primeiro erro."""
    primeiro: BaseException | None = None
    for i in registro.ordem([rotulo for rotulo, _ in candidatos]):
        rotulo, candidato = candidatos[i]
        try:
            return chamar(candidato)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de um candidato passa a vez ao próximo
            Vigia(rotulo).on_llm_error(exc)
            primeiro = primeiro or exc
    raise primeiro or RuntimeError("Nenhum candidato configurado.")
