FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# nginx (reverse proxy), envsubst (config templating), PulseAudio (virtual sound card for audio streaming)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nginx gettext-base ca-certificates pulseaudio pulseaudio-utils fonts-liberation fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# PulseAudio refuses to run as root, so it gets its own user (see scripts/entrypoint.sh)
RUN useradd --system --home-dir /tmp/pa --shell /usr/sbin/nologin pulsed

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chrome \
 && /opt/google/chrome/chrome --version \
 && rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Railway injects $PORT at runtime; nginx listens on it.
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
