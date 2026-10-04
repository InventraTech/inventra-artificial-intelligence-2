from collections.abc import Sequence

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate

from iai.app.llms import llm_rapido
from iai.app.prompts import JUIZ_ALUCINACAO_SYSTEM_PROMPT
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


def extrair_pergunta_fatos_e_resposta(mensagens: Sequence[BaseMessage]) -> tuple[str, str, str]:
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


juiz_prompt = ChatPromptTemplate.from_messages([
    ("system", JUIZ_ALUCINACAO_SYSTEM_PROMPT),
    ("human", (
        "PERGUNTA DO USUÁRIO:\n{pergunta}\n\n"
        "FATOS DISPONÍVEIS:\n{fatos}\n\n"
        "RESPOSTA DO ESPECIALISTA:\n{resposta}"
    )),
])

juiz_chain = juiz_prompt | llm_rapido.with_structured_output(ResultadoJuiz)


def avaliar_alucinacao(pergunta: str, fatos: str, resposta: str) -> ResultadoJuiz:
    """
    Usa o modelo rápido para avaliar, com uma nota de confiança de 0.0 a 1.0, se a
    resposta do especialista está ancorada nos fatos brutos retornados pelas
    ferramentas, à luz da pergunta original. Quem decide aprovar ou não com base
    nessa nota é o chamador (ver LIMIAR_CONFIANCA_JUIZ).
    """
    try:
        resultado = juiz_chain.invoke({"pergunta": pergunta, "fatos": fatos, "resposta": resposta})
        assert isinstance(resultado, ResultadoJuiz)
        return resultado
    except Exception:  # noqa: BLE001 - fail-safe: erro na chamada ao LLM bloqueia a resposta
        return ResultadoJuiz(confianca=0.0, motivo="erro_api_juiz")
