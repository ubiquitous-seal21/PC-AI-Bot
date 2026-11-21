#!/bin/sh
set -eu

REPO=/repo
STATUS="$REPO/.ai-hub-update.log"
BACKUP=$(mktemp -d)

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0='*'
git_safe() {
  git -c safe.directory='*' "$@"
}
git_safe config --global --add safe.directory '*' >/dev/null 2>&1 || true

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
  REPO_HOME=$(dirname "$REPO_SOURCE")
  REPO_UID=$(stat -c '%u' "$REPO_SOURCE")
  REPO_GID=$(stat -c '%g' "$REPO_SOURCE")
  HELPER_SETUP='git config --global --replace-all safe.directory "*" >/dev/null 2>&1 || true; git config --global --add safe.directory "*" >/dev/null 2>&1 || true'
  if [ -f "$REPO_HOME/.gitconfig" ] && [ -d "$REPO_HOME/.config/gh" ]; then
    docker run -d --name "$HELPER" --network host --rm \
      -u "$REPO_UID:$REPO_GID" -e HOME=/tmp/updater \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
      -e AI_HUB_UPDATE_HELPER=1 \
      -v "$REPO_SOURCE:/repo:rw" \
      -v "$REPO_HOME/.gitconfig:/tmp/updater/.gitconfig:ro" \
      -v "$REPO_HOME/.config/gh:/tmp/updater/.config/gh:ro" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      "$IMAGE" sh -c "$HELPER_SETUP; mkdir -p /tmp/updater/.config; gh auth setup-git >/dev/null 2>&1 || true; exec /repo/ai/hub/update.sh" >/dev/null &&
      exit 0
  elif [ -f "$REPO_HOME/.gitconfig" ] && [ -f "$REPO_HOME/.git-credentials" ]; then
    docker run -d --name "$HELPER" --network host --rm \
      -u "$REPO_UID:$REPO_GID" -e HOME=/tmp/updater \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
      -e AI_HUB_UPDATE_HELPER=1 \
      -v "$REPO_SOURCE:/repo:rw" \
      -v "$REPO_HOME/.gitconfig:/tmp/updater/.gitconfig:ro" \
      -v "$REPO_HOME/.git-credentials:/tmp/updater/.git-credentials:ro" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      "$IMAGE" sh -c "$HELPER_SETUP; git config --global credential.helper \"store --file=/tmp/updater/.git-credentials\" >/dev/null 2>&1 || true; exec /repo/ai/hub/update.sh" >/dev/null &&
      exit 0
  elif [ -f "$REPO_HOME/.gitconfig" ]; then
    docker run -d --name "$HELPER" --network host --rm \
      -u "$REPO_UID:$REPO_GID" -e HOME=/tmp/updater \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
      -e AI_HUB_UPDATE_HELPER=1 \
      -v "$REPO_SOURCE:/repo:rw" \
      -v "$REPO_HOME/.gitconfig:/tmp/updater/.gitconfig:ro" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      "$IMAGE" sh -c "$HELPER_SETUP; exec /repo/ai/hub/update.sh" >/dev/null &&
      exit 0
  else
    docker run -d --name "$HELPER" --network host --rm \
      -u "$REPO_UID:$REPO_GID" -e HOME=/tmp/updater \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
      -e AI_HUB_UPDATE_HELPER=1 \
      -v "$REPO_SOURCE:/repo:rw" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      "$IMAGE" sh -c "$HELPER_SETUP; exec /repo/ai/hub/update.sh" >/dev/null &&
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

printf 'Update progress:10:Preparing backup\n' > "$STATUS"

if ! docker info >/dev/null 2>&1; then
  printf 'Update failed: Docker daemon is unavailable from the Hub container\n' > "$STATUS"
  restore_settings
  exit 1
fi

OLD_HEAD=$(git_safe -C "$REPO" rev-parse HEAD)
BRANCH=$(git_safe -C "$REPO" branch --show-current)
LOCAL_CHANGES="$(git_safe -C "$REPO" status --porcelain)"
SOURCE_STASHED=0

if [ -n "$LOCAL_CHANGES" ]; then
  if ! git_safe -C "$REPO" stash push --include-untracked -m "PC-AI-Bot pre-update source backup"; then
    printf 'Update failed: could not preserve local source changes\n' > "$STATUS"
    restore_settings
    exit 1
  fi
  SOURCE_STASHED=1
fi
printf 'Update progress:25:Preserving local source changes\n' > "$STATUS"

FETCH_ERR=$(mktemp)
if ! git_safe -C "$REPO" -c http.timeout=30 -c http.connectTimeout=15 fetch --prune origin 2>"$FETCH_ERR"; then
  FETCH_DETAIL=$(tail -n 3 "$FETCH_ERR" | tr '\n' ' ' | sed 's/[[:space:]]*$//')
  if [ -n "$FETCH_DETAIL" ]; then
    printf 'Update failed: could not fetch GitHub: %s\n' "$FETCH_DETAIL" > "$STATUS"
  else
    printf 'Update failed: could not fetch GitHub\n' > "$STATUS"
  fi
  rm -f "$FETCH_ERR"
  if [ "$SOURCE_STASHED" -eq 1 ]; then git_safe -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi
rm -f "$FETCH_ERR"
printf 'Update progress:45:Fetching GitHub changes\n' > "$STATUS"

if [ -n "$BRANCH" ]; then
  if ! git_safe -C "$REPO" pull --ff-only origin "$BRANCH"; then
    printf 'Update failed: GitHub pull was not fast-forwardable\n' > "$STATUS"
    if [ "$SOURCE_STASHED" -eq 1 ]; then git_safe -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
    restore_settings
    exit 1
  fi
else
  printf 'Update failed: repository is in detached HEAD state\n' > "$STATUS"
  if [ "$SOURCE_STASHED" -eq 1 ]; then git_safe -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi
printf 'Update progress:70:Fast-forwarding repository\n' > "$STATUS"

restore_settings
printf 'Update progress:90:Rebuilding the Hub\n' > "$STATUS"

if docker compose -f "$REPO/docker-compose.yml" --project-name ai-hub --project-directory "$REPO" up -d --build --force-recreate --remove-orphans ai-hub; then
  printf 'Update complete: %s\n' "$(git_safe -C "$REPO" rev-parse --short HEAD)" > "$STATUS"
  rm -rf "$BACKUP"
  exit 0
fi

printf 'Update failed: container rebuild failed; previous data and settings restored; revision was %s\n' "$(git_safe -C "$REPO" rev-parse --short "$OLD_HEAD")" > "$STATUS"
restore_settings
rm -rf "$BACKUP"
exit 1
