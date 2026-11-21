# PC-AI-Bot
Turns a gaming PC into a functional AI rig that only turns on when necessary.
# First-time setup: from two unused computers to a working PC-AI-Bot

Use this guide if neither computer has Docker, Ollama, SSH automation, Wake-on-LAN, or this hub installed. Run the sections in order. Do not skip a verification step. Stop if the expected result does not appear.

## You need

**Server computer**

- Ubuntu 22.04 or newer
- Ethernet or Wi-Fi
- A regular user account with sudo access
- A fixed IP address on the home router
- Docker access
- A phone or second computer for browser testing

**Gaming PC**

- Arch Linux
- An AMD GPU that Ollama/ROCm can use
- Ethernet connected to the motherboard (not Wi-Fi for Wake-on-LAN)
- About 15 GB of free disk space
- Keyboard/monitor access for BIOS, login, and testing

**Accounts and services**

- Router administrator access
- An email account that supports SMTP and app passwords
- FreshRSS running locally or reachable on the LAN
- A public GitHub repository or Git client access

Keep the hub off the public internet. During setup, use a VPN such as Tailscale or WireGuard if you need to reach it outside the home network.

---

## Phase 1: Record the network values

Perform these steps on both computers.

### 1.1 Find the server IP

On the Ubuntu server:

```bash
hostnamectl
ip -br link
ip -br addr
ip route
```

Record:

- `SERVER_IP`: the server's LAN IPv4 address
- `SERVER_INTERFACE`: the interface named in `ip route`, often `eth0` or `enp...`

Reserve this address in the router before continuing.

### 1.2 Find the PC IP and MAC address

On the Arch PC:

```bash
ip -br link
ip -br addr
```

Record:

- `PC_MAC`: the `ether` address next to your wired interface
- `PC_IP`: the wired IPv4 address
- `PC_INTERFACE`: the wired interface name
- `PC_USER`: the Linux account name used for SSH

Reserve `PC_IP` in the router using `PC_MAC`.

**Expected result:** both addresses remain unchanged after the PC and server are rebooted.

**Stop if:** the PC only has Wi-Fi, the addresses change after reboot, or the two computers are on different networks.

### 1.3 Reserve both addresses in the router

Open the router's address reservation/DHCP section and reserve:

- `SERVER_IP` for the server MAC
- `PC_IP` for `PC_MAC`

The exact router setting names vary. Do not add a router port-forward rule for port 8088.

**Verification:** reboot the router or renew the leases, then confirm both values with `ip -br addr`.

---

## Phase 2: Configure Wake-on-LAN and power control on the Arch PC

All commands in this phase run on the Arch PC.

### 2.1 Configure the BIOS

Enter BIOS/UEFI and set:

- Wake on LAN / PCIe wake: **Enabled**
- ErP / Deep Sleep / EuP: **Disabled**
- Boot device: **Arch Linux first**

Save and reboot.

### 2.2 Enable Wake-on-LAN on the network interface

```bash
sudo pacman -S --needed ethtool
sudo ethtool "$PC_INTERFACE"
```

The output must contain `Supports Wake-on: g` or a similar `g` capability.

Enable it persistently with NetworkManager:

```bash
nmcli -t -f NAME,DEVICE connection show --active
sudo nmcli connection modify "CONNECTION_NAME" 802-3-ethernet.wake-on-lan magic
sudo nmcli connection up "CONNECTION_NAME"
sudo ethtool "$PC_INTERFACE" | grep Wake-on
```

Replace `CONNECTION_NAME` with the connection name from the previous command.

**Go/no-go:** the final line must show `Wake-on: g`.

**Stop if:** it shows `d`, `Wake-on: d`, or the interface is missing.

### 2.3 Install Ollama

```bash
sudo pacman -S --needed ollama-rocm
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
systemctl status --no-pager ollama
```

The status must be `active (running)`.

Test it locally:

```bash
curl http://127.0.0.1:11434
ollama list
```

You should see `Ollama is running` and an empty or populated model list.

### 2.4 Download the models

```bash
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b
ollama run qwen3:8b "Reply with exactly five words"
ollama ps
```

`ollama ps` should show the model and a GPU percentage. If it shows CPU, stop and resolve GPU support before continuing.

Record the exact model names with:

```bash
ollama list
```

### 2.5 Enable SSH

```bash
sudo pacman -S --needed openssh
sudo systemctl enable --now sshd
sudo systemctl status --no-pager sshd
```

**Expected result:** `active (running)`.

Allow ports 22 and 11434 through a PC firewall if that firewall is enabled.

### 2.6 Allow only controlled shutdown or suspend

Replace `sudo systemctl poweroff` only if you intentionally want suspend instead.

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl suspend" | sudo tee /etc/sudoers.d/ai-poweroff
sudo chmod 440 /etc/sudoers.d/ai-poweroff
sudo visudo -cf /etc/sudoers.d/ai-poweroff
```

The final command must end with `parsed OK`.

**Safety gate:** do not enable automatic shutdown until the server has tested session detection and you have `IDLE_OFF_MIN=30` or higher.

---

## Phase 3: Configure FreshRSS

Perform these steps on the server or your normal PC using a browser.

### 3.1 Start or install FreshRSS

Record the URL you use to open FreshRSS, for example:

```text
FRESHRSS_URL=http://192.168.1.10:8080
```

Do not include `/api/` or `/api/greader.php`.

### 3.2 Enable API access

In FreshRSS:

1. Open **Settings**.
2. Open **Authentication**.
3. Enable **Allow API access**.
4. Open **Profile**.
5. Set a separate API password.

Record:

- `FRESHRSS_USER`
- `FRESHRSS_API_PASSWORD`

Open `<FRESHRSS_URL>/api/`. The Google Reader API result must say PASS.

**Stop if:** login fails or the API page is unavailable.

---

## Phase 4: Configure SMTP email

Use a dedicated mailbox when possible.

For Gmail:

1. Enable 2-Step Verification.
2. Open Google App Passwords.
3. Create an app password named `home-ai-hub`.
4. Copy the 16-character value once.

Record:

- `SMTP_USER`: full email address
- `SMTP_PASSWORD`: app password
- `MAIL_TO`: recipient email address

For Gmail:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
```

For port 465 providers:

```text
SMTP_PORT=465
SMTP_SECURITY=ssl
```

Do not use your normal Gmail password.

---

## Phase 5: Install Docker on the Ubuntu server

Run these commands on the server.

### 5.1 Install Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Log out and back in to the server. Reconnect over SSH, then verify:

```bash
docker version
docker compose version
```

**Stop if:** `docker compose version` is unavailable.

### 5.2 Clone this repository

```bash
cd ~
git clone https://github.com/ubiquitous-seal21/PC-AI-Bot.git ai
cd ~/ai
mkdir -p data ssh
ls -la
ls -la ai/hub
```

**Expected result:** `docker-compose.yml`, `.env.example`, `ai/hub/app.py`, `ai/hub/engine.py`, and `ai/hub/index.html` exist.

**Do not use an old archive or copy.** The Docker Compose file now builds `./ai/hub`.

---

## Phase 6: Create the SSH key

Run this on the Ubuntu server. The server will use this key only for the PC account and controlled commands.

```bash
cd ~/ai
mkdir -p ssh
chmod 700 ssh
ssh-keygen -t ed25519 -a 64 -N "" -f ssh/id_ed25519
chmod 600 ssh/id_ed25519
chmod 644 ssh/id_ed25519.pub
ssh-copy-id -i ssh/id_ed25519.pub "$PC_USER@$PC_IP"
ssh -i ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" hostname
```

Replace `PC_USER` and `PC_IP` before running the last two commands.

**Expected result:** the hostname prints without a password prompt.

Test the limited sudo command:

```bash
ssh -i ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" "sudo -n systemctl suspend --no-block"
```

Do not run this power test with work open. It is included to verify sudo access, but for a safer test use `sudo -n true` instead:

```bash
ssh -i ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" "sudo -n true"
```

**Expected result:** no output and exit code 0.

Protect the key:

```bash
chmod 700 ~/ai/ssh
chmod 600 ~/ai/ssh/id_ed25519
```

Never commit `ssh/` or `id_ed25519`.

---

## Phase 7: Create `.env`

Run:

```bash
cd ~/ai
cp .env.example .env
chmod 600 .env
nano .env
```

Enter real values. Example:

```dotenv
TZ=America/New_York
HUB_PASSWORD=use-a-long-random-password
HUB_COOKIE_SECURE=false
HUB_TLS_TERMINATED=false
PORT=8088
MAX_UPLOAD_MB=40

PC_MAC=AA:BB:CC:DD:EE:FF
PC_IP=192.168.1.100
PC_SSH_USER=your-arch-user
LAN_BROADCAST=255.255.255.255
IDLE_OFF_MIN=30

FRESHRSS_URL=http://192.168.1.10:8080
FRESHRSS_USER=your-freshrss-user
FRESHRSS_API_PASSWORD=your-freshrss-api-password

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=you@example.com
SMTP_PASSWORD=your-app-password
MAIL_FROM=you@example.com
MAIL_TO=you@example.com

TEXT_MODEL=qwen3:8b
VISION_MODEL=qwen2.5vl:7b
OLLAMA_URL=http://192.168.1.100:11434
```

Rules:

- Use no quotes around values.
- Use a password of at least 8 characters.
- Set `IDLE_OFF_MIN=0` while testing to disable automatic shutdown.
- Set `HUB_COOKIE_SECURE=true` only behind TLS. Keep it false for plain LAN HTTP.
- Set `OLLAMA_URL` explicitly if Ollama is not at `http://PC_IP:11434`.
- Set `LAN_BROADCAST` to your subnet broadcast, such as `192.168.1.255`, if 255.255.255.255 does not work.

Verify the file:

```bash
sed -E 's/(PASSWORD|PASSWD)=.*/\\1=<hidden>/' .env
ls -l .env
```

The permission line must show `600`.

---

## Phase 8: Start the hub

```bash
cd ~/ai
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 ai-hub
```

**Expected result:** one `ai-hub` container is running and the log contains `hub started on port 8088`.

Check a direct HTTP response:

```bash
curl -I http://127.0.0.1:8088/
```

**Expected result:** `HTTP/1.1 200 OK`.

**Stop if:** the container restarts, Docker reports a missing `.env`, or the build cannot find `ai/hub/Dockerfile`.

---

## Phase 9: Test Wake-on-LAN

Keep `IDLE_OFF_MIN=0` for this test.

On the server:

```bash
sudo apt install -y wakeonlan
wakeonlan "$(grep '^PC_MAC=' ~/ai/.env | cut -d= -f2)"
```

Or send through Docker:

```bash
cd ~/ai
docker compose exec ai-hub python -c "import engine; engine.send_magic(); print('magic packet sent')"
```

The PC should boot within two minutes.

From the server:

```bash
curl --max-time 10 http://PC_IP:11434
ssh -i ~/ai/ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" hostname
```

Replace `PC_IP` and `PC_USER`.

**Go/no-go:** Ollama answers and SSH prints the hostname.

**Stop if:** Ollama answers but SSH fails, or SSH works but Wake-on-LAN fails.

Power the PC back down only after this test:

```bash
ssh -i ~/ai/ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" "sudo -n systemctl poweroff"
```

This is a deliberate manual power-off command. Confirm no unsaved work is open first.

---

## Phase 10: Open and authenticate the dashboard

On a browser inside the same LAN:

```text
http://SERVER_IP:8088
```

Log in with `HUB_PASSWORD`.

Verify:

- The header shows the PC state.
- The **Chatbot** tab exists.
- The desktop side widgets appear.
- The **System** tab lists email, recipients, FreshRSS, and Ollama readiness.

Keep automatic shutdown disabled:

```dotenv
IDLE_OFF_MIN=0
```

Recreate the container after changing `.env`:

```bash
cd ~/ai
docker compose up -d
```

---

## Phase 11: Test email

1. Open **Customize**.
2. Enter `MAIL_TO` or another test recipient.
3. Save.
4. Click **Send test email**.

Check the inbox and spam folder.

**Stop if:** SMTP reports an authentication or TLS error. Fix `.env`, then restart the container.

Do not test AI work yet.

---

## Phase 12: Test the PC wake and source work

With the PC off:

1. Open **Ask**.
2. Select **Ask anything**.
3. Enter `Reply with the number 1`.
4. Click **Run**.

Expected flow:

1. The run becomes queued.
2. The PC wake sequence starts.
3. Ollama receives the request.
4. The run displays `Done`.
5. The result is `1` or a short equivalent.

**Stop if:** Wake-on-LAN fails, Ollama is unreachable, or GPU memory is exhausted.

---

## Phase 13: Test briefing and scheduler work

1. Open **Briefing**.
2. Click **Build a briefing now**.
3. Wait for a `ready` run.
4. Review the rendered result.
5. Click **Email this briefing**.
6. Confirm the HTML/text email arrives.

Then open **Customize** and set:

- A small `lookback_hours`, such as 24
- `top_n`, such as 5
- `min_importance`, such as 5
- A delivery time several minutes in the future

Save and observe the next 30-second scheduler tick.

**Stop if:** FreshRSS authentication fails or no article API is configured.

---

## Phase 14: Enable desktop safety

This phase requires the PC to be on.

On the server:

```bash
ssh -i ~/ai/ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" \
  "loginctl list-sessions --no-legend"
```

The server must print active sessions or fail clearly.

Open **System** and verify the displayed desktop-safety status.

Test with no unsaved work:

1. Set `IDLE_OFF_MIN=30`.
2. Restart the hub.
3. Remain logged into the Arch desktop.
4. Wait at least 31 minutes.
5. Confirm the PC **does not power off**.

Then log out of the desktop:

1. Remain with no active user session.
2. Confirm the hub-woken PC does power off after the configured idle period.

**Do not enable automatic shutdown** until both checks pass.

Manual **Power off the PC** remains an override and is always deliberate.

---

## Phase 15: Enable scheduled use

Only after email, wake, SSH, Ollama, briefing, and desktop-safety tests pass:

1. Set `IDLE_OFF_MIN=30` or longer.
2. Open **Scheduled**.
3. Create a one-off task for the next few minutes.
4. Confirm the run executes once.
5. Confirm it emails once.
6. Restart the container.
7. Confirm interrupted runs are visible in History.
8. Resume manually if the hub remains paused.

Test direct **Chatbot** only with no queued, running, or scheduled work.

---

## Phase 16: HTTPS/LAN-only decision

Choose one:

### Option A: Trusted LAN only

- Keep `HUB_COOKIE_SECURE=false`.
- Do not forward port 8088.
- Use Tailscale or WireGuard for remote access.
- Use a strong `HUB_PASSWORD`.

### Option B: HTTPS reverse proxy

Before exposing HTTPS:

1. Install a reverse proxy on the server.
2. Obtain a trusted local or public certificate.
3. Restrict the proxy to your LAN or VPN.
4. Set:

```dotenv
HUB_COOKIE_SECURE=true
HUB_TLS_TERMINATED=true
```

5. Restart the container.
6. Test login over HTTPS.
7. Confirm an HTTP cookie is not accepted.

Do not expose this application without TLS and network restrictions.

---

## Daily operations

Run the setup verifier after Docker and SSH are configured:

```bash
cd ~/ai
chmod 700 scripts/verify_setup.sh
./scripts/verify_setup.sh
```

The verifier prints `PASS` and `FAIL` lines. Do not enable automatic shutdown until every PC, Ollama, and SSH safety check is `PASS`.

```bash
# View status
cd ~/ai
docker compose ps
docker compose logs --tail=200 ai-hub

# Restart after .env changes
docker compose up -d --force-recreate

# Rebuild after code changes
docker compose up -d --build --force-recreate

# Stop the hub
docker compose down

# Start the hub again
docker compose up -d
```

## Updating the Hub

Open **System** in the Home AI Hub and click **Update from GitHub**. The
authenticated update action:

1. Fetches and fast-forwards the current Git branch.
2. Creates a temporary backup of `.env`, `data/hub.db`, and `ssh/`.
3. Rebuilds and recreates only `ai-hub`.
4. Restores the backup automatically if the pull or rebuild fails.

Do not use `docker compose down -v`; that removes Docker volumes. Normal Hub
updates do not replace `.env`, `data/hub.db`, or `ssh/`.

PC and Ollama diagnostics:

```bash
ssh "$PC_USER@$PC_IP" "systemctl status --no-pager ollama"
ssh "$PC_USER@$PC_IP" "ollama ps"
ssh "$PC_USER@$PC_IP" "loginctl list-sessions"
```

---

## Backup

Back up:

```bash
cd ~
tar czf ai-backup.tgz ai/.env ai/data ai/ssh
chmod 600 ai-backup.tgz
```

Store the backup outside the server. Never upload `.env`, `ai/ssh`, or `ai-backup.tgz` to GitHub or chat.

---

## Rollback

To disable all AI and automatic shutdown immediately:

```dotenv
IDLE_OFF_MIN=0
```

Then open **Pause AI** in the dashboard.

To stop the server completely:

```bash
cd ~/ai
docker compose down
```

To stop the PC:

```bash
ssh -i ~/ai/ssh/id_ed25519 -o BatchMode=yes "$PC_USER@$PC_IP" "sudo -n systemctl poweroff"
```
