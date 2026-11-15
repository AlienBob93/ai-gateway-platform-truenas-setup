# Local deployment record

Placeholders used throughout (real values live only in the local `.env`):

| Placeholder | Meaning |
|---|---|
| `<nas-ip>` | the TrueNAS host on the LAN |
| `<proxy-ip>` | the second host running Caddy |
| `<tailscale-ip>` | the NAS's tailscale IP (100.x) |
| `<tailnet-name>` | the tailnet's DNS suffix |
| `<pool>` | the TrueNAS pool holding app data |

## Topology

- **NAS** (TrueNAS 25.04.2.6): Ollama (ix app), OpenWebUI (ix app), vane (ix app), Tailscale
  (ix app), and the `ai-gateway-platform` ix app (gateway + prometheus + jaeger + grafana).
- **Proxy host**: Caddy (Docker, DNS-01 via Cloudflare), HomeAssistant (Docker), the GitHub
  Actions self-hosted runner (`ai-review` label), and the `ai-gateway-platform-registry`
  container. Also on the tailnet.

## Chosen names and paths

- App name: `ai-gateway-platform`; registry container: `ai-gateway-platform-registry`
  (image path stays `registry.example.org/aigw:local` — the registry's repo name).
- Dataset: `Application_Pool/aigw` (gateway policy, prometheus config, Grafana provisioning).
- Ports: gateway `8080`, Grafana `3000` on the NAS; Ollama's ix-app port is auto-assigned
  (`30068` here); OpenWebUI's too (`31028` here).
- Dashboard: `https://aigw.example.org` via Caddy, plus a named Tailscale Service
  `ai-gateway-dashboard.<tailnet-name>.ts.net` (serve `--https=ai-gateway-dashboard`, target
  `http://<nas-ip>:3000`; Caddy on the proxy host also fronts that name for cert-consistent
  access — the ts.net cert comes from the local tailscaled).
- Registry: `https://registry.example.org` → the container (pulls anonymous via Caddy's
  injected credentials; pushes need basic auth via `localhost:5000` from the proxy host).

## Teams and consumers

| Team | Consumer | Models | Rate limit |
|---|---|---|---|
| openwebui | OpenWebUI | qwen3.8-27b, gemma4-12b | 60 rpm |
| vane | vane (search agent) | both | 30 rpm |
| homeassistant | HomeAssistant voice | gemma4-12b only | 20 rpm |
| pr-review | GitHub PR reviews | qwen3.8-27b | 10 rpm |

## Public exposure (verified 2026-11-14)

- `homeai.example.org` (proxied cloudflared tunnel) is **OpenWebUI** — intentionally public,
  authenticated natively via Pocket ID (OpenWebUI OIDC). Verify signups stay closed
  (OpenWebUI admin panel: "Enable New Sign Ups" off; `ENABLE_OAUTH_SIGNUP` default false).
- All Caddy LAN routes for the self-hosted apps (casaos, homeassistant, uptime, jellyfin,
  comfy, code, searxng, arm) were disabled — those apps are tailscale-only via Services.
- `*.example.org` wildcard is a dns-only A record to the proxy host: Caddy-served names are
  unreachable from the internet. The genuinely public surface is the tunnel CNAMEs only,
  governed in the Cloudflare Zero Trust dashboard (token-managed tunnel).
- The proxy host (.46) is a tagged Service host running 8 services: arm, casaos, code, comfy,
  dockhand, homeassistant, jellyfin, local-search (= searxng). Access from tailnet client
  devices (e.g. the user's PC) works; note the two service-HOST nodes themselves (the tagged
  NAS and the proxy host) cannot resolve service DNS from their own resolvers — an ACL/grants
  visibility quirk that does not affect real clients.
- Still missing a Service: **uptime** (uptime-kuma on .46:3001) — define `svc:uptime` in the
  admin console and serve it on .46.

## Wiring details worth knowing

- **OpenWebUI**: the gateway was added as a third OpenAI connection (prefix `AIGW`) by
  editing the `config` table of `webui.db` with the app stopped (full DB backed up first).
  OpenWebUI uses Ollama for RAG embeddings (`embeddinggemma:300m`) — that path stays direct
  because the gateway does not serve embeddings; the Ollama hostname there must track the
  tailscale IP like the gateway policy does.
- **vane**: an `openai`-type model provider was added to `/mnt/<pool>/vane-search/config.json`
  (app stopped). The stored provider `hash` is stale-tolerant; vane recomputes it.
- **HomeAssistant**: HA 2026.9's built-in `openai_conversation` has no custom base URL, so
  `extended_openai_conversation` 3.0.0-beta12 was installed into `custom_components/` and a
  config entry added via `.storage/core.config_entries` (HA stopped). The Assist pipeline
  was renamed "AI Gateway" and its conversation agent pointed at
  `conversation.ai_gateway_conversation`; the direct `ollama` config entry was deleted.
  Stale entity-registry rows prune themselves.
- **GitHub action**: the private `<github-user>/ai-review` repo's composite action had moved
  past the local copy on the proxy host (a guidelines feature). The patch was applied against
  current `main` — when patching local forks, always diff against the repo first.

## Enforcement details

- Ollama's port binding was changed from `0.0.0.0` to the tailscale IP via
  `midclt call app.update ollama '{custom_compose_config_string: ...}'` with the port entry
  gaining `"host_ip": "<tailscale-ip>"`. Verified: LAN unreachable, tailscale IP answers.
- The gateway policy `base_url`s use `http://<tailscale-ip>:30068/v1`; the policy change
  needs a gateway app restart (policy is read at startup).
- Residual: tailnet members can still reach Ollama; a host firewall rule would close it.

## MQTT metrics in HA (2026-11-14 update)

A global token sensor was added: `sensor.ai_gateway_tokens_total` (topic
`aigw/tokens_total/global`, from `sum(aigw_tokens_total)`).

Prometheus subtlety learned: `prometheus_client` does not export a labeled series until its
first `.labels()` call, and the gateway counters reset when its container is recreated — so
per-team and global sensor topics vanish (retained 0s / unknowns) after a gateway restart
until the next request. Expected behavior, documented in the README.

## Backups taken during setup

- OpenWebUI DB: `/mnt/<pool>/aigw/backup-openwebui/`
- vane config: `/tmp/vane-config-backup.json` on the NAS
- HA config entries: `.storage/core.config_entries.bak-aigw` on the proxy host
- mosquitto passwd: `passwd.bak-aigw` in the mosquitto config dir on the proxy host

## MQTT to Home Assistant (2026-11-14)

The `mqtt-publisher` sidecar was added to the ix app (image
`registry.example.org/aigw-mqtt-publisher:local`, script mounted read-only from the dataset
`/mnt/<pool>/aigw/mqtt/publisher.py`, logs to `/mnt/<pool>/aigw/mqtt/logs/publisher.log`).

- Two broker users were created: `aigw` (publisher) and `homeassistant` (HA's MQTT
  integration was moved off the shared old account onto its own user — this fixed discovery:
  the 30 `sensor.ai_gateway_*` entities appeared immediately after the switch).
- Gotchas hit: mosquitto's config mount is read-only (run `mosquitto_passwd` via a separate
  container mounting it rw); `mosquitto_passwd -c` wipes existing users (merge from backup);
  mosquitto restarts drop retained messages (no persistence configured — publisher re-announces
  each cycle); HA's entity registry file on disk lags reality by minutes — don't trust it for
  "is discovery working" checks; use the broker (`mosquitto_sub`) instead.
- The publisher's own container logs aren't reachable without root — that's why it writes
  `/mnt/<pool>/aigw/mqtt/logs/publisher.log`.
