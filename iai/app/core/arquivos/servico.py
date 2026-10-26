"""Pipeline de ingestão: valida → extrai/descreve → guardrail → divide em trechos → embeddings → Qdrant.

Cada arquivo é independente: um recusado não derruba os outros do mesmo envio. O conteúdo só é gravado
no Qdrant depois de passar por todas as etapas; nada vai para o disco.
"""

import hashlib
import logging
from dataclasses import dataclass

from iai.app.core.arquivos import indice
from iai.app.core.arquivos.descricao_imagem import descrever_imagem
from iai.app.core.arquivos.extracao import Secao, extrair_texto
from iai.app.core.arquivos.guardrail import verificar_conteudo
from iai.app.core.arquivos.seguranca import (
    ArquivoRecusado,
    nome_seguro,
    validar_arquivo,
)
from iai.app.core.config import ARQUIVOS_MAX_POR_USUARIO
from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.infra.security import Identity

logger = logging.getLogger("iai.arquivos")


@dataclass(frozen=True)
class ResultadoArquivo:
    nome: str
    status: str  # "indexado" | "recusado" | "erro"
    mensagem: str = ""
    codigo: str = ""
    arquivo_id: str = ""
    tipo: str = ""
    trechos: int = 0
    aviso: str = ""


def pasta_segura(nome_original: str | None) -> str:
    """Pasta relativa enviada junto do arquivo (ex.: 'cardapios/verao'); sem '..', sem caminho absoluto."""
    partes = (nome_original or "").replace("\\", "/").split("/")[:-1]
    limpas = []
    for parte in partes:
        if parte in ("", ".", ".."):
            continue
        try:
            limpas.append(nome_seguro(parte))
        except ArquivoRecusado:
            continue
    return "/".join(limpas[-4:])[:200]


def ingerir_arquivo(
    identity: Identity,
    nome_original: str | None,
    conteudo: bytes,
    *,
    compartilhar: bool = False,
) -> ResultadoArquivo:
    rotulo_log = f"kitchen={identity.kitchen_id} user={identity.user_id}"
    try:
        nome_exibicao = nome_seguro(nome_original)
    except ArquivoRecusado as recusa:
        return ResultadoArquivo(
            "(sem nome)", "recusado", recusa.mensagem, recusa.codigo
        )
    try:
        arquivo = validar_arquivo(nome_original, conteudo)
        sha256 = hashlib.sha256(conteudo).hexdigest()
        arquivo_id = indice.calcular_arquivo_id(identity, sha256)
        if (
            not indice.documento_existe(identity, arquivo_id)
            and indice.contar_documentos(identity) >= ARQUIVOS_MAX_POR_USUARIO
        ):
            raise ArquivoRecusado(
                "limite_usuario",
                f"Você já tem {ARQUIVOS_MAX_POR_USUARIO} documentos. Remova algum para enviar outro.",
            )
        pasta = pasta_segura(nome_original)

        if arquivo.tipo == "imagem":
            secoes = [Secao("descrição da imagem", descrever_imagem(arquivo))]
            truncado = False
        else:
            extraido = extrair_texto(arquivo)
            secoes, truncado = extraido.secoes, extraido.truncado

        # Segredos são mascarados antes de qualquer coisa sair daqui (índice, embeddings e classificador).
        mascaradas = [
            Secao(
                remove_secrets(s.rotulo),
                remove_secrets(s.texto),
                remove_secrets(s.cabecalho),
            )
            for s in secoes
        ]
        segredos_mascarados = mascaradas != secoes
        secoes = mascaradas

        # Tudo que vai ser indexado (inclusive nome, pasta, títulos e cabeçalhos) passa pelo guardrail.
        auditado = "\n\n".join(
            [arquivo.nome, pasta]
            + [f"{s.rotulo}\n{s.cabecalho}\n{s.texto}" for s in secoes]
        )
        verificar_conteudo(auditado, origem=arquivo.tipo)

        trechos = indice.dividir_em_trechos(secoes)
        if not trechos:
            raise ArquivoRecusado(
                "sem_texto", "Não encontrei texto legível neste arquivo."
            )
        total = indice.indexar(
            identity,
            arquivo_id=arquivo_id,
            nome=arquivo.nome,
            tipo=arquivo.tipo,
            extensao=arquivo.extensao,
            pasta=pasta,
            sha256=sha256,
            tamanho=arquivo.tamanho_original,
            trechos=trechos,
            compartilhado=compartilhar,
        )
    except ArquivoRecusado as recusa:
        logger.warning("arquivo_recusado %s codigo=%s", rotulo_log, recusa.codigo)
        return ResultadoArquivo(
            nome_exibicao, "recusado", recusa.mensagem, recusa.codigo
        )
    except indice.IndiceIndisponivel as erro:
        logger.error("arquivo_nao_indexado %s motivo=%s", rotulo_log, erro)
        return ResultadoArquivo(
            nome_exibicao,
            "erro",
            "Não consegui salvar o arquivo agora. Tente novamente.",
            "indice",
        )
    avisos = []
    if truncado:
        avisos.append("Arquivo muito grande: só o começo foi indexado.")
    if segredos_mascarados:
        avisos.append("Senhas/chaves encontradas foram mascaradas.")
    logger.info(
        "arquivo_indexado %s tipo=%s trechos=%d", rotulo_log, arquivo.tipo, total
    )
    return ResultadoArquivo(
        arquivo.nome,
        "indexado",
        "Arquivo indexado.",
        arquivo_id=arquivo_id,
        tipo=arquivo.tipo,
        trechos=total,
        aviso=" ".join(avisos),
    )
