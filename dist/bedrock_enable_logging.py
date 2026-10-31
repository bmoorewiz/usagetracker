#!/usr/bin/env python3
# Turn on Amazon Bedrock model invocation logging to CloudWatch Logs, so bedrock_usage.py can report
# usage per IAM identity. Logging only covers calls made after it is on, so run this early.
#
# Run it in AWS CloudShell (the terminal icon at the top of the AWS console):
#   curl -sO https://customer-sizer.ctglabs.ai/bedrock_enable_logging.py && python3 bedrock_enable_logging.py
# That sets up the four US regions. To do others, name them:   python3 bedrock_enable_logging.py us-east-1 eu-west-1
#
# What it changes in the account (and skips anything already there):
#   - IAM role BedrockInvocationLogging that only Bedrock can assume, allowed only to write to the log group below
#   - CloudWatch log group /aws/bedrock/model-invocations in each region, 400-day retention
#   - Bedrock > Settings > Model invocation logging pointed at that log group, in each region
# Only metadata is logged (identity, model, token counts, timings). Prompt, completion, image, embedding
# and video content is NOT delivered to the log. Nothing else in the account is touched.
# Needs rights to create an IAM role and policy, create a log group, and change Bedrock settings
# (AdministratorAccess or PowerUserAccess plus iam:CreateRole/PutRolePolicy/PassRole is enough).
# Cost: CloudWatch Logs ingestion, roughly a kilobyte per model call.
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

ROLE = "BedrockInvocationLogging"
GROUP = "/aws/bedrock/model-invocations"
RETENTION_DAYS = 400
US_REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def regions():
    return sys.argv[1:] or US_REGIONS


def why(e):
    r = getattr(e, "response", {}) or {}
    op = getattr(e, "operation_name", None) or ""
    code = (r.get("Error") or {}).get("Code") or e.__class__.__name__
    return f"{code} on {op}" if op else code


def ensure_role(account, partition):
    """The role Bedrock assumes to write logs. Trust is limited to this account's Bedrock service;
    the policy is limited to the one log group, in any region."""
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:{partition}:bedrock:*:{account}:*"}}}]}
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"arn:{partition}:logs:*:{account}:log-group:{GROUP}:*"}]}
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        log(f"IAM role {ROLE} already exists.")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Lets Amazon Bedrock write model invocation logs to CloudWatch Logs")["Role"]["Arn"]
        log(f"Created IAM role {ROLE}.")
    iam.put_role_policy(RoleName=ROLE, PolicyName="WriteBedrockInvocationLogs", PolicyDocument=json.dumps(policy))
    return arn


def ensure_group(region):
    logs = boto3.client("logs", region_name=region)
    try:
        logs.create_log_group(logGroupName=GROUP)
        log(f"  {region}: created log group {GROUP}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        log(f"  {region}: log group {GROUP} already exists")
    logs.put_retention_policy(logGroupName=GROUP, retentionInDays=RETENTION_DAYS)


def enable(region, role_arn):
    br = boto3.client("bedrock", region_name=region)
    current = (br.get_model_invocation_logging_configuration().get("loggingConfig") or {}).get("cloudWatchConfig") or {}
    if current.get("logGroupName"):
        log(f"  {region}: logging is already on, to log group {current['logGroupName']}; left as is")
        return False
    ensure_group(region)
    cfg = {"cloudWatchConfig": {"logGroupName": GROUP, "roleArn": role_arn},
           "textDataDeliveryEnabled": False, "imageDataDeliveryEnabled": False,
           "embeddingDataDeliveryEnabled": False, "videoDataDeliveryEnabled": False}
    for attempt in range(6):  # a new IAM role takes a few seconds to become usable
        try:
            br.put_model_invocation_logging_configuration(loggingConfig=cfg)
            log(f"  {region}: model invocation logging is now on (metadata only, to {GROUP})")
            return True
        except ClientError as e:
            if attempt == 5 or e.response["Error"]["Code"] not in ("ValidationException", "AccessDeniedException"):
                raise
            time.sleep(5)


def main():
    me = boto3.client("sts").get_caller_identity()
    account, partition = me["Account"], me["Arn"].split(":")[1]
    log(f"Account {account}; setting up Bedrock invocation logging in {', '.join(regions())}")
    try:
        role_arn = ensure_role(account, partition)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't create the IAM role ({why(e)}). This needs IAM rights; ask an account admin to run it.")
    turned_on, failed = [], []
    for region in regions():
        try:
            if enable(region, role_arn):
                turned_on.append(region)
        except Exception as e:  # noqa: BLE001
            failed.append(region)
            log(f"  {region}: skipped ({why(e)})")
    if failed:
        log(f"Not done in {', '.join(failed)}: see the reasons above (Bedrock isn't available in every region, "
            "and roles are often region-limited).")
    if turned_on:
        log(f"Done. Usage from now on is logged per identity in {', '.join(turned_on)}; run bedrock_usage.py "
            "after some days of use to get bedrock-users.csv.")
    else:
        log("Nothing changed.")


if __name__ == "__main__":
    main()
