# Playwright's image already carries Chromium and every system library it needs.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8477 \
    DOWNLOAD_DIR=/downloads \
    TEMP_DIR=/downloads/.incomplete \
    STATE_FILE=/config/state.json \
    SETTINGS_FILE=/config/settings.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY app ./app
COPY static ./static

RUN mkdir -p /downloads /config

VOLUME ["/downloads", "/config"]
EXPOSE 8477

# Lets Docker/compose restart the container if the app wedges.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8477/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
