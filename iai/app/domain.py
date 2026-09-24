"""Validated business inputs. Identity is never a tool argument."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveId = Annotated[int, Field(gt=0, strict=True)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
Role = Literal["estoquista", "comprador", "supervisor"]


class DomainError(Exception):
    def __init__(self, code: str, message: str, data: Any = None):
        self.code, self.message, self.data = code, message, data
        super().__init__(message)


def result(data: Any, **extra: Any) -> dict:
    return {"success": True, "data": data,
            "count": len(data) if isinstance(data, list) else 1, "message": None, **extra}


def failure(error: DomainError) -> dict:
    return {"success": False, "code": error.code, "message": error.message,
            "data": error.data, "count": 0}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadQuery(Input):
    resource: Literal[
        "lotes", "estoque", "produtos", "fornecedores", "catalogo", "validade_diaria",
        "alertas", "valor_categoria", "requisicoes", "requisicoes_pendentes",
        "itens_requisicao", "movimentacoes", "divergencias", "movimentacao_diaria",
        "ranking_requisicoes", "painel", "abaixo_minimo", "lotes_atencao",
        "desperdicio", "urgencia", "saldo_categoria", "tendencia_categoria", "pre_lista",
    ]
    product: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    product_id: PositiveId | None = None
    supplier: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    supplier_id: PositiveId | None = None
    category: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    batch_id: PositiveId | None = None
    requisition_id: PositiveId | None = None
    status: Literal["ACTIVE", "WRITTEN_OFF", "EXPIRED", "CANCELLED", "UNDER_REVIEW",
                    "APPROVED", "REJECTED", "BELOW_MINIMUM", "ABOVE_MAXIMUM", "NORMAL"] | None = None
    expiration: Literal["EXPIRED", "CRITICAL", "WARNING", "OK", "NOT_APPLICABLE"] | None = None
    period: Literal["hoje", "amanha", "ontem", "esta_semana", "proximos_dias"] | None = None
    days: Annotated[int, Field(ge=1, le=366, strict=True)] | None = None
    start: date | None = None
    end: date | None = None
    limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 25

    @model_validator(mode="after")
    def dates(self) -> "ReadQuery":
        if self.start and self.end and self.start > self.end:
            raise ValueError("A data inicial deve preceder a final.")
        if self.period and (self.start or self.end):
            raise ValueError("Use período relativo ou datas explícitas.")
        if (self.period == "proximos_dias") != (self.days is not None):
            raise ValueError("Informe days somente para proximos_dias.")
        return self


class Withdrawal(Input):
    product_id: PositiveId
    quantity: Quantity
    unit: Annotated[str, Field(min_length=1, max_length=60)]
    batch_id: PositiveId | None = None


class PurchaseItem(Input):
    product_id: PositiveId
    quantity: Quantity
    unit: Annotated[str, Field(min_length=1, max_length=60)]
    supplier_id: PositiveId | None = None
    estimated_price: Price | None = None
    note: Annotated[str, Field(max_length=255)] | None = None


class Purchase(Input):
    items: Annotated[list[PurchaseItem], Field(min_length=1, max_length=25)]
    reason: Annotated[str, Field(max_length=255)] | None = None

    @model_validator(mode="after")
    def no_duplicates(self) -> "Purchase":
        ids = [item.product_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Consolide itens repetidos antes de solicitar confirmação.")
        return self
