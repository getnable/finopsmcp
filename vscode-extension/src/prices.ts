/**
 * Embedded AWS pricing table — on-demand us-east-1, May 2026.
 * No API call needed: works fully offline, zero latency.
 *
 * Structure: resourceType → { key_field → hourly_usd }
 * Special keys:
 *   __flat__  = flat hourly rate regardless of config
 *   __note__  = human note shown in hover (no cost)
 */

export const HOURS_PER_MONTH = 730;

export interface PriceEntry {
  monthly: number;
  detail: string;
  note?: string;
}

// ── EC2 ───────────────────────────────────────────────────────────────────────
const EC2_HOURLY: Record<string, number> = {
  "t3.nano": 0.0052,   "t3.micro": 0.0104,  "t3.small": 0.0208,
  "t3.medium": 0.0416, "t3.large": 0.0832,  "t3.xlarge": 0.1664,
  "t3.2xlarge": 0.3328,
  "t3a.nano": 0.0047,  "t3a.micro": 0.0094, "t3a.small": 0.0188,
  "t3a.medium": 0.0376,"t3a.large": 0.0752, "t3a.xlarge": 0.1504,
  "t4g.nano": 0.0042,  "t4g.micro": 0.0084, "t4g.small": 0.0168,
  "t4g.medium": 0.0336,"t4g.large": 0.0672, "t4g.xlarge": 0.1344,
  "t4g.2xlarge": 0.2688,
  "m5.large": 0.096,   "m5.xlarge": 0.192,  "m5.2xlarge": 0.384,
  "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
  "m5.16xlarge": 3.072,"m5.24xlarge": 4.608,
  "m6i.large": 0.096,  "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
  "m6i.4xlarge": 0.768,"m6i.8xlarge": 1.536,
  "m6g.large": 0.077,  "m6g.xlarge": 0.154, "m6g.2xlarge": 0.308,
  "m6g.4xlarge": 0.616,"m6g.8xlarge": 1.232,
  "m7i.large": 0.1008, "m7i.xlarge": 0.2016,"m7i.2xlarge": 0.4032,
  "m7i.4xlarge": 0.8064,
  "m7g.large": 0.0816, "m7g.xlarge": 0.1632,"m7g.2xlarge": 0.3264,
  "c5.large": 0.085,   "c5.xlarge": 0.17,   "c5.2xlarge": 0.34,
  "c5.4xlarge": 0.68,  "c5.9xlarge": 1.53,  "c5.18xlarge": 3.06,
  "c6i.large": 0.085,  "c6i.xlarge": 0.17,  "c6i.2xlarge": 0.34,
  "c6g.large": 0.068,  "c6g.xlarge": 0.136, "c6g.2xlarge": 0.272,
  "c7g.large": 0.0725, "c7g.xlarge": 0.145, "c7g.2xlarge": 0.29,
  "c7i.large": 0.08925,"c7i.xlarge": 0.1785,"c7i.2xlarge": 0.357,
  "r5.large": 0.126,   "r5.xlarge": 0.252,  "r5.2xlarge": 0.504,
  "r5.4xlarge": 1.008, "r5.8xlarge": 2.016,
  "r6i.large": 0.126,  "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
  "r6g.large": 0.1008, "r6g.xlarge": 0.2016,"r6g.2xlarge": 0.4032,
  "r7g.large": 0.1071, "r7g.xlarge": 0.2142,"r7g.2xlarge": 0.4284,
  "p3.2xlarge": 3.06,  "p3.8xlarge": 12.24, "p3.16xlarge": 24.48,
  "g4dn.xlarge": 0.526,"g4dn.2xlarge": 0.752,"g4dn.4xlarge": 1.204,
  "g5.xlarge": 1.006,  "g5.2xlarge": 1.212, "g5.4xlarge": 1.624,
  "i3.large": 0.156,   "i3.xlarge": 0.312,  "i3.2xlarge": 0.624,
  "i3.4xlarge": 1.248, "i3.8xlarge": 2.496,
};

// ── RDS ───────────────────────────────────────────────────────────────────────
const RDS_HOURLY: Record<string, number> = {
  "db.t3.micro": 0.017, "db.t3.small": 0.034,  "db.t3.medium": 0.068,
  "db.t3.large": 0.136, "db.t4g.micro": 0.016, "db.t4g.small": 0.032,
  "db.t4g.medium": 0.065,"db.t4g.large": 0.13,
  "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
  "db.m5.4xlarge": 1.368,"db.m6i.large": 0.171,"db.m6i.xlarge": 0.342,
  "db.m6g.large": 0.152,"db.m6g.xlarge": 0.304,
  "db.r5.large": 0.24,  "db.r5.xlarge": 0.48,  "db.r5.2xlarge": 0.96,
  "db.r5.4xlarge": 1.92,"db.r6i.large": 0.24,  "db.r6i.xlarge": 0.48,
  "db.r6g.large": 0.192,"db.r6g.xlarge": 0.384,"db.r7g.large": 0.204,
};

// ── ElastiCache ───────────────────────────────────────────────────────────────
const ELASTICACHE_HOURLY: Record<string, number> = {
  "cache.t3.micro": 0.017,  "cache.t3.small": 0.034,   "cache.t3.medium": 0.068,
  "cache.t4g.micro": 0.016, "cache.t4g.small": 0.032,  "cache.t4g.medium": 0.065,
  "cache.m5.large": 0.139,  "cache.m5.xlarge": 0.278,  "cache.m5.2xlarge": 0.556,
  "cache.m6g.large": 0.128, "cache.m6g.xlarge": 0.256,
  "cache.r5.large": 0.207,  "cache.r5.xlarge": 0.414,  "cache.r5.2xlarge": 0.828,
  "cache.r6g.large": 0.186, "cache.r6g.xlarge": 0.372,
};

// ── EBS ─────────────────────────────────────────────────────────────────────
const EBS_PER_GB: Record<string, number> = {
  "gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
  "st1": 0.045, "sc1": 0.025, "standard": 0.05,
};

// ── OpenSearch ───────────────────────────────────────────────────────────────
const OPENSEARCH_HOURLY: Record<string, number> = {
  "t3.small.search": 0.036, "t3.medium.search": 0.073,
  "m5.large.search": 0.142, "m5.xlarge.search": 0.285,
  "r5.large.search": 0.187, "r5.xlarge.search": 0.374,
};

// ── Redshift ──────────────────────────────────────────────────────────────────
const REDSHIFT_HOURLY: Record<string, number> = {
  "dc2.large": 0.25,   "dc2.8xlarge": 4.80,
  "ra3.xlplus": 1.086, "ra3.4xlplus": 3.26, "ra3.16xlarge": 13.04,
};

// ── MSK ───────────────────────────────────────────────────────────────────────
const MSK_HOURLY: Record<string, number> = {
  "kafka.t3.small": 0.021, "kafka.m5.large": 0.142,
  "kafka.m5.xlarge": 0.284,"kafka.m5.2xlarge": 0.568,
  "kafka.m7g.large": 0.128,"kafka.m7g.xlarge": 0.256,
};

// ── Public pricing resolver ────────────────────────────────────────────────────

export function priceResource(
  resourceType: string,
  attrs: Record<string, string>
): PriceEntry | null {
  switch (resourceType) {

    case "aws_instance": {
      const t = attrs["instance_type"];
      const h = t ? EC2_HOURLY[t] : undefined;
      if (!h) return t ? { monthly: 0, detail: `Unknown instance type: ${t}`, note: "Check AWS pricing" } : null;
      return { monthly: Math.round(h * HOURS_PER_MONTH * 100) / 100, detail: `${t} on-demand` };
    }

    case "aws_db_instance":
    case "aws_rds_cluster_instance": {
      const cls = attrs["instance_class"];
      let h = cls ? RDS_HOURLY[cls] : undefined;
      if (!h) return cls ? { monthly: 0, detail: `Unknown class: ${cls}` } : null;
      const maz = attrs["multi_az"]?.toLowerCase() === "true";
      if (maz) h *= 2;
      return {
        monthly: Math.round(h * HOURS_PER_MONTH * 100) / 100,
        detail: `${cls}${maz ? " Multi-AZ" : ""}`,
        note: maz ? "Multi-AZ doubles cost — needed for prod, wasteful in dev/staging" : undefined,
      };
    }

    case "aws_elasticache_cluster":
    case "aws_elasticache_replication_group": {
      const node = attrs["node_type"];
      const count = parseInt(attrs["num_cache_nodes"] || attrs["number_cache_clusters"] || "1", 10);
      const h = node ? ELASTICACHE_HOURLY[node] : undefined;
      if (!h) return node ? { monthly: 0, detail: `Unknown node type: ${node}` } : null;
      return {
        monthly: Math.round(h * count * HOURS_PER_MONTH * 100) / 100,
        detail: `${count}× ${node}`,
      };
    }

    case "aws_ebs_volume": {
      const volType = attrs["type"] || "gp2";
      const sizeGb = parseFloat(attrs["size"] || "0");
      const priceGb = EBS_PER_GB[volType] ?? 0.10;
      const iops = parseFloat(attrs["iops"] || "0");
      let monthly = sizeGb * priceGb;
      if ((volType === "io1" || volType === "io2") && iops) monthly += iops * 0.065;
      const note = volType === "gp2" ? "💡 Switch to gp3 to save 20% with same/better IOPS" : undefined;
      return { monthly: Math.round(monthly * 100) / 100, detail: `${sizeGb} GB ${volType}`, note };
    }

    case "aws_nat_gateway":
      return {
        monthly: Math.round(0.045 * HOURS_PER_MONTH * 100) / 100,
        detail: "$0.045/hr base",
        note: "Add VPC endpoints for S3/DynamoDB to reduce data processing charges",
      };

    case "aws_lb":
    case "aws_alb":
      return { monthly: Math.round(0.008 * HOURS_PER_MONTH * 100) / 100, detail: "$0.008/hr base + LCU" };

    case "aws_elb":
      return { monthly: Math.round(0.025 * HOURS_PER_MONTH * 100) / 100, detail: "$0.025/hr (classic ELB)" };

    case "aws_eks_cluster":
      return {
        monthly: Math.round(0.10 * HOURS_PER_MONTH * 100) / 100,
        detail: "$0.10/hr control plane only",
        note: "Node group costs are EC2 instances billed separately",
      };

    case "aws_lambda_function": {
      const mem = parseInt(attrs["memory_size"] || "128", 10);
      return {
        monthly: 0,
        detail: `${mem} MB — pay per invocation`,
        note: `$0.20/1M requests + $${(mem / 1024 * 0.0000166667).toFixed(7)}/GB-second`,
      };
    }

    case "aws_opensearch_domain":
    case "aws_elasticsearch_domain": {
      const inst = attrs["instance_type"] || "m5.large.search";
      const count = parseInt(attrs["instance_count"] || "1", 10);
      const h = OPENSEARCH_HOURLY[inst] ?? 0.142;
      return {
        monthly: Math.round(h * count * HOURS_PER_MONTH * 100) / 100,
        detail: `${count}× ${inst}`,
      };
    }

    case "aws_redshift_cluster": {
      const node = attrs["node_type"] || "dc2.large";
      const count = parseInt(attrs["number_of_nodes"] || "1", 10);
      const h = REDSHIFT_HOURLY[node] ?? 0.25;
      return {
        monthly: Math.round(h * count * HOURS_PER_MONTH * 100) / 100,
        detail: `${count}× ${node}`,
      };
    }

    case "aws_msk_cluster": {
      const broker = attrs["instance_type"] || "kafka.m5.large";
      const count = parseInt(attrs["number_of_broker_nodes"] || "3", 10);
      const h = MSK_HOURLY[broker] ?? 0.142;
      return {
        monthly: Math.round(h * count * HOURS_PER_MONTH * 100) / 100,
        detail: `${count}× ${broker} brokers`,
      };
    }

    case "aws_cloudwatch_metric_alarm":
      return { monthly: 0.10, detail: "$0.10/alarm-month" };

    case "aws_s3_bucket":
      return { monthly: 0, detail: "Pay per GB stored / requests", note: "$0.023/GB-mo standard storage" };

    case "aws_ecs_service":
    case "aws_ecs_task_definition": {
      const cpu = parseFloat(attrs["cpu"] || "256") / 1024;
      const mem = parseFloat(attrs["memory"] || "512") / 1024;
      const h = cpu * 0.04048 + mem * 0.004445;
      return {
        monthly: Math.round(h * HOURS_PER_MONTH * 100) / 100,
        detail: `${cpu.toFixed(2)} vCPU, ${mem.toFixed(2)} GB Fargate`,
      };
    }

    case "aws_sagemaker_endpoint_configuration":
      return { monthly: 0, detail: "Billed by instance type × uptime", note: "Check ml.* instance pricing" };

    case "aws_kinesis_stream": {
      const shards = parseInt(attrs["shard_count"] || "1", 10);
      const monthly = shards * 0.015 * 24 * 30; // $0.015/shard-hour
      return { monthly: Math.round(monthly * 100) / 100, detail: `${shards} shard(s) @ $0.015/shard-hr` };
    }

    case "aws_dynamodb_table":
      return { monthly: 0, detail: "Pay-per-request or provisioned capacity", note: "On-demand: $1.25/M writes, $0.25/M reads" };

    case "aws_cloudfront_distribution":
      return { monthly: 0, detail: "Pay per request + transfer", note: "~$0.0085/10K HTTPS requests" };

    case "aws_api_gateway_rest_api":
    case "aws_apigatewayv2_api":
      return { monthly: 0, detail: "Pay per call", note: "~$3.50/M API calls" };

    default:
      return null;
  }
}

export function formatMonthly(entry: PriceEntry, showAnnual: boolean): string {
  if (entry.monthly === 0) {
    return `☁ ${entry.detail}`;
  }
  const mo = `$${entry.monthly.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/mo`;
  const yr = showAnnual ? `  ($${(entry.monthly * 12).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}/yr)` : "";
  return `☁ ${mo}${yr} · ${entry.detail}`;
}
