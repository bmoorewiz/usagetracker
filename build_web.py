#!/usr/bin/env python3
"""Build frontier-sizing.html: the whole tool as one standalone web page.

The page has a Data tab where a sales rep copies the analytics-chat prompts, pastes or drops the
answers, and builds the same Usage / On-prem sizing / Grafana report the desktop app makes. It
needs no Python, server or API keys; email it, or host it anywhere static (SharePoint, an
intranet, GitHub Pages). Customer data stays in the browser.

Prompts come from ANALYTICS_PROMPTS.md, prices and hardware from catalog.py. Re-run after
editing either:  python3 build_web.py

Writes two identical pages: dist/index.html to upload to a web server (it is the whole site: no
other files, no server-side code), and frontier-sizing.html to email or open locally. A
Content-Security-Policy in the page blocks every network request, so a customer's data can't
leave the browser even if the page were tampered with in transit; serve it over HTTPS anyway.
"""
from __future__ import annotations

import re
from pathlib import Path

import catalog
from fetch_usage import HERE, TEMPLATE, TOKENS_PER_MESSAGE, _logo, _script_json

WEB = HERE / "web"
OUT = HERE / "frontier-sizing.html"
DIST = HERE / "dist"

# Only inline code and data: URIs; no fetch/XHR/beacons, no external scripts, fonts or images.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "connect-src 'none'; form-action 'none'; base-uri 'none'")
# CTG navy tile for the browser tab.
FAVICON = ("data:image/svg+xml," + "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           "<rect width='32' height='32' rx='6' fill='%231b2556'/><text x='16' y='22' font-family='Arial,sans-serif' "
           "font-size='15' font-weight='700' fill='white' text-anchor='middle'>CTG</text></svg>")
HEAD = f"""<meta http-equiv="Content-Security-Policy" content="{CSP}">
<meta name="description" content="CTG Federal: frontier model usage and on-prem AI cluster sizing">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#1b2556">
<link rel="icon" href="{FAVICON}">"""


def prompts() -> list[dict]:
    """The ```text blocks in ANALYTICS_PROMPTS.md, named by their "## Name (where to paste)" heading."""
    md = (HERE / "ANALYTICS_PROMPTS.md").read_text(encoding="utf-8")
    found = []
    for heading, text in re.findall(r"^## (.+?)\n.*?```text\n(.*?)```", md, re.S | re.M):
        name, _, where = heading.partition(" (")
        found.append({"name": name.strip(), "where": where.rstrip(")").strip(), "text": text.strip()})
    if not found:
        raise SystemExit("No prompts found in ANALYTICS_PROMPTS.md (expected '## Name (...)' headings with ```text blocks).")
    return found


def build() -> Path:
    config = {"prices": catalog.MODEL_PRICES, "fallback": catalog.FALLBACK_PRICE, "cache_write_mult": catalog.CACHE_WRITE_MULT,
              "tokens_per_message": TOKENS_PER_MESSAGE, "catalog": catalog.for_browser(), "prompts": prompts()}
    script = (WEB / "intake.js").read_text(encoding="utf-8").replace("/*__WEB_CONFIG__*/null", _script_json(config))
    html = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in (
        ("<!--__HEAD__-->", HEAD),
        ("<!--__DATA_TAB_BUTTON__-->", '<button role="tab" data-tab="data" aria-selected="false">Data</button>'),
        ("<!--__DATA_TAB__-->", (WEB / "intake.html").read_text(encoding="utf-8")),
        ("/*__WEB_INTAKE__*/", script),
        ("</style>", (WEB / "intake.css").read_text(encoding="utf-8") + "</style>"),
        ("__LOGO_LIGHT__", _logo("logo-light.png")),
        ("__LOGO_DARK__", _logo("logo-dark.png")),
    ):
        if marker not in html:
            raise SystemExit(f"dashboard_template.html is missing {marker}")
        html = html.replace(marker, value, 1 if marker == "</style>" else -1)
    OUT.write_text(html, encoding="utf-8")
    DIST.mkdir(exist_ok=True)
    (DIST / "index.html").write_text(html, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"Wrote {out} and {DIST / 'index.html'} ({out.stat().st_size // 1024} KB)")
