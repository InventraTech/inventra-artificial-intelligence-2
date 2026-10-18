from functools import lru_cache
from pathlib import PurePath

from fastapi import HTTPException
from groq import Groq, GroqError

from iai.app.config import GROQ_API_KEY
from iai.app.llms import modelo_transcricao
from iai.app.prompts import PROMPT_TRANSCRICAO

TAMANHO_MAXIMO_BYTES = 10 * 1024 * 1024
EXTENSOES_ACEITAS = {".webm", ".ogg", ".mp3", ".m4a", ".mp4", ".wav", ".mpeg", ".mpga"}


@lru_cache(maxsize=1)
def cliente() -> Groq:
    return Groq(api_key=GROQ_API_KEY.get_secret_value())


def transcrever_audio(conteudo: bytes, nome_arquivo: str) -> str:
    """Converte áudio em texto em português. Não grava nem registra o áudio."""
    if PurePath(nome_arquivo).suffix.lower() not in EXTENSOES_ACEITAS:
        raise HTTPException(415, "Formato de áudio não suportado.")
    if not conteudo:
        raise HTTPException(422, "Áudio vazio.")
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise HTTPException(413, "Áudio acima de 10 MB.")
    try:
        resposta = cliente().audio.transcriptions.create(
            file=(nome_arquivo, conteudo),
            model=modelo_transcricao,
            language="pt",
            prompt=PROMPT_TRANSCRICAO,
            temperature=0.0,
            response_format="json",
        )
    except GroqError:
        raise HTTPException(502, "Não foi possível transcrever o áudio.") from None
    texto = (resposta.text or "").strip()
    if not texto:
        raise HTTPException(422, "Não consegui entender o áudio. Tente de novo.")
    return texto
