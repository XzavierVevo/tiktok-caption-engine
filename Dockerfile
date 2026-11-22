# TikTok Caption Tool — FastAPI + FFmpeg + Playwright Chromium + Node signer
# Built by `wrangler deploy` (see worker/wrangler.jsonc) and run on
# Cloudflare Containers. Local test:
#   docker build -t caption-tool . && docker run -p 8080:8080 -e ALLOW_DEV_NO_AUTH=1 caption-tool

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    DATA_DIR=/tmp/data \
    LOG_JSON=1 \
    PORT=8080 \
    LOGIN_HEADED=1 \
    XVFB_DISPLAY=:99 \
    TIKTOK_SIGNER_URL=http://127.0.0.1:8081 \
    TIKTOK_SIGNER_ENABLED=1

# FFmpeg, fonts, Xvfb; Node 20 for tiktok-signature sidecar
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-color-emoji \
        ca-certificates \
        xvfb \
        curl \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/tiktok-signature/ vendor/tiktok-signature/
RUN cd vendor/tiktok-signature \
    && npm install --omit=dev \
    && npx playwright install-deps chromium || true \
    && npx playwright install chromium || true

COPY app/ app/
COPY static/ static/
COPY fonts/ fonts/

# Cloudflare Containers: never embed the AdsPower worker (no AdsPower here).
ENV PROVISION_EMBED_WORKER=0
ENV CLOUDFLARE=1
ENV SECRETS_REV=6

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "15"]
