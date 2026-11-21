# PC-AI-Bot
Turns a gaming PC into a functional AI rig that only turns on when necessary.
# Home AI server: setup guide

## Overview

Your PC only runs Ollama; a small Ubuntu server runs one Docker container that wakes the PC when there is work and powers it off when idle. You use it from a web page on your network, and your briefing arrives by email.

**What you get**

- **A morning briefing by email.** The server reads your FreshRSS articles, merges reports of the same event, and ranks stories by importance. It prepares the briefing before your delivery time and emails it at that time, with a "Top stories" section first and then one section per focus area you define.
- **A web page hosted on your server** (no Telegram). It has seven tabs: the latest Briefing, Customize (delivery time, recipients, focus areas, how strict the ranking is), Ask, Scheduled, Presets, History and System.
- **One AI for everything.** On the Ask tab you give it links (articles or videos), PDFs, text files, photos or plain text, then pick a preset: recipe, summary, explain an image, meal plan, or anything you write yourself. Results can be emailed.
- **Scheduled tasks.** Any preset can run on chosen days and be emailed to you, such as a weekly meal plan.

**How "most important" is decided.** There is no outside editor. For each distinct story the model gives an importance score from 0 to 10 (10 is a major event with wide impact; clickbait and fluff score 0 to 2). Stories reported by several of your feeds get a bonus of up to 1.5 points, because many outlets covering the same event is a good sign it matters. Stories under your minimum importance are dropped. Treat the ranking as a first pass from an 8B model, not an editor's judgement; the Customize tab lets you tune it.

**Order of work:** Part 1 on the PC, Part 2 in your router and accounts, Part 3 on the server, Part 4 to start and test, Part 5 for everyday use.

**Before you start**

- PC and server both on wired Ethernet, both reachable over SSH from a machine you control.
- Admin access to your router and FreshRSS, and an email account that can send mail (Gmail works; Part 2 covers it).
- About 15 GB free on the PC for the two models.
- Words in CAPITALS (`PC_IP`, `INTERFACE`, `PC_USER`) are values you replace.

**What is tested:** I ran the code against fake FreshRSS, Ollama and email servers, and drove the web page in a headless browser. That covered login, settings, story merging and ranking, the email, tasks with files, scheduled runs, and the page layout on a phone. I could not test the Docker build or your hardware, so expect to fix a step or two. Troubleshooting is the last section.

## Part 1: Gaming PC (Arch)

After this part the PC wakes on a magic packet, boots straight into Arch, and serves Ollama on your network. Commands run on the PC.

### 1.1 BIOS (reboot, press Del)

- Settings > Platform Power > **ErP**: Disabled.
- Settings > Platform Power > **Wake on LAN**: Enabled (some boards call it PCIe Devices Power On).
- Save and exit. Menu names vary slightly by board.

### 1.2 Find your network details

```bash
ip -br link
ip -br addr
nmcli -t -f NAME,DEVICE connection show --active
```

Write down: the wired **INTERFACE** (like `enp5s0`), its **MAC** (the `AA:BB:CC:...` value on the `ip -br link` line), the PC's **IP**, and the connection **NAME** from the last command.

### 1.3 Make Wake-on-LAN stick

```bash
sudo pacman -S --needed ethtool
sudo nmcli connection modify "CONNECTION_NAME" 802-3-ethernet.wake-on-lan magic
sudo nmcli connection up "CONNECTION_NAME"
sudo ethtool INTERFACE | grep Wake-on
```

The last line should end in `Wake-on: g`. If you don't use NetworkManager, use a service instead (replace `INTERFACE`):

```bash
sudo tee /etc/systemd/system/wol.service >/dev/null <<'EOF'
[Unit]
Description=Enable Wake-on-LAN
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/ethtool -s INTERFACE wol g

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now wol.service
```

### 1.4 Ollama with AMD GPU support

```bash
sudo pacman -S --needed ollama-rocm
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
```

Pull the two models (skip one if you already have a model of that kind; you can name yours in the server's `.env` later):

```bash
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b
ollama run qwen3:8b "Say hi in five words"
ollama ps
```

`ollama ps` should show `100% GPU`. If it says CPU, see Troubleshooting.

### 1.5 Boot into Arch automatically

```bash
grep -E "^menuentry" /boot/grub/grub.cfg | head
sudo nano /etc/default/grub
```

Set `GRUB_DEFAULT=0` (if Arch is the first entry listed; otherwise the entry's position, counting from 0) and `GRUB_TIMEOUT=3`. Then:

```bash
sudo grub-mkconfig -o /boot/grub/grub.cfg
```

### 1.6 Let the server power the PC off

The server powers the PC off after jobs, but only if the server woke it. A PC you turned on yourself is left alone.

```bash
sudo pacman -S --needed openssh
sudo systemctl enable --now sshd
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl suspend" | sudo tee /etc/sudoers.d/ai-poweroff
sudo chmod 440 /etc/sudoers.d/ai-poweroff
sudo visudo -cf /etc/sudoers.d/ai-poweroff
```

The last command should say `parsed OK`. If you run a firewall on the PC, allow TCP ports 22 and 11434 from the server's IP.

## Part 2: Router, email and FreshRSS (UI steps)

Three short browser jobs. Collect the values as you go; Part 3 needs all of them.

### 2.1 Router (browser)

1. Open your router's admin page (often `192.168.1.1`).
2. Find DHCP, LAN or Address Reservation (the name varies by router).
3. Reserve a fixed address for the PC (using its MAC from 1.2) and for the server.
4. Write down **PC\_IP** and the server's IP (**SERVER\_IP**). The hub finds the PC by this address, so it must not change.

Wake-on-LAN stays inside your home network, so there is nothing to port-forward. Do not expose the hub's web page to the internet; it is meant for your home network only.

### 2.2 Email that can send (Gmail example)

The hub sends your briefing through an SMTP account. Gmail no longer accepts your normal password for this, so you need an app password ([source](https://developer.nylas.com/docs/cookbook/email/gmail-smtp-settings.md)).

1. At myaccount.google.com/security, make sure **2-Step Verification** is on.
2. Open myaccount.google.com/apppasswords, name the app `home-ai-hub`, and create it.
3. Copy the 16-character code right away. Google shows it once, and changing your account password revokes it.
4. Write down: **SMTP\_USER** (your full Gmail address) and **SMTP\_PASSWORD** (the 16 characters, without spaces).

The settings for Gmail are host `smtp.gmail.com`, port `587` with STARTTLS, which is the default in the `.env` file. A personal Gmail account can send 500 messages a day, far more than you need.

Other providers: use the SMTP host and port from their help pages. If they say port `465` or "SSL", add `SMTP_SECURITY=ssl` to `.env` in Part 3. A throwaway mailbox just for the hub is safer than your main account.

### 2.3 FreshRSS (browser)

1. Settings > Authentication: tick **Allow API access** (the mobile-apps option) and submit.
2. Settings > Profile: set an **API password** (separate from your login password) and submit.
3. Open `http://FRESHRSS_ADDRESS/api/`. It should show a status page listing the Google Reader API as working (PASS).

Write down the base address you use to open FreshRSS (**FRESHRSS\_URL**, like `http://192.168.1.10:8080`), your username and the API password. If FreshRSS sits behind HTTPS, use the `https://` address.

The briefing reads the unread articles from the last 24 hours across all your feeds. A tidy set of feeds, with categories in FreshRSS, helps the ranking.

## Part 3: Server (Ubuntu)

After this part the server holds one Docker container with the web page, the briefing engine and the email sender. Commands run on the server over SSH unless it says otherwise.

### 3.1 Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in, then check `docker compose version`.

### 3.2 Copy the hub to the server

The code (about 1,400 lines: the web page, the engine and the Docker files) is in the file **ai-hub.tar.gz**, which I sent in our chat. Download it to the computer you SSH from. Use the latest copy I sent, which includes timezone support so that \`TZ\` works. If you can't find it, ask me and I'll send it again.

On that computer, in the folder where you saved it (replace `SERVER_USER` and `SERVER_IP`):

```bash
scp ai-hub.tar.gz SERVER_USER@SERVER_IP:~/
```

Then on the server:

```bash
cd ~
tar xzf ai-hub.tar.gz
mkdir -p ~/ai/data ~/ai/ssh
ls -a ~/ai ~/ai/hub
```

You should see `docker-compose.yml` and `.env.example` in `~/ai`, and `Dockerfile`, `engine.py`, `app.py` and `index.html` in `~/ai/hub`.

### 3.3 Your settings (.env)

```bash
cd ~/ai
cp .env.example .env
chmod 600 .env
nano .env
```

Edit every line. Write values without quotes.

| Setting | Value |
| --- | --- |
| `TZ` | Your timezone name, such as `America/New_York`. It decides when 07:00 is. |
| `HUB_PASSWORD` | A password for the web page, at least 8 characters. The hub refuses to start without it. |
| `PC_MAC`, `PC_IP`, `PC_SSH_USER` | From 1.2, 2.1 and your username on the PC |
| `FRESHRSS_URL`, `FRESHRSS_USER`, `FRESHRSS_API_PASSWORD` | From 2.3 |
| `SMTP_HOST`, `SMTP_PORT` | From 2.2 (Gmail: `smtp.gmail.com`, `587`) |
| `SMTP_USER`, `SMTP_PASSWORD` | From 2.2 |
| `MAIL_TO` | Where the briefing goes. You can change it later on the Customize tab. |
| `TEXT_MODEL`, `VISION_MODEL` | Names exactly as `ollama list` shows them on the PC |

Optional extras you can add as new lines:

- `SMTP_SECURITY=ssl` for port 465 providers (`none` for a local test server)
- `MAIL_FROM=` if the sender address differs from `SMTP_USER`
- `IDLE_OFF_MIN=5` is the minutes idle before the PC is powered off; `0` means never
- `PC_OFF_CMD=sudo systemctl suspend` suspends instead of powering off
- `LAN_BROADCAST=192.168.1.255` if wake-up packets don't reach the PC
- `WHISPER_MODEL=tiny`, `SUB_LANG=en`, `FRAMES=8`, `MAX_VIDEO_MIN=30` tune video handling
- `MAX_UPLOAD_MB=40` and `PORT=8088` for the web page

### 3.4 SSH key so the hub can power the PC off

The PC must be on and running `sshd` (Part 1) for this step. Use your PC's username and address.

```bash
ssh-keygen -t ed25519 -N "" -f ~/ai/ssh/id_ed25519
ssh-copy-id -i ~/ai/ssh/id_ed25519.pub PC_USER@PC_IP
ssh -i ~/ai/ssh/id_ed25519 PC_USER@PC_IP hostname
```

The last command should print the PC's hostname without asking for a password. The sudo rule from 1.6 limits what the hub can do as root (power off or suspend), but the key itself gives a normal login to your PC account, so anyone who copies it can log in as you. Keep `~/ai/ssh` private.

## Part 4: Start it and test each piece

Test wake-up by hand first, so a failure later points to the right place.

### 4.1 Test Wake-on-LAN by hand

On the PC, run `systemctl poweroff`. Wait 30 seconds, then on the server (use your PC's MAC):

```bash
sudo apt install -y wakeonlan
wakeonlan AA:BB:CC:DD:EE:FF
```

The PC should boot into Arch within about a minute. Then check Ollama from the server (use your PC's IP):

```bash
curl http://PC_IP:11434
```

It should answer `Ollama is running`. If either step fails, go to Troubleshooting before continuing.

### 4.2 Start the hub

```bash
cd ~/ai
docker compose up -d --build
docker compose logs -f
```

The first build takes a few minutes. Look for `hub started on port 8088`, then press Ctrl-C; the container keeps running and restarts after a reboot. If it exits instead, the log says which `.env` setting is missing.

### 4.3 First run in the browser

Open `http://SERVER_IP:8088` on any device at home and log in with `HUB_PASSWORD`.

1. **Customize tab:** enter your email address and click **Send test email**. Check your inbox and spam folder. Nothing else works by email until this does.
2. Still on Customize: set the **delivery time**, then look at the three starter focus areas (World, Technology and AI, Science and health). Rename, remove or add your own, with a plain-words description of what each covers. Click **Save**.
3. **Briefing tab:** click **Build a briefing now**. If the PC is off, the status line says it is waking it, then shows `Scoring story N of M`. My estimate is 5 to 15 minutes for a full day of articles, mostly the first model load and the scoring; your first run will tell you the real figure.
4. When the page appears, read it. If you like it, click **Email this briefing** and check the email.
5. **Ask tab:** choose **Recipe from a link, video or photo**, paste an article link, click **Run**. Then try a cooking video link, then a photo.
6. **System tab:** PC, email, recipients and FreshRSS should all read as configured.

### 4.4 What happens each morning

The hub starts preparing the briefing before the delivery time (45 minutes by default, set on Customize). It wakes the PC, reads the articles, ranks them, and emails the result at the delivery time. About 5 minutes after its last job, it powers the PC off, but only if the hub woke it.

If you start or restart the container during the preparation window or up to 3 hours after the delivery time, today's briefing is built and sent right away, once. A failed build shows in the History tab with the reason, and no email goes out.

## Part 5: Everyday use and making it do more

The hub is built around one idea: a **preset** is a saved instruction, and the AI applies it to whatever you give it (links, videos, PDFs, photos, text). Recipes and summaries are just presets, so any new use is a new preset, not new code.

### Tuning the briefing

| If the briefing is... | Change this on Customize |
| --- | --- |
| Too long or too trivial | Raise **Minimum importance** (6 or 7 is strict) or lower **Top stories shown** |
| Missing things you care about | Add a **focus area** with a clear description; focus sections accept stories 2 points below the minimum, or lower Minimum importance |
| Showing articles you already read | Leave **Include read articles** off, and mark articles read in FreshRSS as usual |
| Thin on a quiet day | Raise **Look back** to 48 hours |

Focus area ideas: "Local: my city, council decisions, transport, weather warnings", "Money: markets, interest rates, consumer prices", "Cooking: recipes, food science, restaurant news". The model decides which stories belong to a focus from your description, so be specific. Changing a focus re-scores stories, which makes the next build slower.

### The built-in presets (Ask tab)

| Preset | Give it | You get |
| --- | --- | --- |
| Recipe from a link, video or photo | An article link, a video link, or a photo | Title, ingredients (estimated quantities marked `~`), steps, notes |
| Summarize | Links, PDFs, text files or pasted text | A 2-sentence overview, 5 key points and anything to act on |
| Explain an image or screenshot | A photo or screenshot | What it shows, with technical parts explained and text transcribed |
| Meal plan and shopping list | Notes: what you have, diet, time limits | A 5-day dinner plan and a shopping list by aisle |
| Ask anything | A question, optionally with sources | A direct answer that says so when unsure |

### Making your own presets

Open the Presets tab and write instructions in plain words, for example:

- **Action items:** "List every task, owner and deadline mentioned in the sources, one per line. Say if something is unclear."
- **Explain simply:** "Explain the sources to someone new to the subject, using short sentences and one example."
- **Translate:** "Translate the sources into Spanish. Keep names and numbers unchanged."

Tick **Use the vision model** for presets that work on photos or video frames. Any request that includes an image uses the vision model automatically.

### Scheduled tasks

On the Scheduled tab pick a preset, days and a time, and the result is emailed to you. Useful examples: a Monday 08:00 meal plan (put your diet and time limits in the notes); a Friday summary of links you list in the task. Each task runs once a day at most, within 3 hours after its time.

### Limits to know about

- Each source is cut to about 16,000 characters, and PDFs to their first 40 pages, so very long documents are only partly read.
- Video quantities are often spoken vaguely, so check the `~` estimates in recipes.
- The model is an 8B model on a 12 GB card. It is good at summaries and sorting, and weaker at fine factual judgement, so for anything important follow the link in the briefing.
- Scanned PDFs without text won't read; send the page as a photo instead.
- To reach the hub away from home, use a VPN such as WireGuard or Tailscale to your home network. Don't forward the port on your router.

## Troubleshooting and day-to-day notes

Most failures are in the wake-up chain, the email account or the GPU. Check the log first: `docker compose logs -f` on the server, `journalctl -u ollama -e` on the PC. Failed jobs also show their reason on the History tab.

| Symptom | Likely cause and fix |
| --- | --- |
| The container exits right away | The log names the problem. Usually `HUB_PASSWORD` is missing or under 8 characters. |
| Can't open the web page | Check the container is running with `docker ps`, the address is `http://SERVER_IP:8088`, and the server's firewall allows port 8088 from your home network. |
| Test email fails | Gmail: you need an app password with 2-Step Verification on, not your normal password. Check `SMTP_USER`, `SMTP_PASSWORD` and `SMTP_PORT`. Port 465 needs `SMTP_SECURITY=ssl`. The error text appears in the page. |
| Briefing never arrives | System tab: is email configured, and is a recipient set? Check History for a failed build. A briefing that built but didn't send shows "Email failed" with the reason; fix the cause and click **Email this briefing**. |
| "FreshRSS login failed" | Use the API password from 2.3, not your login password, and open `FRESHRSS_URL/api/` to confirm the API is on. |
| Briefing says "No articles in the window" | Nothing unread arrived in the look-back window. Raise **Look back**, or tick **Include read**. |
| PC never wakes | Recheck ErP (Disabled) and Wake on LAN (Enabled) in BIOS, a cable in the motherboard port, and `sudo ethtool INTERFACE \| grep Wake-on` showing `g` after a reboot. Try adding `LAN_BROADCAST=192.168.1.255` (your subnet's broadcast address) to `.env`, then `docker compose up -d`. |
| Log says the PC did not come up | The PC woke but Ollama is unreachable. Check `systemctl status ollama` on the PC, the `OLLAMA_HOST=0.0.0.0` override, the firewall on port 11434, and that GRUB did not boot Windows. |
| `ollama ps` shows CPU, not GPU | Read `journalctl -u ollama -e` on the PC. If it lists no GPU, an AMD integrated GPU on your CPU may be confusing ROCm; the Arch wiki on Ollama covers GPU selection overrides. Send me the log lines if you get stuck. |
| YouTube videos fail or return no formats | yt-dlp now wants a JavaScript runtime such as Deno for full YouTube support ([source](https://github.com/yt-dlp/yt-dlp/issues/15012)). Add Deno to `~/ai/hub/Dockerfile` (untested here), then rebuild: `RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && curl -fsSL https://deno.land/install.sh \| DENO_INSTALL=/usr/local sh`. |
| Instagram or TikTok links fail | These often need a logged-in session, which this setup does not do. Send a screenshot or the recipe page link instead. |
| Transcription is very slow | No captions means Whisper runs on the server CPU. Add `WHISPER_MODEL=tiny` to `.env` for speed, or accept the wait. If transcription fails, the hub falls back to the video's title, description and frames. |
| Vision job fails or the PC runs out of memory | Lower `FRAMES` in `.env` to 4. |

**Day to day**

- After editing `.env`: `cd ~/ai && docker compose up -d`.
- After changing code or the Dockerfile: `docker compose up -d --build`.
- Your settings, history and scores live in `~/ai/data`. Back that folder up, along with `.env`.
- The hub powers off only a PC it woke itself. If you start using the PC mid-job, set `IDLE_OFF_MIN=0` to stop automatic power-off, or use the Power off button on the System tab when you're done.
- Suspend is faster to wake than a full boot, but Ollama can lose the GPU after resume. That is why the default is power off. If you switch to suspend and the GPU disappears, run `sudo systemctl restart ollama` on the PC.
- Keep `.env` private: it holds your web page password, FreshRSS password and email password.
- The web page uses plain HTTP and one shared password, which is fine on a home network and not for the internet.
