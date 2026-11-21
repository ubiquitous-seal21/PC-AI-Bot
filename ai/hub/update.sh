#!/bin/sh
set -eu

REPO=/repo
STATUS="$REPO/.ai-hub-update.log"
BACKUP=$(mktemp -d)

git config --global --add safe.directory "$REPO" >/dev/null 2>&1 || true

# The helper container runs as root. Reapply the repository host owner's
# UID/GID so Git remains writable after the helper has fetched or stashed files.
if [ "$(id -u)" -eq 0 ] && [ -d "$REPO" ]; then
  REPO_UID=$(stat -c '%u' "$REPO")
  REPO_GID=$(stat -c '%g' "$REPO")
  chown -R "$REPO_UID:$REPO_GID" "$REPO" 2>/dev/null || true
fi

if [ "${AI_HUB_UPDATE_HELPER:-0}" != "1" ]; then
  if ! docker info >/dev/null 2>&1; then
    printf 'Update failed: Docker daemon is unavailable from the Hub container\n' > "$STATUS"
    exit 1
  fi
  IMAGE=$(docker inspect ai-hub --format '{{.Image}}' 2>/dev/null) || {
    printf 'Update failed: could not identify the running Hub image\n' > "$STATUS"
    exit 1
  }
  REPO_SOURCE=$(docker inspect ai-hub --format '{{range .Mounts}}{{if eq .Destination "/repo"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
  if [ -z "$REPO_SOURCE" ]; then
    printf 'Update failed: could not identify the repository mount\n' > "$STATUS"
    exit 1
  fi
  HELPER="ai-hub-updater-$$"
  if docker rm -f "$HELPER" >/dev/null 2>&1; then :; fi
  printf 'Update started in a helper container\n' > "$STATUS"
  if docker run -d --name "$HELPER" --rm \
    -e AI_HUB_UPDATE_HELPER=1 \
    -v "$REPO_SOURCE:/repo:rw" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    "$IMAGE" sh -c 'exec /app/update.sh' >/dev/null; then
    exit 0
  fi
  printf 'Update failed: could not start the update helper\n' > "$STATUS"
  exit 1
fi

restore_settings() {
  if [ -f "$BACKUP/.env" ]; then cp "$BACKUP/.env" "$REPO/.env"; fi
  if [ -f "$BACKUP/hub.db" ]; then
    mkdir -p "$REPO/data"
    cp "$BACKUP/hub.db" "$REPO/data/hub.db"
  fi
  if [ -d "$BACKUP/ssh" ]; then
    mkdir -p "$REPO/ssh"
    cp -a "$BACKUP/ssh/." "$REPO/ssh/"
  fi
}

mkdir -p "$BACKUP/data" "$BACKUP/ssh"
[ -f "$REPO/.env" ] && cp "$REPO/.env" "$BACKUP/.env"
[ -f "$REPO/data/hub.db" ] && cp "$REPO/data/hub.db" "$BACKUP/hub.db"
[ -d "$REPO/ssh" ] && cp -a "$REPO/ssh/." "$BACKUP/ssh/"

printf 'Update started\n' > "$STATUS"

if ! docker info >/dev/null 2>&1; then
  printf 'Update failed: Docker daemon is unavailable from the Hub container\n' > "$STATUS"
  restore_settings
  exit 1
fi

OLD_HEAD=$(git -C "$REPO" rev-parse HEAD)
BRANCH=$(git -C "$REPO" branch --show-current)
LOCAL_CHANGES="$(git -C "$REPO" status --porcelain)"
SOURCE_STASHED=0

if [ -n "$LOCAL_CHANGES" ]; then
  if ! git -C "$REPO" stash push --include-untracked -m "PC-AI-Bot pre-update source backup"; then
    printf 'Update failed: could not preserve local source changes\n' > "$STATUS"
    restore_settings
    exit 1
  fi
  SOURCE_STASHED=1
fi

if ! git -C "$REPO" fetch origin; then
  printf 'Update failed: could not fetch GitHub\n' > "$STATUS"
  if [ "$SOURCE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi

if [ -n "$BRANCH" ]; then
  if ! git -C "$REPO" pull --ff-only origin "$BRANCH"; then
    printf 'Update failed: GitHub pull was not fast-forwardable\n' > "$STATUS"
    if [ "$SOURCE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
    restore_settings
    exit 1
  fi
else
  printf 'Update failed: repository is in detached HEAD state\n' > "$STATUS"
  if [ "$SOURCE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi

restore_settings

if docker compose -f "$REPO/docker-compose.yml" --project-name ai-hub --project-directory "$REPO" up -d --build --force-recreate --remove-orphans ai-hub; then
  printf 'Update complete: %s\n' "$(git -C "$REPO" rev-parse --short HEAD)" > "$STATUS"
  rm -rf "$BACKUP"
  exit 0
fi

printf 'Update failed: container rebuild failed; previous data and settings restored; revision was %s\n' "$(git -C "$REPO" rev-parse --short "$OLD_HEAD")" > "$STATUS"
restore_settings
rm -rf "$BACKUP"
exit 1
