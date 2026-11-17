"""Publish AI-gateway metrics to MQTT for Home Assistant (Option A sidecar).

Reads the gateway's Prometheus endpoint (internal to the aigw compose network) and
publishes per-team aggregates + health to MQTT, with HA auto-discovery configs.
Stdlib + paho-mqtt. All values are aggregates from the audit events the dashboard
shows — never prompt or response content.

Env:
    PROM_URL    Prometheus base (default http://prometheus:9090)
    MQTT_HOST   broker host (required)
    MQTT_PORT   broker port (default 1883)
    MQTT_USER / MQTT_PASS  broker credentials
    TEAMS       comma-separated team list to always publish (0 when no traffic yet)
    INTERVAL    publish interval seconds (default 60)
    TOPIC_PREFIX (default aigw)
    LOG_FILE    optional log file path (else stdout only)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

PROM_URL = os.environ.get("PROM_URL", "http://prometheus:9090").rstrip("/")
MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
TEAMS = [t.strip() for t in os.environ.get("TEAMS", "").split(",") if t.strip()]
INTERVAL = int(os.environ.get("INTERVAL", "60"))
PREFIX = os.environ.get("TOPIC_PREFIX", "aigw")
LOG_FILE = os.environ.get("LOG_FILE", "")

LOG = open(LOG_FILE, "a", buffering=1) if LOG_FILE else sys.stdout


def log(msg):
    LOG.write(f"{time.strftime('%F %T')} {msg}\n")


QUERIES = {
    "requests_ok": 'sum by (team) (aigw_requests_total{status="ok"})',
    "requests_denied": 'sum by (team) (aigw_requests_total{status="denied"})',
    "requests_error": 'sum by (team) (aigw_requests_total{status="error"})',
    "spend_usd": "sum by (team) (aigw_cost_usd_total)",
    "tokens_total": "sum by (team) (aigw_tokens_total)",
    # Gateway-wide usage rate (both directions, all teams): tokens per minute
    # averaged over the last 10 minutes. rate() treats counter resets (gateway
    # restarts) as resets, so this stays meaningful across restarts.
    "tokens_rate": "sum(rate(aigw_tokens_total[10m])) * 60",
    "latency_p95_seconds":
        "histogram_quantile(0.95, sum by (le) (rate(aigw_request_duration_seconds_bucket[10m])))",
    "fallbacks": "sum by (team) (aigw_fallbacks_total)",
    "content_findings": "sum by (team) (aigw_content_findings_total)",
}


def prom(query):
    url = PROM_URL + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    out = {}
    for series in (data.get("data") or {}).get("result", []):
        team = series["metric"].get("team", "-global")
        try:
            out[team] = float(series["value"][1])
        except (KeyError, ValueError):
            pass
    return out


def by_team(results, team, default=0.0):
    return results.get(team, results.get("-global", default))


def fmt(value):
    if value != value or value in (float("inf"), float("-inf")):
        return None  # NaN/Inf: keep the last retained value
    if value == int(value):
        return str(int(value))
    return f"{value:.6g}"


def pub(client, topic, payload, retain=True):
    info = client.publish(topic, payload, retain=retain)
    log(f"pub {topic} rc={info.rc} mid={info.mid} payload={payload!r}")


def publish_discovery(client):
    device = {"identifiers": ["ai-gateway"], "name": "AI Gateway",
              "manufacturer": "ai-gateway-platform"}
    entities = [
        ("spend_usd", "Spend", "$", "total", "monetary"),
        ("requests_ok", "Requests OK", "requests", "total_increasing", None),
        ("requests_denied", "Requests Denied", "requests", "total_increasing", None),
        ("requests_error", "Requests Errored", "requests", "total_increasing", None),
        ("fallbacks", "Fallbacks Used", "requests", "total_increasing", None),
        ("content_findings", "Content Findings", "findings", "total_increasing", None),
        ("tokens_total", "Tokens", "tokens", "total_increasing", None),
    ]
    for key, name, unit, state_class, dev_class in entities:
        for team in TEAMS:
            eid = f"{key}_{team}"
            cfg = {
                "name": f"{name} {team}", "unique_id": f"aigw_{eid}",
                "state_topic": f"{PREFIX}/{key}/{team}",
                "unit_of_measurement": unit, "state_class": state_class,
                "device": device, "value_template": "{{ value | float }}",
            }
            if dev_class:
                cfg["device_class"] = dev_class
            pub(client, f"homeassistant/sensor/aigw/{eid}/config", json.dumps(cfg))
    for eid, name, unit, state_class, ic in [
        ("tokens_rate", "Tokens per Minute", "tokens/min", "measurement", "mdi:chart-line"),
        ("latency_p95_seconds", "Latency p95", "s", "measurement", "mdi:speedometer"),
        ("gateway_up", "Gateway Up", "", None, None),
    ]:
        # Tokens and latency are published to <prefix>/<key>/global; gateway_up is
        # published directly to <prefix>/gateway_up. Keep discovery in sync.
        state_topic = f"{PREFIX}/{eid}" + ("" if eid == "gateway_up" else "/global")
        cfg = {"name": name, "unique_id": f"aigw_{eid}",
               "state_topic": state_topic, "device": device}
        if unit:
            cfg["unit_of_measurement"] = unit
        if state_class:
            cfg["state_class"] = state_class
        if ic:
            cfg["icon"] = ic
        pub(client, f"homeassistant/sensor/aigw/{eid}/config", json.dumps(cfg))

    # Retire the old cumulative gateway-wide "Tokens Total" sensor and its retained
    # state; it is replaced by the tokens/min rate sensor above. An empty retained
    # discovery payload tells Home Assistant to remove the entity.
    pub(client, "homeassistant/sensor/aigw/tokens_total/config", "")
    pub(client, f"{PREFIX}/tokens_total/global", "")


def publish_state(client, key, team, value):
    text = fmt(value)
    if text is not None:
        pub(client, f"{PREFIX}/{key}/{team}", text)


def on_disconnect(client, userdata, rc):
    log(f"disconnected rc={rc}")


def main():
    log(f"starting: prom={PROM_URL} broker={MQTT_HOST}:{MQTT_PORT} teams={TEAMS}")
    client = mqtt.Client(client_id="aigw-mqtt-publisher")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS or None)
    client.on_disconnect = on_disconnect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    publish_discovery(client)
    log("discovery published")

    while True:
        try:
            for key, query in QUERIES.items():
                res = prom(query)
                if key in ("latency_p95_seconds", "tokens_rate"):
                    val = res.get("-global")
                    if val is not None:
                        publish_state(client, key, "global", val)
                    continue
                for team in TEAMS:
                    publish_state(client, key, team, by_team(res, team))
            up = prom('up{job="aigw"}').get("-global", 0)
            client.publish(f"{PREFIX}/gateway_up", "1" if up else "0", retain=True)
            log("cycle done")
        except Exception as exc:
            log(f"publish cycle failed: {exc!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
