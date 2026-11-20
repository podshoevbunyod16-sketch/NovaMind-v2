# syntax=docker/dockerfile:1
FROM python:3.15-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin novamind

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY --chown=novamind:novamind . .
USER novamind
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" || exit 1

# gthread keeps streaming NDJSON responses responsive.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gthread", \
     "--workers", "2", "--threads", "4", "--timeout", "120", \
     "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", \
     "app:app"]
