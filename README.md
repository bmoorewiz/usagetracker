# Frontier usage & on-prem sizing

`fetch_usage.py` pulls token usage from provider admin APIs, attributes it to users, and writes
`out/dashboard.html`, a single offline file with the data embedded. Python stdlib only. The report has
three tabs:

- **Usage**: tokens and estimated API spend per user, per day, per model.
- **On-prem sizing**: the right-sized cluster (RTX PRO 6000, H200, B200, B300, Rubin NVL8, Vera Rubin
  NVL72) to carry that workload, and when it breaks even against the API bill.
- **Grafana**: download the dashboard JSON or publish it straight to Grafana.

## Web page (no install, no keys)

`frontier-sizing.html` is the whole tool as one web page. Email it to a rep, or host it on SharePoint, an
intranet or GitHub Pages. On its **Data** tab the rep copies a prompt for the customer's Claude or ChatGPT
analytics chat, then pastes or drops the answer and clicks **Build report**. That opens the same Usage,
On-prem sizing and Grafana tabs as the desktop app. Everything runs in the browser, so customer data
isn't uploaded anywhere. See [ANALYTICS_PROMPTS.md](ANALYTICS_PROMPTS.md) for how the prompts work.

The page can't pull usage with API keys (browsers block those calls) or publish to Grafana directly. Use
**Download Grafana dashboard JSON** and import it instead. After editing prices or hardware in
`catalog.py`, the prompts in `ANALYTICS_PROMPTS.md` or the report template, rebuild the page:

```bash
python3 build_web.py
```

### Putting it on a web server

`python3 build_web.py` writes `dist/index.html`. That one file is the whole site: no other files, no
database, no server code. Upload it to any static host (IIS, nginx, Apache, Azure Static Web Apps, S3,
SharePoint, GitHub Pages) and give reps the URL.

- Serve it over HTTPS.
- The page has a Content-Security-Policy that blocks all network requests, so customer data pasted
  into it can't be sent anywhere. Don't add analytics or tracking scripts; the policy blocks them anyway.
- It asks search engines not to index it (`robots: noindex`). Remove that meta tag in `build_web.py`
  if you want it public.
- Updating it means rebuilding and re-uploading `dist/index.html`.

## Quick start (no command line)

1. **Run the installer for your OS once.** It installs Python and Tk if they're missing, then puts a
   "Frontier Usage" shortcut on the Desktop.

   | OS | Run | What it installs |
   |---|---|---|
   | Windows 10/11 | double-click `install-windows.bat` | latest Python for the current user via winget, or python.org if winget isn't available. No admin rights needed. |
   | macOS | double-click `install-mac.command` (the first time, right-click it and choose **Open**) | Python + Tk via Homebrew if you have it, otherwise the official python.org package (asks for your Mac password), plus `certifi` so HTTPS works |
   | Linux | `sh install-linux.sh` | `python3`, Tk and `secret-tool` via apt, dnf, yum, zypper, pacman or apk (uses sudo), plus an applications-menu entry |

   Running an installer again is safe: it skips anything already installed. To see what an
   installer would do without changing anything, run it with `DRY_RUN=1` (macOS/Linux) or `-DryRun` (Windows).
2. Double-click **Frontier Usage** on the Desktop. Or use the launcher in this folder:
   - **Windows:** `Frontier Usage.bat`
   - **macOS:** `Frontier Usage.command`. The first time, right-click it and choose **Open** to get past Gatekeeper.
   - **Linux:** `Frontier Usage.sh`. This needs the `python3-tk` package.
3. In the window, paste a key next to each provider the customer uses and click **Connect**. Click **Get a
   key** if you don't have one. Keys are checked and saved, so you only do this once per computer. The
   Anthropic rows are easy to mix up:
   - **Anthropic API**: Claude Console / API traffic. Needs a Console **Admin key** (`sk-ant-admin01-`).
   - **Claude Enterprise**: claude.ai chat, Cowork, Claude Code, Claude for Office/Chrome/Slack on an
     Enterprise plan. Needs a claude.ai key with the `read:analytics` scope (`sk-ant-api01-`).
4. Choose a time period and click **Generate report**. The dashboard opens in your browser. Reports are
   saved to `Documents/Frontier Usage Reports`, and **Open folder** takes you there.

To see what a report looks like without any keys, click **Try with sample data**. The sample
is an 80-person org spending about $90K a month on APIs.

5. *(Optional)* Under **3. Grafana**, enter your Grafana address and a service account token
   (Editor role), click **Connect**, and tick **Publish to Grafana after each report**.

## On-prem sizing tab

The filters at the top (provider, tenant, date range, user) choose which usage gets sized, so you can
size for one team or the whole org. For each system type the tab:

1. Takes the 90th-percentile day's output and uncached input tokens, spreads them over the working day
   (10 h default) and multiplies by a burst factor (2× default) to get peak tokens/s.
2. Buys enough systems to keep that peak under 80% of capacity, rounding up so the chosen open model
   fits in memory, then adds systems at the start of any year whose growth needs them.
3. Builds month-by-month cumulative cost for on-prem (hardware, fabric, install, power × PUE, staff,
   support after the included years, minus resale) and for the API (current spend growing yearly), and
   reports the breakeven month. The cheapest option over the horizon is recommended. If none beats the
   API, it says roughly how many times today's usage would make on-prem pay off.

Every assumption can be edited in the browser, including each system's price, kW and throughput.
Change the defaults for everyone in `catalog.py`, which also holds the API list prices used to price
each usage row. Hardware prices and throughputs are estimates. Rubin and Vera Rubin numbers in
particular are pre-release projections, so check them against a current quote before a customer sees them.

## Grafana

Each report also writes `grafana-dashboard.json`: 12 panels (totals, daily tokens and spend by provider,
per-user bars, a user × day usage map, model mix, tenants, and a user × model table). The data is inlined,
so the only requirement is the free **Infinity** data source plugin
(`grafana-cli plugins install yesoreyeram-infinity-datasource`; Grafana Cloud already has it).

- **Publish from the app** (or `--grafana-url https://grafana.example.com` on the command line). This checks
  for the plugin, creates an Infinity data source if there isn't one, and creates or overwrites the
  "Frontier usage" dashboard. The token goes in the OS credential store like the provider keys. The URL is
  kept in `settings.json` next to the fallback credentials file.
- **Import by hand**: Dashboards → New → Import, upload the JSON, and pick your Infinity data source.

## Command line

Works on Windows, macOS and Linux with Python 3.8+ (use `py` instead of `python3` on Windows).

```bash
python3 fetch_usage.py --demo                      # synthetic data, no keys needed
python3 fetch_usage.py                             # prompts for any missing keys, then fetches everything
python3 fetch_usage.py --days 30 --providers anthropic,openai --claude-code --open
python3 fetch_usage.py --providers none --csv gemini.csv   # CSV only
```

## Logging in

The first time you run it, the script asks for an **Admin API key** for each provider (input is hidden).
It checks the key with a live API call and saves it in the OS credential store. Later runs reuse the
saved key without asking. If a saved key gets revoked, you're asked for a new one. To skip a provider,
press Enter at the prompt. Regular API keys and OAuth logins won't work.

| Provider | Where to create the key | Env var override |
|---|---|---|
| `anthropic` (Anthropic API) | platform.claude.com > Settings > Admin keys (`sk-ant-admin01-...`) | `ANTHROPIC_ADMIN_KEY` |
| `claude_enterprise` | claude.ai > Organization settings > API, primary owner only, scope `read:analytics` (`sk-ant-api01-...`) | `CLAUDE_ENTERPRISE_KEY` |
| OpenAI | platform.openai.com > Settings > Organization > Admin keys (`sk-admin-...`) | `OPENAI_ADMIN_KEY` |

The two Anthropic keys are not interchangeable: an Admin key can't read Claude Enterprise analytics, and
an Enterprise key can't read API usage. If a key goes in the wrong row, the report skips that provider
and explains why.

No key? See [ANALYTICS_PROMPTS.md](ANALYTICS_PROMPTS.md) for prompts to paste into the claude.ai and ChatGPT
analytics chats. They produce a CSV you load under **Extra CSV files**.

Claude Enterprise notes: the tenant is the product surface (Claude chat, Cowork, Claude Code, ...). Only
usage by seat users is included. Data starts 2026-01-01 and arrives within about 4 to 24 hours. Seat-based
Enterprise plans report usage credits only. Spend is priced from token counts at API list price, so it
can differ a little from the claude.ai Analytics page.

```bash
python3 fetch_usage.py login               # (re)enter keys for all providers
python3 fetch_usage.py login openai        # just one
python3 fetch_usage.py status              # which store is in use, and whether each saved key still works
python3 fetch_usage.py logout anthropic    # delete the saved key
python3 fetch_usage.py --no-prompt         # for cron or CI: never prompt; fail if no valid key
```

Where keys get stored (the first one available wins):

| OS | Store |
|---|---|
| any | the `keyring` Python package, if it's installed |
| macOS | login Keychain (`security`), service `frontier-usage` |
| Windows | Credential Manager, target `frontier-usage:<provider>` |
| Linux | Secret Service (GNOME Keyring or KWallet) via `secret-tool` (`apt install libsecret-tools`) |
| fallback | `credentials.json` (mode 0600) in `%APPDATA%\frontier-usage\` or `~/.config/frontier-usage/`, used only when no store above is available. The script prints a warning when it falls back to this file. |

If one of the env vars above is set, that key is used and the store is ignored.

Outputs (`--out DIR` changes the folder; `--open` opens the dashboard in your browser): `out/dashboard.html`, `out/usage.json` (you can also load it into the template from the file picker), `out/usage.csv`.

## How usage gets attributed to a user

| Provider | Source | User = | Tenant = |
|---|---|---|---|
| Anthropic API | `GET /v1/organizations/usage_report/messages` grouped by `api_key_id`, `model`, `workspace_id` | email of the user who **created** the API key | workspace |
| Claude Code (`--claude-code`) | `GET /v1/organizations/usage_report/claude_code` | user's email (actor) | `claude-code` |
| OpenAI | `GET /v1/organization/usage/completions` grouped by `user_id`, `api_key_id`, `model`, `project_id` | `user_id` → email, or else the API key owner | project |
| Anything else | `--csv` | the `user` column | the `tenant` column |

Caveats:
- Anthropic API usage is tracked per key, not per person. A shared key or service key shows up as
  its creator (or as `key:<name>`). If you want true per-person numbers, give each person their own key.
- Google Gemini/Vertex has no per-user token usage API. Export from Cloud Billing → BigQuery (or the
  `aiplatform.googleapis.com/publisher/online_serving/token_count` metric) into the CSV format below.

CSV columns: `date,provider,user,model,input_tokens,output_tokens[,cached_tokens,requests,tenant]`

Token fields: `input_tokens` is uncached input (Anthropic cache writes count here too), and
`cached_tokens` is cache reads. The dashboard's metric selector switches between total, input + output,
input, output, and cache reads.
