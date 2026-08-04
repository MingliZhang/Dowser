# Playwright's image already carries Chromium and every system library it needs.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8081 \
    DOWNLOAD_DIR=/downloads \
    TEMP_DIR=/downloads/.incomplete \
    STATE_FILE=/config/state.json

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
EXPOSE 8081

CMD ["python", "-m", "app.main"]
