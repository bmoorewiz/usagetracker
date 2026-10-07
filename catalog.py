"""Prices and hardware assumptions shared by the report, the on-prem sizing tab and Grafana.

API prices are list prices checked on 2026-10-07 against the providers' pricing pages
(platform.claude.com/docs/en/about-claude/pricing, developers.openai.com/api/docs/pricing,
ai.google.dev/gemini-api/docs/pricing); short-context, standard tier. Hardware prices are
street-price estimates. The sizing tab lets the user override any of them in the browser.
Edit this file to change the defaults for everyone, then rebuild the web page (build_web.py).
"""
from __future__ import annotations

# API list prices, USD per million tokens: (input, cache read, output).
# Matched against the provider's model id (lowercased), first substring hit wins, so put
# specific ids before general ones (e.g. "opus-5-5" before "opus", "gpt-5.4-mini" before "gpt-5").
MODEL_PRICES: list[tuple[str, float, float, float]] = [
    # Anthropic
    ("fable-5-1", 10.00, 0.25, 50.00), ("fable-5.1", 10.00, 0.25, 50.00),
    ("mythos-5-1", 10.00, 0.25, 50.00), ("mythos-5.1", 10.00, 0.25, 50.00),
    ("fable", 10.00, 1.00, 50.00), ("mythos", 10.00, 1.00, 50.00),
    ("opus-5-5", 4.00, 0.20, 20.00), ("opus-5.5", 4.00, 0.20, 20.00),
    ("opus-4-1", 15.00, 1.50, 75.00), ("opus-4-2025", 15.00, 1.50, 75.00), ("claude-3-opus", 15.00, 1.50, 75.00),
    ("opus", 5.00, 0.50, 25.00),                       # Opus 5, 4.8, 4.7, 4.6, 4.5
    ("sonnet-5", 2.00, 0.20, 10.00), ("sonnet 5", 2.00, 0.20, 10.00),  # Sonnet 5 and 5.5
    ("sonnet", 3.00, 0.30, 15.00),                     # Sonnet 4.6, 4.5, 4
    ("haiku-5-5", 0.10, 0.01, 0.50), ("haiku-5.5", 0.10, 0.01, 0.50),  # prompts up to 100K tokens
    ("haiku-3-5", 0.80, 0.08, 4.00), ("3-5-haiku", 0.80, 0.08, 4.00), ("claude-3-haiku", 0.25, 0.03, 1.25),
    ("haiku", 1.00, 0.10, 5.00),                       # Haiku 4.5
    # OpenAI (up to 272K input tokens)
    ("gpt-6-astra", 10.00, 1.00, 50.00), ("gpt-6.1-sol", 2.00, 0.10, 10.00), ("gpt-6-sol", 2.00, 0.20, 10.00),
    ("gpt-6-luna", 0.10, 0.01, 0.50), ("gpt-6", 10.00, 1.00, 50.00),
    ("cyber", 12.50, 1.25, 75.00),
    ("gpt-5.6-sol", 4.00, 0.40, 20.00), ("gpt-5.6-terra", 2.00, 0.20, 12.00), ("gpt-5.6-luna", 0.20, 0.02, 1.20),
    ("gpt-5.5-pro", 30.00, 30.00, 180.00), ("gpt-5.4-pro", 30.00, 30.00, 180.00),
    ("gpt-5.2-pro", 21.00, 21.00, 168.00), ("gpt-5-pro", 15.00, 15.00, 120.00),
    ("gpt-5.5", 5.00, 0.50, 30.00), ("chat-latest", 5.00, 0.50, 30.00),
    ("gpt-5.4-mini", 0.75, 0.075, 4.50), ("gpt-5.4-nano", 0.20, 0.02, 1.25), ("gpt-5.4", 2.50, 0.25, 15.00),
    ("codex", 1.75, 0.175, 14.00), ("gpt-5.2", 1.75, 0.175, 14.00),
    ("gpt-5-nano", 0.05, 0.005, 0.40), ("gpt-5-mini", 0.25, 0.025, 2.00), ("gpt-5", 1.25, 0.125, 10.00),
    ("gpt-4.1-nano", 0.10, 0.025, 0.40), ("gpt-4.1-mini", 0.40, 0.10, 1.60), ("gpt-4.1", 2.00, 0.50, 8.00),
    ("gpt-4o-mini", 0.15, 0.075, 0.60), ("gpt-4o", 2.50, 1.25, 10.00),
    ("o1-pro", 150.00, 150.00, 600.00), ("o3-pro", 20.00, 20.00, 80.00),
    ("o4-mini", 1.10, 0.275, 4.40), ("o3-mini", 1.10, 0.55, 4.40), ("o3", 2.00, 0.50, 8.00), ("o1", 15.00, 7.50, 60.00),
    # Google (paid tier, prompts up to 200K tokens; 3.6-3.8 Flash at their 2026 price)
    ("gemini-3.8-flash", 0.75, 0.075, 3.75), ("gemini-3.7-flash", 0.75, 0.075, 3.75), ("gemini-3.6-flash", 0.75, 0.075, 3.75),
    ("gemini-3.5-flash-lite", 0.30, 0.03, 2.50), ("gemini-3.5-flash", 1.50, 0.15, 9.00),
    ("gemini-3.1-flash-lite", 0.25, 0.025, 1.50), ("gemini-3-flash", 0.50, 0.05, 3.00),
    ("gemini-2.5-pro", 1.25, 0.125, 10.00),
    ("flash-lite", 0.10, 0.01, 0.40), ("flash", 0.30, 0.03, 2.50),
    ("gemini", 2.00, 0.20, 12.00),                     # Gemini 3.1 / 3 Pro
    # xAI / others commonly seen in CSV imports (not re-checked)
    ("grok", 3.00, 0.75, 15.00),
]
FALLBACK_PRICE = (3.00, 0.30, 15.00)  # unknown model: priced like a mid-tier frontier model

# Anthropic bills prompt-cache writes above the input price: 1.25x for the 5-minute cache, 2x for 1-hour.
CACHE_WRITE_MULT = {"cache_write_5m": 1.25, "cache_write_1h": 2.0}


def price_for(model: str) -> tuple[tuple[float, float, float], bool]:
    """((input, cache read, output) $/M, known?) for a model id."""
    m = (model or "").lower()
    for key, inp, cached, out in MODEL_PRICES:
        if key in m:
            return (inp, cached, out), True
    return FALLBACK_PRICE, False


def cost_of(row: dict) -> float:
    """List-price cost of a usage row. input_tokens includes any cache writes, which are billed at a premium."""
    (inp, cached, out), _ = price_for(row["model"])
    premium = sum(row.get(k, 0) * (mult - 1) for k, mult in CACHE_WRITE_MULT.items())
    return ((row["input_tokens"] + premium) * inp + row["cached_tokens"] * cached + row["output_tokens"] * out) / 1e6


# On-prem systems. Throughput is sustained output tokens/s per system for a large open MoE
# model (~1T params, Kimi/DeepSeek class) at the system's best precision (FP4 on Blackwell
# and Rubin, FP8 on Hopper), with a realistic serving stack; prefill is uncached input tokens/s.
# Calibrated so a DGX B300 is 8,000 output tok/s; the others scale by memory bandwidth and
# low-precision compute. Prices are all-in street estimates (system, storage share, rails).
SYSTEMS: list[dict] = [
    {"id": "rtx6000", "name": "RTX PRO 6000 server", "detail": "8× RTX PRO 6000 Blackwell Server Edition",
     "gpus": 8, "mem_gb": 768, "fp4": True, "price": 140_000, "kw": 7.0, "out_tps": 1_800, "in_tps": 15_000,
     "fabric": 15_000, "install": 10_000, "ru": 4},
    {"id": "h200", "name": "HGX H200", "detail": "8× H200 141 GB, NVLink",
     "gpus": 8, "mem_gb": 1128, "fp4": False, "price": 330_000, "kw": 10.2, "out_tps": 2_600, "in_tps": 25_000,
     "fabric": 40_000, "install": 20_000, "ru": 8},
    {"id": "b200", "name": "DGX B200", "detail": "8× B200 180 GB, NVLink 5",
     "gpus": 8, "mem_gb": 1440, "fp4": True, "price": 520_000, "kw": 14.3, "out_tps": 6_000, "in_tps": 60_000,
     "fabric": 50_000, "install": 25_000, "ru": 10},
    {"id": "b300", "name": "DGX B300", "detail": "8× B300 288 GB, NVLink 5",
     "gpus": 8, "mem_gb": 2304, "fp4": True, "price": 850_000, "kw": 14.5, "out_tps": 8_000, "in_tps": 80_000,
     "fabric": 60_000, "install": 25_000, "ru": 10},
    {"id": "rubin8", "name": "HGX Rubin NVL8", "detail": "8× Rubin 288 GB HBM4 (estimate)",
     "gpus": 8, "mem_gb": 2304, "fp4": True, "price": 1_150_000, "kw": 20.0, "out_tps": 18_000, "in_tps": 200_000,
     "fabric": 70_000, "install": 40_000, "ru": 10},
    {"id": "vr72", "name": "Vera Rubin NVL72", "detail": "Rack: 72× Rubin, 36× Vera CPU, liquid cooled (estimate)",
     "gpus": 72, "mem_gb": 20_736, "fp4": True, "price": 6_500_000, "kw": 190.0, "out_tps": 160_000, "in_tps": 1_800_000,
     "fabric": 250_000, "install": 150_000, "ru": 48},
]

# What the on-prem cluster would serve. weights_gb is (FP8, FP4); speed scales SYSTEMS throughput.
ONPREM_MODELS: list[dict] = [
    {"id": "large", "name": "Frontier-class open MoE (~1T params)", "weights_gb": [1050, 560], "speed": 1.0},
    {"id": "mid", "name": "Mid-size open MoE (~300B params)", "weights_gb": [330, 180], "speed": 2.2},
    {"id": "small", "name": "Efficient open model (~120B params)", "weights_gb": [125, 70], "speed": 5.0},
]

# Defaults for the sizing tab; the user can change all of these in the browser.
SIZING_DEFAULTS = {
    "years": 5,             # horizon
    "growth": 25,           # % usage growth per year
    "price_adj": 0,         # % on top of list price (negative = negotiated discount)
    "seat": 0,              # platform seat $/user/month on top of tokens
    "onprem_model": "large",
    "hours": 10,            # working day the traffic lands in
    "burst": 2,             # peak-to-average within that day
    "util": 80,             # plan to run at most this % of capacity at peak
    "load": 60,             # average power draw, % of max, 24/7
    "ovh_kw": 1.5,          # switch / storage kW per system
    "pue": 1.3,
    "rate": 0.13,           # $/kWh
    "sup_yrs": 3,           # support included
    "sup_pct": 8,           # support renewal, % of hardware per year
    "fte": 0.5,
    "fte_cost": 180_000,
    "resid": 10,            # resale value at end, % of hardware
}


def for_browser() -> dict:
    return {"systems": SYSTEMS, "models": ONPREM_MODELS, "defaults": SIZING_DEFAULTS}
