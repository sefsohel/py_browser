FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# nginx (reverse proxy), envsubst (config templating)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nginx gettext-base ca-certificates fonts-liberation fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && ls /root/.cache/ms-playwright \
 && rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Railway injects $PORT at runtime; nginx listens on it.
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
