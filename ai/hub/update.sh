#!/bin/sh
set -eu

REPO=/repo
STATUS="$REPO/.ai-hub-update.log"
BACKUP=$(mktemp -d)

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

OLD_HEAD=$(git -C "$REPO" rev-parse HEAD)
BRANCH=$(git -C "$REPO" branch --show-current)
COMPOSE_STASHED=0

if [ -n "$(git -C "$REPO" status --porcelain -- docker-compose.yml)" ]; then
  if ! git -C "$REPO" stash push -m "PC-AI-Bot server compose backup" -- docker-compose.yml; then
    printf 'Update failed: could not preserve local docker-compose.yml changes\n' > "$STATUS"
    restore_settings
    exit 1
  fi
  COMPOSE_STASHED=1
fi

if ! git -C "$REPO" fetch origin; then
  printf 'Update failed: could not fetch GitHub\n' > "$STATUS"
  if [ "$COMPOSE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi

if [ -n "$BRANCH" ]; then
  if ! git -C "$REPO" pull --ff-only origin "$BRANCH"; then
    printf 'Update failed: GitHub pull was not fast-forwardable\n' > "$STATUS"
    if [ "$COMPOSE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
    restore_settings
    exit 1
  fi
else
  printf 'Update failed: repository is in detached HEAD state\n' > "$STATUS"
  if [ "$COMPOSE_STASHED" -eq 1 ]; then git -C "$REPO" stash pop >/dev/null 2>&1 || true; fi
  restore_settings
  exit 1
fi

restore_settings

if [ "$COMPOSE_STASHED" -eq 1 ]; then
  if ! git -C "$REPO" stash pop; then
    printf 'Update blocked: local docker-compose.yml changes conflicted with the update\n' > "$STATUS"
    restore_settings
    exit 1
  fi
fi

if docker compose -f "$REPO/docker-compose.yml" --project-directory "$REPO" up -d --build --force-recreate ai-hub; then
  printf 'Update complete: %s\n' "$(git -C "$REPO" rev-parse --short HEAD)" > "$STATUS"
  rm -rf "$BACKUP"
  exit 0
fi

printf 'Update failed: container rebuild failed; previous data and settings restored; revision was %s\n' "$(git -C "$REPO" rev-parse --short "$OLD_HEAD")" > "$STATUS"
restore_settings
rm -rf "$BACKUP"
exit 1
