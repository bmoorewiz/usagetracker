# Prompts for the built-in analytics chats

Use these when the customer won't share an API key. Someone with admin access pastes the prompt into
their analytics chat, saves the answer as a `.csv` file, and sends it to you. Paste or drop it on the
**Data** tab of `aisizing.html` (or add it under **Extra CSV files** in the desktop app).

The analytics chats can't send a full day × user × product × model grid in one reply; it's too long.
So each prompt asks for three small tables that fit in one answer, about 300 rows for a 50-user org:

1. **Daily usage per product.** Exact, and all the on-prem sizing needs.
2. **Each user's total** for the 30 days.
3. **Totals per product and model** for the 30 days (the model mix, which sets the API price).

The web page (`aisizing.html`) combines them. Daily totals per product, each user's total and
each product/model total stay exact. Each user's split by day and model is estimated from their share,
and the report says so. The desktop app loads table 1 only.

If the chat offers its answer as downloadable CSV files instead (claude.ai does: a per-user file and
daily files by product, by model and by product × model), drop all of them on the page. It reads
claude.ai's own column names, fills in the provider, uses the file with the most days for the daily
totals and the others for the model mix, so nothing is counted twice. Claude Console usage exports
(`claude_api_tokens_*.csv`) work too; they're attributed by API key and workspace.

The page accepts the answer as pasted: extra text, code fences, `#` notes, markdown tables and numbers
like `1,234` or `1.2M` are all fine. If a chat can only count messages, the page estimates tokens at
3,000 in and 600 out per message (change `TOKENS_PER_MESSAGE` in `fetch_usage.py`), and the sizing
numbers are rougher.

---

## Claude (claude.ai > Analytics > Analytics chat)

```text
Export my organization's Claude usage for the last 30 complete days (UTC) as three CSV tables, for a
capacity-planning tool. Include every product in the data. Use the product and model ids exactly as
the data labels them (for example chat, claude_code, cowork, office_agent; claude-opus-5-5).
Token columns everywhere: input_tokens = uncached input plus 5-minute and 1-hour cache-write tokens,
output_tokens = output tokens, cached_tokens = cache-read input tokens, requests = number of requests.

TABLE 1, daily usage: one row per day per product, every day in the window. Header:
date,provider,tenant,input_tokens,output_tokens,cached_tokens,requests
(date is YYYY-MM-DD; provider is always "claude enterprise"; tenant is the product id)

TABLE 2, per-user totals for the 30 days: one row per user with any usage. Header:
provider,user,input_tokens,output_tokens,cached_tokens,requests
(user is the member's email address, or their name if there is no email)

TABLE 3, model mix for the 30 days: one row per product and model. Header:
provider,tenant,model,input_tokens,output_tokens,cached_tokens,requests

Rules for all three tables:
- Plain integers only: no commas, units, currency symbols or rounding to K/M.
- Leave a cell empty if the value isn't in the data. Never estimate, and never drop rows to save space.
- Skip rows where every number is zero.
- Answer in this one reply. Don't ask me anything or offer options, and don't stop partway. If something
  isn't available, still output the three tables and add a short note on its own line starting with #
  after them.
- Output only the three CSV tables and any # notes, in this order, each starting with its header line.
```

## ChatGPT (ChatGPT Enterprise > Workspace settings > Analytics, or the workspace analytics chat)

```text
Export my workspace's ChatGPT usage for the last 30 complete days (UTC) as three CSV tables, for a
capacity-planning tool. Include every product the workspace uses (ChatGPT, Codex, GPTs, Agent,
Deep Research, API usage tied to the workspace), named as the data names them.
Token columns everywhere: input_tokens, output_tokens and cached_tokens are token counts if the data
has them; requests = number of requests; messages = number of messages sent. Token counts are often
not available for ChatGPT seats; then leave the three token columns empty and always fill in messages.

TABLE 1, daily usage: one row per day per product, every day in the window. Header:
date,provider,tenant,input_tokens,output_tokens,cached_tokens,requests,messages
(date is YYYY-MM-DD; provider is always "chatgpt enterprise"; tenant is the product)

TABLE 2, per-user totals for the 30 days: one row per user with any usage. Header:
provider,user,input_tokens,output_tokens,cached_tokens,requests,messages
(user is the member's email address, or their name if there is no email)

TABLE 3, model mix for the 30 days: one row per product and model. Header:
provider,tenant,model,input_tokens,output_tokens,cached_tokens,requests,messages

Rules for all three tables:
- Plain integers only: no commas, units, currency symbols or rounding to K/M.
- Leave a cell empty if the value isn't in the data. Never estimate, and never drop rows to save space.
- Skip rows where every number is zero.
- Answer in this one reply. Don't ask me anything or offer options, and don't stop partway. If something
  isn't available, still output the three tables and add a short note on its own line starting with #
  after them.
- Output only the three CSV tables and any # notes, in this order, each starting with its header line.
```

## AWS Bedrock (run in AWS CloudShell, the terminal icon at the top of the AWS console: `curl -sO https://customer-sizer.ctglabs.ai/bedrock_usage.py && python3 bedrock_usage.py`, then Actions > Download file for each CSV it names. Pasting the script into a file also works, but CloudShell cuts off big pastes, so check that its last line is `main()`)

> Tested only against simulated AWS responses, not a customer account yet. Check the first few Bedrock
> customers' numbers against their Bedrock bill. Per-model daily totals always work (CloudWatch keeps
> them for 15 months with no setup). Per-user figures need model invocation logging to CloudWatch Logs
> (Bedrock console > Settings), which is off by default and only covers usage since it was turned on,
> so ask new customers to turn it on now. Cache-token fields aren't in AWS's documented log format;
> they count when present. A shared IAM role shows as one user.

Bedrock has no analytics chat, so this is a script rather than a prompt. It checks the four US commercial
regions unless others are named on the command line. It needs read-only access
(the AWS managed `ReadOnlyAccess` policy is enough), reads only metrics and logs, and costs well under a
dollar in CloudWatch API charges. It is the file `bedrock_usage.py` in this folder; `build_web.py` inlines it into the web page and copies it
to `dist/`, so the hosted site serves it at `/bedrock_usage.py` for the `curl` above. It writes:

- `bedrock-daily.csv`: tokens per day, model and region from CloudWatch's `AWS/Bedrock` metrics
  (`InputTokenCount` + `CacheWriteInputTokenCount` as input, `OutputTokenCount`, `CacheReadInputTokenCount`,
  `Invocations`). This is all the on-prem sizing needs. The user column is empty, so the Usage tab shows one
  "(unattributed)" user.
- `bedrock-users.csv`: the same per IAM identity, from a Logs Insights query over the invocation logs,
  only where logging is on. Calls through the `bedrock-mantle` endpoint aren't logged.

Drop both files on the Data tab. It keeps the per-user file when it covers the whole period and the daily
file otherwise, so nothing is counted twice. The desktop app doesn't do that: give it one file or the other.
The tenant is the AWS Region; model ids such as `us.anthropic.claude-sonnet-...` are priced from the
Anthropic list, while Amazon's own models get the fallback price until they're added to `catalog.py`.

```text
@include bedrock_usage.py
```

---

## Checking the answer

- Table 1 should have about 30 rows per product, table 2 one row per active user, and table 3 one row per
  product and model. Read any `#` notes the chat added; they say what was missing.
- Spot-check one user's total against the Analytics page.
- If you paste the same table twice, it's counted once.
