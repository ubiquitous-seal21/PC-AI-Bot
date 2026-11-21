# PC-AI-Bot handoff

## Current objective

The AI dashboard is running on the Ubuntu server, but Caddy cannot reach it through the existing Docker/VPN layout. The next task is to diagnose and repair only the Caddy-to-`ai-hub` route without changing or removing the existing VPN routing.

## Current confirmed state

- Ubuntu server hostname: `kalenas`
- Server LAN address used in the latest checks: `192.168.4.58`
- Arch gaming PC hostname: `archlinux`
- Arch PC LAN address: `192.168.4.86`
- Arch PC wired interface: `enp5s0`
- Arch PC wired MAC: `10:ff:e0:af:0f:06`
- Ollama is running on the Arch PC and listening on port `11434`.
- Ollama reports the AMD Radeon RX 7700 XT with 12 GB VRAM.
- Ollama accepted a direct container request after port `11434` was allowed through the Arch PC firewall.
- The Arch PC has a model named `qwen2.5:14b-instruct-q4_K_M`.
- The former text model name `qwen2.5:14b-instruct-q4_K-M` does not exist on the PC.
- The Docker `ai-hub` container is running and listens on port `8088`.
- Direct host access to `ai-hub` returned `HTTP/1.1 200 OK`.
- Caddy routes for the Tailscale and public AI hostnames exist.
- Caddy has not yet reached `ai-hub`; the browser receives `502 Bad Gateway`.

## Current Docker state

Confirmed:

```text
ai-hub network_mode=host
ai-hub container network membership={"host": ...}
ai-hub is absent from caddy_net
```

Confirmed Caddy state:

```text
Caddy container: caddy-proxy-caddy-1
Caddy network mode: container:<gluetun-ingress-container-id>
Caddy network membership: {}
```

The Caddy container shares the `gluetun-ingress` container network namespace.

The Caddy container is not on `caddy_net` in the inspected state. `gluetun-ingress` is on `caddy_net`, but Caddy itself is not.

`caddy_net` contains the existing application containers, including:

- `freshrss`
- `jellyfin`
- `seerr`
- `gitea-server-1`
- `vikunja-vikunja-1`
- `uptime-kuma`
- `nextcloud-app-1`
- `immich_server`
- `vaultwarden`
- `navidrome`
- `authelia`
- `trillium-trilium-1`

## Caddy routes currently intended for the AI hub

Tailscale route:

```caddy
kalenas.newt-crocodile.ts.net:18088 {
	tls /certs/kalenas.newt-crocodile.ts.net.crt /certs/kalenas.newt-crocodile.ts.net.key
	reverse_proxy ai-ai-hub:8088 {
		transport http {
			resolvers 127.0.0.11
		}
	}
}
```

Public DuckDNS route:

```caddy
ai.kalenas.duckdns.org {
	import security
	import authelia
	reverse_proxy ai-ai-hub:8088 {
		transport http {
			resolvers 127.0.0.11
		}
	}
}
```

The container name `ai-ai-hub` has not been confirmed to resolve. Docker inspection shows `ai-hub` is only on the `host` network and is absent from `caddy_net`.

## Reachability tests already run

From Caddy:

```text
127.0.0.1:8088 -> connection refused
host.docker.internal:8088 -> bad address / unresolved
172.17.0.1:8088 -> timed out
192.168.4.58:8088 -> timed out
```

Direct host access from Ubuntu:

```text
127.0.0.1:8088 -> 200 OK
```

The Caddy container showed no route for `ai-hub`.

## AI Compose file current state

The current `~/ai/docker-compose.yml` was shown as:

```yaml
services:
  ai-hub:
    build: ./ai/hub
    container_name: ai-hub
    restart: unless-stopped
    network_mode: host
    env_file: .env
    volumes:
      - ./data:/data
      - ./ssh:/ssh:ro
    networks:
      - caddy_net

networks:
  caddy_net:
    external: true
```

The `network_mode: host` and `networks: caddy_net` combination is not active for `ai-hub` as a Docker bridge network; inspection showed only the `host` network.

## Security state

Credentials previously appeared in conversation output and should be treated as compromised:

- Hub password
- FreshRSS API password
- Gmail app password

They should be rotated before normal use.

Do not copy `.env` credentials into this handoff or any future handoff.

## Not yet completed

- Caddy-to-`ai-hub` routing
- Caddy route reload and 502 removal
- End-to-end dashboard login through Caddy
- Model inference through the dashboard
- Email test
- FreshRSS briefing test
- SSH and desktop-session shutdown-safety test
- Final automatic PC shutdown enablement

## Preserve while fixing

- Keep the existing VPN/Gluetun route intact.
- Do not remove Caddy’s VPN namespace unless the VPN behavior is separately and intentionally redesigned.
- Do not change Ollama listener settings unless a new test proves they are still wrong.
- Keep `IDLE_OFF_MIN=0` until SSH and desktop-session detection are verified.
- Keep automatic PC shutdown disabled.
- Preserve Caddy certificates and existing routes for other services.

## Next AI: first actions

Do not assume that adding Caddy to `caddy_net` or changing `ai-hub` to bridge networking is safe without tracing the current Compose layout.

1. Locate the Compose project that owns `caddy-proxy-caddy-1`.
2. Confirm that project’s exact Caddy service and whether Caddy intentionally shares `gluetun-ingress`.
3. Confirm whether `gluetun-ingress` already exposes the host route or DNS bridge needed for Caddy to reach host port `8088`.
4. Trace routes and firewall rules from Caddy’s namespace to `192.168.4.58:8088`.
5. Give `ai-hub` a route reachable from Caddy without changing the VPN containers.
6. Reload Caddy only after the upstream returns `200 OK`.
7. Test both AI hostnames and confirm Caddy logs contain no `502`.
