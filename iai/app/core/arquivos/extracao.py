"""Extrai texto de arquivos JÁ validados por `seguranca.validar_arquivo`.

Tudo em memória, sem gravar em disco e sem executar nada. OOXML é lido com zipfile + xml.etree (o
`seguranca` já recusou DOCTYPE/ENTITY, então não há expansão de entidades). Texto escondido em
documentos (runs `vanish`, caracteres invisíveis, tag characters do Unicode) é descartado, porque é o
canal clássico de prompt injection que o humano não enxerga.
"""

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

from iai.app.core.arquivos.seguranca import (
    ArquivoRecusado,
    ArquivoValidado,
    decodificar_texto,
)
from iai.app.core.config import ARQUIVOS_MAX_CARACTERES

MAX_LINHAS_PLANILHA = 20000
MAX_COLUNAS = 60
MAX_PLANILHAS = 30
MAX_SLIDES = 500
MIN_CARACTERES_PDF = 20

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Invisíveis usados para esconder instruções: zero-width, bidi, tag characters, controles C0/C1.
_INVISIVEIS = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u180e"
    r"\U000e0000-\U000e007f\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)


@dataclass(frozen=True)
class Secao:
    rotulo: str  # "página 3", "planilha Estoque", "slide 2", "Compras > Fornecedores"
    texto: str
    cabecalho: str = ""  # repetido no começo de cada trecho (tabelas)


@dataclass(frozen=True)
class Extraido:
    secoes: list[Secao]
    truncado: bool = False


def limpar_texto(texto: str) -> str:
    texto = unicodedata.normalize("NFC", texto)
    texto = _INVISIVEIS.sub("", texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _xml(zf: zipfile.ZipFile, nome: str) -> ElementTree.Element | None:
    try:
        with zf.open(nome) as parte:
            return ElementTree.fromstring(parte.read())
    except (KeyError, ElementTree.ParseError):
        return None


def _numero(nome: str) -> int:
    achado = re.search(r"(\d+)\.xml$", nome)
    return int(achado.group(1)) if achado else 0


def _abrir_zip(dados: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        raise ArquivoRecusado(
            "estrutura_invalida", "O documento está corrompido."
        ) from None


# --- PDF ----------------------------------------------------------------------------------------
def _extrair_pdf(dados: bytes) -> list[Secao]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        leitor = PdfReader(io.BytesIO(dados), strict=False)
        secoes = []
        for numero, pagina in enumerate(leitor.pages, start=1):
            texto = limpar_texto(pagina.extract_text() or "")
            if texto:
                secoes.append(Secao(f"página {numero}", texto))
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
            "estrutura_invalida", "Não consegui ler o texto do PDF."
        ) from None
    if sum(len(s.texto) for s in secoes) < MIN_CARACTERES_PDF:
        raise ArquivoRecusado(
            "sem_texto",
            "O PDF não tem texto selecionável (parece escaneado). Envie como imagem (PNG/JPG).",
        )
    return secoes


# --- DOCX ---------------------------------------------------------------------------------------
def _texto_paragrafo_word(par: ElementTree.Element) -> str:
    partes: list[str] = []
    for run in par.iter(f"{_W}r"):
        propriedades = run.find(f"{_W}rPr")
        if propriedades is not None and propriedades.find(f"{_W}vanish") is not None:
            continue  # texto oculto: não é lido
        for no in run:
            if no.tag == f"{_W}t" and no.text:
                partes.append(no.text)
            elif no.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
                partes.append(" ")
    return "".join(partes)


def _extrair_docx(dados: bytes) -> list[Secao]:
    with _abrir_zip(dados) as zf:
        raiz = _xml(zf, "word/document.xml")
    if raiz is None:
        raise ArquivoRecusado("estrutura_invalida", "O documento Word está corrompido.")
    corpo = raiz.find(f"{_W}body")
    if corpo is None:
        raise ArquivoRecusado("estrutura_invalida", "O documento Word está vazio.")
    secoes: list[Secao] = []
    titulo = "documento"
    linhas: list[str] = []

    def fechar() -> None:
        texto = limpar_texto("\n".join(linhas))
        if texto:
            secoes.append(Secao(titulo, texto))
        linhas.clear()

    for no in corpo:
        if no.tag == f"{_W}p":
            texto = _texto_paragrafo_word(no).strip()
            if not texto:
                continue
            estilo = no.find(f"{_W}pPr/{_W}pStyle")
            nome_estilo = (estilo.get(f"{_W}val") or "") if estilo is not None else ""
            if (
                re.match(r"(?i)^(heading|t[ií]tulo|title)\s*\d*$", nome_estilo)
                and len(texto) < 120
            ):
                fechar()
                titulo = texto
            else:
                linhas.append(texto)
        elif no.tag == f"{_W}tbl":
            for linha in no.iter(f"{_W}tr"):
                celulas = []
                for celula in linha.findall(f"{_W}tc"):
                    celulas.append(
                        " ".join(
                            t
                            for p in celula.iter(f"{_W}p")
                            if (t := _texto_paragrafo_word(p).strip())
                        )
                    )
                if any(celulas):
                    linhas.append(" | ".join(celulas))
    fechar()
    return secoes


# --- XLSX ---------------------------------------------------------------------------------------
def _texto_rico(no: ElementTree.Element) -> str:
    return "".join(t.text or "" for t in no.iter(f"{_S}t"))


def _coluna_indice(referencia: str) -> int:
    letras = re.match(r"[A-Za-z]+", referencia)
    indice = 0
    for c in (letras.group(0) if letras else "A").upper():
        indice = indice * 26 + (ord(c) - 64)
    return indice - 1


_FORMATOS_DATA_NATIVOS = (
    set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
)


def _formatos_de_data(zf: zipfile.ZipFile) -> set[int]:
    """Índices de estilo (atributo `s` da célula) cujo formato é data/hora."""
    raiz = _xml(zf, "xl/styles.xml")
    if raiz is None:
        return set()
    de_data = set(_FORMATOS_DATA_NATIVOS)
    formatos = raiz.find(f"{_S}numFmts")
    if formatos is not None:
        for fmt in formatos.findall(f"{_S}numFmt"):
            codigo = re.sub(
                r'"[^"]*"|\[[^\]]*\]|\\.', "", fmt.get("formatCode") or ""
            ).lower()
            if re.search(r"[dmyh]", codigo):
                try:
                    de_data.add(int(fmt.get("numFmtId") or -1))
                except ValueError:
                    pass
    estilos = raiz.find(f"{_S}cellXfs")
    if estilos is None:
        return set()
    return {
        i
        for i, xf in enumerate(estilos.findall(f"{_S}xf"))
        if int(xf.get("numFmtId") or 0) in de_data
    }


def _numero_legivel(texto: str, data: bool) -> str:
    try:
        valor = float(texto)
    except ValueError:
        return texto
    if data and 0 < valor < 2958466:
        momento = datetime(1899, 12, 30, tzinfo=UTC) + timedelta(days=valor)
        return momento.strftime("%d/%m/%Y" if valor == int(valor) else "%d/%m/%Y %H:%M")
    if "." in texto or "e" in texto.lower():
        return f"{valor:.12g}"  # 79.90000000000001 -> 79.9
    return texto


def _extrair_xlsx(dados: bytes) -> list[Secao]:
    with _abrir_zip(dados) as zf:
        compartilhadas: list[str] = []
        raiz_ss = _xml(zf, "xl/sharedStrings.xml")
        if raiz_ss is not None:
            compartilhadas = [_texto_rico(si) for si in raiz_ss.findall(f"{_S}si")]
        estilos_data = _formatos_de_data(zf)
        livro = _xml(zf, "xl/workbook.xml")
        relacoes = _xml(zf, "xl/_rels/workbook.xml.rels")
        if livro is None or relacoes is None:
            raise ArquivoRecusado("estrutura_invalida", "A planilha está corrompida.")
        alvos = {
            r.get("Id"): (r.get("Target") or "").lstrip("/")
            for r in relacoes.iter(f"{_REL}Relationship")
        }
        secoes: list[Secao] = []
        planilhas = livro.find(f"{_S}sheets")
        if planilhas is None:
            raise ArquivoRecusado("estrutura_invalida", "A planilha está vazia.")
        for planilha in list(planilhas.findall(f"{_S}sheet"))[:MAX_PLANILHAS]:
            if planilha.get("state") in ("hidden", "veryHidden"):
                continue  # abas ocultas não são lidas
            alvo = alvos.get(planilha.get(f"{_R}id"), "")
            caminho = alvo if alvo.startswith("xl/") else f"xl/{alvo}"
            raiz = _xml(zf, caminho)
            if raiz is None:
                continue
            linhas: list[str] = []
            for linha in raiz.iter(f"{_S}row"):
                if linha.get("hidden") == "1":
                    continue
                if len(linhas) >= MAX_LINHAS_PLANILHA:
                    break
                valores: dict[int, str] = {}
                for celula in linha.findall(f"{_S}c"):
                    indice = _coluna_indice(celula.get("r") or "A")
                    if indice >= MAX_COLUNAS:
                        continue
                    tipo = celula.get("t")
                    bruto = celula.find(f"{_S}v")
                    if tipo == "inlineStr":
                        inline = celula.find(f"{_S}is")
                        texto = _texto_rico(inline) if inline is not None else ""
                    elif bruto is None or bruto.text is None:
                        continue
                    elif tipo == "s":
                        try:
                            texto = compartilhadas[int(bruto.text)]
                        except (ValueError, IndexError):
                            continue
                    elif tipo == "b":
                        texto = "sim" if bruto.text == "1" else "não"
                    elif tipo in (None, "n"):
                        texto = _numero_legivel(
                            bruto.text, int(celula.get("s") or -1) in estilos_data
                        )
                    else:
                        texto = bruto.text
                    texto = texto.replace("\n", " ").strip()
                    if texto:
                        valores[indice] = texto
                if valores:
                    ultimo = max(valores)
                    linhas.append(
                        " | ".join(valores.get(i, "") for i in range(ultimo + 1))
                    )
            if linhas:
                secoes.append(
                    Secao(
                        f"planilha {limpar_texto(planilha.get('name') or '?')}",
                        limpar_texto("\n".join(linhas[1:])) or linhas[0],
                        cabecalho=limpar_texto(linhas[0]) if len(linhas) > 1 else "",
                    )
                )
    return secoes


# --- PPTX ---------------------------------------------------------------------------------------
def _texto_slide(raiz: ElementTree.Element) -> str:
    paragrafos = []
    for par in raiz.iter(f"{_A}p"):
        texto = "".join(t.text or "" for t in par.iter(f"{_A}t")).strip()
        if texto:
            paragrafos.append(texto)
    return "\n".join(paragrafos)


def _extrair_pptx(dados: bytes) -> list[Secao]:
    with _abrir_zip(dados) as zf:
        nomes = zf.namelist()
        slides = sorted(
            (n for n in nomes if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=_numero,
        )[:MAX_SLIDES]
        secoes: list[Secao] = []
        for ordem, nome in enumerate(slides, start=1):
            raiz = _xml(zf, nome)
            texto = _texto_slide(raiz) if raiz is not None else ""
            notas = ""
            raiz_notas = _xml(zf, f"ppt/notesSlides/notesSlide{_numero(nome)}.xml")
            if raiz_notas is not None:
                notas = _texto_slide(raiz_notas)
            conteudo = limpar_texto(
                texto + (f"\nNotas do apresentador: {notas}" if notas else "")
            )
            if conteudo:
                secoes.append(Secao(f"slide {ordem}", conteudo))
    return secoes


# --- CSV / texto --------------------------------------------------------------------------------
def _extrair_csv(dados: bytes) -> list[Secao]:
    texto = decodificar_texto(dados)
    try:
        dialeto = csv.Sniffer().sniff(texto[:4096], delimiters=",;\t|")
    except csv.Error:
        dialeto = csv.excel
    linhas = []
    for linha in csv.reader(io.StringIO(texto), dialeto):
        celulas = [c.replace("\n", " ").strip() for c in linha[:MAX_COLUNAS]]
        if any(celulas):
            linhas.append(" | ".join(celulas))
        if len(linhas) >= MAX_LINHAS_PLANILHA:
            break
    if not linhas:
        return []
    return [
        Secao(
            "tabela",
            limpar_texto("\n".join(linhas[1:])) or linhas[0],
            cabecalho=limpar_texto(linhas[0]) if len(linhas) > 1 else "",
        )
    ]


def _extrair_texto(dados: bytes, extensao: str) -> list[Secao]:
    texto = limpar_texto(decodificar_texto(dados))
    if not texto:
        return []
    if extensao != ".md":
        return [Secao("texto", texto)]
    secoes: list[Secao] = []
    titulo = "texto"
    linhas: list[str] = []
    for linha in texto.split("\n"):
        achado = re.match(r"^#{1,3}\s+(.{1,120})$", linha)
        if achado:
            if "\n".join(linhas).strip():
                secoes.append(Secao(titulo, "\n".join(linhas).strip()))
            titulo, linhas = achado.group(1).strip(), []
        else:
            linhas.append(linha)
    if "\n".join(linhas).strip():
        secoes.append(Secao(titulo, "\n".join(linhas).strip()))
    return secoes


def extrair_texto(arquivo: ArquivoValidado) -> Extraido:
    """Texto do arquivo em seções. Imagens não passam por aqui (ver descricao_imagem)."""
    if arquivo.tipo == "pdf":
        secoes = _extrair_pdf(arquivo.conteudo)
    elif arquivo.extensao == ".docx":
        secoes = _extrair_docx(arquivo.conteudo)
    elif arquivo.extensao == ".xlsx":
        secoes = _extrair_xlsx(arquivo.conteudo)
    elif arquivo.extensao == ".pptx":
        secoes = _extrair_pptx(arquivo.conteudo)
    elif arquivo.tipo == "csv":
        secoes = _extrair_csv(arquivo.conteudo)
    elif arquivo.tipo == "texto":
        secoes = _extrair_texto(arquivo.conteudo, arquivo.extensao)
    else:
        raise ArquivoRecusado("tipo_nao_suportado", "Tipo de arquivo sem leitor.")
    secoes = [s for s in secoes if s.texto.strip()]
    if not secoes:
        raise ArquivoRecusado("sem_texto", "Não encontrei texto legível neste arquivo.")
    restante = ARQUIVOS_MAX_CARACTERES
    final: list[Secao] = []
    truncado = False
    for secao in secoes:
        if restante <= 0:
            truncado = True
            break
        if len(secao.texto) > restante:
            secao = Secao(secao.rotulo, secao.texto[:restante], secao.cabecalho)
            truncado = True
        restante -= len(secao.texto)
        final.append(secao)
    return Extraido(final, truncado)
