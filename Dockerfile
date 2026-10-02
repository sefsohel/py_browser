FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------
# Install system packages
# ---------------------------------------------------------

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-sandbox \
        xvfb \
        openbox \
        x11vnc \
        nginx \
        novnc \
        websockify \
        tini \
        gettext-base \
        ca-certificates \
        fonts-liberation \
        fonts-noto-color-emoji \
        fonts-noto-cjk \
        fonts-noto-core \
        fonts-noto-extra \
        procps \
        bash \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------
# Create browser user
# ---------------------------------------------------------

RUN useradd \
    --create-home \
    --shell /bin/bash \
    --uid 1000 \
    browser

# ---------------------------------------------------------
# Application directories
# ---------------------------------------------------------

RUN mkdir -p \
        /app \
        /data \
        /data/chromium \
        /data/downloads \
        /etc/nginx/templates \
        /tmp/.X11-unix \
    && chown -R browser:browser \
        /app \
        /data \
        /home/browser \
    && chmod 1777 /tmp/.X11-unix

# ---------------------------------------------------------
# Application working directory
# ---------------------------------------------------------

WORKDIR /app

# ---------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------

COPY requirements.txt /app/requirements.txt

RUN pip install \
        --no-cache-dir \
        --upgrade pip \
    && pip install \
        --no-cache-dir \
        -r /app/requirements.txt

# ---------------------------------------------------------
# Python application
# ---------------------------------------------------------

COPY app.py /app/app.py

# ---------------------------------------------------------
# nginx configuration template
# ---------------------------------------------------------

COPY nginx/default.conf.template \
    /etc/nginx/templates/default.conf.template

# ---------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------

COPY scripts/entrypoint.sh \
    /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

# ---------------------------------------------------------
# Environment
# ---------------------------------------------------------

ENV DISPLAY=:99

ENV APP_PORT=8000

ENV BROWSER_DATA_DIR=/data/chromium

ENV BROWSER_DOWNLOAD_DIR=/data/downloads

# ---------------------------------------------------------
# Ports
# ---------------------------------------------------------

EXPOSE 8080
EXPOSE 8000
EXPOSE 6080
EXPOSE 5900

# ---------------------------------------------------------
# Tini
# ---------------------------------------------------------

ENTRYPOINT ["/usr/bin/tini", "--"]

# ---------------------------------------------------------
# Start application
# ---------------------------------------------------------

CMD ["/app/entrypoint.sh"]
