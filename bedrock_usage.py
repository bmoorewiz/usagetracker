#!/usr/bin/env python3
# Export Amazon Bedrock token usage for the last 30 days (including today so far), for the Frontier Usage report.
#
# Run it in AWS CloudShell (the terminal icon at the top of the AWS console; nothing to install):
#   1. Get the script:  curl -sO https://customer-sizer.ctglabs.ai/bedrock_usage.py
#      (or paste it into a file; big pastes get cut off in CloudShell, so check the last line is "main()")
#   2. Run it:          python3 bedrock_usage.py
#      That checks the four US regions. To check others, name them:   python3 bedrock_usage.py us-east-1 eu-west-1
#   3. Download the CSVs: CloudShell > Actions > Download file > bedrock-daily.csv (and bedrock-users.csv)
# Needs read-only access (the AWS managed ReadOnlyAccess policy is enough). It only reads metrics
# and logs; nothing leaves the account except the CSV files you download.
#
# Writes:
#   bedrock-daily.csv  tokens per day, model and region from CloudWatch metrics. Always available,
#                      no setup needed; CloudWatch keeps daily figures for 15 months.
#   bedrock-users.csv  tokens per day, IAM identity, model and region from model invocation logs.
#                      Only if logging to CloudWatch Logs is turned on (Bedrock console > Settings);
#                      it's off by default and only covers usage since it was turned on.
# Drop both files on the report's Data tab; it uses the per-user rows on the days they cover and
# the daily rows for the rest, so nothing is counted twice.
import csv
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3

DAYS = 30
END = datetime.now(timezone.utc).replace(microsecond=0)
START = END.replace(hour=0, minute=0, second=0) - timedelta(days=DAYS - 1)  # 30 UTC days including today so far
PROVIDER = "aws bedrock"
HEADER = ["date", "provider", "user", "model", "tenant", "input_tokens", "output_tokens", "cached_tokens", "requests"]
# CloudWatch publishes these per model. InputTokenCount excludes cache reads and writes, so the report's
# input_tokens (which includes cache writes) is InputTokenCount + CacheWriteInputTokenCount.
METRICS = ["InputTokenCount", "CacheWriteInputTokenCount", "OutputTokenCount", "CacheReadInputTokenCount", "Invocations"]
# Same figures per IAM identity, from invocation logs. Cache fields aren't in AWS's documented log
# format, so they're zero unless the log has them.
LOGS_QUERY = """
fields datefloor(@timestamp, 1d) as day, identity.arn as user, modelId as model, region as tenant,
       coalesce(input.inputTokenCount, 0) + coalesce(input.cacheWriteInputTokenCount, 0) as inp,
       coalesce(output.outputTokenCount, 0) as outp, coalesce(input.cacheReadInputTokenCount, 0) as cached
| filter schemaType = "ModelInvocationLog"
| stats sum(inp) as input_tokens, sum(outp) as output_tokens, sum(cached) as cached_tokens, count(*) as requests
        by day, user, model, tenant
| sort day asc
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


US_REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]


def regions():
    """Regions named on the command line, else the US commercial regions (GovCloud needs its own credentials)."""
    return sys.argv[1:] or US_REGIONS


def model_ids(region):
    """Model ids that may have metrics. ListMetrics only shows models used in the last two weeks, so the
    region's foundation models and inference profiles are added to catch anything used earlier."""
    ids = set()
    cw = boto3.client("cloudwatch", region_name=region)
    for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/Bedrock", MetricName="Invocations"):
        for m in page["Metrics"]:
            ids.update(d["Value"] for d in m["Dimensions"] if d["Name"] == "ModelId")
    br = boto3.client("bedrock", region_name=region)
    ids.update(m["modelId"] for m in br.list_foundation_models()["modelSummaries"])
    token = None
    while True:
        page = br.list_inference_profiles(**({"nextToken": token} if token else {}))
        ids.update(p["inferenceProfileId"] for p in page.get("inferenceProfileSummaries", []))
        token = page.get("nextToken")
        if not token:
            break
    return sorted(ids)


def daily_rows(region, ids):
    """One row per UTC day and model from CloudWatch, 500 metric queries per call. Bedrock also publishes
    the same metrics with no model dimension; usage there that no listed model accounts for (custom or
    imported models, application inference profile ARNs) is reported as model "unknown"."""
    cw = boto3.client("cloudwatch", region_name=region)
    queries, key = [], {}
    for i, mid in enumerate([None] + ids):
        for j, met in enumerate(METRICS):
            qid = f"q{i}_{j}"
            key[qid] = (mid, met)
            queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock", "MetricName": met,
                           "Dimensions": [{"Name": "ModelId", "Value": mid}] if mid else []},
                "Period": 86400, "Stat": "Sum"}})
    totals = {}  # (day, model) -> {metric: sum}; model None = account-wide
    for k in range(0, len(queries), 500):
        token = None
        while True:
            kw = {"MetricDataQueries": queries[k:k + 500], "StartTime": START, "EndTime": END, "ScanBy": "TimestampAscending"}
            if token:
                kw["NextToken"] = token
            resp = cw.get_metric_data(**kw)
            for r in resp["MetricDataResults"]:
                mid, met = key[r["Id"]]
                for ts, v in zip(r["Timestamps"], r["Values"]):
                    if v:
                        t = totals.setdefault((ts.strftime("%Y-%m-%d"), mid), {})
                        t[met] = t.get(met, 0) + v
            token = resp.get("NextToken")
            if not token:
                break
    for (day, mid), t in [(k, v) for k, v in totals.items() if k[1] is None]:  # leave only the unexplained part
        for met in METRICS:
            seen = sum(v.get(met, 0) for (d, m), v in totals.items() if d == day and m)
            t[met] = max(0, t.get(met, 0) - seen)
        if not any(t.values()):
            del totals[(day, None)]
    rows = []
    for (day, mid), t in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        rows.append([day, PROVIDER, "", mid or "unknown", region,
                     int(t.get("InputTokenCount", 0) + t.get("CacheWriteInputTokenCount", 0)),
                     int(t.get("OutputTokenCount", 0)), int(t.get("CacheReadInputTokenCount", 0)), int(t.get("Invocations", 0))])
    return rows


def short_user(arn):
    """A readable identity: arn:aws:sts::123:assumed-role/Role/session -> Role/session; iam::123:user/jane -> jane."""
    tail = arn.split(":")[-1] if arn.startswith("arn:") else arn
    for prefix in ("assumed-role/", "user/", "role/"):
        if tail.startswith(prefix):
            return tail[len(prefix):]
    return tail


def user_rows(region):
    """Per-identity rows from invocation logs, or None if logging to CloudWatch Logs isn't on here."""
    cfg = boto3.client("bedrock", region_name=region).get_model_invocation_logging_configuration().get("loggingConfig") or {}
    group = (cfg.get("cloudWatchConfig") or {}).get("logGroupName")
    if not group:
        return None
    logs = boto3.client("logs", region_name=region)
    qid = logs.start_query(logGroupName=group, startTime=int(START.timestamp()), endTime=int(END.timestamp()),
                           queryString=LOGS_QUERY, limit=10000)["queryId"]
    while True:
        res = logs.get_query_results(queryId=qid)
        if res["status"] not in ("Scheduled", "Running"):
            break
        time.sleep(2)
    if res["status"] != "Complete":
        log(f"  {region}: the log query ended with status {res['status']}; skipping per-user rows here.")
        return []
    rows = []
    for fields in res["results"]:
        f = {c["field"]: c["value"] for c in fields}
        if not f.get("day"):
            continue
        rows.append([f["day"][:10], PROVIDER, short_user(f.get("user", "")), f.get("model", ""), f.get("tenant") or region,
                     int(float(f.get("input_tokens") or 0)), int(float(f.get("output_tokens") or 0)),
                     int(float(f.get("cached_tokens") or 0)), int(float(f.get("requests") or 0))])
    if len(rows) >= 10000:
        log(f"  {region}: the log query hit its 10,000-row limit, so per-user rows are incomplete here.")
    return rows


def write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def why(e):
    """Short reason for a failed AWS call: the operation and error code, not the whole ARN."""
    r = getattr(e, "response", {}) or {}
    op = getattr(e, "operation_name", None) or (r.get("ResponseMetadata") or {}).get("OperationName") or ""
    code = (r.get("Error") or {}).get("Code") or e.__class__.__name__
    return f"{code} on {op}" if op else code


def main():
    log(f"Bedrock usage {START:%Y-%m-%d} to {END:%Y-%m-%d %H:%M} UTC (metrics can lag a few minutes)")
    daily, users, logged, denied, checked = [], [], [], [], []
    for region in regions():
        try:
            ids = model_ids(region)
        except Exception as e:  # noqa: BLE001 - Bedrock isn't in every region, and roles are often region-limited
            if "AccessDenied" in why(e):
                denied.append(region)
            else:
                log(f"  {region}: skipped ({why(e)})")
            continue
        checked.append(region)
        try:
            got = daily_rows(region, ids)
        except Exception as e:  # noqa: BLE001 - e.g. ListMetrics allowed but GetMetricData denied
            log(f"  {region}: couldn't read metrics ({why(e)})")
            continue
        daily += got
        if got:
            log(f"  {region}: {len(got)} daily rows across {len({r[3] for r in got})} models")
            if any(r[3] == "unknown" for r in got):
                log(f"  {region}: some usage is under model ids CloudWatch doesn't list (custom models or application "
                    "inference profiles); it's included as model \"unknown\" and priced at the fallback rate")
        else:
            log(f"  {region}: no Bedrock metrics in the window (checked {len(ids)} model ids)")
        try:
            u = user_rows(region)
        except Exception as e:  # noqa: BLE001
            log(f"  {region}: couldn't read invocation logs ({why(e)})")
            u = None
        if u is not None:
            logged.append(region)
            users += u
    if denied:
        log(f"  Access denied in {len(denied)} region(s): {', '.join(denied)}; the role can't read CloudWatch there. "
            f"Rerun with only the readable ones:  python3 bedrock_usage.py {' '.join(checked) or '<region>'}")
    if not daily and not users:
        sys.exit("No CSV written: no Bedrock usage found in the last 30 days in "
                 + (", ".join(checked) if checked else "any readable region")
                 + ".\nTo confirm: CloudWatch console > Metrics > All metrics > AWS/Bedrock, in the region the customer uses.")
    write("bedrock-daily.csv", daily)
    log(f"Wrote bedrock-daily.csv: {len(daily)} rows, {len({r[0] for r in daily})} days with usage.")
    if users:
        write("bedrock-users.csv", users)
        days = {r[0] for r in users}
        log(f"Wrote bedrock-users.csv: {len(users)} rows, {len({r[2] for r in users})} identities, {len(days)} days "
            f"(logging is on in {', '.join(logged)}; the report uses these rows on the days they cover).")
    else:
        log("No invocation logs found, so there's no per-user file. Turn on model invocation logging "
            "(Bedrock console > Settings) to get per-user figures from now on.")
    log("Download the CSV file(s): CloudShell > Actions > Download file.")


if __name__ == "__main__":
    main()
