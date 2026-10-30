FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata

# Real Google Chrome (headful, under Xvfb), fonts for Indic scripts and emoji, tini as PID 1, tzdata for TZ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini tzdata xvfb \
        fonts-noto-core fonts-noto-color-emoji \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# Managed Chrome policy: WebRTC may not reveal the real IP around the proxy.
COPY docker/chrome-policies/webrtc.json /etc/opt/chrome/policies/managed/webrtc.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN install -m 0755 docker/worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /tmp/profiles /data/vault \
    && chown -R app:app /tmp/profiles /data /app

USER app
ENV PROFILE_ROOT=/tmp/profiles \
    CHROME_PATH=/usr/bin/google-chrome \
    CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage"

ENTRYPOINT ["tini", "--", "worker-entrypoint.sh"]
