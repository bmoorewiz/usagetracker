#!/usr/bin/env python3
"""Pull per-user token usage from frontier-model providers and build an HTML dashboard.

Providers
  anthropic  Admin API usage report, grouped by API key; keys are attributed to the
             user who created them. Optionally adds Claude Code per-user analytics.
             Needs an Anthropic admin key (sk-ant-admin...).
  claude_enterprise
             Claude Enterprise Analytics API: per-user daily tokens across claude.ai chat,
             Cowork, Claude Code, Claude in Chrome/Office/Slack, by product and model.
             Needs a claude.ai key with read:analytics (sk-ant-api01..., primary owner only).
  openai     Organization Usage API, grouped by user_id and api_key_id; key owners
             fill in rows that have no user_id. Needs an OpenAI admin key.
  csv        Any other provider (Gemini/Vertex billing export, xAI, Bedrock, ...) via
             --csv files with columns: date,provider,user,model,input_tokens,
             output_tokens[,cached_tokens,requests]

Credentials: on first run you're asked for each provider's admin key (hidden input). It is
verified against the provider, then saved to the OS secret store (macOS Keychain, Windows
Credential Manager, Linux Secret Service) for reuse. ANTHROPIC_ADMIN_KEY / CLAUDE_ENTERPRISE_KEY /
OPENAI_ADMIN_KEY env vars override saved keys.

Outputs: dashboard.html (usage, on-prem sizing and Grafana tabs), usage.json, usage.csv and
grafana-dashboard.json (import into Grafana, or use --grafana-url to publish it directly).

Stdlib only; Windows, macOS, Linux. Examples
  python3 fetch_usage.py --demo --open
  python3 fetch_usage.py login                # prompt for, verify, and save all keys
  python3 fetch_usage.py status               # check saved keys still work
  python3 fetch_usage.py logout openai
  python3 fetch_usage.py --days 30 --providers anthropic,openai
  python3 fetch_usage.py --days 30 --providers claude_enterprise,openai
  python3 fetch_usage.py --days 14 --providers anthropic --claude-code --csv gemini.csv
  python3 fetch_usage.py --grafana-url https://grafana.example.com    # also publish to Grafana
"""
from __future__ import annotations

import argparse
import base64
import csv
import http.client
import json
import os
import random
import sys
import time
import urllib.error
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import auth
import catalog
import grafana
from auth import ApiError, http_get

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "dashboard_template.html"


def row(day: str, provider: str, user: str, model: str, inp=0, out=0, cached=0, requests=0, tenant="",
        write_5m=0, write_1h=0) -> dict:
    """input_tokens includes cache writes; cache_write_5m/1h say how much of it was, for pricing."""
    return {
        "date": day, "provider": provider, "tenant": tenant, "user": user or "(unattributed)",
        "model": model or "unknown", "input_tokens": int(inp or 0), "output_tokens": int(out or 0),
        "cached_tokens": int(cached or 0), "requests": int(requests or 0),
        "cache_write_5m": int(write_5m or 0), "cache_write_1h": int(write_1h or 0),
    }


# ---------------------------------------------------------------- Anthropic

ANTHROPIC_BASE = "https://api.anthropic.com/v1/organizations"


def anthropic_list(path: str, headers: dict[str, str]) -> list[dict]:
    """Cursor-paginated list endpoints (users, api_keys, workspaces)."""
    items, after = [], None
    while True:
        params = [("limit", "100")] + ([("after_id", after)] if after else [])
        page = http_get(f"{ANTHROPIC_BASE}/{path}", headers, params)
        items += page.get("data", [])
        if not page.get("has_more"):
            return items
        after = page.get("last_id")


def fetch_anthropic(h: dict[str, str], start: date, end: date, claude_code: bool) -> list[dict]:
    users = {u["id"]: u.get("email") or u.get("name") or u["id"] for u in anthropic_list("users", h)}
    key_owner, key_name = {}, {}
    for k in anthropic_list("api_keys", h):
        creator = (k.get("created_by") or {}).get("id")
        key_owner[k["id"]] = users.get(creator, creator)
        key_name[k["id"]] = k.get("name") or k["id"]
    workspaces = {w["id"]: w.get("name", w["id"]) for w in anthropic_list("workspaces", h)}

    rows, page = [], None
    while True:
        params = [
            ("starting_at", f"{start.isoformat()}T00:00:00Z"),
            ("ending_at", f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"),
            ("bucket_width", "1d"), ("limit", "31"),
            ("group_by[]", "api_key_id"), ("group_by[]", "model"), ("group_by[]", "workspace_id"),
        ] + ([("page", page)] if page else [])
        resp = http_get(f"{ANTHROPIC_BASE}/usage_report/messages", h, params)
        for bucket in resp.get("data", []):
            day = bucket["starting_at"][:10]
            for r in bucket.get("results", []):
                cc = r.get("cache_creation") or {}
                key_id = r.get("api_key_id")
                # Console/Workbench traffic has no key; label keys with no known creator by key name.
                user = key_owner.get(key_id) or (f"key:{key_name[key_id]}" if key_id in key_name else "(console / no key)")
                rows.append(row(
                    day, "anthropic", user, r.get("model"),
                    inp=(r.get("uncached_input_tokens") or 0)
                    + (cc.get("ephemeral_5m_input_tokens") or 0) + (cc.get("ephemeral_1h_input_tokens") or 0),
                    out=r.get("output_tokens"), cached=r.get("cache_read_input_tokens"),
                    tenant=workspaces.get(r.get("workspace_id"), r.get("workspace_id") or "default"),
                    write_5m=cc.get("ephemeral_5m_input_tokens"), write_1h=cc.get("ephemeral_1h_input_tokens"),
                ))
        if not resp.get("has_more"):
            break
        page = resp.get("next_page")

    if claude_code:
        rows += fetch_claude_code(start, end, h)
    return rows


def fetch_claude_code(start: date, end: date, h: dict[str, str]) -> list[dict]:
    """Claude Code analytics: one day per request, attributed to the user's email."""
    rows, day = [], start
    while day <= end:
        page = None
        while True:
            params = [("starting_at", day.isoformat()), ("limit", "1000")] + ([("page", page)] if page else [])
            resp = http_get(f"{ANTHROPIC_BASE}/usage_report/claude_code", h, params)
            for rec in resp.get("data", []):
                actor = rec.get("actor") or {}
                user = actor.get("email_address") or f"key:{actor.get('api_key_name', '?')}"
                for mb in rec.get("model_breakdown", []):
                    t = mb.get("tokens") or {}
                    rows.append(row(
                        day.isoformat(), "anthropic (claude code)", user, mb.get("model"),
                        inp=(t.get("input") or 0) + (t.get("cache_creation") or 0),
                        out=t.get("output"), cached=t.get("cache_read"), tenant="claude-code",
                    ))
            if not resp.get("has_more"):
                break
            page = resp.get("next_page")
        day += timedelta(days=1)
    return rows


# ---------------------------------------------------------------- Claude Enterprise

ANALYTICS_BASE = f"{ANTHROPIC_BASE}/analytics"
ANALYTICS_FIRST_DAY = date(2026, 1, 1)  # the API has no data before this
PRODUCT_NAMES = {"chat": "Claude chat", "claude_code": "Claude Code", "cowork": "Cowork",
                 "office_agent": "Claude for Office", "claude_in_chrome": "Claude in Chrome",
                 "claude_design": "Claude Design", "claude-tag": "Claude in Slack", "voice_mode": "Voice mode"}


def analytics_get(path: str, params: list[tuple[str, str]], h: dict[str, str]) -> dict:
    """One request; waits out the org-wide 60 requests/minute limit and dropped connections instead of failing."""
    for wait in (5, 15, 30, 60):
        try:
            return http_get(f"{ANALYTICS_BASE}/{path}", h, params)
        except ApiError as e:
            if e.status not in (429, 500, 502, 503, 504, 529):
                raise
        except (http.client.IncompleteRead, ConnectionError, TimeoutError):
            pass
        time.sleep(wait)
    return http_get(f"{ANALYTICS_BASE}/{path}", h, params)


def analytics_query(path: str, params: list[tuple[str, str]], h: dict[str, str]) -> list[dict]:
    """Every row of a paginated query. A cursor expires (HTTP 410) when the data refreshes; start over then."""
    for _ in range(3):
        items, page = [], None
        try:
            while True:
                resp = analytics_get(path, params + ([("page", page)] if page else []), h)
                items += resp.get("data", [])
                if not resp.get("has_more"):
                    return items
                page = resp.get("next_page")
        except ApiError as e:
            if e.status != 410:
                raise
    raise ApiError(410, f"{ANALYTICS_BASE}/{path}", "data kept refreshing while paging; try again in a few minutes")


def fetch_claude_enterprise(h: dict[str, str], start: date, end: date) -> list[dict]:
    """Per-user daily tokens by product and model. Only seat users' usage; tenant = product surface."""
    start = max(start, ANALYTICS_FIRST_DAY)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows, chunk = [], start
    while chunk <= end:
        stop = min(chunk + timedelta(days=30), end)  # one query may span at most 31 days
        until = min(datetime(stop.year, stop.month, stop.day, tzinfo=timezone.utc) + timedelta(days=1), now)
        params = [("starting_at", f"{chunk.isoformat()}T00:00:00Z"),
                  ("ending_at", until.isoformat().replace("+00:00", "Z")),
                  ("bucket_width", "1d"), ("limit", "500"),
                  ("group_by[]", "product"), ("group_by[]", "model")]
        for r in analytics_query("user_usage_report", params, h):
            actor = r.get("actor") or {}
            cc = r.get("cache_creation") or {}
            product = r.get("product")
            rows.append(row(
                (r.get("starting_at") or chunk.isoformat())[:10], "claude enterprise",
                actor.get("email_address") or actor.get("name") or actor.get("user_id"), r.get("model"),
                inp=(r.get("uncached_input_tokens") or 0)
                + (cc.get("ephemeral_5m_input_tokens") or 0) + (cc.get("ephemeral_1h_input_tokens") or 0),
                out=r.get("output_tokens"), cached=r.get("cache_read_input_tokens"), requests=r.get("requests"),
                tenant=PRODUCT_NAMES.get(product, product or "other"),
                write_5m=cc.get("ephemeral_5m_input_tokens"), write_1h=cc.get("ephemeral_1h_input_tokens"),
            ))
        chunk = stop + timedelta(days=1)
    return rows


# ---------------------------------------------------------------- OpenAI

OPENAI_BASE = "https://api.openai.com/v1/organization"


def openai_list(url: str, h: dict[str, str]) -> list[dict]:
    items, after = [], None
    while True:
        page = http_get(url, h, [("limit", "100")] + ([("after", after)] if after else []))
        items += page.get("data", [])
        if not page.get("has_more"):
            return items
        after = page.get("last_id")


def fetch_openai(h: dict[str, str], start: date, end: date) -> list[dict]:
    users = {u["id"]: u.get("email") or u.get("name") or u["id"] for u in openai_list(f"{OPENAI_BASE}/users", h)}
    projects = {p["id"]: p.get("name", p["id"]) for p in openai_list(f"{OPENAI_BASE}/projects", h)}
    key_owner = {}
    for pid in projects:
        for k in openai_list(f"{OPENAI_BASE}/projects/{pid}/api_keys", h):
            owner = k.get("owner") or {}
            who = (owner.get("user") or {}).get("email") or (owner.get("service_account") or {}).get("name")
            key_owner[k["id"]] = who or k.get("name") or k["id"]

    rows, page = [], None
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    end_d = end + timedelta(days=1)
    end_ts = int(datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc).timestamp())
    while True:
        params = [
            ("start_time", str(start_ts)), ("end_time", str(end_ts)), ("bucket_width", "1d"), ("limit", "31"),
            ("group_by", "user_id"), ("group_by", "api_key_id"), ("group_by", "model"), ("group_by", "project_id"),
        ] + ([("page", page)] if page else [])
        resp = http_get(f"{OPENAI_BASE}/usage/completions", h, params)
        for bucket in resp.get("data", []):
            day = datetime.fromtimestamp(bucket["start_time"], tz=timezone.utc).date().isoformat()
            for r in bucket.get("results", []):
                uid, kid = r.get("user_id"), r.get("api_key_id")
                user = users.get(uid) or key_owner.get(kid) or (f"key:{kid}" if kid else None)
                cached = r.get("input_cached_tokens") or 0
                rows.append(row(
                    day, "openai", user, r.get("model"),
                    inp=(r.get("input_tokens") or 0) - cached,  # OpenAI input_tokens includes cached
                    out=r.get("output_tokens"), cached=cached, requests=r.get("num_model_requests"),
                    tenant=projects.get(r.get("project_id"), r.get("project_id") or "default"),
                ))
        if not resp.get("has_more"):
            break
        page = resp.get("next_page")
    return rows


# ---------------------------------------------------------------- CSV / demo

# Analytics exports that only count messages (e.g. ChatGPT Enterprise) get tokens estimated from this.
# Typical chat turn including conversation history and attachments; agentic tools (Codex, Cowork) run higher.
TOKENS_PER_MESSAGE = (3_000, 600)  # (input, output)


def _num(v) -> float:
    """'1,234', '1.2M', '$5', '' -> number. Chat-assistant output is rarely clean."""
    t = str(v or "").strip().replace(",", "").replace("$", "").replace("_", "").upper()
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(t[-1:], 1) if t else 1
    try:
        return float(t[:-1] if mult != 1 else t) * mult
    except ValueError:
        return 0.0


def load_csv(path: str, log=lambda msg: print(msg, file=sys.stderr)) -> list[dict]:
    """Columns: date,provider,user,model,input_tokens,output_tokens[,cached_tokens,requests,tenant,messages].

    Tolerates pasted chat output: code fences, header case/spacing, formatted numbers. Rows with
    messages but no token counts are estimated with TOKENS_PER_MESSAGE.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("```")]
    header = next((i for i, ln in enumerate(lines) if "date" in ln.lower() and "," in ln), 0)
    lines = lines[header:]  # skip any prose the assistant put before the table
    reader = csv.DictReader(lines)
    reader.fieldnames = [(h or "").strip().lower().replace(" ", "_") for h in reader.fieldnames or []]
    rows, estimated = [], 0
    for r in reader:
        if not (r.get("date") or "").strip()[:4].isdigit():
            continue  # stray prose or a repeated header
        inp, out, msgs = _num(r.get("input_tokens")), _num(r.get("output_tokens")), _num(r.get("messages"))
        if not inp and not out and msgs:
            inp, out = msgs * TOKENS_PER_MESSAGE[0], msgs * TOKENS_PER_MESSAGE[1]
            estimated += 1
        rows.append(row(r["date"].strip()[:10], (r.get("provider") or Path(path).stem).strip(), (r.get("user") or "").strip(),
                        (r.get("model") or "").strip(), inp, out, _num(r.get("cached_tokens")),
                        _num(r.get("requests")) or msgs, tenant=(r.get("tenant") or "").strip()))
    if estimated:
        log(f"  {Path(path).name}: {estimated} rows had messages but no tokens; estimated at "
            f"{TOKENS_PER_MESSAGE[0]:,} in / {TOKENS_PER_MESSAGE[1]:,} out per message (TOKENS_PER_MESSAGE in fetch_usage.py)")
    return rows


def demo_rows(start: date, end: date) -> list[dict]:
    """A plausible 80-person org with heavy agentic use, big enough for on-prem sizing to be interesting."""
    rng = random.Random(7)
    first = ["avery", "blake", "casey", "devon", "emery", "finley", "harper", "jordan", "kai", "logan",
             "morgan", "quinn", "riley", "sage", "taylor", "rowan", "skyler", "reese", "parker", "dakota"]
    last = ["ng", "patel", "garcia", "kim", "okafor", "silva", "cohen", "berg", "rossi", "haas"]
    teams = [("engineering", 40, 1.6), ("research", 16, 2.2), ("analytics", 14, 0.7), ("operations", 10, 0.35)]
    people = []
    for team, n, intensity in teams:
        for _ in range(n):
            name = f"{rng.choice(first)}.{rng.choice(last)}"
            while any(p[0] == name for p in people):
                name = f"{rng.choice(first)}.{rng.choice(last)}{rng.randint(2, 9)}"
            people.append((name, team, intensity * rng.lognormvariate(0, 0.9)))
    models = {"anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-sonnet-5", "claude-haiku-4-5"],
              "openai": ["gpt-5.5", "gpt-5", "gpt-5-mini"], "google": ["gemini-3-pro", "gemini-3-flash"]}
    rows, day = [], start
    while day <= end:
        weekday = day.weekday() < 5
        for name, team, weight in people:
            for prov, ms in models.items():
                if rng.random() < (0.35 if prov == "google" else 0.75):
                    m = rng.choice(ms)
                    base = weight * (1.0 if weekday else 0.2) * rng.uniform(0.3, 1.7) * 3_000_000
                    rows.append(row(day.isoformat(), prov, f"{name}@example.com", m, base, base * rng.uniform(0.06, 0.2),
                                    base * rng.uniform(1, 6), int(base / 9000) + 1, tenant=team))
        day += timedelta(days=1)
    return rows


# ---------------------------------------------------------------- main

def cmd_login(targets: list[str]) -> None:
    for prov in targets:
        try:
            auth.login(prov, interactive=True, force_prompt=True, remember=True)
        except LookupError as e:
            print(f"  {e}", file=sys.stderr)


def cmd_logout(targets: list[str]) -> None:
    for prov in targets:
        removed = auth.forget(prov)
        print(f"{auth.PROVIDERS[prov]['label']}: {'saved key removed' if removed else 'nothing saved'}")


def cmd_status(targets: list[str]) -> None:
    print(f"Credential store: {auth.store_name()}")
    for prov in targets:
        print(auth.status(prov))


class ReportError(Exception):
    pass


def generate(days: int, providers: list[str], claude_code: bool, csvs: list[str], demo: bool, out_dir,
             get_headers, log=lambda msg: print(msg, file=sys.stderr), explain=str) -> Path:
    """Fetch, merge and write usage.json/usage.csv/dashboard.html. Returns the dashboard path.

    get_headers(provider) returns auth headers, or raises LookupError to skip that provider. A provider
    that rejects its key (401/403) is skipped with explain(error) logged, unless nothing else has data.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    rows: list[dict] = []
    rejected: list[str] = []
    if demo:
        rows = demo_rows(start, end)
    else:
        unknown = [p for p in providers if p not in auth.PROVIDERS]
        if unknown:
            raise ReportError(f"Unknown provider(s): {', '.join(unknown)}. Choose from {', '.join(auth.PROVIDERS)}.")
        fetchers = {"anthropic": lambda h: fetch_anthropic(h, start, end, claude_code),
                    "claude_enterprise": lambda h: fetch_claude_enterprise(h, start, end),
                    "openai": lambda h: fetch_openai(h, start, end)}
        for prov in providers:
            label = auth.PROVIDERS[prov]["label"]
            try:
                headers = get_headers(prov)
            except LookupError as e:
                log(f"{e}; continuing without it.")
                continue
            log(f"Fetching {label} usage...")
            try:
                got = fetchers[prov](headers)
            except ApiError as e:
                if e.status not in (401, 403):
                    raise ReportError(f"{label}: {e}") from None
                rejected.append(f"{label}: {explain(e)}")
                log(f"  {rejected[-1]}\n  Skipping {label}; continuing with the other providers.")
                continue
            log(f"  {label}: {len(got)} rows")
            if prov == "claude_enterprise":
                if start < ANALYTICS_FIRST_DAY:
                    log(f"  {label}: the Analytics API has no data before {ANALYTICS_FIRST_DAY}.")
                if not got:
                    log(f"  {label}: no usage returned. Seat-based Enterprise plans only report usage credits, "
                        "and new usage can take up to 24 hours to appear.")
            rows += got
    for path in csvs:
        got = load_csv(path, log)
        log(f"Loaded {len(got)} rows from {Path(path).name}")
        rows += got
    if rejected and not rows:
        raise ReportError("\n\n".join(rejected))

    # Collapse duplicate keys (e.g. one user with several API keys) to keep the payload small.
    merged: dict[tuple, dict] = defaultdict(lambda: None)
    for r in rows:
        k = (r["date"], r["provider"], r["tenant"], r["user"], r["model"])
        if merged[k] is None:
            merged[k] = dict(r)
        else:
            for f in ("input_tokens", "output_tokens", "cached_tokens", "requests", "cache_write_5m", "cache_write_1h"):
                merged[k][f] += r.get(f, 0)
    rows = sorted(merged.values(), key=lambda r: (r["date"], r["provider"], r["user"]))
    unpriced = set()
    for r in rows:
        r["cost_usd"] = round(catalog.cost_of(r), 4)
        if not catalog.price_for(r["model"])[1]:
            unpriced.add(r["model"])
    if unpriced:
        log(f"No list price for {', '.join(sorted(unpriced))}; estimated at "
            f"${catalog.FALLBACK_PRICE[0]:g}/${catalog.FALLBACK_PRICE[2]:g} per M in/out (edit catalog.py)")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "start": start.isoformat(), "end": end.isoformat(), "demo": demo, "rows": rows,
               "unpriced_models": sorted(unpriced), "catalog": catalog.for_browser()}
    (out / "usage.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    with open(out / "usage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["date"])
        w.writeheader()
        w.writerows(rows)
    board = grafana.build_dashboard(payload)
    (out / GRAFANA_FILE).write_text(json.dumps(board, indent=1), encoding="utf-8")
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("/*__USAGE_DATA__*/null", _script_json(payload))
            .replace("/*__GRAFANA__*/null", _script_json(board))
            .replace("__LOGO_LIGHT__", _logo("logo-light.png"))
            .replace("__LOGO_DARK__", _logo("logo-dark.png")))
    dashboard = out / "dashboard.html"
    dashboard.write_text(html, encoding="utf-8")
    log(f"{len(rows)} rows -> {dashboard} (+ {GRAFANA_FILE})")
    return dashboard


def _script_json(obj) -> str:
    """JSON that is safe to drop inside a <script> block."""
    return json.dumps(obj).replace("</", "<\\/")


def _logo(name: str) -> str:
    path = HERE / "assets" / name
    if not path.exists():  # report still works, just without branding
        return "data:image/gif;base64,R0lGODlhAQABAAAAACw="
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


GRAFANA_FILE = "grafana-dashboard.json"


def publish_grafana(out_dir, url: str, token: str) -> str:
    """Push the last generated report in out_dir to Grafana. Returns the dashboard URL."""
    path = Path(out_dir) / GRAFANA_FILE
    if not path.exists():
        raise ReportError("Generate a report first; there is no Grafana dashboard to publish yet.")
    return grafana.publish(url, token, json.loads(path.read_text(encoding="utf-8")))


def grafana_token(interactive: bool) -> str:
    token = os.environ.get("GRAFANA_TOKEN") or auth.load_saved(grafana.TOKEN_ACCOUNT)
    if token:
        return token
    if not interactive:
        raise ReportError("No Grafana token: set GRAFANA_TOKEN or run once interactively to save one.")
    import getpass
    token = getpass.getpass("Grafana service account token (input hidden): ").strip()
    if not token:
        raise ReportError("No Grafana token given.")
    return token


def build(args) -> None:
    wanted = [p.strip() for p in args.providers.split(",") if p.strip() and p.strip() != "none"]
    interactive = sys.stdin.isatty() and not args.no_prompt
    try:
        dashboard = generate(args.days, wanted, args.claude_code, args.csv, args.demo, args.out,
                             lambda prov: auth.login(prov, interactive))
        url = args.grafana_url or (auth.load_settings().get("grafana_url") if args.grafana else None)
        if args.grafana or args.grafana_url:
            if not url:
                raise ReportError("--grafana needs --grafana-url the first time.")
            token = grafana_token(interactive)
            grafana.verify(url, token)
            link = publish_grafana(args.out, url, token)
            auth.save_settings(grafana_url=url)
            if not os.environ.get("GRAFANA_TOKEN"):
                auth.save(grafana.TOKEN_ACCOUNT, token)
            print(f"Grafana dashboard: {link}", file=sys.stderr)
    except (ReportError, grafana.GrafanaError) as e:
        raise SystemExit(str(e)) from None
    if args.open:
        webbrowser.open(dashboard.resolve().as_uri())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "login", "logout", "status"],
                    help="run (default): log in as needed and build the dashboard; "
                         "login/logout/status: manage saved credentials")
    ap.add_argument("targets", nargs="*", help="providers for login/logout/status (default: all)")
    ap.add_argument("--days", type=int, default=30, help="lookback window in days (default 30)")
    ap.add_argument("--providers", default="anthropic,openai", help="comma list: anthropic,claude_enterprise,openai (or none)")
    ap.add_argument("--claude-code", action="store_true", help="also pull Claude Code per-user analytics")
    ap.add_argument("--csv", action="append", default=[], help="extra CSV file(s) for other providers")
    ap.add_argument("--demo", action="store_true", help="generate synthetic data instead of calling APIs")
    ap.add_argument("--no-prompt", action="store_true", help="never ask for credentials (for cron/CI)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser when done")
    ap.add_argument("--out", default=str(HERE / "out"), help="output directory")
    ap.add_argument("--grafana", action="store_true",
                    help="publish the dashboard to the saved Grafana (token: GRAFANA_TOKEN or saved)")
    ap.add_argument("--grafana-url", help="Grafana base URL, e.g. https://grafana.example.com (implies --grafana)")
    args = ap.parse_args()

    targets = args.targets or list(auth.PROVIDERS)
    bad = [t for t in targets if t not in auth.PROVIDERS]
    if bad:
        ap.error(f"unknown provider(s): {', '.join(bad)}")
    try:
        {"login": cmd_login, "logout": cmd_logout, "status": cmd_status}.get(
            args.command, lambda _: build(args))(targets)
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled.")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error: {e.reason}")


if __name__ == "__main__":
    main()
