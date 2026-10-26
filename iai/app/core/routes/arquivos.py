"""Envio, listagem e remoção de documentos que o IAI usa como contexto (RAG sobre o Qdrant)."""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Path, Request, UploadFile
from pydantic import BaseModel

from iai.app.core.arquivos import indice
from iai.app.core.arquivos.servico import ingerir_arquivo
from iai.app.core.config import ARQUIVOS_MAX_BYTES, ARQUIVOS_MAX_POR_ENVIO
from iai.app.core.infra.security import Authenticated

router = APIRouter(tags=["arquivos"])

FOLGA_MULTIPART_BYTES = 1024 * 1024


class ResultadoArquivoResponse(BaseModel):
    nome: str
    status: str  # indexado | recusado | erro
    mensagem: str = ""
    codigo: str = ""
    arquivo_id: str = ""
    tipo: str = ""
    trechos: int = 0
    aviso: str = ""


class EnvioArquivosResponse(BaseModel):
    resultados: list[ResultadoArquivoResponse]
    indexados: int
    recusados: int


class DocumentoResponse(BaseModel):
    arquivo_id: str
    nome: str
    tipo: str
    pasta: str
    trechos: int
    compartilhado: bool
    enviado_em: str
    dono: bool


class ListaDocumentosResponse(BaseModel):
    documentos: list[DocumentoResponse]


def _exigir_indice() -> None:
    if not indice.configurado():
        raise HTTPException(
            503, "Recurso de documentos indisponível: Qdrant não configurado."
        )


@router.post("/arquivos", response_model=EnvioArquivosResponse)
def enviar_arquivos(
    request: Request,
    ctx: Authenticated,
    arquivos: Annotated[
        list[UploadFile],
        # OpenAPI 3.1 descreve arquivo como contentMediaType e o Swagger UI mostra campos de
        # texto; com format=binary ele mostra o seletor de arquivos.
        File(json_schema_extra={"items": {"type": "string", "format": "binary"}}),
    ],
    compartilhar: Annotated[bool, Form()] = False,
) -> EnvioArquivosResponse:
    _exigir_indice()
    if not arquivos:
        raise HTTPException(422, "Envie ao menos um arquivo.")
    if len(arquivos) > ARQUIVOS_MAX_POR_ENVIO:
        raise HTTPException(
            413, f"Máximo de {ARQUIVOS_MAX_POR_ENVIO} arquivos por envio."
        )
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > (
        ARQUIVOS_MAX_POR_ENVIO * ARQUIVOS_MAX_BYTES + FOLGA_MULTIPART_BYTES
    ):
        raise HTTPException(413, "Envio grande demais.")
    identity = ctx.identity
    if compartilhar and identity.role != "SUPERVISOR":
        raise HTTPException(
            403, "Só o supervisor pode compartilhar documentos com a cozinha."
        )
    resultados = []
    for arquivo in arquivos:
        conteudo = arquivo.file.read(ARQUIVOS_MAX_BYTES + 1)
        resultado = ingerir_arquivo(
            identity, arquivo.filename, conteudo, compartilhar=compartilhar
        )
        resultados.append(ResultadoArquivoResponse(**resultado.__dict__))
    indexados = sum(1 for r in resultados if r.status == "indexado")
    return EnvioArquivosResponse(
        resultados=resultados,
        indexados=indexados,
        recusados=len(resultados) - indexados,
    )


@router.get("/arquivos", response_model=ListaDocumentosResponse)
def listar_arquivos(ctx: Authenticated) -> ListaDocumentosResponse:
    _exigir_indice()
    try:
        documentos = indice.listar(ctx.identity)
    except indice.IndiceIndisponivel:
        raise HTTPException(503, "Não consegui listar os documentos agora.") from None
    return ListaDocumentosResponse(
        documentos=[DocumentoResponse(**d.__dict__) for d in documentos]
    )


@router.delete("/arquivos/{arquivo_id}", status_code=204)
def remover_arquivo(
    ctx: Authenticated,
    arquivo_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
) -> None:
    _exigir_indice()
    try:
        removido = indice.remover(ctx.identity, arquivo_id)
    except indice.IndiceIndisponivel:
        raise HTTPException(503, "Não consegui remover o documento agora.") from None
    if not removido:
        raise HTTPException(404, "Documento não encontrado.")
