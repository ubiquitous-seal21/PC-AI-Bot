FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg openssh-client ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask waitress requests trafilatura pypdf faster-whisper "yt-dlp[default]"
WORKDIR /app
COPY engine.py app.py index.html ./
# Refresh yt-dlp on every start; video sites change often and old versions break.
CMD ["sh", "-c", "pip install -U -q 'yt-dlp[default]' || true; exec python -u app.py"]
