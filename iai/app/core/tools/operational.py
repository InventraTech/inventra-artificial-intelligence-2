"""Tools leem dados ou CRIAM PROPOSTAS; não expõem execução/decisão ao modelo."""

import json
from decimal import Decimal

from fastapi import HTTPException
from langchain_core.tools import StructuredTool, tool
from pydantic import Field, ValidationError, create_model, model_validator
from pymongo.errors import PyMongoError

from iai.app.core.assistant.tool_gate import protect_tool
from iai.app.core.config import STORAGE_MODE, business_date
from iai.app.core.infra.retrieval import normalize
from iai.app.core.infra.security import context
from iai.app.core.inventra.acoes import solicitar_confirmacao
from iai.app.core.inventra.approvals import (
    get_proposal,
    list_proposals,
    public,
)
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import (
    BUY_OPERATIONS,
    PARAMETERS,
    STOCK_OPERATIONS,
    Action,
    Operation,
    PositiveId,
    Quantity,
    Query,
    StrictModel,
    ToolResult,
)


@tool(args_schema=Query)
def consultar_inventra(**parameters) -> str:
    """Consulta produtos, estoque, lotes, validade, fornecedores, requisições, inventários, alertas e catálogos autorizados. Não altera dados. Para o saldo geral do estoque da cozinha use resource=estoque sem product_id; com product_id retorna os parâmetros e o saldo daquele produto. O resource=produtos é o catálogo global e exige name. O resource=consumo devolve o ranking dos produtos que mais saem (consumo estimado pelos lotes: quantidade inicial menos atual), do maior para o menor; nele a unidade vem em product.unit."""
    try:
        query = Query.model_validate(parameters)
        if query.resource == "produtos" and not (query.name or query.product_id):
            # O catálogo é global (todas as cozinhas): nunca listar sem filtro.
            return ToolResult(
                ok=False,
                code="USE_ESTOQUE",
                message=(
                    "O catálogo de produtos não é da cozinha. Para saber o que a cozinha tem, "
                    "use resource=estoque (sem product_id). Use produtos apenas com name para "
                    "localizar um produto específico."
                ),
                source="IAI",
                mocked=False,
            ).model_dump_json()
        outcome = gateway().query(query, context())
        return outcome.model_dump_json()
    except PyMongoError:
        return ToolResult(
            ok=False,
            code="PERSISTENCIA_INDISPONIVEL",
            message="Persistência indisponível.",
            source="IAI",
            mocked=False,
        ).model_dump_json()
    except HTTPException as exc:
        return ToolResult(
            ok=False,
            code="SEM_PERMISSAO",
            message=str(exc.detail),
            source="IAI",
            mocked=False,
        ).model_dump_json()


class PlanoDeBaixa(StrictModel):
    product_name: str | None = Field(
        default=None,
        max_length=150,
        description="Nome (ou parte do nome) do produto citado pelo usuário.",
    )
    product_id: PositiveId | None = Field(
        default=None,
        description="ID do produto, quando já confirmado por consulta anterior.",
    )
    quantity: Quantity = Field(
        description="Quantidade a baixar, exatamente como o usuário disse."
    )
    unit: str | None = Field(
        default=None,
        max_length=20,
        description=(
            "Unidade que o usuário disse junto da quantidade (kg, g, L, ml, un...). "
            "Omita se ele não disse nenhuma."
        ),
    )

    @model_validator(mode="after")
    def produto_obrigatorio(self):
        if not self.product_name and not self.product_id:
            raise ValueError("Informe product_name ou product_id.")
        return self


def _texto_quantidade(valor) -> str:
    return format(Decimal(str(valor)).normalize(), "f")


def _plano_resultado(code, message, data, outcome) -> str:
    return ToolResult(
        ok=True,
        code=code,
        message=message,
        data=data,
        source=outcome.source,
        mocked=outcome.mocked,
        incomplete=outcome.incomplete,
    ).model_dump_json()


@tool(args_schema=PlanoDeBaixa)
def planejar_baixa(**parameters) -> str:
    """Descobre o produto pelo NOME e monta o plano de baixa por lote, consumindo primeiro o que vence primeiro (FEFO), só com lotes ativos, não vencidos e com saldo. Não altera dados e dispensa pedir o número do lote ao usuário. Use ANTES de propor_registrar_saida: depois chame propor_registrar_saida uma vez por item do plano, com batchId e quantity exatos."""
    try:
        plano = PlanoDeBaixa.model_validate(parameters)
        consulta = Query(
            resource="lotes",
            product_id=plano.product_id,
            name=None if plano.product_id else plano.product_name,
            size=100,
        )
        outcome = gateway().query(consulta, context())
        if not outcome.ok:
            return outcome.model_dump_json()
        dados = outcome.data
        lotes: list = (
            dados.get("content", [])
            if isinstance(dados, dict)
            else (dados if isinstance(dados, list) else [])
        )
        hoje = business_date().isoformat()

        def disponivel(lote):
            validade = lote.get("expirationDate")
            return (
                lote.get("status") == "ACTIVE"
                and Decimal(str(lote.get("currentQuantity") or 0)) > 0
                and not (validade and validade < hoje)
            )

        por_produto: dict = {}
        for lote in lotes:
            produto = lote.get("product") or {}
            if produto.get("id") is None:
                continue
            por_produto.setdefault(produto["id"], {"produto": produto, "lotes": []})
            por_produto[produto["id"]]["lotes"].append(lote)
        if not por_produto:
            return _plano_resultado(
                "SEM_LOTES",
                "Nenhum lote da cozinha corresponde a esse produto. Consulte "
                "consultar_inventra(resource=estoque) para ver o que existe e confirme "
                "o nome com o usuário, sem pedir ID.",
                None,
                outcome,
            )
        com_saldo = {
            k: v for k, v in por_produto.items() if any(map(disponivel, v["lotes"]))
        }
        if not com_saldo:
            nomes = sorted(
                {v["produto"].get("name", "?") for v in por_produto.values()}
            )
            return _plano_resultado(
                "SEM_SALDO",
                "O produto existe, mas não há lote ativo, não vencido e com saldo. "
                "Informe isso ao usuário; não proponha baixa.",
                {"produtos": nomes},
                outcome,
            )
        escolhido = None
        if len(com_saldo) == 1:
            escolhido = next(iter(com_saldo.values()))
        elif plano.product_name:
            exato = [
                v
                for v in com_saldo.values()
                if normalize(v["produto"].get("name", ""))
                == normalize(plano.product_name)
            ]
            if len(exato) == 1:
                escolhido = exato[0]
        if escolhido is None:
            candidatos = [
                {
                    "productId": k,
                    "name": v["produto"].get("name"),
                    "unit": (v["produto"].get("unit") or {}).get("symbol"),
                    "available": _texto_quantidade(
                        sum(
                            Decimal(str(lote["currentQuantity"]))
                            for lote in v["lotes"]
                            if disponivel(lote)
                        )
                    ),
                }
                for k, v in com_saldo.items()
            ]
            return _plano_resultado(
                "PRODUTO_AMBIGUO",
                "Mais de um produto corresponde. Pergunte ao usuário qual deles (pelo "
                "nome, marca ou unidade) e chame planejar_baixa de novo com product_id.",
                {"candidatos": candidatos},
                outcome,
            )
        produto = escolhido["produto"]
        unidade = (produto.get("unit") or {}).get("symbol")
        pedido = plano.quantity
        if plano.unit and unidade:
            from iai.app.core.assistant.artifacts import grandeza

            grandeza_pedida, fator_pedido = grandeza(plano.unit)
            grandeza_produto, fator_produto = grandeza(unidade)
            if grandeza_pedida != grandeza_produto:
                return _plano_resultado(
                    "UNIDADE_INCOMPATIVEL",
                    f"O usuário falou em {plano.unit}, mas este produto é controlado em "
                    f"{unidade}; não dá para converter com segurança. Pergunte quanto é "
                    f"em {unidade}. Não proponha agora.",
                    {"product": {"name": produto.get("name"), "unit": unidade}},
                    outcome,
                )
            pedido = (plano.quantity * fator_pedido / fator_produto).quantize(
                Decimal("0.001")
            )
        ativos = sorted(
            (lote for lote in escolhido["lotes"] if disponivel(lote)),
            key=lambda lote: (
                lote.get("expirationDate") or "9999-12-31",
                lote.get("entryDate") or "",
                lote["id"],
            ),
        )
        total = sum(Decimal(str(lote["currentQuantity"])) for lote in ativos)
        identificacao = {
            "id": produto.get("id"),
            "name": produto.get("name"),
            "unit": unidade,
        }
        if total < pedido:
            return _plano_resultado(
                "SALDO_INSUFICIENTE",
                "O saldo disponível é menor que o pedido. Informe o saldo ao usuário e "
                "pergunte se quer baixar a quantidade disponível; não proponha agora.",
                {
                    "product": identificacao,
                    "requested": _texto_quantidade(pedido),
                    "availableTotal": _texto_quantidade(total),
                },
                outcome,
            )
        restante, itens = pedido, []
        for lote in ativos:
            if restante <= 0:
                break
            saldo = Decimal(str(lote["currentQuantity"]))
            retirar = min(saldo, restante)
            itens.append(
                {
                    "batchId": lote["id"],
                    "batchNumber": lote.get("batchNumber"),
                    "quantity": _texto_quantidade(retirar),
                    "availableInBatch": _texto_quantidade(saldo),
                    "expirationDate": lote.get("expirationDate"),
                }
            )
            restante -= retirar
        return _plano_resultado(
            "PLANO_PRONTO",
            "Plano de baixa por validade (FEFO) pronto, já na unidade do produto. Chame "
            "propor_registrar_saida uma vez por item de plan, com batchId e quantity "
            "exatos, e justification curta. Não pergunte o lote nem a unidade ao usuário.",
            {
                "product": identificacao,
                "requested": _texto_quantidade(pedido),
                "plan": itens,
            },
            outcome,
        )
    except ValidationError:
        return ToolResult(
            ok=False,
            code="PARAMETROS_INVALIDOS",
            message="Informe o produto (nome) e a quantidade.",
            source="IAI",
            mocked=False,
        ).model_dump_json()
    except PyMongoError:
        return ToolResult(
            ok=False,
            code="PERSISTENCIA_INDISPONIVEL",
            message="Persistência indisponível.",
            source="IAI",
            mocked=False,
        ).model_dump_json()
    except HTTPException as exc:
        return ToolResult(
            ok=False,
            code="SEM_PERMISSAO",
            message=str(exc.detail),
            source="IAI",
            mocked=False,
        ).model_dump_json()


@tool
def consultar_solicitacoes_iai(solicitacao_id: str = "") -> str:
    """Consulta estado e resultado de propostas no escopo autenticado; não aprova nem executa."""
    try:
        data = (
            public(get_proposal(solicitacao_id, context()))
            if solicitacao_id
            else list_proposals(context())
        )
        return ToolResult(
            ok=True,
            code="OK",
            message="Estado persistido; verifique source/mocked do resultado de negócio.",
            data=data,
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()
    except HTTPException:
        return ToolResult(
            ok=False,
            code="NAO_ENCONTRADO",
            message="Solicitação não encontrada no seu escopo.",
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()
    except PyMongoError:
        return ToolResult(
            ok=False,
            code="PERSISTENCIA_INDISPONIVEL",
            message="Estado indisponível; não inferir execução.",
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()


def proposal_tool(operation):
    schema = create_model(
        f"Proposta_{operation.value}",
        __base__=PARAMETERS[operation],
        justification=(str, Field(min_length=1, max_length=1000)),
    )

    def prepare(**parameters):
        try:
            ctx = context()
            justification = parameters.pop("justification")
            action = Action(operation=operation, parameters=parameters)
            proposal = solicitar_confirmacao(action, justification, ctx)
            return json.dumps(
                {
                    "ok": True,
                    "code": "AGUARDANDO_APROVACAO",
                    "executed": False,
                    "message": "Proposta registrada. Nenhuma alteração de negócio foi executada.",
                    "proposal": proposal,
                },
                default=str,
                ensure_ascii=False,
            )
        except (HTTPException, ValidationError) as exc:
            message = (
                str(exc.detail)
                if isinstance(exc, HTTPException)
                else "Parâmetros inválidos; peça os dados necessários."
            )
            return json.dumps(
                {
                    "ok": False,
                    "code": "PROPOSTA_NAO_CRIADA",
                    "executed": False,
                    "message": message,
                },
                ensure_ascii=False,
            )
        except PyMongoError:
            return '{"ok":false,"code":"PERSISTENCIA_INDISPONIVEL","executed":false,"message":"Não foi possível confirmar o registro da proposta. Consulte suas solicitações antes de repetir."}'

    return StructuredTool.from_function(
        prepare,
        name=f"propor_{operation.value}",
        description=f"Prepara {operation.value} para revisão e confirmação do próprio solicitante. NÃO EXECUTA. Confirme IDs/dados; nunca invente.",
        args_schema=schema,
    )


PROPOSAL_TOOLS = {operation: proposal_tool(operation) for operation in Operation}
for operational_tool in [
    consultar_inventra,
    consultar_solicitacoes_iai,
    planejar_baixa,
    *PROPOSAL_TOOLS.values(),
]:
    protect_tool(operational_tool)
    operational_tool.handle_validation_error = "Parâmetros incompletos ou inválidos. Consulte os IDs e peça os campos necessários; nenhuma operação foi executada."
STOCK_TOOLS = [consultar_inventra, consultar_solicitacoes_iai, planejar_baixa] + [
    PROPOSAL_TOOLS[o] for o in Operation if o in STOCK_OPERATIONS
]
BUY_TOOLS = [consultar_inventra, consultar_solicitacoes_iai] + [
    PROPOSAL_TOOLS[o] for o in Operation if o in BUY_OPERATIONS
]
SUPERVISOR_TOOLS = [
    consultar_inventra,
    consultar_solicitacoes_iai,
    planejar_baixa,
] + list(PROPOSAL_TOOLS.values())
