# AI Gateway on TrueNAS (.132) — setup guide

Deploys the [ai-gateway-platform](../ai-gateway-platform) stack in front of Ollama as **one
TrueNAS 25.04 (Fangtooth) ix app** managed from the GUI, like tailscale, vane, and ollama
already are. The stack is only the services the repo provides: gateway, Prometheus, Jaeger,
Grafana (the repo's MCP demo server is left out — the policy has no MCP servers).

Only two ports are published on the host; everything else stays inside the app's compose
network, and no other observability services exist on .132 or .46:

```
published:  <nas-ip>:8080   gateway  (clients: OpenWebUI, vane, HomeAssistant, GH workflows)
published:  <nas-ip>:3000   grafana  (the dashboard — the only observability surface)
internal:   gateway -> prometheus (scrape), gateway -> jaeger (traces), grafana -> both
```

```
Tailnet (WireGuard)
  humans ──► https://<aigw132>.<tailnet>.ts.net      (tailscale serve on .132, Step 5)
                        │
Browser/LAN ──► https://aigw.example.org ──► Caddy (.46) ──► <nas-ip>:3000
                        │
HomeAssistant (.46) ──► http://<nas-ip>:8080 ──► gateway ──► ollama :30068 (ix app "ollama" on .132)
GH workflows        ──► gateway (self-hosted runner on .46, over the tailnet)
OpenWebUI, vane (.132) ──► gateway
```

## Final state (2026-11-14)

Everything below is deployed and verified:

- **`ai-gateway-platform` ix app RUNNING** on .132 with all four services (gateway, grafana,
  prometheus, jaeger), installed via `midclt app.custom.create`. Verified end to end: team key →
  `/v1/chat/completions` → Ollama → response, on both models.
- **Dashboard**: `https://aigw.example.org` via Caddy on .46 (cert issued, HTTP 200), plus LAN
  `:3000` on .132, plus the tailnet via `tailscale serve`.
- **All four consumers routed through the gateway**: OpenWebUI (OpenAI connection "AIGW"), vane
  (ai-gateway provider), HomeAssistant (`extended_openai_conversation` agent selected in the
  Assist pipeline), and the GitHub PR-review action (pushed + `AIGW_KEY` secret set).
- **Enforcement**: Ollama publishes only on the host's tailscale IP — LAN clients cannot reach it
  directly; the gateway is the only path for chat. OpenWebUI's direct Ollama connection and vane's
  direct provider were removed; HA's pipeline was switched to the gateway agent and its direct
  ollama entry deleted.
- **Team keys are live** (values in the local `.env`, hashes in the policy), Grafana admin
  password set at install (also in `.env` and the app's compose in the GUI).

What the public version of this repo contains: placeholders instead of your domain, tailnet, IP,
usernames, and passwords. Keep `.env` out of any published copy (gitignored here).

## Step 1 — dataset and files

One dataset path (adjust `tank` to your pool):

```bash
cd ~/vibes/ai-gateway-platform-truenas-setup
./scripts/prepare-dataset.sh /mnt/Application_Pool/aigw
```

This copies `prometheus.yml`, `rules.yaml`, and the Grafana provisioning + dashboard into
`/mnt/Application_Pool/aigw/`, and `policies/gateway.home.yaml` as `gateway.yaml` (after Step 2).
The app mounts everything read-only, so config survives app upgrades.

## Step 2 — team keys and the policy

One key per consumer, so each gets its own rate limit and audit attribution:

```bash
cd ~/vibes/ai-gateway-platform/gateway && make install          # once; creates .venv
for team in openwebui vane homeassistant pr-review; do
  .venv/bin/python -m aigw.keys $team
done
```

Each run prints the key (give to the consumer; store in its own secret store) and a
`key_sha256` (paste into `policies/gateway.home.yaml`, replacing the `<sha256:team>`
placeholders). Also set `upstream_model` there to your real Ollama tag (`ollama list`),
one model block per Ollama model you want to expose. Then validate and deploy:

```bash
.venv/bin/python -m aigw.check ../ai-gateway-platform-truenas-setup/policies/gateway.home.yaml
./scripts/prepare-dataset.sh /mnt/Application_Pool/aigw      # re-run: now copies the policy too
```

## Step 3 — build the image

```bash
cd ~/vibes/ai-gateway-platform/gateway
docker build -t ai-gateway:local .
```

TrueNAS apps run on the host's Docker engine, so `ai-gateway:local` is visible to the app.

## Step 4 — install the ix app

TrueNAS GUI → Apps → Discover Apps → ⋮ → **Install via YAML**, paste
[`compose/ai-gateway-platform.yaml`](compose/ai-gateway-platform.yaml). Before installing, edit in the YAML:

- `GF_SECURITY_ADMIN_PASSWORD` — a real password
- `GF_SERVER_ROOT_URL` — `https://aigw.example.org`

Set the app's **web portal port** to `3000` in the GUI form.

Verify:

```bash
curl -s http://<nas-ip>:8080/healthz
curl -s http://<nas-ip>:8080/v1/chat/completions \
  -H "Authorization: Bearer <openwebui-key>" -H "Content-Type: application/json" \
  -d '{"model": "gemma4-12b", "messages": [{"role": "user", "content": "hello"}]}'
```

Then `http://<nas-ip>:3000`: the "AI Gateway" dashboard should show the request after a
few seconds (prometheus scrapes the gateway inside the app; no host port involved).
Prometheus's and Jaeger's own UIs have no published ports by design — Grafana is the
only way in, which is the point.

## Step 5 — the tailscale service for the dashboard

One-time, in the Tailscale app's shell (Apps → Tailscale → Shell) or via SSH on .132:

```bash
docker exec -it ix-tailscale-<suffix>-tailscale-1 \
  tailscale serve --bg http://<nas-ip>:3000
```

The serve config persists in the app's state. This creates the tailnet service: the
dashboard is reachable only inside the tailnet at
`https://<aigw132-hostname>.<tailnet-name>.ts.net` (the tailnet .46 is on), with an
automatic Tailscale certificate. The machine-name placeholder becomes known when the
service is created; adjust the ts.net Caddy block (see below) to match.

Optionally, Caddy on .46 can also serve the ts.net name (its Caddyfile already sketches
this for other services — it fetches .ts.net certs from the local tailscaled):

```
aigw.<tailnet-name>.ts.net { reverse_proxy <nas-ip>:3000 }
```

## Step 6 — connect it to Caddy on .46

Add the block from [`caddy/grafana.caddy`](caddy/grafana.caddy) to .46's Caddyfile
(adjust the domain), reload Caddy:

```
aigw.example.org {
    reverse_proxy <nas-ip>:3000
}
```

The dashboard is now on your existing HTTPS domain alongside your other services, while
the Caddy → Grafana hop stays on the home LAN. `GF_SERVER_ROOT_URL` (Step 4) makes
Grafana's links come out right.

## Enforcement (2026-11-14): Ollama is LAN-blocked

All four consumers now route through the gateway, and the direct bypass is closed:

- **Ollama's published port is bound to the host's tailscale IP only** (`<tailscale-ip>:30068`).
  LAN clients (`.46`, any LAN machine) can no longer reach Ollama directly — verified: `<nas-ip>:30068` is
  unreachable, the tailscale IP answers. The gateway, OpenWebUI, and vane run in containers on .132 and
  reach it via host routing.
- **OpenWebUI**: the direct Ollama chat connection is disabled (`ollama.enable=false`, empty URLs).
  Note: RAG embeddings still use Ollama (`embeddinggemma:300m` via the tailscale IP) — the gateway does not
  serve embeddings, so that path stays by design.
- **vane**: the `direct-Ollama` provider is removed; only Transformers + ai-gateway remain.
- **HomeAssistant**: the preferred Assist pipeline (renamed "AI Gateway") now uses
  `conversation.ai_gateway_conversation`; the direct `ollama` config entry is removed. Zero errors in HA logs.
- Residual: tailnet members (e.g. HA on .46) could still reach Ollama via the tailscale IP. Closing that
  fully needs a host firewall rule (root) — optional, not applied.

If the tailscale IP ever changes, update three places: the ollama app's port binding, the gateway policy
`base_url`s (dataset `gateway.yaml`), and OpenWebUI's `rag.ollama.base_url`.

## Step 7 — point the consumers at the gateway

Status after the 2026-11-14 deployment: **all four consumers are wired up** except the GitHub
push, which needs a token (the repo is private). Keys are in the local `.env`.

| Consumer | Status | Change made |
|---|---|---|
| **vane** (.132) | ✅ live | Added an `openai`-type model provider "ai-gateway" to its `config.json` (`/mnt/Application_Pool/vane-search/`, edited with the app stopped, backup in `/tmp/vane-config-backup.json`), app restarted. Pick the ai-gateway provider in vane's UI when chatting. |
| **OpenWebUI** (.132) | ✅ live | Added a third OpenAI connection (prefix `AIGW`, base `http://<nas-ip>:8080/v1`, openwebui key, models qwen3.8-27b + gemma4-12b) to its DB (`webui.db` config table, edited with the app stopped; backup in `/mnt/Application_Pool/aigw/backup-openwebui/`). Direct Ollama connection left intact. |
| **HomeAssistant** (.46) | ✅ live | HA 2026.9's official `openai_conversation` has no custom base URL, so the `extended_openai_conversation` custom component (3.0.0-beta12) was installed into `/DATA/AppData/homeassistant/config/custom_components/` and a config entry "AI Gateway" added (conversation agent `conversation.ai_gateway_conversation`, model gemma4-12b). Loaded with zero errors. Backup: `.storage/core.config_entries.bak-aigw`. Select it in Settings → Devices → Assistants as the pipeline agent if wanted. |
| **GH workflows** | ✅ live | Pushed to the private repo `<github-user>/ai-review` (2026-11-14): `pr_review.py` now calls the gateway's OpenAI API with the `AIGW_KEY` Actions secret (set in the repo), defaults point at `http://<nas-ip>:8080`, model `qwen3.8-27b`. The repo's newer guidelines feature was preserved (re-patched against current main, not the stale local copy). The old Ollama version is kept in-repo as `pr_review.py.ollama-orig`. Patched copies also live in `consumers/`. |

### GitHub side — done (2026-11-14)

The commit "Route PR reviews through the AI gateway instead of direct Ollama" is on `main` of
`<github-user>/ai-review`, and the `AIGW_KEY` Actions secret is set. No further steps needed.

## Maintenance

- **Policy change:** edit `policies/gateway.home.yaml` here, run `aigw.check`, re-run
  `prepare-dataset.sh`, restart the app from the GUI.
- **App upgrades:** the GUI pulls new image tags; the dataset keeps all config.
- **Key rotation:** generate a new key, add the new hash, restart; remove the old hash
  on a later restart after consumers have switched.
- **Budgets:** intentionally absent — Ollama is free, so budgets never trigger; rate
  limits are the control. Adding a paid provider (a `price` block) activates them.
- **Audit log:** gateway stdout, one JSON line per request (metadata only, never
  prompts). Per-team spend: `gateway/.venv/bin/python -m aigw.report < docker logs ...`.

## Design notes

- **One ix app, four services** (gateway, prometheus, jaeger, grafana): one GUI entry,
  one restart button, and the observability wiring (scrape, traces, datasources) never
  touches the host network.
- **Dashboard-only observability:** prometheus and jaeger publish no ports at all —
  Grafana queries them by compose service name. Grafana is the single observability
  surface, on LAN (3000) and tailnet (tailscale serve), and via your HTTPS domain (Caddy).
- **No Vault, no SSO, no Redis:** Ollama needs no credentials, humans use keys not
  sign-in, and a single gateway replica keeps limits in-process. All are optional later;
  the policy format is the same, so migration is additive.
- **Four teams** (openwebui, vane, homeassistant, pr-review): separate keys, separate
  rate limits, one audit line per request naming the consumer.
