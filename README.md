# AI Gateway Platform on TrueNAS + Tailscale + Caddy

A setup guide for deploying the [ai-gateway-platform](https://github.com/csraovaradev/ai-gateway-platform)
stack on a home network that already runs **TrueNAS (25.04+, ix apps)**, **Tailscale**, and a
**Caddy reverse proxy** with DNS-01 certificates.

Result: one GUI-managed ix app on the NAS containing the gateway plus Prometheus, Jaeger and
Grafana, fronting Ollama — with every consumer (chat UIs, voice assistants, CI workflows) routed
through a single governed endpoint, per-consumer rate limits, and a full audit trail. A separate
page records the [specifics of the deployment this guide was written from](local-deployment.md).

```
                        Tailscale (WireGuard)
  humans ──► https://<dashboard-hostname>.<tailnet>.ts.net     (tailscale serve on the NAS)
                        │
  Browser/LAN ──► https://dashboard.example.org ──► Caddy ──► NAS:3000 (Grafana)
                        │
  consumers ──► http://<nas-ip>:8080 ──► gateway ──► Ollama (bound to the tailscale IP only)
```

## What you need

- TrueNAS 25.04 (Fangtooth) or newer, with Tailscale already running as an ix app
- A second host running Caddy with valid certificates (DNS-01 via Cloudflare is what the
  example blocks assume; any ACME method works)
- Ollama on the NAS, reachable on the host (its ix app publishes an auto-assigned port,
  e.g. `30068` — find it with `midclt call app.get_instance ollama`)
- The gateway image, built from the repo and pushed to a private registry behind Caddy
  (see Step 3 — the repo's CI builds but never publishes the image)

## Step 1 — dataset and config files

One ZFS dataset for config the app mounts read-only (survives app upgrades):

```bash
./scripts/prepare-dataset.sh /mnt/<pool>/aigw
```

This copies `prometheus.yml`, `rules.yaml` (SLO + burn-rate alerts), and the Grafana
provisioning/dashboard from the repo checkout into the dataset. Run it again after any
config change, then restart the app.

## Step 2 — team keys and the policy

One team per consumer — each gets its own key, rate limit, and audit attribution:

```bash
cd ai-gateway-platform/gateway && make install     # once; creates .venv
for team in chat search voice ci; do .venv/bin/python -m aigw.keys $team; done
```

Each run prints the key (give to the consumer) and a `key_sha256` (paste into
`policies/gateway.home.yaml`). Set one model block per Ollama model you expose — the
client-facing name is yours, `upstream_model` is the Ollama tag (`ollama list`). Validate
and deploy:

```bash
.venv/bin/python -m aigw.check ../ai-gateway-platform-truenas-setup/policies/gateway.home.yaml
./scripts/prepare-dataset.sh /mnt/<pool>/aigw     # copies the policy as gateway.yaml
```

Store the key values in a local `.env` (gitignored) — the gateway keeps only hashes.

## Step 3 — build and publish the gateway image

Build on any host with Docker, push to a private registry, let the TrueNAS daemon pull
anonymously over TLS:

```bash
git clone https://github.com/csraovaradev/ai-gateway-platform && cd ai-gateway-platform/gateway
docker build -t ai-gateway:local .

docker run -d --name aigw-registry --restart unless-stopped \
  -p 127.0.0.1:5000:5000 -v ~/registry/auth:/auth:ro \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=registry \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd registry:2
```

The full Caddy route for the registry is in [`caddy/registry.caddy`](caddy/registry.caddy):
**pulls anonymous** (Caddy injects the registry's own credentials server-side, so the NAS
needs no `docker login`), **pushes require basic auth**. Push locally via
`localhost:5000`, create the DNS record, and the image is pullable at
`https://registry.example.org/aigw:local`.

## Step 4 — install the ix app

TrueNAS GUI → Apps → Discover Apps → ⋮ → **Install via YAML**, paste
[`compose/ai-gateway-platform.yaml`](compose/ai-gateway-platform.yaml). It contains:

- **gateway** — the ai-gateway-platform container; publishes `8080` (clients); policy mounted
  from the dataset; pushes traces to `jaeger:4318` inside the compose network
- **prometheus** — scrapes `gateway:8080` internally; no published ports
- **jaeger** — receives traces internally; no published ports
- **grafana** — publishes `3000` (the dashboard — the only observability surface); queries
  the other two by service name

Before installing, set `GF_SECURITY_ADMIN_PASSWORD` and `GF_SERVER_ROOT_URL`, and set the
app's web portal port to `3000` in the GUI.

Verify:

```bash
curl http://<nas-ip>:8080/healthz
curl http://<nas-ip>:8080/v1/chat/completions \
  -H "Authorization: Bearer <chat-key>" -H "Content-Type: application/json" \
  -d '{"model": "main", "messages": [{"role": "user", "content": "hello"}]}'
```

## Step 5 — publish the dashboard on the tailnet

One-time, in the Tailscale app's shell (Apps → Tailscale → Shell) or via SSH on the NAS:

```bash
sudo docker exec -it <tailscale-container> tailscale serve --bg http://<nas-ip>:3000
```

The serve config persists in the app's state. The dashboard is then reachable only inside
the tailnet at `https://<hostname>.<tailnet>.ts.net` with an automatic certificate.

## Step 6 — dashboard via Caddy

Add the block from [`caddy/grafana.caddy`](caddy/grafana.caddy) to the proxy host's
Caddyfile and reload:

```caddyfile
dashboard.example.org {
    reverse_proxy <nas-ip>:3000
}
```

Only Grafana is proxied — Prometheus and Jaeger have no published ports at all, so there is
nothing else to reach. Set `GF_SERVER_ROOT_URL` (Step 4) so Grafana's links come out right.

## Step 7 — point the consumers at the gateway

| Consumer | Change |
|---|---|
| Any OpenAI-compatible client | base URL `http://<nas-ip>:8080/v1`, its team key |
| **OpenWebUI** | Admin → Settings → Connections → OpenAI API: add the gateway URL + key. Leave the direct Ollama connection off (see enforcement). |
| **HomeAssistant** | The official `openai_conversation` integration hardcodes OpenAI's endpoint in HA ≤ 2026.9 — install [`extended_openai_conversation`](https://github.com/jekalmin/extended_openai_conversation) (check the version against your HA release) and configure the gateway URL + key. |
| **GitHub PR reviews** | A stdlib-only review script + composite action that call the gateway are in [`consumers/`](consumers/); store the CI team key as an Actions secret. |

## Enforcement: Ollama behind the gateway

Removing client-side connections changes defaults, not possibilities — any client could
point back at Ollama directly. To close that, bind Ollama's published port to the host's
**tailscale IP** only (containers on the NAS still reach it via host routing; the gateway
policy's `base_url` uses the same address):

```bash
midclt call app.update ollama "$(jq -n --arg c "$(midclt call app.config ollama \
  | python3 -c 'import json,sys; c=json.load(sys.stdin); c["services"]["ollama"]["ports"][0]["host_ip"]="<tailscale-ip>"; print(json.dumps(c))')" \
  '{custom_compose_config_string: $c}')"
```

Afterwards the LAN cannot reach Ollama at all. Residual: tailnet members can — closing that
fully needs a host firewall rule. Three places reference this address: the Ollama app's port
binding, the gateway policy `base_url`s, and any client's RAG/embedding config
(the gateway does not serve embeddings — keep a direct path for those).

## Maintenance

- **Policy change:** edit the policy, run `aigw.check`, re-run `prepare-dataset.sh`, restart the app.
- **App upgrades:** the GUI pulls new image tags; dataset mounts keep all config.
- **Image update:** rebuild, push to the registry, restart the gateway from the GUI.
- **Key rotation:** generate a new key, add its hash, restart; remove the old hash later.
- **Audit log:** gateway stdout, one JSON line per request (metadata only, never prompts);
  `python -m aigw.report` turns it into per-team spend.

## Design notes

- **One ix app, four services**: one GUI entry, one restart button; observability wiring never
  touches the host network.
- **Dashboard-only observability**: Prometheus and Jaeger publish no ports; Grafana is the
  single surface (LAN :3000, tailnet via `tailscale serve`, and your domain via Caddy).
- **No Vault, SSO, or Redis** for a home setup: Ollama needs no credentials, humans use keys,
  and a single gateway replica keeps limits in-process. All are additive later — same policy format.
- **Budgets** need a model `price` (Ollama is free); rate limits are the control that works.
- The repo's MCP demo server is left out; the policy has no MCP servers.

## Deployment-specific notes

The values this guide was built and verified against — real host topology, app names, dataset
paths, discovered ports, HA storage edits, and the RAG-embeddings exception — are recorded in
[local-deployment.md](local-deployment.md). Real addresses live only in a local `.env`.
