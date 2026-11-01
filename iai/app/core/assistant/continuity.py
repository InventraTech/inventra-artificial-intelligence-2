"""Impede que um especialista "prometa" uma consulta e pare sem executá-la.

Sintoma observado em produção: o modelo responde "Primeiro, vou buscar os lotes
disponíveis" como TEXTO FINAL, sem chamar nenhuma ferramenta. O usuário confirma
("pode procurar") e o modelo repete a mesma frase, de modo que a proposta (e o cartão
de aprovação) nunca chega a existir.

A correção é só de controle de fluxo: quando a resposta do modelo é uma promessa de
ação, sem pergunta e sem chamada de ferramenta, a mesma requisição é refeita uma vez
com um lembrete que NÃO fica salvo no histórico. Nenhuma ferramenta é chamada por este
módulo: o modelo continua escolhendo a chamada, que segue passando pelo JEV.
"""

import re

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from iai.app.core.infra.retrieval import normalize

_VERBOS = (
    "buscar|consultar|verificar|procurar|checar|pesquisar|localizar|levantar|conferir|"
    "olhar|analisar|listar|gerar|montar|criar|registrar|propor|preparar|calcular|ver|"
    "separar|comparar|dar baixa"
)
_PROMESSA = re.compile(
    rf"\b(vou|irei|vamos|ja vou|agora vou|primeiro vou|deixa eu|deixe me)\s+(?:\w+\s+){{0,2}}(?:{_VERBOS})\b"
    r"|\b(um momento|so um instante|um instante|aguarde um|ja volto)\b"
)

LEMBRETE = (
    "Você acabou de dizer que ia executar uma etapa, mas não chamou nenhuma ferramenta. "
    "Não anuncie: chame AGORA a ferramenta necessária (consultar_inventra, "
    "planejar_baixa, propor_*...) e só responda ao usuário depois de ter o resultado. "
    "Se falta algum dado que só o usuário sabe, pergunte de forma objetiva, com ponto "
    "de interrogação, em vez de prometer."
)


def eh_promessa_vazia(mensagem) -> bool:
    """True se a resposta só anuncia uma ação futura, sem ferramenta e sem pergunta."""
    if not isinstance(mensagem, AIMessage) or getattr(mensagem, "tool_calls", None):
        return False
    conteudo = mensagem.content
    if isinstance(conteudo, list):
        conteudo = " ".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in conteudo
        )
    texto = normalize(str(conteudo or "")).strip()
    if not texto or "?" in texto:
        return False
    return bool(_PROMESSA.search(texto))


class SemPromessaVazia(AgentMiddleware):
    """Refaz UMA vez a chamada ao modelo quando ele só promete agir."""

    def wrap_model_call(self, request, handler):
        resposta = handler(request)
        resultado = getattr(resposta, "result", None)
        if not resultado or not eh_promessa_vazia(resultado[-1]):
            return resposta
        nova = request.override(
            messages=[*request.messages, resultado[-1], HumanMessage(content=LEMBRETE)]
        )
        segunda = handler(nova)
        # Se o modelo insistir na promessa, a resposta original segue como está.
        return segunda
