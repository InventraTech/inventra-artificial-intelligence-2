import re

from langchain_core.prompts import ChatPromptTemplate

from iai.app.llms import llm_rapido
from iai.app.prompts import GUARDRAIL_ENTRADA_SYSTEM_PROMPT
from iai.app.schemas import ResultadoGuardrail

TERMOS_PROIBIDOS = [
    "idiota", "burro", "imbecil", "merda", "maldito", "lixo", 
    "ignore todas as instruções", "esqueça o que eu disse",   
    "me dê uma receita", "como cozinhar"                      
]

def verificar(texto: str) -> dict | None:
    """Verificação antes de utilizar o prompt do guardrail"""
    texto_limpo = texto.lower()
    for termo in TERMOS_PROIBIDOS:
        if termo in texto_limpo:
            return {
                "bloqueado": True, 
                "motivo": "filtro_hardcoded", 
                "mensagem": "Sua mensagem contém termos não permitidos ou ofensivos. Por favor, mantenha o foco na gestão do restaurante."
            }
    return None 

def anonimizar_entrada(texto: str) -> tuple[str, dict]:
    """
    Substitui dados sensíveis por tokens (ex: [EMAIL_0]) e guarda o valor real no mapa.
    """
    mapa_pii = {}
    texto_anonimizado = texto
    
    padrao_email = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(padrao_email, texto_anonimizado)
    for i, email in enumerate(emails):
        token = f"[EMAIL_{i}]"
        mapa_pii[token] = email
        texto_anonimizado = texto_anonimizado.replace(email, token)
        
    padrao_cpf = r'\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b'
    cpfs = re.findall(padrao_cpf, texto_anonimizado)
    for i, cpf in enumerate(cpfs):
        token = f"[CPF_{i}]"
        mapa_pii[token] = cpf
        texto_anonimizado = texto_anonimizado.replace(cpf, token)

    return texto_anonimizado, mapa_pii

def guardrail_entrada(mensagem_usuario: str) -> dict:
    resultado = verificar(mensagem_usuario)
    if resultado:
        return resultado
        
    prompt = ChatPromptTemplate.from_messages([
        ("system", GUARDRAIL_ENTRADA_SYSTEM_PROMPT),
        ("human", "{mensagem}")
    ])
    chain = prompt | llm_rapido.with_structured_output(ResultadoGuardrail)
    
    try:
        res = chain.invoke({"mensagem": mensagem_usuario})
        assert isinstance(res, ResultadoGuardrail)
        return {"bloqueado": res.bloqueado, "motivo": res.motivo, "mensagem": res.mensagem}
    except Exception:  # noqa: BLE001 - fail-safe: qualquer erro na chamada ao LLM deve bloquear a mensagem
        return {"bloqueado": True, "motivo": "erro_api", "mensagem": "Erro interno de segurança. Tente novamente."}

def guardrail_saida(texto_gerado: str, mapa_pii: dict, extra: dict) -> dict:
    """
    Restaura os dados originais na resposta para que o usuário veja a informação real.
    """
    texto_final = texto_gerado
    
    if mapa_pii:
        for token, valor_original in mapa_pii.items():
            texto_final = texto_final.replace(token, valor_original)
            
    return {"conteudo": texto_final}