# ══════════════════════════════════════════════════════════════════
# NovaMind · production-образ
# ──────────────────────────────────────────────────────────────────
#   docker build -t novamind:v3 .
#   docker run -p 5000:5000 --env-file .env.production \
#              -v novamind-data:/data novamind:v3
# ══════════════════════════════════════════════════════════════════
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=5000 \
    NOVA_ENV=production \
    NOVA_DB_PATH=/data/novamind_chats.db

RUN groupadd --system nova \
 && useradd --system --gid nova --create-home --home-dir /app nova

WORKDIR /app

# Слой зависимостей отдельно: пересобирается только при смене requirements.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /app/traces /app/workspace_data \
 && chown -R nova:nova /app /data \
 && chmod 755 /data

USER nova

EXPOSE 5000

# Проба без curl: тот же /healthz, что проверяет платформа.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"]
