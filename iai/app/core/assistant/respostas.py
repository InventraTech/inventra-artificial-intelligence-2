"""Textos que o usuário lê quando o fluxo valida pedidos de ação ou falha.

Nada aqui chama modelo: é formatação determinística, para que a resposta nunca
dependa do LLM e nunca exponha JSON cru, IDs internos desnecessários ou segredos.
"""

from iai.app.core.infra.privacy import remove_secrets

ROTULOS_OPERACAO = {
    "criar_produto": "Cadastrar produto",
    "editar_produto": "Editar produto",
    "desativar_produto": "Desativar produto",
    "cadastrar_fornecedor": "Cadastrar fornecedor",
    "desativar_fornecedor": "Desativar fornecedor",
    "criar_lote": "Criar lote de estoque",
    "registrar_entrada": "Registrar entrada em lote",
    "registrar_saida": "Registrar saída de estoque",
    "ajustar_estoque": "Ajustar estoque",
    "criar_requisicao": "Criar requisição",
    "adicionar_item_requisicao": "Adicionar item à requisição",
    "remover_item_requisicao": "Remover item da requisição",
    "cancelar_requisicao": "Cancelar requisição",
    "registrar_inventario": "Abrir inventário",
    "registrar_contagem": "Registrar contagem",
    "alterar_contagem": "Alterar contagem",
}

ROTULOS_PARAMETRO = {
    "type": "Tipo",
    "origin": "Origem",
    "productId": "Produto",
    "supplierId": "Fornecedor",
    "suggestedSupplierId": "Fornecedor sugerido",
    "batchId": "Lote",
    "requisitionId": "Requisição",
    "inventoryId": "Inventário",
    "itemId": "Item",
    "countId": "Contagem",
    "quantity": "Quantidade",
    "estimatedPrice": "Preço estimado",
    "note": "Observação",
    "reason": "Motivo",
    "name": "Nome",
    "brand": "Marca",
}

VALORES = {
    "PURCHASE": "Compra",
    "TRANSFER": "Transferência",
    "CONSUMPTION": "Consumo",
}

ESTADOS = {
    "AGUARDANDO_APROVACAO": "aguardando sua confirmação",
    "APROVADA_PENDENTE_INTEGRACAO": "confirmada; aguardando integração",
    "CONCLUIDA": "executada",
    "RECUSADA": "recusada",
    "EXPIRADA": "expirada",
    "FALHOU": "falhou",
    "RESULTADO_INCERTO": "resultado incerto; não repetir automaticamente",
}


def _nome_do_alvo(proposta: dict, parametro: str) -> str | None:
    """Nome legível do registro (produto, fornecedor...) guardado no snapshot da proposta."""
    for alvo in proposta.get("before") or []:
        if alvo.get("parameter") != parametro:
            continue
        registro = alvo.get("record") or {}
        return registro.get("name") or (registro.get("product") or {}).get("name")
    return None


def _formatar_valor(chave: str, valor, proposta: dict) -> str:
    if chave.endswith("Id"):
        nome = _nome_do_alvo(proposta, chave)
        return f"{nome} (nº {valor})" if nome else f"nº {valor}"
    return VALORES.get(str(valor), str(valor))


def descrever_proposta(proposta: dict) -> str:
    """Bloco em markdown leve descrevendo uma proposta, sem JSON."""
    acao = proposta.get("action") or {}
    operacao = acao.get("operation", "")
    titulo = ROTULOS_OPERACAO.get(operacao, operacao.replace("_", " ").capitalize())
    linhas = [f"**{titulo}** — proposta `{str(proposta.get('id', ''))[:8]}`"]
    for chave, valor in (acao.get("parameters") or {}).items():
        rotulo = ROTULOS_PARAMETRO.get(chave, chave)
        linhas.append(f"- {rotulo}: {_formatar_valor(chave, valor, proposta)}")
    estado = proposta.get("status", "")
    linhas.append(
        f"- Estado: {estado} — {ESTADOS.get(estado, estado.lower().replace('_', ' '))}"
    )
    return "\n".join(linhas)


def resposta_de_propostas(propostas: list[dict], falhas: list[str]) -> str:
    """Texto final quando o turno validou pedidos de ação (ordem cronológica).

    A revisão legível complementa pending_actions; não altera o payload persistido.
    """
    blocos = [descrever_proposta(p) for p in propostas]
    texto = (
        "\n\n".join(blocos)
        + "\nSomente as propostas listadas foram registradas; outros itens ou dados ainda não confirmados não foram propostos."
    )
    texto += (
        "\n\nNada foi alterado nesta conversa. Revise cada proposta e confirme ou rejeite "
        "com sua própria identidade; a execução depende da confirmação e revalidação."
    )
    if falhas:
        texto += "\n\nEtapas não concluídas — Não consegui concluir:\n" + "\n".join(
            f"- {f}" for f in falhas
        )
    return texto


def _cadeia(exc: BaseException):
    visto = set()
    atual: BaseException | None = exc
    while atual is not None and id(atual) not in visto:
        visto.add(id(atual))
        yield atual
        atual = atual.__cause__ or atual.__context__


def _classificar(exc: BaseException) -> str:
    nomes = " ".join(type(e).__name__.lower() for e in _cadeia(exc))
    codigos = {
        getattr(e, "status_code", None) or getattr(e, "code", None)
        for e in _cadeia(exc)
    }
    if "recursion" in nomes:
        return "passos"
    if "timeout" in nomes or "deadline" in nomes:
        return "timeout"
    if "ratelimit" in nomes or "resourceexhausted" in nomes or 429 in codigos:
        return "limite"
    if (
        "authentication" in nomes
        or "permissiondenied" in nomes
        or "unauthenticated" in nomes
        or codigos & {401, 403}
    ):
        return "acesso"
    if "badrequest" in nomes or "invalidargument" in nomes or 400 in codigos:
        return "formato"
    if "connect" in nomes or "unavailable" in nomes or "internalserver" in nomes:
        return "indisponivel"
    return "interno"


MENSAGENS_DE_FALHA = {
    "passos": (
        "Esse pedido precisou de passos demais e eu parei para não me perder. "
        "Peça uma coisa de cada vez (ex.: primeiro só criar a requisição)."
    ),
    "timeout": "A IA demorou demais para responder. Tente de novo em instantes.",
    "limite": (
        "O provedor de IA atingiu o limite de uso. Aguarde cerca de um minuto "
        "e tente de novo."
    ),
    "acesso": (
        "A IA está sem acesso ao provedor de modelo (chave inválida ou expirada). "
        "Avise o time técnico."
    ),
    "formato": (
        "O modelo recusou a chamada por um problema de formato. "
        "Tente reformular o pedido em uma frase mais simples."
    ),
    "indisponivel": "O provedor de IA está indisponível agora. Tente de novo em instantes.",
    "interno": "Tive um erro interno ao processar seu pedido. Tente de novo.",
}


def mensagem_de_falha(exc: BaseException) -> tuple[str, str]:
    """(mensagem para o usuário, tipo do erro). A mensagem nunca traz texto do SDK."""
    tipo = type(exc).__name__
    return f"{MENSAGENS_DE_FALHA[_classificar(exc)]} (código: {tipo})", tipo


def resumo_para_log(exc: BaseException) -> str:
    """Cadeia de tipos + mensagem curta sem segredos, para diagnosticar no Render."""
    cadeia = " <- ".join(type(e).__name__ for e in _cadeia(exc))
    return f"{cadeia}: {remove_secrets(str(exc))[:500]}"
