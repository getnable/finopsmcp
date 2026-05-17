# nable — Cloud Cost Estimates for Terraform

See estimated monthly AWS costs **inline** while writing Terraform — before code review, before apply.

![nable extension demo](assets/demo.gif)

## Features

- **Inline cost hints** — ghost text shows `☁ $560.24/mo · m5.4xlarge on-demand` on every resource block
- **Colour coding** — red (>$500/mo), amber ($100–500), green (<$100), grey (pay-per-use)
- **Savings tips** — orange highlight when a cheaper alternative exists (e.g. gp2 → gp3)
- **File summary CodeLens** — total cost of all resources at the top of the file
- **Hover details** — monthly, annual, detail breakdown + savings tip on hover
- **Works offline** — all pricing data embedded, zero API calls, zero latency

## Supported resources

| Resource | Priced by |
|---|---|
| `aws_instance` | instance_type (70+ types) |
| `aws_db_instance` / `aws_rds_cluster_instance` | instance_class + Multi-AZ |
| `aws_elasticache_cluster` | node_type × num_cache_nodes |
| `aws_ebs_volume` | size_gb × volume_type (gp2/gp3/io1/io2/st1/sc1) |
| `aws_nat_gateway` | flat $32.85/mo base |
| `aws_lb` / `aws_alb` / `aws_elb` | base rate |
| `aws_eks_cluster` | $73/mo control plane |
| `aws_ecs_task_definition` | vCPU + memory (Fargate) |
| `aws_opensearch_domain` | instance_type × instance_count |
| `aws_redshift_cluster` | node_type × number_of_nodes |
| `aws_msk_cluster` | instance_type × broker count |
| `aws_kinesis_stream` | shard_count |
| `aws_lambda_function` | memory_size (shows per-invocation note) |
| `aws_s3_bucket` | pay-per-use note |
| `aws_cloudwatch_metric_alarm` | $0.10/alarm-month |

## Settings

| Setting | Default | Description |
|---|---|---|
| `nable.enabled` | `true` | Toggle all decorations |
| `nable.showAnnual` | `false` | Also show annual cost |
| `nable.minCostToShow` | `1` | Hide estimates below $X/mo |
| `nable.region` | `us-east-1` | Pricing region |

## Commands

- **nable: Refresh Cost Estimates** — force refresh decorations
- **nable: Show File Cost Summary** — output panel with all resources + total

## Install

Search for `nable` in the VS Code Extensions panel, or:

```
ext install nable.nable-finops
```

## Part of nable finops

This extension is part of [nable](https://github.com/chaandannn/finopsmcp) — ask Claude about your cloud costs.

```bash
pip install finops-mcp
finops setup aws
```

---

Prices: AWS on-demand, us-east-1. Not affiliated with AWS.
