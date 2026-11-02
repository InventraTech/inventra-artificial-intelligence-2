from functools import cache
from pathlib import PurePath

from fastapi import HTTPException
from groq import Groq, GroqError

from iai.app.core.assistant.llms import modelo_transcricao
from iai.app.core.assistant.prompts import PROMPT_TRANSCRICAO
from iai.app.core.assistant.rodizio import tentar_em_rodizio
from iai.app.core.config import GROQ_API_KEYS

TAMANHO_MAXIMO_BYTES = 10 * 1024 * 1024
EXTENSOES_ACEITAS = {".webm", ".ogg", ".mp3", ".m4a", ".mp4", ".wav", ".mpeg", ".mpga"}


@cache
def cliente(indice: int = 0) -> Groq:
    chave = GROQ_API_KEYS[indice].get_secret_value() if GROQ_API_KEYS else ""
    return Groq(api_key=chave, max_retries=0)


def transcrever_audio(conteudo: bytes, nome_arquivo: str) -> str:
    """Converte áudio em texto em português. Não grava nem registra o áudio."""
    if PurePath(nome_arquivo).suffix.lower() not in EXTENSOES_ACEITAS:
        raise HTTPException(415, "Formato de áudio não suportado.")
    if not conteudo:
        raise HTTPException(422, "Áudio vazio.")
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise HTTPException(413, "Áudio acima de 10 MB.")
    try:
        # Uma chave por candidato: se a Groq limitar uma, a próxima transcreve o mesmo áudio.
        resposta = tentar_em_rodizio(
            [(f"groq/whisper#{n}", n - 1) for n in range(1, max(len(GROQ_API_KEYS), 1) + 1)],
            lambda indice: cliente(indice).audio.transcriptions.create(
                file=(nome_arquivo, conteudo),
                model=modelo_transcricao,
                language="pt",
                prompt=PROMPT_TRANSCRICAO,
                temperature=0.0,
                response_format="json",
            ),
        )
    except GroqError:
        raise HTTPException(502, "Não foi possível transcrever o áudio.") from None
    texto = (resposta.text or "").strip()
    if not texto:
        raise HTTPException(422, "Não consegui entender o áudio. Tente de novo.")
    return texto
