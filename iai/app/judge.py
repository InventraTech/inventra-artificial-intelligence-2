import json
from collections.abc import Sequence
from typing import Literal

import httpx
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import BaseModel, Field

from iai.app.config import JEV_HTTP_TIMEOUT, JEV_MODEL, TYPESAFE_API_KEY
from iai.app.privacy import remove_secrets
from iai.app.prompts import JEV_ALUCINACAO_INSTRUCTIONS, JEV_TOOL_INSTRUCTIONS
from iai.app.schemas import ResultadoJuiz

MAX_TENTATIVAS_JUIZ = 3

LIMIAR_CONFIANCA_JUIZ = 0.7

FATOS_VAZIOS = (
    "(Nenhuma ferramenta foi chamada nesta rodada — não há nenhum fato retornado "
    "pelo sistema para embasar a resposta.)"
)


def extrair_texto(mensagem: BaseMessage) -> str:
    """Normaliza o content de uma BaseMessage (str ou list) para str puro."""
    conteudo = mensagem.content
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return " ".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in conteudo
        )
    return str(conteudo)


def extrair_pergunta_fatos_e_resposta(
    mensagens: Sequence[BaseMessage],
) -> tuple[str, str, str]:
    """
    Varre a rodada atual (do fim até a última mensagem humana) e separa:
    - pergunta: a pergunta original do usuário que originou a rodada.
    - fatos: conteúdo bruto devolvido pelas ferramentas chamadas pelo especialista.
    - resposta: última resposta do especialista, a ser auditada pelo juiz.
    """
    fatos: list[str] = []
    resposta = ""
    pergunta = ""

    for mensagem in reversed(mensagens):
        if mensagem.type == "human":
            if mensagem.additional_kwargs.get("juiz_feedback"):
                continue
            pergunta = extrair_texto(mensagem)
            break
        if mensagem.type == "tool":
            fatos.append(extrair_texto(mensagem))
        elif mensagem.type == "ai" and mensagem.content and not resposta:
            resposta = extrair_texto(mensagem)

    return pergunta, "\n".join(reversed(fatos)), resposta


def ultima_mensagem_ai(mensagens: Sequence[BaseMessage]) -> BaseMessage | None:
    for mensagem in reversed(mensagens):
        if mensagem.type == "ai" and mensagem.content:
            return mensagem
    return None


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevNoulAnswer(BaseModel):
    type: Literal["noul"]
    noul: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


class JevAnswers(BaseModel):
    fundamentada: JevNoulAnswer


class JevResponse(BaseModel):
    answers: JevAnswers


def avaliar_alucinacao(pergunta: str, fatos: str, resposta: str) -> ResultadoJuiz:
    """
    JEV/TypeSafe estima a probabilidade de a resposta estar fundamentada nos fatos.
    O campo legado confianca recebe esse Noul (não a certeza de uma classe).
    O grafo continua responsável pelo limiar e pelas tentativas de correção.
    """
    return _avaliar_jev(
        {"pergunta": pergunta, "fatos": fatos or FATOS_VAZIOS, "resposta": resposta},
        JEV_ALUCINACAO_INSTRUCTIONS,
    )


def avaliar_chamada_tool(mensagens, ferramenta, argumentos, descricao, schema):
    """Avalia a chamada proposta sem executar a ferramenta nem consultar o negócio."""
    pergunta, fatos, _ = extrair_pergunta_fatos_e_resposta(mensagens)
    return _avaliar_jev(
        {
            "pergunta": pergunta,
            "fatos": fatos or FATOS_VAZIOS,
            "contexto": "\n".join(
                f"{message.type}: {extrair_texto(message)}"
                for message in mensagens[:-1]
            ),
            "ferramenta": ferramenta,
            "descricao": descricao,
            "parametros": json.dumps(argumentos, ensure_ascii=False),
            "schema": json.dumps(schema, ensure_ascii=False),
        },
        JEV_TOOL_INSTRUCTIONS,
    )


@wrap_tool_call
def jev_antes_da_tool(request, handler):
    """O handler só pode executar a chamada depois da aprovação do JEV."""
    if request.tool is not None:
        result = avaliar_chamada_tool(
            request.state["messages"],
            request.tool.name,
            request.tool_call["args"],
            request.tool.description,
            request.tool.args,
        )
        if result.confianca >= LIMIAR_CONFIANCA_JUIZ:
            return handler(request)
    return ToolMessage(
        content=json.dumps(
            {
                "ok": False,
                "code": "JEV_TOOL_BLOQUEADA",
                "executed": False,
                "message": "Chamada não aprovada pelo JEV. Esta ferramenta não foi executada. Corrija os parâmetros com base no pedido e nos dados disponíveis ou peça esclarecimento.",
            },
            ensure_ascii=False,
        ),
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
        status="error",
    )


def _avaliar_jev(state: dict[str, str], instructions: str) -> ResultadoJuiz:
    api_key = TYPESAFE_API_KEY.get_secret_value()
    if not api_key:
        return ResultadoJuiz(confianca=0.0, motivo="jev_nao_configurado")

    try:
        state = {
            name: remove_secrets(text).replace(api_key, "[SEGREDO_REMOVIDO]")
            for name, text in state.items()
        }
        with httpx.Client(timeout=JEV_HTTP_TIMEOUT, follow_redirects=False) as client:
            response = client.post(
                JEV_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": JEV_MODEL,
                    "state": state,
                    "questions": {
                        "fundamentada": {
                            "type": "noul",
                            "instructions": instructions,
                            "criteria": {
                                "true": "A condição definida nas instruções é integralmente atendida pelos dados fornecidos.",
                                "false": "A condição definida nas instruções não é atendida ou faltam dados para confirmá-la.",
                            },
                        }
                    },
                },
            )
            response.raise_for_status()
            probability = JevResponse.model_validate(
                response.json()
            ).answers.fundamentada.noul
        return ResultadoJuiz(
            confianca=probability,
            motivo="jev:resposta_fundamentada"
            if probability >= LIMIAR_CONFIANCA_JUIZ
            else "jev:fundamentacao_insuficiente",
        )
    except (httpx.HTTPError, ValueError):
        # Erros HTTP, timeout ou contrato inválido bloqueiam; nunca usar fallback LLM.
        # Não persistir corpo, headers ou mensagem da exceção: podem conter segredos.
        return ResultadoJuiz(confianca=0.0, motivo="erro_api_juiz")
