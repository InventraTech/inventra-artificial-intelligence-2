import re

SECRET_PATTERN = (
    r"(?:Bearer\s+[A-Za-z0-9._~-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?:gsk_|AIza)[A-Za-z0-9_-]+|(?:password|senha|api[_-]?key|token)\s*[:=]\s*[^\s,;]+)"
)


def remove_secrets(text):
    return re.sub(SECRET_PATTERN, "[SEGREDO_REMOVIDO]", text, flags=re.IGNORECASE)
