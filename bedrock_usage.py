#!/usr/bin/env python3
# Export Amazon Bedrock token usage for the last 30 days, for the Frontier Usage report.
#
# Run it in AWS CloudShell (the terminal icon at the top of the AWS console; nothing to install):
#   1. Paste this whole script into a file:   cat > bedrock_usage.py   (paste, then Ctrl-D)
#   2. Run it:                                python3 bedrock_usage.py
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
# Drop both files on the report's Data tab; it uses the per-user file when it covers the whole
# period and the daily file otherwise, so nothing is counted twice.
import csv
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3

DAYS = 30
END = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)  # last complete day
START = END - timedelta(days=DAYS)
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


def regions():
    """Every region enabled on the account; falls back to the current one if EC2 can't be asked."""
    try:
        return sorted(r["RegionName"] for r in boto3.client("ec2").describe_regions()["Regions"])
    except Exception as e:  # noqa: BLE001
        log(f"Can't list regions ({e.__class__.__name__}); using the current region only.")
        return [boto3.session.Session().region_name or "us-east-1"]


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
    """One row per UTC day and model from CloudWatch, 500 metric queries per call."""
    cw = boto3.client("cloudwatch", region_name=region)
    queries, key = [], {}
    for i, mid in enumerate(ids):
        for j, met in enumerate(METRICS):
            qid = f"q{i}_{j}"
            key[qid] = (mid, met)
            queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock", "MetricName": met, "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                "Period": 86400, "Stat": "Sum"}})
    totals = {}  # (day, model) -> {metric: sum}
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
    rows = []
    for (day, mid), t in sorted(totals.items()):
        rows.append([day, PROVIDER, "", mid, region,
                     int(t.get("InputTokenCount", 0) + t.get("CacheWriteInputTokenCount", 0)),
                     int(t.get("OutputTokenCount", 0)), int(t.get("CacheReadInputTokenCount", 0)), int(t.get("Invocations", 0))])
    return rows


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
        rows.append([f["day"][:10], PROVIDER, f.get("user", ""), f.get("model", ""), f.get("tenant") or region,
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


def main():
    log(f"Bedrock usage {START:%Y-%m-%d} to {(END - timedelta(days=1)):%Y-%m-%d} (UTC)")
    daily, users, logged = [], [], []
    for region in regions():
        try:
            ids = model_ids(region)
        except Exception as e:  # noqa: BLE001 - Bedrock isn't in every region
            log(f"  {region}: skipped ({e.__class__.__name__}: {str(e)[:160]})")
            continue
        got = daily_rows(region, ids)
        daily += got
        if got:
            log(f"  {region}: {len(got)} daily rows across {len({r[3] for r in got})} models")
        try:
            u = user_rows(region)
        except Exception as e:  # noqa: BLE001
            log(f"  {region}: couldn't read invocation logs ({e.__class__.__name__})")
            u = None
        if u is not None:
            logged.append(region)
            users += u
    if not daily and not users:
        sys.exit("No Bedrock usage found in the last 30 days in any enabled region.")
    write("bedrock-daily.csv", daily)
    log(f"Wrote bedrock-daily.csv: {len(daily)} rows, {len({r[0] for r in daily})} days with usage.")
    if users:
        write("bedrock-users.csv", users)
        days = {r[0] for r in users}
        log(f"Wrote bedrock-users.csv: {len(users)} rows, {len({r[2] for r in users})} identities, {len(days)} days "
            f"(logging is on in {', '.join(logged)}; the report uses this file only if it covers the whole period).")
    else:
        log("No invocation logs found, so there's no per-user file. Turn on model invocation logging "
            "(Bedrock console > Settings) to get per-user figures from now on.")
    log("Download the CSV file(s): CloudShell > Actions > Download file.")


if __name__ == "__main__":
    main()
