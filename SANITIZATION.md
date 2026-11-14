Sanitization summary
====================
This directory is published publicly. Before publishing:

- `.env` (real team API keys + the Grafana admin password) is gitignored —
  it exists only in your local copy.
- Real values replaced with placeholders: your domain -> example.org, tailnet name ->
  <tailnet-name>, host tailscale IP -> <tailscale-ip>, hostnames -> <caddy-host>,
  GitHub username -> <github-user>, Grafana admin password -> CHANGE-ME.
- Local IP addresses are not published — replaced with `<nas-ip>` / `<proxy-ip>` /
  `<tailscale-ip>` placeholders throughout. The policy file's SHA-256 key
  hashes (one-way hashes of 256-bit random keys; the gateway itself stores only hashes).
