"""Porta de entrada dos arquivos enviados: nada daqui executa, abre em programa externo ou grava em disco.

Camadas (qualquer uma recusa o arquivo):
1. nome seguro e extensão numa lista de permitidos (executáveis, compactados e Office antigo têm motivo próprio);
2. assinatura real do conteúdo (renomear um .exe para .pdf não passa);
3. inspeção estrutural por tipo: PDF sem JavaScript/Launch/anexos, OOXML sem macro/OLE/vínculo externo/zip bomb,
   imagem que decodifica de verdade (e é reencodada, o que remove metadados e payloads escondidos);
4. limites de tamanho.

Isto NÃO é antivírus: não há varredura por assinatura de malware. É uma lista de permitidos + validação estrutural.
"""

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from iai.app.core.config import ARQUIVOS_MAX_BYTES, ARQUIVOS_MAX_CARACTERES


class ArquivoRecusado(Exception):
    """Arquivo barrado. `codigo` é estável (testes/métricas); `mensagem` é para o usuário."""

    def __init__(self, codigo: str, mensagem: str):
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


TIPOS_POR_EXTENSAO = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".csv": "csv",
    ".txt": "texto",
    ".md": "texto",
    ".png": "imagem",
    ".jpg": "imagem",
    ".jpeg": "imagem",
    ".webp": "imagem",
}
EXTENSOES_OOXML = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}

EXECUTAVEIS = {
    ".exe", ".dll", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".psm1", ".vbs", ".vbe",
    ".js", ".jse", ".mjs", ".jar", ".sh", ".bash", ".apk", ".app", ".lnk", ".reg", ".hta",
    ".wsf", ".dmg", ".bin", ".so", ".dylib", ".pif", ".cpl", ".msc", ".py", ".pyc", ".php",
}  # fmt: skip
COMPACTADOS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".iso",
    ".cab",
    ".z",
}
COM_MACRO_OU_ATIVOS = {
    ".docm", ".xlsm", ".pptm", ".dotm", ".xltm", ".potm", ".xlam", ".ppam", ".svg", ".html",
    ".htm", ".xhtml", ".xml", ".swf",
}  # fmt: skip
OFFICE_ANTIGO = {".doc", ".xls", ".ppt", ".rtf", ".dot", ".xlt", ".pot"}
EXTENSOES_PERIGOSAS = EXECUTAVEIS | COMPACTADOS | COM_MACRO_OU_ATIVOS

MAX_ENTRADAS_ZIP = 2000
MAX_PARTE_XML = 20 * 1024 * 1024
MAX_DESCOMPACTADO = 120 * 1024 * 1024
MAX_RAZAO_COMPRESSAO = 150
MAX_PAGINAS_PDF = 300
MAX_PIXELS = 25_000_000
LADO_MAXIMO_IMAGEM = 2048

MIME_POR_TIPO_IMAGEM = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ArquivoValidado:
    nome: str
    tipo: str
    extensao: str
    conteudo: bytes  # imagens já reencodadas; demais, os bytes originais
    mime: str
    tamanho_original: int


def nome_seguro(nome: str | None) -> str:
    bruto = unicodedata.normalize("NFKC", nome or "")
    bruto = bruto.replace("\\", "/")
    base = PurePath(bruto).name
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
    base = re.sub(r"\s+", " ", base).strip(" .")
    if not base or base.startswith("."):
        raise ArquivoRecusado("nome_invalido", "O nome do arquivo é inválido.")
    return base[:120]


def _extensao(nome: str) -> str:
    partes = nome.lower().split(".")
    if len(partes) < 2:
        raise ArquivoRecusado(
            "sem_extensao", "O arquivo precisa ter extensão (ex.: .pdf, .xlsx)."
        )
    # "relatorio.exe.pdf" é o truque clássico de dupla extensão.
    for parte in partes[1:-1]:
        if f".{parte}" in EXTENSOES_PERIGOSAS:
            raise ArquivoRecusado(
                "dupla_extensao", "Nome de arquivo suspeito (dupla extensão)."
            )
    return f".{partes[-1]}"


def _checar_extensao(extensao: str) -> str:
    if extensao in EXECUTAVEIS:
        raise ArquivoRecusado(
            "executavel", "Arquivos executáveis ou de script não são aceitos."
        )
    if extensao in COMPACTADOS:
        raise ArquivoRecusado(
            "compactado",
            "Arquivos compactados (zip, rar, 7z...) não são aceitos. Envie os arquivos um a um.",
        )
    if extensao in COM_MACRO_OU_ATIVOS:
        raise ArquivoRecusado(
            "conteudo_ativo",
            "Arquivos com macro, HTML ou SVG não são aceitos (podem executar código).",
        )
    if extensao in OFFICE_ANTIGO:
        raise ArquivoRecusado(
            "office_antigo",
            "Formato antigo do Office não é aceito. Salve como .docx, .xlsx ou .pptx.",
        )
    if extensao not in TIPOS_POR_EXTENSAO:
        raise ArquivoRecusado(
            "tipo_nao_suportado",
            "Tipo não suportado. Aceitos: PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), "
            "CSV, TXT, MD, PNG, JPG e WEBP.",
        )
    return TIPOS_POR_EXTENSAO[extensao]


# --- assinatura real do conteúdo ---------------------------------------------------------------
_ASSINATURAS_PROIBIDAS = (
    (b"MZ", "executavel", "O conteúdo é um executável do Windows, mesmo com outra extensão."),
    (b"\x7fELF", "executavel", "O conteúdo é um executável do Linux, mesmo com outra extensão."),
    (b"\xca\xfe\xba\xbe", "executavel", "O conteúdo é um executável, mesmo com outra extensão."),
    (b"\xfe\xed\xfa", "executavel", "O conteúdo é um executável, mesmo com outra extensão."),
    (b"\xcf\xfa\xed\xfe", "executavel", "O conteúdo é um executável, mesmo com outra extensão."),
    (b"#!", "executavel", "O conteúdo é um script executável, mesmo com outra extensão."),
    (b"Rar!", "compactado", "O conteúdo é um arquivo compactado, mesmo com outra extensão."),
    (b"7z\xbc\xaf\x27\x1c", "compactado", "O conteúdo é um arquivo compactado, mesmo com outra extensão."),
    (b"\x1f\x8b", "compactado", "O conteúdo é um arquivo compactado, mesmo com outra extensão."),
    (b"BZh", "compactado", "O conteúdo é um arquivo compactado, mesmo com outra extensão."),
    (b"\xfd7zXZ", "compactado", "O conteúdo é um arquivo compactado, mesmo com outra extensão."),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "office_antigo", "Formato antigo do Office (pode conter macros) não é aceito."),
    (b"MSCF", "compactado", "O conteúdo é um pacote compactado, mesmo com outra extensão."),
)  # fmt: skip


_ASSINATURAS_ASCII = (
    b"MZ",
    b"BZh",
    b"#!",
)  # podem abrir um texto legítimo; para texto vale a checagem de NUL


def _checar_assinatura(cabeca: bytes, extensao: str, tipo: str) -> None:
    for assinatura, codigo, mensagem in _ASSINATURAS_PROIBIDAS:
        if tipo == "texto" and assinatura in _ASSINATURAS_ASCII:
            continue
        if cabeca.startswith(assinatura):
            raise ArquivoRecusado(codigo, mensagem)
    if len(cabeca) > 262 and cabeca[257:262] == b"ustar":
        raise ArquivoRecusado(
            "compactado",
            "O conteúdo é um arquivo compactado, mesmo com outra extensão.",
        )
    zip_ = cabeca.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if extensao in EXTENSOES_OOXML:
        if not zip_:
            raise ArquivoRecusado(
                "assinatura_invalida",
                "O conteúdo não corresponde à extensão informada.",
            )
    elif zip_:
        raise ArquivoRecusado(
            "compactado",
            "O conteúdo é um arquivo compactado, mesmo com outra extensão.",
        )
    if tipo == "pdf" and b"%PDF-" not in cabeca[:1024]:
        raise ArquivoRecusado(
            "assinatura_invalida", "O conteúdo não corresponde à extensão informada."
        )
    if tipo == "imagem" and not (
        cabeca.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
        or (cabeca[:4] == b"RIFF" and cabeca[8:12] == b"WEBP")
    ):
        raise ArquivoRecusado(
            "assinatura_invalida", "O conteúdo não corresponde à extensão informada."
        )


# --- inspeção por tipo ------------------------------------------------------------------------
_PARTES_PROIBIDAS = (
    "vbaproject", "vbadata", "/embeddings/", "/activex", "/oleobject", "macrosheets/",
    "/printersettings", "/ole", "externallinks/", "/customui", "/vmldrawing.bin",
)  # fmt: skip
_TIPO_EXTERNO_OK = ("/hyperlink",)
_ATRIBUTOS_RELACIONAMENTO = re.compile(rb"<Relationship\b[^>]*>", re.IGNORECASE)


def _inspecionar_ooxml(dados: bytes, extensao: str) -> None:
    try:
        arquivo = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        raise ArquivoRecusado(
            "estrutura_invalida",
            "O arquivo está corrompido ou não é um documento Office válido.",
        ) from None
    with arquivo:
        itens = arquivo.infolist()
        if len(itens) > MAX_ENTRADAS_ZIP:
            raise ArquivoRecusado(
                "zip_bomb", "O documento tem partes demais; recusado por segurança."
            )
        total = 0
        nomes = set()
        for item in itens:
            nome = item.filename
            baixo = nome.lower()
            if (
                nome.startswith(("/", "\\"))
                or ".." in PurePath(nome.replace("\\", "/")).parts
                or "\\" in nome
            ):
                raise ArquivoRecusado(
                    "estrutura_invalida", "O documento tem caminhos internos suspeitos."
                )
            if item.flag_bits & 0x1:
                raise ArquivoRecusado(
                    "criptografado", "Documentos protegidos por senha não são aceitos."
                )
            total += item.file_size
            if item.file_size > MAX_PARTE_XML * 2 or total > MAX_DESCOMPACTADO:
                raise ArquivoRecusado(
                    "zip_bomb", "O documento é grande demais depois de descompactado."
                )
            if (
                item.compress_size
                and item.file_size / max(item.compress_size, 1) > MAX_RAZAO_COMPRESSAO
                and item.file_size > 1024 * 1024
            ):
                raise ArquivoRecusado(
                    "zip_bomb",
                    "Compressão suspeita no documento; recusado por segurança.",
                )
            if any(marca in baixo for marca in _PARTES_PROIBIDAS):
                raise ArquivoRecusado(
                    "conteudo_ativo",
                    "O documento contém macro, objeto embutido ou componente ativo.",
                )
            sufixo = PurePath(baixo).suffix
            if sufixo in EXECUTAVEIS or sufixo in COMPACTADOS:
                raise ArquivoRecusado(
                    "conteudo_ativo",
                    "O documento carrega um arquivo embutido perigoso.",
                )
            nomes.add(nome)
        principal = EXTENSOES_OOXML[extensao]
        if "[Content_Types].xml" not in nomes or principal not in nomes:
            raise ArquivoRecusado(
                "assinatura_invalida",
                "O conteúdo não corresponde à extensão informada.",
            )
        for nome in nomes:
            baixo = nome.lower()
            if not baixo.endswith((".xml", ".rels")):
                continue
            with arquivo.open(nome) as parte:
                bruto = parte.read(MAX_PARTE_XML + 1)
            if len(bruto) > MAX_PARTE_XML:
                raise ArquivoRecusado(
                    "zip_bomb", "Parte do documento grande demais; recusado."
                )
            if re.search(rb"<!DOCTYPE|<!ENTITY", bruto, re.IGNORECASE):
                raise ArquivoRecusado(
                    "conteudo_ativo",
                    "O documento usa declarações XML perigosas (DOCTYPE/ENTITY).",
                )
            if nome == "[Content_Types].xml" and re.search(
                rb"macroEnabled|vbaProject", bruto, re.IGNORECASE
            ):
                raise ArquivoRecusado(
                    "conteudo_ativo", "O documento está habilitado para macros."
                )
            if baixo.endswith(".rels"):
                for relacao in _ATRIBUTOS_RELACIONAMENTO.findall(bruto):
                    if re.search(
                        rb'TargetMode\s*=\s*"External"', relacao, re.IGNORECASE
                    ):
                        tipo_rel = re.search(
                            rb'Type\s*=\s*"([^"]*)"', relacao, re.IGNORECASE
                        )
                        alvo = (
                            tipo_rel.group(1).decode("latin-1").lower()
                            if tipo_rel
                            else ""
                        )
                        if not alvo.endswith(_TIPO_EXTERNO_OK):
                            raise ArquivoRecusado(
                                "conteudo_ativo",
                                "O documento busca conteúdo externo (modelo, objeto ou vínculo remoto).",
                            )


_NOMES_PDF_ATIVOS = (
    b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/EmbeddedFiles", b"/RichMedia",
    b"/OpenAction", b"/AA", b"/XFA", b"/SubmitForm", b"/ImportData", b"/GoToR", b"/GoToE",
    b"/URI", b"/Sound", b"/Movie", b"/FileAttachment", b"/Encrypt",
)  # fmt: skip
_ACOES_PDF_PERIGOSAS = {
    "/JavaScript",
    "/Launch",
    "/SubmitForm",
    "/ImportData",
    "/GoToR",
    "/GoToE",
    "/Sound",
    "/Movie",
    "/Rendition",
}


def _desofuscar_nomes_pdf(dados: bytes) -> bytes:
    """Nomes PDF aceitam /J#61vaScript; desfaz #xx para a varredura não ser enganada."""
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), dados)


def _inspecionar_pdf(dados: bytes) -> None:
    visto = _desofuscar_nomes_pdf(dados)
    for nome in _NOMES_PDF_ATIVOS:
        # (?![A-Za-z]) evita pegar /JSON, /URIs etc. e aceita /JS seguido de delimitador.
        if re.search(re.escape(nome) + rb"(?![A-Za-z0-9])", visto):
            if nome == b"/Encrypt":
                raise ArquivoRecusado(
                    "criptografado", "PDF protegido por senha não é aceito."
                )
            if nome == b"/URI":
                continue  # links comuns; não executam nada
            raise ArquivoRecusado(
                "conteudo_ativo",
                "O PDF contém JavaScript, ação automática ou arquivo anexo.",
            )
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        leitor = PdfReader(io.BytesIO(dados), strict=False)
        if leitor.is_encrypted:
            raise ArquivoRecusado(
                "criptografado", "PDF protegido por senha não é aceito."
            )
        if len(leitor.pages) > MAX_PAGINAS_PDF:
            raise ArquivoRecusado(
                "muito_grande", f"O PDF tem mais de {MAX_PAGINAS_PDF} páginas."
            )
        raiz = leitor.trailer["/Root"]
        if "/OpenAction" in raiz or "/AA" in raiz:
            raise ArquivoRecusado("conteudo_ativo", "O PDF contém ação automática.")
        nomes = raiz.get("/Names")
        if nomes is not None and any(
            c in nomes.get_object() for c in ("/JavaScript", "/EmbeddedFiles")
        ):
            raise ArquivoRecusado(
                "conteudo_ativo", "O PDF contém JavaScript ou arquivo anexo."
            )
        for pagina in leitor.pages:
            if "/AA" in pagina:
                raise ArquivoRecusado("conteudo_ativo", "O PDF contém ação automática.")
            for anotacao in pagina.get("/Annots", []) or []:
                acao = anotacao.get_object().get("/A")
                if (
                    acao is not None
                    and str(acao.get_object().get("/S")) in _ACOES_PDF_PERIGOSAS
                ):
                    raise ArquivoRecusado(
                        "conteudo_ativo", "O PDF contém uma ação perigosa em anotação."
                    )
    except ArquivoRecusado:
        raise
    except (
        PyPdfError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        OSError,
    ):
        raise ArquivoRecusado(
            "estrutura_invalida", "O PDF está corrompido ou não pôde ser verificado."
        ) from None


def _processar_imagem(dados: bytes) -> tuple[bytes, str]:
    """Decodifica de verdade e REENCODA: remove metadados/EXIF, polyglots e trailing bytes."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(dados)) as teste:
            formato = teste.format
            teste.verify()
        if formato not in MIME_POR_TIPO_IMAGEM:
            raise ArquivoRecusado(
                "tipo_nao_suportado", "Formato de imagem não suportado."
            )
        with Image.open(io.BytesIO(dados)) as imagem:
            if getattr(imagem, "n_frames", 1) > 1:
                raise ArquivoRecusado(
                    "tipo_nao_suportado", "Imagens animadas não são aceitas."
                )
            if imagem.width * imagem.height > MAX_PIXELS:
                raise ArquivoRecusado(
                    "muito_grande", "A imagem tem resolução alta demais."
                )
            imagem = ImageOps.exif_transpose(imagem)
            imagem.thumbnail((LADO_MAXIMO_IMAGEM, LADO_MAXIMO_IMAGEM))
            saida = io.BytesIO()
            if formato == "PNG" and imagem.mode in {"RGBA", "LA", "P", "L", "RGB"}:
                imagem.save(saida, format="PNG")
                return saida.getvalue(), "image/png"
            imagem.convert("RGB").save(saida, format="JPEG", quality=88)
            return saida.getvalue(), "image/jpeg"
    except ArquivoRecusado:
        raise
    except (
        UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
        OSError, ValueError, SyntaxError, MemoryError,
    ):  # fmt: skip
        raise ArquivoRecusado(
            "estrutura_invalida", "A imagem está corrompida ou é inválida."
        ) from None


def decodificar_texto(dados: bytes) -> str:
    if b"\x00" in dados:
        raise ArquivoRecusado(
            "assinatura_invalida", "O conteúdo não é um arquivo de texto."
        )
    for codificacao in ("utf-8-sig", "cp1252"):
        try:
            texto = dados.decode(codificacao)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ArquivoRecusado(
            "assinatura_invalida", "O conteúdo não é um arquivo de texto."
        )
    controle = sum(1 for c in texto[:20000] if ord(c) < 32 and c not in "\n\r\t")
    if controle > 20:
        raise ArquivoRecusado(
            "assinatura_invalida", "O conteúdo não é um arquivo de texto."
        )
    return texto


def validar_arquivo(nome: str | None, conteudo: bytes) -> ArquivoValidado:
    """Valida um arquivo recebido. Levanta ArquivoRecusado; nunca executa nem grava o conteúdo."""
    seguro = nome_seguro(nome)
    extensao = _extensao(seguro)
    tipo = _checar_extensao(extensao)
    if not conteudo:
        raise ArquivoRecusado("vazio", "O arquivo está vazio.")
    if len(conteudo) > ARQUIVOS_MAX_BYTES:
        limite = ARQUIVOS_MAX_BYTES // (1024 * 1024)
        raise ArquivoRecusado("muito_grande", f"O arquivo passa de {limite} MB.")
    _checar_assinatura(conteudo[:1100], extensao, tipo)
    mime = "application/octet-stream"
    final = conteudo
    if tipo == "pdf":
        _inspecionar_pdf(conteudo)
        mime = "application/pdf"
    elif extensao in EXTENSOES_OOXML:
        _inspecionar_ooxml(conteudo, extensao)
        mime = "application/vnd.openxmlformats-officedocument"
    elif tipo == "imagem":
        final, mime = _processar_imagem(conteudo)
    else:
        texto = decodificar_texto(conteudo)
        if len(texto) > ARQUIVOS_MAX_CARACTERES * 4:
            raise ArquivoRecusado("muito_grande", "O arquivo de texto é grande demais.")
        mime = "text/plain"
    return ArquivoValidado(seguro, tipo, extensao, final, mime, len(conteudo))
