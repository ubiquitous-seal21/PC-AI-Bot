#!/usr/bin/env bash
set -u

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1

fail=0

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; fail=1; }

printf 'Checking %s\n' "$ROOT"

for path in docker-compose.yml ai/hub/Dockerfile ai/hub/app.py ai/hub/engine.py ai/hub/index.html .env; do
  [ -e "$path" ] && pass "$path exists" || fail "$path is missing"
done

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  pass "Docker Compose is installed"
  if docker compose config >/dev/null 2>&1; then
    pass "docker-compose.yml and .env parse"
  else
    fail "docker-compose.yml or .env is invalid"
  fi
else
  fail "Docker Compose is unavailable"
fi

for key in HUB_PASSWORD PC_MAC PC_IP PC_SSH_USER FRESHRSS_URL FRESHRSS_USER FRESHRSS_API_PASSWORD SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD MAIL_TO TEXT_MODEL VISION_MODEL; do
  value="$(sed -n "s/^${key}=//p" .env 2>/dev/null | head -n1)"
  [ -n "$value" ] && ! [[ "$value" == change-this-* ]] && ! [[ "$value" == your-* ]] && pass "$key is configured" || fail "$key is missing or still a placeholder"
done

password="$(sed -n 's/^HUB_PASSWORD=//p' .env 2>/dev/null | head -n1)"
[ ${#password} -ge 8 ] && pass "HUB_PASSWORD is at least 8 characters" || fail "HUB_PASSWORD is shorter than 8 characters"

pc_ip="$(sed -n 's/^PC_IP=//p' .env 2>/dev/null | head -n1)"
pc_user="$(sed -n 's/^PC_SSH_USER=//p' .env 2>/dev/null | head -n1)"

if [ -n "$pc_ip" ] && curl --max-time 5 -fsS "http://$pc_ip:11434/" >/dev/null 2>&1; then
  pass "Ollama responds at http://$pc_ip:11434"
else
  fail "Ollama does not respond at http://$pc_ip:11434"
fi

if [ -n "$pc_ip" ] && [ -n "$pc_user" ] && [ -f ssh/id_ed25519 ]; then
  if ssh -i ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 "$pc_user@$pc_ip" hostname >/dev/null 2>&1; then
    pass "Passwordless SSH key login works"
  else
    fail "Passwordless SSH key login failed"
  fi

  if ssh -i ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 "$pc_user@$pc_ip" "sudo -n true" >/dev/null 2>&1; then
    pass "Limited passwordless sudo check works"
  else
    fail "Limited passwordless sudo check failed"
  fi

  if ssh -i ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 "$pc_user@$pc_ip" "loginctl list-sessions --no-legend >/dev/null 2>&1" >/dev/null 2>&1; then
    pass "Desktop-session command is available"
  else
    fail "Desktop-session command is unavailable"
  fi
else
  fail "SSH key or PC settings are missing"
fi

if docker compose ps --status running ai-hub 2>/dev/null | grep -q ai-hub; then
  pass "ai-hub container is running"
else
  fail "ai-hub container is not running"
fi

if curl --max-time 5 -fsSI "http://127.0.0.1:8088/" >/dev/null 2>&1; then
  pass "The dashboard responds locally on port 8088"
else
  fail "The dashboard does not respond locally on port 8088"
fi

printf '\n%s\n' 'Automatic shutdown must remain disabled until every PC safety check passes.'
exit "$fail"
