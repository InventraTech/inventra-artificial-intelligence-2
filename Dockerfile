# ---- build ----
FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache --virtual .build-deps \
    gcc musl-dev libffi-dev

COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---- imagem final ----
FROM python:3.12-alpine

RUN apk upgrade --no-cache

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# UID numérico: o Kubernetes (runAsNonRoot) não aceita usuário só por nome.
RUN adduser -D -u 10001 appuser && chown -R appuser:appuser /app
USER 10001

EXPOSE 8000

CMD ["uvicorn", "iai.app.main:app", "--host", "0.0.0.0", "--port", "8000"]