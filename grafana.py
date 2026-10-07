"""Grafana dashboard for a usage report: build the JSON, or publish it straight to a Grafana.

The data travels inside the dashboard (Infinity data source, inline CSV), so there is no
database to stand up: import the JSON, or let publish() create it through the Grafana API.
Needs the Infinity plugin (yesoreyeram-infinity-datasource), which Grafana Cloud has and
self-hosted Grafana gets with:  grafana-cli plugins install yesoreyeram-infinity-datasource
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from auth import ApiError, http_request

PLUGIN = "yesoreyeram-infinity-datasource"
DS = {"type": PLUGIN, "uid": "${DS_INFINITY}"}
UID = "frontier-usage"
TOKEN_ACCOUNT = "grafana"  # name the service-account token is saved under in the OS secret store
TOP_USERS = 25
HEATMAP_USERS = 15


class GrafanaError(Exception):
    pass


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _ms(day: str) -> int:
    d = date.fromisoformat(day)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _target(data: str, columns: list[tuple[str, str]], fmt: str = "table", ref: str = "A") -> dict:
    return {
        "refId": ref, "datasource": DS, "type": "csv", "source": "inline", "parser": "backend",
        "format": fmt, "data": data, "root_selector": "", "filters": [],
        "columns": [{"selector": c, "text": c, "type": t} for c, t in columns],
    }


def _panel(kind: str, title: str, grid: tuple[int, int, int, int], target: dict, desc: str = "",
           unit: str = "short", custom: dict | None = None, options: dict | None = None,
           color: dict | None = None, overrides: list | None = None) -> dict:
    x, y, w, h = grid
    defaults = {"unit": unit, "custom": custom or {}}
    if color:
        defaults["color"] = color
    return {"type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": [target],
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {}}


def _stat(title: str, x: int, value, unit: str, desc: str) -> dict:
    t = _target(_csv(["value"], [[round(value, 2)]]), [("value", "number")])
    return _panel("stat", title, (x, 0, 6, 4), t, desc, unit, options={
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "none", "graphMode": "none", "textMode": "value"})


BARS = {"drawStyle": "bars", "fillOpacity": 85, "lineWidth": 1, "barAlignment": 0,
        "stacking": {"mode": "normal", "group": "A"}, "showPoints": "never"}
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}


def build_dashboard(payload: dict) -> dict:
    """Importable dashboard JSON (with a DS_INFINITY input) for a fetch_usage payload."""
    rows = payload["rows"]
    tok = lambda r: r["input_tokens"] + r["output_tokens"] + r["cached_tokens"]  # noqa: E731
    providers = sorted({r["provider"] for r in rows})
    dates = sorted({r["date"] for r in rows})

    daily_tok, daily_cost = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    users, tenants, models = defaultdict(lambda: [0, 0, 0, 0.0]), defaultdict(lambda: [0, 0.0]), defaultdict(int)
    user_day, table = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: [0, 0, 0, 0, 0.0])
    for r in rows:
        t, c = tok(r), r.get("cost_usd", 0.0)
        daily_tok[r["date"]][r["provider"]] += t
        daily_cost[r["date"]][r["provider"]] += c
        u = users[r["user"]]
        u[0] += r["input_tokens"]; u[1] += r["output_tokens"]; u[2] += r["cached_tokens"]; u[3] += c
        tenants[r["tenant"] or "(none)"][0] += t
        tenants[r["tenant"] or "(none)"][1] += c
        models[f"{r['provider']} / {r['model']}"] += t
        user_day[r["user"]][r["date"]] += t
        k = (r["user"], r["tenant"], r["provider"], r["model"])
        a = table[k]
        a[0] += r["input_tokens"]; a[1] += r["output_tokens"]; a[2] += r["cached_tokens"]; a[3] += r["requests"]; a[4] += c

    total_tokens = sum(tok(r) for r in rows)
    total_cost = sum(r.get("cost_usd", 0.0) for r in rows)
    ndays = max(1, (date.fromisoformat(payload["end"]) - date.fromisoformat(payload["start"])).days + 1)
    ranked_users = sorted(users, key=lambda u: -(users[u][0] + users[u][1] + users[u][2]))

    panels = [
        _stat("Total tokens", 0, total_tokens, "short", "Input + output + cache reads, whole period"),
        _stat("Active users", 6, len(users), "none", "Distinct users with any usage"),
        _stat("API spend (period)", 12, total_cost, "currencyUSD", "Estimated at list price"),
        _stat("API spend / month", 18, total_cost / ndays * 30.44, "currencyUSD", "Run-rate from this period"),
    ]
    tcols = [("time", "timestamp_epoch")] + [(p, "number") for p in providers]
    panels.append(_panel("timeseries", "Daily tokens by provider", (0, 4, 12, 9), _target(
        _csv(["time"] + providers, [[_ms(d)] + [daily_tok[d][p] for p in providers] for d in dates]),
        tcols, "timeseries"), custom=BARS, options={"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}}))
    panels.append(_panel("timeseries", "Daily API spend by provider", (12, 4, 12, 9), _target(
        _csv(["time"] + providers, [[_ms(d)] + [round(daily_cost[d][p], 2) for p in providers] for d in dates]),
        tcols, "timeseries"), unit="currencyUSD", custom=BARS,
        options={"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}}))

    top = ranked_users[:TOP_USERS]
    panels.append(_panel("barchart", f"Tokens per user (top {TOP_USERS})", (0, 13, 12, 14), _target(
        _csv(["user", "input", "output", "cache reads"], [[u] + users[u][:3] for u in top]),
        [("user", "string"), ("input", "number"), ("output", "number"), ("cache reads", "number")]),
        custom={"fillOpacity": 85, "lineWidth": 0},
        options={"orientation": "horizontal", "stacking": "normal", "xField": "user", "showValue": "never",
                 "barWidth": 0.8, "groupWidth": 0.7, "legend": LEGEND, "tooltip": {"mode": "multi"}}))
    panels.append(_panel("barchart", f"API spend per user (top {TOP_USERS})", (12, 13, 12, 14), _target(
        _csv(["user", "spend"], [[u, round(users[u][3], 2)] for u in sorted(users, key=lambda u: -users[u][3])[:TOP_USERS]]),
        [("user", "string"), ("spend", "number")]),
        unit="currencyUSD", custom={"fillOpacity": 85, "lineWidth": 0},
        options={"orientation": "horizontal", "xField": "user", "showValue": "auto", "barWidth": 0.8,
                 "legend": {**LEGEND, "showLegend": False}, "tooltip": {"mode": "single"}}))

    heat = ranked_users[:HEATMAP_USERS]
    panels.append(_panel("status-history", f"Usage map: daily tokens per user (top {HEATMAP_USERS})", (0, 27, 24, 12),
        _target(_csv(["time"] + heat, [[_ms(d)] + [user_day[u].get(d, 0) for u in heat] for d in dates]),
                [("time", "timestamp_epoch")] + [(u, "number") for u in heat], "timeseries"),
        "Darker = more tokens that day", color={"mode": "continuous-blues"},
        custom={"fillOpacity": 90, "lineWidth": 1},
        options={"showValue": "never", "rowHeight": 0.85, "colWidth": 0.95, "legend": {**LEGEND, "showLegend": False},
                 "tooltip": {"mode": "single"}}))

    panels.append(_panel("piechart", "Tokens by model", (0, 39, 8, 11), _target(
        _csv(["model", "tokens"], sorted(models.items(), key=lambda kv: -kv[1])),
        [("model", "string"), ("tokens", "number")]),
        options={"pieType": "donut", "displayLabels": ["percent"],
                 "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
                 "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"], "showLegend": True}}))
    panels.append(_panel("barchart", "Tokens and spend by tenant / workspace", (8, 39, 16, 11), _target(
        _csv(["tenant", "tokens"], sorted(([k, v[0]] for k, v in tenants.items()), key=lambda kv: -kv[1])),
        [("tenant", "string"), ("tokens", "number")]),
        custom={"fillOpacity": 85, "lineWidth": 0},
        options={"orientation": "horizontal", "xField": "tenant", "showValue": "auto", "barWidth": 0.7,
                 "legend": {**LEGEND, "showLegend": False}, "tooltip": {"mode": "single"}}))

    trows = sorted(([*k, *v[:4], round(v[4], 2)] for k, v in table.items()), key=lambda r: -r[-1])
    panels.append(_panel("table", "User × model breakdown", (0, 50, 24, 14), _target(
        _csv(["user", "tenant", "provider", "model", "input", "output", "cache reads", "requests", "spend"], trows),
        [("user", "string"), ("tenant", "string"), ("provider", "string"), ("model", "string"), ("input", "number"),
         ("output", "number"), ("cache reads", "number"), ("requests", "number"), ("spend", "number")]),
        options={"showHeader": True, "sortBy": [{"displayName": "spend", "desc": True}]},
        overrides=[{"matcher": {"id": "byName", "options": "spend"},
                    "properties": [{"id": "unit", "value": "currencyUSD"}]}]))

    for i, p in enumerate(panels, 1):
        p["id"] = i
    end_excl = (date.fromisoformat(payload["end"]) + timedelta(days=1)).isoformat()
    return {
        "__inputs": [{"name": "DS_INFINITY", "label": "Infinity", "description": "Infinity data source",
                      "type": "datasource", "pluginId": PLUGIN, "pluginName": "Infinity"}],
        "__requires": [{"type": "datasource", "id": PLUGIN, "name": "Infinity", "version": "2.0.0"}],
        "uid": UID, "title": "Frontier model usage" + (" (sample data)" if payload.get("demo") else ""),
        "description": f"Generated by Frontier Usage at {payload['generated_at']}",
        "tags": ["frontier-usage", "ai"], "timezone": "utc", "editable": True, "schemaVersion": 39,
        "version": 1, "refresh": "", "graphTooltip": 1,
        "time": {"from": f"{payload['start']}T00:00:00.000Z", "to": f"{end_excl}T00:00:00.000Z"},
        "panels": panels,
    }


# ---------------------------------------------------------------- publishing

def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _explain(e: ApiError, what: str) -> GrafanaError:
    if e.status == 401:
        return GrafanaError("Grafana rejected the token. Create a service account token "
                            "(Administration > Users and access > Service accounts) and paste it again.")
    if e.status == 403:
        return GrafanaError(f"The Grafana token isn't allowed to {what}. Give its service account the "
                            "Admin role (or Editor plus data source permissions).")
    return GrafanaError(f"Grafana returned HTTP {e.status} while trying to {what}: {e.api_message}")


def verify(base_url: str, token: str) -> str:
    """Check URL + token; returns the Grafana organization name."""
    try:
        return http_request("GET", f"{base_url.rstrip('/')}/api/org", _headers(token)).get("name", "Grafana")
    except ApiError as e:
        raise _explain(e, "read the organization") from None


def ensure_datasource(base: str, h: dict[str, str]) -> str:
    try:
        http_request("GET", f"{base}/api/plugins/{PLUGIN}/settings", h)
    except ApiError as e:
        if e.status == 404:
            raise GrafanaError("The Infinity plugin isn't installed in this Grafana. Install it from "
                               "Administration > Plugins (search \"Infinity\"), or on the server run:\n"
                               f"  grafana-cli plugins install {PLUGIN}\nthen restart Grafana.") from None
        raise _explain(e, "check installed plugins") from None
    try:
        for ds in http_request("GET", f"{base}/api/datasources", h) or []:
            if ds.get("type") == PLUGIN:
                return ds["uid"]
        made = http_request("POST", f"{base}/api/datasources", h,
                            {"name": "Infinity", "type": PLUGIN, "access": "proxy", "jsonData": {}})
        return made["datasource"]["uid"]
    except ApiError as e:
        raise _explain(e, "find or create the Infinity data source") from None


def publish(base_url: str, token: str, dashboard: dict) -> str:
    """Create or replace the dashboard in Grafana. Returns its URL."""
    base, h = base_url.rstrip("/"), _headers(token)
    uid = ensure_datasource(base, h)
    body = json.loads(json.dumps(dashboard).replace("${DS_INFINITY}", uid))
    body.pop("__inputs", None)
    body.pop("__requires", None)
    body["id"] = None
    try:
        resp = http_request("POST", f"{base}/api/dashboards/db", h,
                            {"dashboard": body, "overwrite": True, "message": "Frontier Usage report"})
    except ApiError as e:
        raise _explain(e, "save the dashboard") from None
    return base + resp.get("url", f"/d/{UID}")
