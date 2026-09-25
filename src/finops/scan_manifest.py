"""What a scan will do, before it does any of it.

Asked for directly on r/selfhosted, 2026-08-08: "A dry run listing every required
permission before the first scan would also be useful." It is the right ask. The
current answer to "what will this touch?" is "read the source", and telling a
security-minded operator to trust a claim they cannot check is how a tool gets
declined at review.

Every entry is a check the scanner actually runs, the AWS API calls it makes,
and the IAM action each call needs. All of them are Describe/List/Get: nothing
here mutates, which is the point a reader most wants verified and the reason the
verbs are printed rather than summarised.

The manifest is the single source for both `nable scan --dry-run` and the IAM
policy we hand people, so the policy cannot drift from what the scanner does. A
test asserts the two agree and that no mutating verb ever appears.
"""
from __future__ import annotations

# check -> (what it finds, [(api call, iam action)])
SCAN_CHECKS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "ebs": ("Unattached volumes, and gp2 that should be gp3", [
        ("ec2.describe_volumes", "ec2:DescribeVolumes")]),
    "snapshots": ("EBS snapshots older than the retention you'd choose", [
        ("ec2.describe_snapshots", "ec2:DescribeSnapshots"),
        # The AMI list: a snapshot behind a registered AMI is never flagged.
        ("ec2.describe_images", "ec2:DescribeImages")]),
    "eips": ("Elastic IPs billing while unassociated", [
        ("ec2.describe_addresses", "ec2:DescribeAddresses")]),
    "nat": ("NAT gateways with no meaningful traffic", [
        ("ec2.describe_nat_gateways", "ec2:DescribeNatGateways"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "ec2": ("Idle and stopped instances still costing money", [
        ("ec2.describe_instances", "ec2:DescribeInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "rds": ("Backup retention beyond what the workload needs", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances")]),
    "rds_rightsizing": ("Oversized database instances, by observed CPU", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "rds_idle": ("Databases with no connections", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "cloudtrail": ("Duplicate trails, expensive data events, and stopped trails", [
        ("cloudtrail.describe_trails", "cloudtrail:DescribeTrails"),
        ("cloudtrail.get_event_selectors", "cloudtrail:GetEventSelectors"),
        ("cloudtrail.get_trail_status", "cloudtrail:GetTrailStatus")]),
    "cloudwatch": ("Log groups with no retention set, stored forever", [
        ("logs.describe_log_groups", "logs:DescribeLogGroups")]),
    "s3": ("Buckets on a storage class their access pattern doesn't justify", [
        ("s3.list_buckets", "s3:ListAllMyBuckets"),
        ("s3.get_bucket_location", "s3:GetBucketLocation"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "s3_multipart": ("Incomplete multipart uploads billing silently", [
        ("s3.list_buckets", "s3:ListAllMyBuckets"),
        ("s3.list_multipart_uploads", "s3:ListBucketMultipartUploads"),
        # The size of each stale upload, which is what prices it.
        ("s3.list_parts", "s3:ListMultipartUploadParts")]),
    "lambda": ("Functions provisioned well above their observed memory", [
        ("lambda.list_functions", "lambda:ListFunctions"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "load_balancer": ("Load balancers carrying almost no traffic", [
        ("elbv2.describe_load_balancers", "elasticloadbalancing:DescribeLoadBalancers"),
        ("elb.describe_load_balancers", "elasticloadbalancing:DescribeLoadBalancers"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "ecr": ("Untagged images nothing has pulled in months", [
        ("ecr.describe_repositories", "ecr:DescribeRepositories"),
        ("ecr.describe_images", "ecr:DescribeImages")]),
    "ecs": ("Fargate tasks reserving far more CPU than they use", [
        ("ecs.list_clusters", "ecs:ListClusters"),
        ("ecs.list_services", "ecs:ListServices"),
        ("ecs.describe_services", "ecs:DescribeServices"),
        ("ecs.describe_task_definition", "ecs:DescribeTaskDefinition"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "dynamodb": ("Provisioned tables reserving far more read/write capacity than they use", [
        ("dynamodb.list_tables", "dynamodb:ListTables"),
        ("dynamodb.describe_table", "dynamodb:DescribeTable"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
}

# Asked once per scan, from us-east-1, when the ec2, lambda or rds checks run.
# Free: Compute Optimizer does not bill for reading its recommendations. It is
# an enrichment, so an account that has not opted in to Compute Optimizer, or a
# policy without these, loses only the extra rightsizing findings.
COMPUTE_OPTIMIZER_ACTIONS: list[tuple[str, str]] = [
    ("compute-optimizer.get_ec2_instance_recommendations",
     "compute-optimizer:GetEC2InstanceRecommendations"),
    ("compute-optimizer.get_lambda_function_recommendations",
     "compute-optimizer:GetLambdaFunctionRecommendations"),
    ("compute-optimizer.get_rds_database_recommendations",
     "compute-optimizer:GetRDSDatabaseRecommendations"),
]

# Only when a host opts in to batched CloudWatch reads
# (FINOPS_CLOUDWATCH_GETMETRICDATA=1). AWS bills GetMetricData at $0.01 per
# 1,000 metrics with no free tier, while GetMetricStatistics has 1,000,000 free
# requests a month, so the default scan reads through GetMetricStatistics and
# this is not in the default policy.
GET_METRIC_DATA_ACTIONS: list[tuple[str, str]] = [
    ("cloudwatch.get_metric_data", "cloudwatch:GetMetricData"),
]

# Always needed, whatever checks run: identity, and which regions to sweep.
BASE_ACTIONS: list[tuple[str, str]] = [
    ("sts.get_caller_identity", "sts:GetCallerIdentity"),
    ("ec2.describe_regions", "ec2:DescribeRegions"),
]

# Only on `--spend`, and only because each request is BILLED at $0.01. The
# default scan never calls Cost Explorer, which is the whole reason this is a
# separate list rather than folded into the policy above. The spend breakdown
# and the Bedrock leg of the AI view both read through GetCostAndUsage alone;
# GetCostForecast and GetDimensionValues were listed here and nothing on the
# `--spend` path calls them.
SPEND_ACTIONS: list[tuple[str, str]] = [
    ("ce.get_cost_and_usage", "ce:GetCostAndUsage"),
]

# Only for `nable guard reconcile`, which reads CloudTrail's management events
# to line the guard's ledger up against what happened. Not a scan call, so not
# in the scan policy; `nable guard reconcile` prints it when it is missing.
# LookupEvents is free and read-only.
GUARD_RECONCILE_ACTIONS: list[tuple[str, str]] = [
    ("cloudtrail.lookup_events", "cloudtrail:LookupEvents"),
]

# A verb that changes anything. The manifest is a promise that none appear, and
# the test enforces it, so a mutating call cannot be added here quietly.
_MUTATING = ("create", "delete", "put", "update", "modify", "terminate",
             "release", "attach", "detach", "start", "stop", "reboot", "run",
             "purchase", "tag", "untag", "set")


def _get_metric_data_in_use(include_get_metric_data: bool | None) -> bool:
    if include_get_metric_data is None:
        from .analyzers.cloudwatch import get_metric_data_opted_in
        return get_metric_data_opted_in()
    return include_get_metric_data


def iam_actions(include_spend: bool = False,
                include_get_metric_data: bool | None = None) -> list[str]:
    """Every IAM action a scan needs, sorted and deduplicated. GetMetricData is
    in only when this host has opted in to it (None follows the env flag)."""
    actions = {a for _, a in BASE_ACTIONS}
    for _, calls in SCAN_CHECKS.values():
        actions.update(a for _, a in calls)
    actions.update(a for _, a in COMPUTE_OPTIMIZER_ACTIONS)
    if include_spend:
        actions.update(a for _, a in SPEND_ACTIONS)
    if _get_metric_data_in_use(include_get_metric_data):
        actions.update(a for _, a in GET_METRIC_DATA_ACTIONS)
    return sorted(actions)


def iam_policy(include_spend: bool = False,
               include_get_metric_data: bool | None = None) -> dict:
    """The least-privilege policy for exactly what the scanner calls."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "NableReadOnlyScan",
            "Effect": "Allow",
            "Action": iam_actions(include_spend, include_get_metric_data),
            "Resource": "*",
        }],
    }


def render_dry_run(include_spend: bool = False) -> str:
    """The human-readable manifest printed by `nable scan --dry-run`."""
    out = [
        "nable scan --dry-run",
        "Nothing below was executed. This is what a real scan would call.",
        "",
        "ALWAYS:",
    ]
    for call, action in BASE_ACTIONS:
        out.append(f"  {call:<42} {action}")
    out += ["", f"CHECKS ({len(SCAN_CHECKS)}):"]
    for name, (what, calls) in SCAN_CHECKS.items():
        out.append(f"  {name}")
        out.append(f"      {what}")
        for call, action in calls:
            out.append(f"      {call:<38} {action}")
    out += ["", "ONCE PER SCAN, free (skipped if Compute Optimizer is not enabled):"]
    for call, action in COMPUTE_OPTIMIZER_ACTIONS:
        out.append(f"  {call:<55} {action}")
    if include_spend:
        out += ["", "WITH --spend (each Cost Explorer request is billed $0.01):"]
        for call, action in SPEND_ACTIONS:
            out.append(f"  {call:<42} {action}")
    else:
        out += ["", "Cost Explorer is NOT called. `--spend` adds it, and each",
                "request is billed to your account at $0.01."]
    if _get_metric_data_in_use(None):
        out += ["", "WITH FINOPS_CLOUDWATCH_GETMETRICDATA=1 (billed $0.01 per 1,000 metrics):"]
        for call, action in GET_METRIC_DATA_ACTIONS:
            out.append(f"  {call:<42} {action}")
    else:
        out += ["", "CloudWatch is read with GetMetricStatistics, inside its free",
                "request tier. FINOPS_CLOUDWATCH_GETMETRICDATA=1 batches the reads",
                "through GetMetricData instead, billed at $0.01 per 1,000 metrics."]
    out += [
        "",
        f"{len(iam_actions(include_spend))} IAM actions, every one a "
        "Describe/List/Get. The scan changes nothing.",
        "",
        "Policy JSON:  nable scan --dry-run" + (" --spend" if include_spend else "") + " --json",
    ]
    return "\n".join(out)
