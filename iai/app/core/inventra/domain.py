"""Contratos estritos de intenção. Nenhum campo de identidade é decidido pelo LLM."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveId = Annotated[int, Field(gt=0, strict=True)]
Quantity = Annotated[
    Decimal, Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)
]
NonNegative = Annotated[
    Decimal, Field(ge=0, max_digits=12, decimal_places=3, allow_inf_nan=False)
]
Price = Annotated[
    Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
]
Name = Annotated[str, Field(min_length=1, max_length=150)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateProduct(StrictModel):
    name: Name
    unitId: PositiveId
    brand: str | None = Field(default=None, max_length=80)
    categoryId: PositiveId | None = None
    barcode: str | None = Field(default=None, max_length=50)


class EditProduct(StrictModel):
    productId: PositiveId
    name: Name | None = None
    unitId: PositiveId | None = None
    brand: str | None = Field(default=None, max_length=80)
    categoryId: PositiveId | None = None
    barcode: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def changes_required(self):
        if not self.model_dump(exclude_none=True, exclude={"productId"}):
            raise ValueError("Informe pelo menos um campo para editar.")
        return self


class ProductId(StrictModel):
    productId: PositiveId


class CreateSupplier(StrictModel):
    legalName: Name
    cnpj: str = Field(min_length=14, max_length=18, pattern=r"^[\d./-]+$")
    email: str | None = Field(
        default=None, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=150
    )
    whatsapp: str | None = Field(default=None, max_length=20)
    rating: int | None = Field(default=None, ge=1, le=5)


class SupplierId(StrictModel):
    supplierId: PositiveId


class CreateBatch(StrictModel):
    productId: PositiveId
    batchNumber: str = Field(min_length=1, max_length=100)
    initialQuantity: Quantity
    entryDate: date
    supplierId: PositiveId | None = None
    invoiceNumber: str | None = Field(default=None, max_length=100)
    expirationDate: date | None = None
    unitPrice: Price | None = None

    @model_validator(mode="after")
    def dates_ordered(self):
        if self.expirationDate and self.expirationDate < self.entryDate:
            raise ValueError("Validade não pode anteceder a entrada.")
        return self


class BatchMovement(StrictModel):
    batchId: PositiveId
    quantity: Quantity


class BatchAdjustment(StrictModel):
    batchId: PositiveId
    newQuantity: NonNegative


class CreateRequisition(StrictModel):
    type: Literal["PURCHASE", "TRANSFER", "CONSUMPTION"]
    origin: str = Field(min_length=1, max_length=20)


class RequisitionItem(StrictModel):
    requisitionId: PositiveId
    productId: PositiveId
    quantity: Quantity
    suggestedSupplierId: PositiveId | None = None
    estimatedPrice: Price | None = None
    note: str | None = Field(default=None, max_length=255)


class RemoveRequisitionItem(StrictModel):
    requisitionId: PositiveId
    itemId: PositiveId


class CancelRequisition(StrictModel):
    requisitionId: PositiveId
    reason: str = Field(min_length=1, max_length=255)


class CreateInventory(StrictModel):
    note: str | None = Field(default=None, max_length=255)


class InventoryCount(StrictModel):
    inventoryId: PositiveId
    batchId: PositiveId
    physicalQuantity: NonNegative
    note: str | None = Field(default=None, max_length=255)


class EditInventoryCount(InventoryCount):
    countId: PositiveId


class Operation(StrEnum):
    CREATE_PRODUCT = "criar_produto"
    EDIT_PRODUCT = "editar_produto"
    DEACTIVATE_PRODUCT = "desativar_produto"
    CREATE_SUPPLIER = "cadastrar_fornecedor"
    DEACTIVATE_SUPPLIER = "desativar_fornecedor"
    CREATE_BATCH = "criar_lote"
    RESTOCK = "registrar_entrada"
    CONSUME = "registrar_saida"
    ADJUST = "ajustar_estoque"
    CREATE_REQUISITION = "criar_requisicao"
    ADD_ITEM = "adicionar_item_requisicao"
    REMOVE_ITEM = "remover_item_requisicao"
    CANCEL_REQUISITION = "cancelar_requisicao"
    CREATE_INVENTORY = "registrar_inventario"
    ADD_COUNT = "registrar_contagem"
    EDIT_COUNT = "alterar_contagem"


STOCK_OPERATIONS = {
    Operation.CREATE_BATCH,
    Operation.RESTOCK,
    Operation.CONSUME,
    Operation.ADJUST,
    Operation.CREATE_INVENTORY,
    Operation.ADD_COUNT,
    Operation.EDIT_COUNT,
}
BUY_OPERATIONS = {
    Operation.CREATE_REQUISITION,
    Operation.ADD_ITEM,
    Operation.REMOVE_ITEM,
    Operation.CANCEL_REQUISITION,
    Operation.CREATE_SUPPLIER,
    Operation.DEACTIVATE_SUPPLIER,
}
# Quem pode PROPOR cada operação. Produtos (criar/editar/desativar) só o Supervisor.
# Defesa em profundidade: a API Spring continua aplicando as permissões na execução.
ROLE_OPERATIONS = {
    "ESTOQUISTA": STOCK_OPERATIONS,
    "COMPRADOR": BUY_OPERATIONS,
    "SUPERVISOR": set(Operation),
}


PARAMETERS: dict[Operation, type[StrictModel]] = dict(
    zip(
        Operation,
        [
            CreateProduct,
            EditProduct,
            ProductId,
            CreateSupplier,
            SupplierId,
            CreateBatch,
            BatchMovement,
            BatchMovement,
            BatchAdjustment,
            CreateRequisition,
            RequisitionItem,
            RemoveRequisitionItem,
            CancelRequisition,
            CreateInventory,
            InventoryCount,
            EditInventoryCount,
        ],
        strict=True,
    )
)


class Action(StrictModel):
    operation: Operation
    parameters: dict

    @model_validator(mode="after")
    def typed_parameters(self):
        parsed = PARAMETERS[self.operation].model_validate(self.parameters)
        self.parameters = parsed.model_dump(mode="json", exclude_none=True)
        # 10, 10.0 e 10.000 representam a mesma proposta nos retries do modelo.
        for name in self.parameters:
            value = getattr(parsed, name)
            if isinstance(value, Decimal):
                self.parameters[name] = format(value.normalize(), "f")
        return self


class Query(StrictModel):
    resource: Literal[
        "produtos",
        "estoque",
        "lotes",
        "validade",
        "estoque_minimo",
        "fornecedores",
        "fornecedores_recorrentes",
        "fornecedores_produto",
        "parametros_produto",
        "requisicoes",
        "itens_requisicao",
        "inventarios",
        "contagens",
        "alertas",
        "categorias",
        "unidades",
        "cozinha",
    ]
    product_id: PositiveId | None = None
    record_id: PositiveId | None = None
    name: str | None = Field(default=None, max_length=150)
    status: str | None = Field(default=None, max_length=40)
    days: int = Field(default=7, ge=0, le=365)
    page: int = Field(default=0, ge=0, le=10000)
    size: int = Field(default=20, ge=1, le=100)
    unread: bool = False
    mine: bool = Field(
        default=False,
        description="Requisições/inventários do próprio usuário autenticado; não informe user_id.",
    )

    @model_validator(mode="after")
    def required_ids(self):
        if self.mine and self.resource not in {"requisicoes", "inventarios"}:
            raise ValueError("Filtro mine é apenas de requisições ou inventários.")
        if self.resource in {"itens_requisicao", "contagens"} and not self.record_id:
            raise ValueError("Informe record_id da requisição/inventário.")
        if (
            self.resource in {"fornecedores_produto", "parametros_produto"}
            and not self.product_id
        ):
            raise ValueError(
                "Informe product_id; consulte produtos para localizar o ID."
            )
        if (
            self.resource == "requisicoes"
            and self.status
            and self.status
            not in {"DRAFT", "UNDER_REVIEW", "APPROVED", "REJECTED", "CANCELLED"}
        ):
            raise ValueError("Status de requisição inválido.")
        return self


class ToolResult(BaseModel):
    ok: bool
    code: str
    message: str
    data: object = None
    source: str
    mocked: bool
    request_id: str | None = None
    incomplete: bool = False
