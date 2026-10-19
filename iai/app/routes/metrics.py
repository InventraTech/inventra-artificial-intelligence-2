from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from iai.app.metrics import gerar_metricas
from iai.app.schemas import MetricasResponse
from iai.app.security import Authenticated

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_model=MetricasResponse)
def metricas(
    ctx: Authenticated,
    periodo: Literal["semana", "mes"] = "semana",
    usuarios: Annotated[list[Annotated[int, Field(ge=1)]] | None, Query()] = None,
) -> MetricasResponse:
    """
    Observabilidade/SRE do IAI: latência, índice de erros, custo, custo por resolução, ROI e
    projeção de custo. Exige autenticação e só considera a cozinha do usuário; quem não é
    SUPERVISOR vê apenas as próprias requisições. `periodo` troca a janela observada e o
    horizonte da projeção; `usuarios` (repetível, ex.: ?usuarios=100&usuarios=5000) troca os
    cenários.
    """
    return gerar_metricas(periodo=periodo, usuarios=usuarios, identity=ctx.identity)
