# ---- build ----
FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
    
WORKDIR /app
    
RUN apk add --no-cache --virtual .build-deps \
    gcc musl-dev libffi-dev postgresql-dev
    
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
    
# ---- imagem final ----
FROM python:3.12-alpine
    
RUN apk add --no-cache libpq && \
    apk upgrade --no-cache
    
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .
    
ENV PATH=/root/.local/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
    
RUN adduser -D appuser && chown -R appuser:appuser /app
USER appuser
    
EXPOSE 8000

CMD ["uvicorn", "iai.app.main:app", "--host", "0.0.0.0", "--port", "8000"]