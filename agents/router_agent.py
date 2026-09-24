"""
ROTEADOR
Na arquitetura, o Guardrail de entrada se ramifica em dois caminhos:
  1) FAQ — perguntas sobre o sistema em si (funcionalidades, contatos, "como uso").
  2) Roteador — pedidos operacionais (estoque, requisição, compra, aprovação), que o
     roteador direciona para o agente especializado do CARGO do usuário
     ("Agente ser especializado de acordo com o cargo").

classify_query() cobre a primeira decisão (FAQ x operacional) com uma checagem simples
por palavras-chave — suficiente pois o professor usa esse mesmo estilo de regra direta
nos outros exemplos (ex.: _resolve_type_id). select_specialist() cobre a segunda decisão,
mapeando o cargo já conhecido do usuário (informado pelo app) para o módulo do agente.
"""
from agents import comprador_agent, estoquista_agent, supervisor_agent

PALAVRAS_CHAVE_FAQ = [
    "o que é o inventra", "como funciona o inventra", "como uso", "como usar",
    "funcionalidade", "funcionalidades", "com quem eu falo", "quem eu procuro",
    "contato", "suporte", "ajuda", "não sei usar", "dúvida sobre o sistema",
    "o que o app faz", "o que o sistema faz",
]

AGENTES_POR_CARGO = {
    "estoquista": estoquista_agent,
    "comprador": comprador_agent,
    "supervisor": supervisor_agent,
}


def classify_query(texto: str) -> str:
    """Retorna 'faq' ou 'operacional'."""
    texto_lower = texto.lower()
    for palavra in PALAVRAS_CHAVE_FAQ:
        if palavra in texto_lower:
            return "faq"
    return "operacional"


def select_specialist(cargo: str):
    """Retorna o módulo do agente especializado correspondente ao cargo do usuário."""
    cargo_normalizado = (cargo or "").strip().lower()
    agente = AGENTES_POR_CARGO.get(cargo_normalizado)
    if agente is None:
        raise ValueError(
            f"Cargo '{cargo}' não reconhecido. Use: estoquista, comprador ou supervisor."
        )
    return agente
