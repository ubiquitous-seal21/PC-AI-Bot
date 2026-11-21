FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg openssh-client ca-certificates tzdata git curl tar docker.io gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
RUN arch="$(dpkg --print-architecture)" && case "$arch" in \
      amd64) compose_arch=x86_64 ;; \
      arm64) compose_arch=aarch64 ;; \
      *) echo "Unsupported Docker Compose architecture: $arch" >&2; exit 1 ;; \
    esac \
 && mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-${compose_arch}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose \
 && chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
RUN arch="$(dpkg --print-architecture)" && case "$arch" in \
      amd64) docker_arch=x86_64 ;; \
      arm64) docker_arch=aarch64 ;; \
      *) echo "Unsupported Docker CLI architecture: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-28.0.1.tgz" -o /tmp/docker.tgz \
 && tar xzf /tmp/docker.tgz -C /tmp \
 && install -m 0755 /tmp/docker/docker /usr/local/bin/docker \
 && rm -rf /tmp/docker /tmp/docker.tgz
RUN pip install --no-cache-dir flask waitress requests trafilatura pypdf faster-whisper "yt-dlp[default]"
WORKDIR /app
COPY engine.py app.py index.html update.sh ./
RUN chmod 0755 /app/update.sh
# Refresh yt-dlp on every start; video sites change often and old versions break.
CMD ["sh", "-c", "pip install -U -q 'yt-dlp[default]' || true; exec python -u app.py"]
