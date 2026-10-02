FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    PORT=8080 \
    APP_PORT=8000 \
    BROWSER_DATA_DIR=/data/chromium \
    BROWSER_DOWNLOAD_DIR=/data/downloads

# The container runs a complete graphical Chromium session behind noVNC.
# Railway publishes the HTTP port it supplies through $PORT.
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
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash browser \
    && mkdir -p /data/chromium /data/downloads /run/nginx \
    && chown -R browser:browser /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY scripts/entrypoint.sh ./entrypoint.sh

RUN chmod +x ./entrypoint.sh \
    && rm -f /etc/nginx/sites-enabled/default \
    && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
