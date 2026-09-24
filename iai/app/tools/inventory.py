from collections.abc import Callable

from langchain_core.tools import tool
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from iai.app.confirmation import prepare_confirmation
from iai.app.domain import DomainError, Purchase, ReadQuery, Withdrawal, failure
from iai.app.security import current_context
from iai.app.services import service


def safe_call(action: Callable) -> dict:
    try:
        return action()
    except DomainError as exc:
        return failure(exc)
    except ValidationError:
        return failure(DomainError("INVALID_PARAMETER", "Parâmetros inválidos; revise campos, quantidade e unidade."))
    except PyMongoError:
        return failure(DomainError("SESSION_UNAVAILABLE", "Não foi possível registrar a confirmação na sessão."))
    except Exception:  # noqa: BLE001 -- contain unexpected tool failures without claiming success
        return failure(DomainError("UNEXPECTED_ERROR", "Erro inesperado; nenhuma operação foi confirmada."))


@tool
def consultar_dados(consulta: ReadQuery) -> dict:
    """Consulta dados reais por recurso e filtros tipados, na cozinha do usuário.

    Recursos: produtos (cadastro global), lotes, pre_lista (somente ACTIVE), estoque
    (posição com mínimo/máximo), catalogo, fornecedores, validade_diaria, alertas,
    valor_categoria, requisicoes, requisicoes_pendentes, itens_requisicao, movimentacoes,
    divergencias, movimentacao_diaria, ranking_requisicoes, painel, abaixo_minimo,
    lotes_atencao, desperdicio (proxy), urgencia, saldo_categoria, tendencia_categoria.
    Para nomes ambíguos consulte produtos e peça o ID correto; não escolha sozinho.
    expiration filtra lotes por EXPIRED/CRITICAL/WARNING/OK/NOT_APPLICABLE.
    period calcula hoje/amanha/ontem/esta_semana/proximos_dias no relógio do banco.
    Nunca aceita SQL, cargo, cozinha ou usuário como parâmetros.
    """
    return safe_call(lambda: service.read(current_context(), consulta))


@tool
def preparar_baixa(baixa: Withdrawal) -> dict:
    """Prepara resumo de consumo sem gravar no PostgreSQL. Exige produto, quantidade,
    unidade e lote quando ambíguo. Somente Estoquista. Retorna confirmação pendente;
    a confirmação pertence ao usuário via API, nunca ao modelo. Não exclui produtos.
    """
    return safe_call(lambda: prepare_confirmation(
        current_context(), service.prepare(current_context(), "withdrawal", baixa)))


@tool
def preparar_requisicao(requisicao: Purchase) -> dict:
    """Prepara resumo de compra sem criar ainda a requisição. Somente Comprador.
    Informe itens com produto, quantidade e unidade; fornecedor e preço são opcionais
    e não devem ser inventados. A execução requer confirmação explícita pelo usuário.
    """
    return safe_call(lambda: prepare_confirmation(
        current_context(), service.prepare(current_context(), "purchase", requisicao)))


for inventory_tool in (consultar_dados, preparar_baixa, preparar_requisicao):
    inventory_tool.handle_validation_error = "INVALID_PARAMETER: revise os parâmetros obrigatórios e suas unidades."

ESTOQUISTA_TOOLS = [consultar_dados, preparar_baixa]
COMPRADOR_TOOLS = [consultar_dados, preparar_requisicao]
SUPERVISOR_TOOLS = [consultar_dados]
