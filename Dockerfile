FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

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
    && rm -rf /var/lib/apt/lists/* \
    # Debian's default nginx site would clash with our generated config
    && rm -f /etc/nginx/sites-enabled/default

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

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

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

COPY scripts/entrypoint.sh /app/entrypoint.sh

# sed strips Windows (CRLF) line endings if the script was edited on
# Windows, which would otherwise break the shebang.
RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

# ---------------------------------------------------------
# Environment
# ---------------------------------------------------------

ENV DISPLAY=:99 \
    PORT=8080 \
    APP_PORT=8000 \
    BROWSER_DATA_DIR=/data/chromium \
    BROWSER_DOWNLOAD_DIR=/data/downloads

# ---------------------------------------------------------
# Ports
# ---------------------------------------------------------
# Only nginx is public. Gunicorn (8000), noVNC (6080) and VNC (5900)
# are internal and reached through nginx.

EXPOSE 8080

# ---------------------------------------------------------
# Tini (PID 1)
# ---------------------------------------------------------
# -g forwards signals to the whole process group so every child
# shuts down cleanly. Tini also reaps zombie processes.

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]

# ---------------------------------------------------------
# Start application
# ---------------------------------------------------------

CMD ["/app/entrypoint.sh"]
