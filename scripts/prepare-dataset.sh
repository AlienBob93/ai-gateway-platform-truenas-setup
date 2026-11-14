#!/usr/bin/env bash
# Populate the TrueNAS dataset directory for the aigw app (README Step 1).
#
#   ./scripts/prepare-dataset.sh /mnt/Application_Pool/aigw
#
# Copies the gateway policy, Prometheus config, and the repo's Grafana provisioning and
# dashboard files into the dataset, which the app mounts read-only. Re-run any time to
# refresh config (then restart the app from the GUI).
set -euo pipefail

TARGET="${1:?usage: prepare-dataset.sh /mnt/Application_Pool/aigw}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HERE/../ai-gateway-platform" && pwd)"

[ -d "$REPO/observability/grafana" ] || { echo "repo not found at $REPO" >&2; exit 1; }

mkdir -p "$TARGET"
cp "$HERE/compose/prometheus.yml" "$TARGET/prometheus.yml"
cp "$HERE/compose/rules.yaml" "$TARGET/rules.yaml"
[ -f "$TARGET/gateway.yaml" ] || echo "note: policies/gateway.home.yaml not copied yet (README Step 2)"
[ -f "$HERE/policies/gateway.home.yaml" ] && cp "$HERE/policies/gateway.home.yaml" "$TARGET/gateway.yaml"
rm -rf "$TARGET/provisioning" "$TARGET/dashboards"
cp -r "$REPO/observability/grafana/provisioning" "$TARGET/provisioning"
cp -r "$REPO/observability/grafana/dashboards" "$TARGET/dashboards"

echo "dataset files in $TARGET:"
find "$TARGET" -type f | sort
