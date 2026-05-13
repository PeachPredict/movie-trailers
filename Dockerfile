FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    WHISPER_MODEL_DIR=/app/models \
    HF_HUB_DISABLE_TELEMETRY=1

# System deps:
# - ca-certificates: TLS verification
# - ffmpeg: required by faster-whisper to decode audio (and by yt-dlp)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.4.29 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies (cached layer).
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake a Whisper model into the image so cold starts skip the Hugging Face
# download. Override with `--build-arg WHISPER_BAKE_MODEL=medium` (peach_server
# uses medium; Cloud Run uses small to fit the 4 GiB limit).
ARG WHISPER_BAKE_MODEL=small
ENV WHISPER_BAKE_MODEL=${WHISPER_BAKE_MODEL}
RUN /app/.venv/bin/python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_BAKE_MODEL'], download_root='${WHISPER_MODEL_DIR}')"

# Install project.
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["mt"]
CMD ["run-daily"]
