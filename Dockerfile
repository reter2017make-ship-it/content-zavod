# Контент-завод в Docker (Linux, CPU). Сборка: docker compose up -d --build → http://127.0.0.1:8765
# Модели (нейросеть ~4.7 ГБ, распознавание речи ~0.5 ГБ, голоса ~0.25 ГБ) в образ не входят:
# они скачиваются при первом запуске в том ./models и ./data/voices.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow \
    HF_HOME=/app/models/hf \
    FACTORY_WEB_HOST=0.0.0.0 \
    FACTORY_DOWNLOAD_DIR=/app/Reels \
    FACTORY_ENV_FILE=/app/credentials/.env

# ffmpeg, open fonts instead of Arial/Impact (macOS only), compilers for llama-cpp-python (removed after)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-liberation2 fonts-dejavu-core tzdata ca-certificates \
      libgomp1 build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/logs /app/data /app/database /app/credentials /app/models /app/Reels
COPY requirements.txt .
# llama.cpp for CPU, portable build (no Metal in Linux, no CPU-specific instructions)
RUN CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_METAL=OFF" pip install -r requirements.txt \
 && apt-get purge -y build-essential cmake && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY docs ./docs
COPY tests ./tests
COPY Makefile README.md PROGRESS.md CLAUDE.md ./

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/worker', timeout=5)"
# models download in the background while the interface is already open
CMD ["sh", "-c", "python -m app.cli download-models > /app/logs/models.log 2>&1 & exec python -m app.cli web"]
