FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YTDIGEST_HOME=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates tzdata libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp 解析 YouTube 的 JS challenge 需要 Deno
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && /usr/local/bin/deno --version

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY auth/ /auth/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]
ENTRYPOINT ["/entrypoint.sh"]
