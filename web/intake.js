// ===================================================================== Data tab (web page only)
// The browser does what fetch_usage.py does for the desktop app: parse the analytics-chat CSVs,
// price them, build the payload and the Grafana JSON. Built into frontier-sizing.html by build_web.py.

const WEB = /*__WEB_CONFIG__*/null;  // prices, fallback, tokens_per_message, catalog, prompts
const intake = { sources: [], prompt: 0, pasted: 0 };

// ---------------------------------------------------------- parsing

const num = (v) => {
  let t = String(v ?? "").trim().replace(/[,$_\s]/g, "").toUpperCase();
  if (!t) return 0;
  const mult = { K: 1e3, M: 1e6, B: 1e9 }[t.slice(-1)];
  if (mult) t = t.slice(0, -1);
  const n = parseFloat(t);
  return Number.isFinite(n) ? n * (mult || 1) : 0;
};

function csvCells(line) {
  const out = [];
  let cur = "", quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c !== '"') cur += c;
      else if (line[i + 1] === '"') { cur += '"'; i++; }
      else quoted = false;
    } else if (c === '"') quoted = true;
    else if (c === ",") { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}
const pipeCells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((s) => s.trim());

// Analytics product ids -> the names the desktop app uses (fetch_usage.PRODUCT_NAMES), so a paste with
// ids and one with names land in the same tenant.
const PRODUCT_NAMES = { chat: "Claude chat", claude_code: "Claude Code", cowork: "Cowork", office_agent: "Claude for Office",
  claude_in_chrome: "Claude in Chrome", claude_design: "Claude Design", "claude-tag": "Claude in Slack", voice_mode: "Voice mode", other: "Other",
  "claude chat": "Claude chat", "claude in slack": "Claude in Slack", "claude for office": "Claude for Office",
  "claude in chrome": "Claude in Chrome", "claude design": "Claude Design", "claude code": "Claude Code" };
const tenantName = (t) => { const k = (t || "").trim(); return PRODUCT_NAMES[k.toLowerCase()] || k; };

// input_tokens includes cache writes; cache_write_5m/1h say how much of it was, for pricing.
const mkRow = (date, provider, user, model, inp, out, cached, requests, tenant, write5m = 0, write1h = 0) => ({
  date, provider: (provider || "").trim() || "unknown", tenant: tenantName(tenant),
  user: (user || "").trim() || "(unattributed)", model: (model || "").trim() || "unknown",
  input_tokens: Math.round(inp), output_tokens: Math.round(out), cached_tokens: Math.round(cached),
  requests: Math.round(requests), cache_write_5m: Math.round(write5m), cache_write_1h: Math.round(write1h),
});

const isSeparator = (line) => /^[\s|:-]*$/.test(line);

// Column names in the prompts' tables, claude.ai Analytics downloads and Claude Console usage exports.
const COLUMNS = {
  date: ["date", "day", "usage_date_utc", "usage_date"],
  provider: ["provider"],
  user: ["user", "email", "user_email", "email_address", "member", "user_name"],
  api_key: ["api_key", "api_key_name"],
  model: ["model_id", "model_version", "model"],  // first match wins: model_id beats the display name
  tenant: ["tenant", "product", "workspace"],
  input: ["input_tokens", "uncached_input_tokens", "cache_creation_ephemeral_5m_input_tokens",
    "cache_creation_ephemeral_1h_input_tokens", "cache_creation_input_tokens", "usage_input_tokens_no_cache",
    "usage_input_tokens_cache_write_5m", "usage_input_tokens_cache_write_1h"],  // all of these are summed
  cached: ["cached_tokens", "cache_read_input_tokens", "usage_input_tokens_cache_read"],
  write5m: ["cache_creation_ephemeral_5m_input_tokens", "usage_input_tokens_cache_write_5m"],
  write1h: ["cache_creation_ephemeral_1h_input_tokens", "usage_input_tokens_cache_write_1h"],
  output: ["output_tokens", "usage_output_tokens"],
  requests: ["requests"],
  messages: ["messages"],
};
const SUMMED = new Set(["input"]);
const normCol = (c) => c.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");

/** Map a header row to {field: [column index...]}, or null if it isn't a usage table header. */
function readHeader(names) {
  const norm = names.map(normCol), map = {};
  for (const [field, aliases] of Object.entries(COLUMNS)) {
    const idx = SUMMED.has(field)
      ? aliases.map((a) => norm.indexOf(a)).filter((i) => i >= 0)
      : [aliases.map((a) => norm.indexOf(a)).find((i) => i >= 0)].filter((i) => i !== undefined);
    if (idx.length) map[field] = idx;
  }
  const keyed = map.date || map.user || map.api_key || map.model;
  const counted = map.input || map.output || map.cached || map.messages;
  if (!keyed || !counted) return null;
  // Exports without a provider column: Console exports have api_key columns, claude.ai ones ephemeral cache columns.
  map.defaultProvider = map.api_key ? "anthropic api" : norm.some((c) => c.startsWith("cache_creation_ephemeral")) ? "claude enterprise" : "";
  return map;
}

const providerFromModel = (m) => /^claude/i.test(m) ? "claude enterprise" : /^(gpt|o\d|codex|chatgpt)/i.test(m) ? "chatgpt enterprise"
  : /^gemini/i.test(m) ? "gemini" : "";

/**
 * Same rules as fetch_usage.load_csv, plus markdown tables, the column names of the providers' own CSV
 * downloads, and three kinds of table (analytics chats can't send a full day x user x product x model
 * grid in one reply, and claude.ai's downloads come one cut per file):
 *   daily   - has a date: one row per day and whatever of user/product/model the table has
 *   totals  - has a user, no date: each user's total for the period
 *   mix     - has a model, no date or user: the period's totals per product and model
 * Lines starting with # are the chat's notes and are ignored.
 */
function parseUsage(text, fallbackProvider) {
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/)
    .filter((l) => l.trim() && !l.trim().startsWith("```") && !l.trim().startsWith("#"));
  const [tpmIn, tpmOut] = WEB.tokens_per_message;
  const rows = [], userTotals = [], modelMix = [];
  let map = null, kind = null, grain = 0, table = "", tables = 0, cells = csvCells, estimated = 0;
  for (const line of lines) {
    const split = !line.includes(",") && line.includes("|") ? pipeCells : csvCells;
    const cand = split(line);
    const header = !/^\d{4}/.test(cand[0] || "") && readHeader(cand);
    if (header) {
      map = header; cells = split;
      const hasUser = !!(map.user || map.api_key);
      kind = map.date ? "daily" : hasUser ? "totals" : "mix";
      grain = (map.tenant ? 1 : 0) + (map.model ? 2 : 0) + (hasUser ? 4 : 0);
      table = `${fallbackProvider}#${++tables}`;
      continue;
    }
    if (!map || isSeparator(line)) continue;
    const v = cand;
    const get = (f) => (map[f] ? (v[map[f][0]] ?? "").trim() : "");
    const sum = (f) => (map[f] || []).reduce((s, i) => s + num(v[i]), 0);
    const model = get("model");
    const user = get("user") || (get("api_key") ? `key:${get("api_key")}` : "");
    const provider = get("provider") || map.defaultProvider || providerFromModel(model) || fallbackProvider;
    let inp = sum("input"), out = sum("output");
    const msgs = sum("messages");
    if (!inp && !out && msgs) { inp = msgs * tpmIn; out = msgs * tpmOut; estimated++; }
    const t = { input_tokens: inp, output_tokens: out, cached_tokens: sum("cached"), requests: sum("requests") || msgs,
      cache_write_5m: sum("write5m"), cache_write_1h: sum("write1h") };
    if (kind === "daily") {
      const date = get("date");
      if (!/^\d{4}/.test(date)) continue;  // prose, "part 2", repeated headers
      const r = mkRow(date.slice(0, 10).replace(/\//g, "-"), provider, user, model,
        t.input_tokens, t.output_tokens, t.cached_tokens, t.requests, get("tenant"), t.cache_write_5m, t.cache_write_1h);
      r._grain = grain; r._table = table;
      rows.push(r);
    } else if (kind === "totals") {
      if (!user || /^(user|total|email)$/i.test(user)) continue;
      userTotals.push({ provider, user, ...t });
    } else {
      if (!model || /^(model|total)$/i.test(model)) continue;
      modelMix.push({ provider, tenant: tenantName(get("tenant")), model, ...t });
    }
  }
  if (!map) throw new Error("Couldn't find a header row with a date, user or model column and token counts.");
  if (!rows.length && !userTotals.length && !modelMix.length)
    throw new Error("Found the header but no data rows. Dates must look like 2026-09-01.");
  return { rows, userTotals, modelMix, estimated };
}

/**
 * claude.ai's downloads cut the same usage several ways (daily by product, daily by model, weekly by
 * product x model). Keep one table per provider so nothing is counted twice: the one with the most
 * days (sizing needs real daily peaks), then the most detail. Every other table that has models feeds
 * the model mix used to split the kept rows by model.
 */
function pickDaily(rows, modelMix) {
  const notes = [], keep = [], mix = [...modelMix];
  const bits = (g) => (g & 1) + ((g >> 1) & 1) + ((g >> 2) & 1);
  for (const prov of [...new Set(rows.map((r) => r.provider))]) {
    const tables = new Map();
    for (const r of rows.filter((x) => x.provider === prov)) {
      const id = r._table ?? "app";
      if (!tables.has(id)) tables.set(id, { rows: [], dates: new Set(), grain: r._grain ?? 7 });
      tables.get(id).rows.push(r); tables.get(id).dates.add(r.date);
    }
    const ranked = [...tables.values()].sort((a, b) => b.dates.size - a.dates.size || bits(b.grain) - bits(a.grain));
    const best = ranked[0];
    keep.push(...best.rows);
    if (ranked.length === 1) continue;
    if (!(best.grain & 2)) {
      for (const t of ranked.slice(1).filter((t) => t.grain & 2)) {
        for (const r of t.rows) mix.push({ provider: prov, tenant: t.grain & 1 ? r.tenant : "", model: r.model,
          input_tokens: r.input_tokens, output_tokens: r.output_tokens, cached_tokens: r.cached_tokens, requests: r.requests,
          cache_write_5m: r.cache_write_5m || 0, cache_write_1h: r.cache_write_1h || 0 });
      }
    }
    notes.push(`${prov}: ${ranked.length} tables cover the same usage. Used the one with ${best.dates.size} days for the totals` +
      (best.grain & 2 ? "" : " and the others for the model mix") + ", so nothing is counted twice.");
  }
  return { rows: keep.map(({ _grain, _table, ...r }) => r), modelMix: mix, notes };
}

const tokOf = (r) => r.input_tokens + r.output_tokens + r.cached_tokens;
const FIELDS = ["input_tokens", "output_tokens", "cached_tokens", "requests", "cache_write_5m", "cache_write_1h"];

/** Split each row by weights [[value, share]], setting `field` to value. Shares sum to <= 1. */
function splitRow(r, field, shares) {
  const out = [];
  for (const [value, f] of shares) {
    const piece = { ...r, [field]: value };
    for (const k of FIELDS) piece[k] = Math.round(r[k] * f);
    if (tokOf(piece) || piece.requests) out.push(piece);
  }
  return out;
}

/**
 * Rebuild per-user, per-model daily rows from the one-paste tables. Daily rows with no model are split
 * by that product's model mix; daily rows with no user are split by each user's share of the period's
 * tokens. Daily totals per product stay exact (they're all the on-prem sizing needs), and so do each
 * user's total and each product/model total; the finer splits are estimates, and the notes say so.
 */
function allocate(rows, userTotals, modelMix) {
  const notes = [];
  let out = rows;
  for (const prov of [...new Set([...userTotals, ...modelMix].map((x) => x.provider))]) {
    const said = [];
    const mix = modelMix.filter((m) => m.provider === prov);
    const noModel = out.filter((r) => r.provider === prov && r.model === "unknown");
    if (mix.length && noModel.length) {
      const weights = (list) => {
        const by = new Map();
        for (const m of list) by.set(m.model, (by.get(m.model) || 0) + tokOf(m));
        const sum = [...by.values()].reduce((a, b) => a + b, 0);
        return sum ? [...by].map(([model, t]) => [model, t / sum]) : null;
      };
      const all = weights(mix);
      const split = noModel.flatMap((r) => {
        const w = weights(mix.filter((m) => m.tenant === r.tenant)) || all;
        return w ? splitRow(r, "model", w) : [r];
      });
      out = out.filter((r) => !noModel.includes(r)).concat(split);
      said.push("models within each product per day");
    }
    const users = userTotals.filter((u) => u.provider === prov);
    let pool = out.filter((r) => r.provider === prov && r.user === "(unattributed)");
    const pools = [...new Set(out.filter((r) => r.user === "(unattributed)").map((r) => r.provider))];
    if (users.length && !pool.length && pools.length === 1) {  // a totals file whose provider couldn't be told apart
      pool = out.filter((r) => r.provider === pools[0] && r.user === "(unattributed)");
    }
    if (users.length && !pool.length && !out.some((r) => r.provider === prov)) {
      notes.push(`${prov}: per-user totals came without a daily table, so they weren't used.`);
    } else if (users.length && pool.length) {
      const daily = pool.reduce((s, r) => s + tokOf(r), 0), seats = users.reduce((s, u) => s + tokOf(u), 0);
      const denom = Math.max(daily, seats) || 1;
      const shares = users.map((u) => [u.user, tokOf(u) / denom]);
      const rest = 1 - shares.reduce((s, [, f]) => s + f, 0);
      if (rest > 0.001) shares.push(["(not tied to a seat)", rest]);  // below that it's rounding
      out = out.filter((r) => !pool.includes(r)).concat(pool.flatMap((r) => splitRow(r, "user", shares)));
      said.push("each user's usage per day, product and model (from their share of the total)" +
        (rest > 0.005 ? `; ${Math.round(rest * 100)}% of usage isn't tied to a seat (API keys, automation)` : ""));
    }
    if (said.length) notes.push(`${prov}: daily totals per product and each user's total are exact. Estimated: ${said.join("; ")}.`);
  }
  return { rows: out, notes };
}

// ---------------------------------------------------------- pricing and payload (mirrors catalog.py / generate())

function priceFor(model) {
  const m = (model || "").toLowerCase();
  for (const [key, inp, cached, out] of WEB.prices) if (m.includes(key)) return [[inp, cached, out], true];
  return [WEB.fallback, false];
}

function buildPayload(rows, demo, notes = []) {
  const merged = new Map();
  for (const r of rows) {
    const k = [r.date, r.provider, r.tenant, r.user, r.model].join("\u0000");
    const m = merged.get(k);
    if (!m) merged.set(k, { ...r });
    else for (const f of FIELDS) m[f] = (m[f] || 0) + (r[f] || 0);
  }
  const out = [...merged.values()].sort((a, b) =>
    a.date.localeCompare(b.date) || a.provider.localeCompare(b.provider) || a.user.localeCompare(b.user));
  const unpriced = new Set();
  for (const r of out) {
    const [[inp, cached, o], known] = priceFor(r.model);
    const premium = Object.entries(WEB.cache_write_mult).reduce((s, [k, mult]) => s + (r[k] || 0) * (mult - 1), 0);
    r.cost_usd = Math.round(((r.input_tokens + premium) * inp + r.cached_tokens * cached + r.output_tokens * o) / 1e6 * 1e4) / 1e4;
    if (!known) unpriced.add(r.model);
  }
  const dates = out.map((r) => r.date);
  return {
    generated_at: new Date().toISOString().slice(0, 19) + "Z",
    start: dates.reduce((a, b) => (b < a ? b : a)), end: dates.reduce((a, b) => (b > a ? b : a)),
    demo: !!demo, rows: out, unpriced_models: [...unpriced].sort(), catalog: WEB.catalog, notes,
  };
}

// ---------------------------------------------------------- sample data (same shape as fetch_usage.demo_rows)

function demoRows() {
  let seed = 7;
  const rnd = () => {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const pick = (a) => a[Math.floor(rnd() * a.length)];
  const uniform = (a, b) => a + (b - a) * rnd();
  const lognorm = (sd) => Math.exp(sd * Math.sqrt(-2 * Math.log(1 - rnd())) * Math.cos(2 * Math.PI * rnd()));
  const first = ["avery", "blake", "casey", "devon", "emery", "finley", "harper", "jordan", "kai", "logan",
    "morgan", "quinn", "riley", "sage", "taylor", "rowan", "skyler", "reese", "parker", "dakota"];
  const last = ["ng", "patel", "garcia", "kim", "okafor", "silva", "cohen", "berg", "rossi", "haas"];
  const teams = [["engineering", 40, 1.6], ["research", 16, 2.2], ["analytics", 14, 0.7], ["operations", 10, 0.35]];
  const people = [], taken = new Set();
  for (const [team, n, intensity] of teams) {
    for (let i = 0; i < n; i++) {
      let name = `${pick(first)}.${pick(last)}`;
      while (taken.has(name)) name = `${pick(first)}.${pick(last)}${2 + Math.floor(rnd() * 8)}`;
      taken.add(name);
      people.push([name, team, intensity * lognorm(0.9)]);
    }
  }
  const models = { anthropic: ["claude-opus-5", "claude-sonnet-5", "claude-sonnet-5", "claude-haiku-4-5"],
    openai: ["gpt-5.5", "gpt-5", "gpt-5-mini"], google: ["gemini-3-pro", "gemini-3-flash"] };
  const today = new Date(); const end = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const rows = [];
  for (let t = end - 29 * 864e5; t <= end; t += 864e5) {
    const d = new Date(t), weekday = d.getUTCDay() % 6 !== 0, day = d.toISOString().slice(0, 10);
    for (const [name, team, weight] of people) {
      for (const [prov, ms] of Object.entries(models)) {
        if (rnd() < (prov === "google" ? 0.35 : 0.75)) {
          const m = pick(ms), base = weight * (weekday ? 1 : 0.2) * uniform(0.3, 1.7) * 3e6;
          rows.push(mkRow(day, prov, `${name}@example.com`, m, base, base * uniform(0.06, 0.2),
            base * uniform(1, 6), Math.floor(base / 9000) + 1, team));
        }
      }
    }
  }
  return rows;
}

// ---------------------------------------------------------- Grafana JSON (port of grafana.build_dashboard)

function buildGrafana(payload) {
  const PLUGIN = "yesoreyeram-infinity-datasource", DS = { type: PLUGIN, uid: "${DS_INFINITY}" };
  const TOP = 25, HEAT = 15, LEGEND = { displayMode: "list", placement: "bottom", showLegend: true };
  const BARS = { drawStyle: "bars", fillOpacity: 85, lineWidth: 1, barAlignment: 0,
    stacking: { mode: "normal", group: "A" }, showPoints: "never" };
  const cell = (v) => { const s = String(v); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const csv = (head, rows) => [head, ...rows].map((r) => r.map(cell).join(",")).join("\n") + "\n";
  const ms = (day) => Date.parse(`${day}T00:00:00Z`);
  const r2 = (x) => Math.round(x * 100) / 100;
  const target = (data, columns, format = "table") => ({
    refId: "A", datasource: DS, type: "csv", source: "inline", parser: "backend", format, data,
    root_selector: "", filters: [], columns: columns.map(([c, t]) => ({ selector: c, text: c, type: t })) });
  const panel = (type, title, [x, y, w, h], tgt, o = {}) => {
    const defaults = { unit: o.unit || "short", custom: o.custom || {} };
    if (o.color) defaults.color = o.color;
    return { type, title, description: o.desc || "", datasource: DS, gridPos: { x, y, w, h }, targets: [tgt],
      fieldConfig: { defaults, overrides: o.overrides || [] }, options: o.options || {} };
  };
  const stat = (title, x, value, unit, desc) => panel("stat", title, [x, 0, 6, 4],
    target(csv(["value"], [[r2(value)]]), [["value", "number"]]), { desc, unit, options: {
      reduceOptions: { calcs: ["lastNotNull"], fields: "", values: false }, colorMode: "none", graphMode: "none", textMode: "value" } });

  const rows = payload.rows, tok = (r) => r.input_tokens + r.output_tokens + r.cached_tokens;
  const providers = [...new Set(rows.map((r) => r.provider))].sort();
  const dates = [...new Set(rows.map((r) => r.date))].sort();
  const get = (m, k, init) => { if (!m.has(k)) m.set(k, init()); return m.get(k); };
  const dailyTok = new Map(), dailyCost = new Map(), users = new Map(), tenants = new Map(), models = new Map();
  const userDay = new Map(), table = new Map();
  for (const r of rows) {
    const t = tok(r), c = r.cost_usd || 0, ten = r.tenant || "(none)";
    const dt = get(dailyTok, r.date, () => ({})), dc = get(dailyCost, r.date, () => ({}));
    dt[r.provider] = (dt[r.provider] || 0) + t;
    dc[r.provider] = (dc[r.provider] || 0) + c;
    const u = get(users, r.user, () => [0, 0, 0, 0]);
    u[0] += r.input_tokens; u[1] += r.output_tokens; u[2] += r.cached_tokens; u[3] += c;
    const tn = get(tenants, ten, () => [0, 0]); tn[0] += t; tn[1] += c;
    models.set(`${r.provider} / ${r.model}`, (models.get(`${r.provider} / ${r.model}`) || 0) + t);
    const ud = get(userDay, r.user, () => ({})); ud[r.date] = (ud[r.date] || 0) + t;
    const a = get(table, JSON.stringify([r.user, r.tenant, r.provider, r.model]), () => [0, 0, 0, 0, 0]);
    a[0] += r.input_tokens; a[1] += r.output_tokens; a[2] += r.cached_tokens; a[3] += r.requests; a[4] += c;
  }
  const totalTokens = rows.reduce((s, r) => s + tok(r), 0), totalCost = rows.reduce((s, r) => s + (r.cost_usd || 0), 0);
  const ndays = Math.max(1, (Date.parse(payload.end) - Date.parse(payload.start)) / 864e5 + 1);
  const ranked = [...users.keys()].sort((a, b) => {
    const ua = users.get(a), ub = users.get(b); return (ub[0] + ub[1] + ub[2]) - (ua[0] + ua[1] + ua[2]); });
  const tcols = [["time", "timestamp_epoch"], ...providers.map((p) => [p, "number"])];
  const multi = { legend: LEGEND, tooltip: { mode: "multi", sort: "desc" } };
  const top = ranked.slice(0, TOP), heat = ranked.slice(0, HEAT);
  const bySpend = [...users.keys()].sort((a, b) => users.get(b)[3] - users.get(a)[3]).slice(0, TOP);
  const trows = [...table].map(([k, v]) => [...JSON.parse(k), ...v.slice(0, 4), r2(v[4])]).sort((a, b) => b[8] - a[8]);

  const panels = [
    stat("Total tokens", 0, totalTokens, "short", "Input + output + cache reads, whole period"),
    stat("Active users", 6, users.size, "none", "Distinct users with any usage"),
    stat("API spend (period)", 12, totalCost, "currencyUSD", "Estimated at list price"),
    stat("API spend / month", 18, totalCost / ndays * 30.44, "currencyUSD", "Run-rate from this period"),
    panel("timeseries", "Daily tokens by provider", [0, 4, 12, 9], target(
      csv(["time", ...providers], dates.map((d) => [ms(d), ...providers.map((p) => dailyTok.get(d)[p] || 0)])),
      tcols, "timeseries"), { custom: BARS, options: multi }),
    panel("timeseries", "Daily API spend by provider", [12, 4, 12, 9], target(
      csv(["time", ...providers], dates.map((d) => [ms(d), ...providers.map((p) => r2(dailyCost.get(d)[p] || 0))])),
      tcols, "timeseries"), { unit: "currencyUSD", custom: BARS, options: multi }),
    panel("barchart", `Tokens per user (top ${TOP})`, [0, 13, 12, 14], target(
      csv(["user", "input", "output", "cache reads"], top.map((u) => [u, ...users.get(u).slice(0, 3)])),
      [["user", "string"], ["input", "number"], ["output", "number"], ["cache reads", "number"]]),
      { custom: { fillOpacity: 85, lineWidth: 0 }, options: { orientation: "horizontal", stacking: "normal", xField: "user",
        showValue: "never", barWidth: 0.8, groupWidth: 0.7, legend: LEGEND, tooltip: { mode: "multi" } } }),
    panel("barchart", `API spend per user (top ${TOP})`, [12, 13, 12, 14], target(
      csv(["user", "spend"], bySpend.map((u) => [u, r2(users.get(u)[3])])), [["user", "string"], ["spend", "number"]]),
      { unit: "currencyUSD", custom: { fillOpacity: 85, lineWidth: 0 }, options: { orientation: "horizontal", xField: "user",
        showValue: "auto", barWidth: 0.8, legend: { ...LEGEND, showLegend: false }, tooltip: { mode: "single" } } }),
    panel("status-history", `Usage map: daily tokens per user (top ${HEAT})`, [0, 27, 24, 12], target(
      csv(["time", ...heat], dates.map((d) => [ms(d), ...heat.map((u) => userDay.get(u)[d] || 0)])),
      [["time", "timestamp_epoch"], ...heat.map((u) => [u, "number"])], "timeseries"),
      { desc: "Darker = more tokens that day", color: { mode: "continuous-blues" }, custom: { fillOpacity: 90, lineWidth: 1 },
        options: { showValue: "never", rowHeight: 0.85, colWidth: 0.95, legend: { ...LEGEND, showLegend: false }, tooltip: { mode: "single" } } }),
    panel("piechart", "Tokens by model", [0, 39, 8, 11], target(
      csv(["model", "tokens"], [...models].sort((a, b) => b[1] - a[1])), [["model", "string"], ["tokens", "number"]]),
      { options: { pieType: "donut", displayLabels: ["percent"], reduceOptions: { values: true, calcs: ["lastNotNull"], fields: "" },
        legend: { displayMode: "table", placement: "right", values: ["value", "percent"], showLegend: true } } }),
    panel("barchart", "Tokens and spend by tenant / workspace", [8, 39, 16, 11], target(
      csv(["tenant", "tokens"], [...tenants].map(([k, v]) => [k, v[0]]).sort((a, b) => b[1] - a[1])),
      [["tenant", "string"], ["tokens", "number"]]),
      { custom: { fillOpacity: 85, lineWidth: 0 }, options: { orientation: "horizontal", xField: "tenant", showValue: "auto",
        barWidth: 0.7, legend: { ...LEGEND, showLegend: false }, tooltip: { mode: "single" } } }),
    panel("table", "User × model breakdown", [0, 50, 24, 14], target(
      csv(["user", "tenant", "provider", "model", "input", "output", "cache reads", "requests", "spend"], trows),
      [["user", "string"], ["tenant", "string"], ["provider", "string"], ["model", "string"], ["input", "number"],
        ["output", "number"], ["cache reads", "number"], ["requests", "number"], ["spend", "number"]]),
      { options: { showHeader: true, sortBy: [{ displayName: "spend", desc: true }] },
        overrides: [{ matcher: { id: "byName", options: "spend" }, properties: [{ id: "unit", value: "currencyUSD" }] }] }),
  ];
  panels.forEach((p, i) => { p.id = i + 1; });
  const endExcl = new Date(Date.parse(payload.end) + 864e5).toISOString().slice(0, 10);
  return {
    __inputs: [{ name: "DS_INFINITY", label: "Infinity", description: "Infinity data source",
      type: "datasource", pluginId: PLUGIN, pluginName: "Infinity" }],
    __requires: [{ type: "datasource", id: PLUGIN, name: "Infinity", version: "2.0.0" }],
    uid: "frontier-usage", title: "Frontier model usage" + (payload.demo ? " (sample data)" : ""),
    description: `Generated by Frontier Usage at ${payload.generated_at}`,
    tags: ["frontier-usage", "ai"], timezone: "utc", editable: true, schemaVersion: 39,
    version: 1, refresh: "", graphTooltip: 1,
    time: { from: `${payload.start}T00:00:00.000Z`, to: `${endExcl}T00:00:00.000Z` },
    panels,
  };
}

// ---------------------------------------------------------- UI

function setReportTabs(enabled) {
  document.querySelectorAll(".tabs button").forEach((b) => { if (b.dataset.tab !== "data") b.disabled = !enabled; });
}

function renderPrompt() {
  const p = WEB.prompts[intake.prompt];
  $("in-pick").innerHTML = WEB.prompts.map((q, i) =>
    `<button type="button" role="radio" aria-checked="${i === intake.prompt}" data-i="${i}">${esc(q.name)}</button>`).join("");
  $("in-where").textContent = `Paste into: ${p.where}`;
  $("in-prompt").textContent = p.text;
  $("in-copied").textContent = "";
}

function renderSources() {
  const s = intake.sources;
  $("in-sources").innerHTML = s.map((src, i) => {
    const totals = src.userTotals || [];
    const users = new Set([...src.rows.map((r) => r.user), ...totals.map((u) => u.user)].filter((u) => u !== "(unattributed)")).size;
    const provs = [...new Set([...src.rows, ...totals, ...(src.modelMix || [])].map((r) => r.provider))].join(", ");
    const dates = src.rows.map((r) => r.date).sort();
    const mixN = (src.modelMix || []).length;
    const parts = [src.rows.length ? `${src.rows.length.toLocaleString()} daily rows, ${dates[0]} → ${dates[dates.length - 1]}` : "no daily rows",
      ...(mixN ? [`model mix (${mixN} product/model pairs)`] : []),
      totals.length ? `per-user totals for ${totals.length.toLocaleString()} ${totals.length === 1 ? "user" : "users"}`
        : `${users.toLocaleString()} ${users === 1 ? "user" : "users"}`, esc(provs)];
    const est = src.estimated
      ? `<span class="notes warn">${src.estimated.toLocaleString()} rows had messages but no tokens; tokens are estimated at
         ${WEB.tokens_per_message[0].toLocaleString()} in / ${WEB.tokens_per_message[1].toLocaleString()} out per message.</span>` : "";
    return `<div class="in-src"><div><b>${esc(src.name)}</b>
      <span class="notes">${parts.join(" · ")}</span>${est}</div>
      <button type="button" data-i="${i}" aria-label="Remove ${esc(src.name)}" title="Remove">×</button></div>`;
  }).join("");
  $("in-build").disabled = !s.length;
}

function addSource(name, text) {
  try {
    let parsed;
    if (/^\s*[{[]/.test(text)) {  // a usage.json from the desktop app
      const j = JSON.parse(text);
      parsed = { rows: (Array.isArray(j) ? j : j.rows || []).map((r) => mkRow(r.date, r.provider, r.user, r.model,
        num(r.input_tokens), num(r.output_tokens), num(r.cached_tokens), num(r.requests), r.tenant)), userTotals: [], modelMix: [], estimated: 0 };
    } else parsed = parseUsage(text, name.replace(/\.[^.]+$/, ""));
    intake.sources = intake.sources.filter((s) => !s.demo);
    intake.sources.push({ name, ...parsed });
    $("in-msg").textContent = "";
    renderSources();
    return true;
  } catch (e) {
    $("in-msg").textContent = `${name}: ${e.message}`;
    return false;
  }
}

/** Chats split long answers into parts and sometimes resend one; a repeated row replaces, never adds. */
function dedupe(items, key) {
  const m = new Map();
  for (const it of items) m.set(key(it), it);
  return { items: [...m.values()], dropped: items.length - m.size };
}

function buildReport() {
  const demo = intake.sources.length > 0 && intake.sources.every((s) => s.demo);
  const daily = dedupe(intake.sources.flatMap((s) => s.rows), (r) => [r.date, r.provider, r.tenant, r.user, r.model].join("\u0000"));
  const totals = dedupe(intake.sources.flatMap((s) => s.userTotals || []), (u) => u.provider + "\u0000" + u.user);
  const mix = dedupe(intake.sources.flatMap((s) => s.modelMix || []), (m) => [m.provider, m.tenant, m.model].join("\u0000"));
  const picked = pickDaily(daily.items, mix.items);
  const { rows, notes } = allocate(picked.rows, totals.items, picked.modelMix);
  notes.unshift(...picked.notes);
  if (daily.dropped + totals.dropped + mix.dropped)
    notes.push(`${(daily.dropped + totals.dropped + mix.dropped).toLocaleString()} rows appeared more than once in the pasted data and were counted once.`);
  if (!rows.length) { $("in-msg").textContent = notes.join(" ") || "No daily rows to build from."; showTab("data"); return; }
  const payload = buildPayload(rows, demo, notes);
  GRAFANA = buildGrafana(payload);
  setReportTabs(true);
  history.replaceState(null, "", "#usage");
  load(payload);
}

async function addFiles(files) {
  for (const f of files) addSource(f.name, await f.text());
}

function intakeStart() {
  $("loader").style.display = "none";
  setReportTabs(false);
  $("sub").textContent = "Paste the usage from the customer's analytics chat, then build the report.";
  renderPrompt();
  renderSources();
  renderGrafana();
  showTab("data");
}

$("in-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  intake.prompt = +b.dataset.i; renderPrompt();
});
$("in-copy").addEventListener("click", async () => {
  const text = WEB.prompts[intake.prompt].text;
  try { await navigator.clipboard.writeText(text); }
  catch {  // file:// pages in some browsers have no clipboard API
    const r = document.createRange(); r.selectNodeContents($("in-prompt"));
    getSelection().removeAllRanges(); getSelection().addRange(r); document.execCommand("copy");
  }
  $("in-copied").textContent = "Copied.";
});
$("in-add").addEventListener("click", () => {
  const text = $("in-paste").value;
  if (!text.trim()) { $("in-msg").textContent = "Paste the chat's answer into the box first."; return; }
  if (addSource(`Pasted data ${++intake.pasted}`, text)) $("in-paste").value = "";
});
$("in-files").addEventListener("change", async (e) => { await addFiles([...e.target.files]); e.target.value = ""; });
$("in-sources").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  intake.sources.splice(+b.dataset.i, 1); renderSources();
});
$("in-build").addEventListener("click", buildReport);
$("in-demo").addEventListener("click", () => {
  intake.sources = [{ name: "Sample data", rows: demoRows(), userTotals: [], modelMix: [], estimated: 0, demo: true }];
  $("in-msg").textContent = "";
  renderSources();
  buildReport();
});
const drop = $("in-drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("dragging"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("dragging")));
drop.addEventListener("drop", (e) => { e.preventDefault(); addFiles([...e.dataTransfer.files]); });
