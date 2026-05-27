# nable Recommendation Types

All recommendation types supported by nable, organized by category.
Each entry lists the MCP tool name and which providers support it.

---

## Compute

| Type | MCP Tool | Providers |
|------|----------|-----------|
| EC2 rightsizing (Compute Optimizer) | `get_rightsizing_recommendations` | AWS |
| EC2 rightsizing (CloudWatch fallback) | `get_rightsizing_recommendations` | AWS |
| Lambda memory rightsizing | `get_rightsizing_recommendations` | AWS |
| ECS Fargate CPU rightsizing | `get_ecs_rightsizing_recommendations` | AWS |
| Idle EC2 (low CPU) | `audit_aws_waste`, `list_idle_resources` | AWS |
| Stopped EC2 instances | `list_idle_resources` | AWS |
| Deep single-instance analysis | `get_instance_deep_analysis` | AWS |

## Database

| Type | MCP Tool | Providers |
|------|----------|-----------|
| RDS rightsizing (low CPU) | `get_rds_rightsizing_recommendations` | AWS |
| RDS idle instances (no connections) | `get_idle_rds_instances` | AWS |
| RDS excessive backup retention | `audit_aws_waste` | AWS |
| Databricks cluster efficiency | `get_databricks_cluster_efficiency` | Databricks |
| Databricks job cost analysis | `get_databricks_job_costs` | Databricks |

## Storage

| Type | MCP Tool | Providers |
|------|----------|-----------|
| EBS unattached volumes | `audit_aws_waste`, `list_idle_resources` | AWS |
| EBS gp2 to gp3 migration | `audit_aws_waste` | AWS |
| Old EBS snapshots | `audit_aws_waste` | AWS |
| S3 suboptimal storage class | `audit_aws_waste` | AWS |
| S3 incomplete multipart uploads | `get_s3_incomplete_multipart_uploads` | AWS |
| ECR old untagged images | `get_ecr_cleanup_recommendations` | AWS |

## Networking

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Unassociated Elastic IPs | `audit_aws_waste`, `list_idle_resources` | AWS |
| Idle NAT Gateways | `audit_aws_waste` | AWS |
| Idle ALBs / NLBs / Classic ELBs | `get_idle_load_balancers` | AWS |
| Data transfer costs (egress, inter-region, cross-AZ) | `get_data_transfer_costs` | AWS |

## Commitments and Pricing

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Savings Plan coverage gap | `get_commitment_analysis` | AWS |
| Savings Plan underutilization | `get_commitment_analysis` | AWS |
| Reserved Instance underutilization | `get_commitment_analysis`, `get_ri_waste_detail` | AWS |
| RI waste detail (CUR) | `get_ri_waste_detail` | AWS |
| Commitment coverage by tag/team | `get_commitment_coverage_by_tag` | AWS |
| Savings Plan showback by team | `get_savings_plan_showback` | AWS |
| Effective rate profile (EDP/MOSA detection) | `get_effective_rate_profile` | AWS |
| Azure reservation utilization | `get_azure_reservation_utilization` | Azure |

## Observability and Governance

| Type | MCP Tool | Providers |
|------|----------|-----------|
| CloudWatch log groups without retention | `scan_cloudwatch_waste`, `audit_aws_waste` | AWS |
| CloudTrail data events waste | `audit_aws_waste` | AWS |
| Terraform resources missing required tags | `audit_terraform_tags` | AWS, Azure, GCP |

## Kubernetes

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Namespace cost breakdown | `get_kubernetes_costs`, `get_kubernetes_namespace_breakdown` | AWS EKS, Azure AKS, GCP GKE |
| Workload over-provisioning | `get_kubernetes_costs` | AWS, Azure, GCP |
| Idle nodes | `get_kubernetes_costs`, `get_cluster_efficiency` | AWS, Azure, GCP |
| PVC storage cost | `get_kubernetes_costs` | AWS, Azure, GCP |
| Cluster efficiency score | `get_cluster_efficiency` | AWS, Azure, GCP |
| Helm release cost breakdown | `get_helm_release_costs` | AWS, Azure, GCP |
| Helm diff cost estimation (PR preview) | `estimate_helm_diff_cost` | AWS, Azure, GCP |
| Label-based cost attribution | `get_label_costs`, `get_workload_costs` | AWS, Azure, GCP |
| K8s cost trends | `get_kubernetes_cost_trends` | AWS, Azure, GCP |
| Cross-cluster comparison | `compare_kubernetes_clusters` | AWS, Azure, GCP |

## AI and LLM

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Total LLM spend by provider | `get_llm_costs` | OpenAI, Anthropic, Bedrock, Azure OpenAI, Vertex AI |
| Cost breakdown by model | `get_llm_cost_by_model` | OpenAI, Anthropic, Bedrock |
| Model switching recommendations | `get_llm_cost_by_model` | OpenAI, Anthropic, Bedrock |
| Cost per unit (per request, per user) | `get_llm_unit_economics` | All LLM providers |
| Full LLM unit economics with team benchmarks | `get_llm_unit_economics_full` | All LLM providers |
| Langfuse model costs and token usage | `get_langfuse_model_costs` | Langfuse |
| Langfuse trace volume trends | `get_langfuse_trace_volume` | Langfuse |
| Databricks DBU breakdown | `get_databricks_dbu_breakdown` | Databricks |
| Databricks costs | `get_databricks_costs` | Databricks |

## Anomaly Detection

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Statistical cost spikes (z-score) | `get_anomalies` | AWS, Azure, GCP, all SaaS |
| Account-level anomalies (multi-account) | `get_account_anomalies` | AWS |
| Cost spike acknowledgment | `acknowledge_anomaly` | All |
| Custom alert policies (mute, thresholds) | `set_alert_policy` | All |

## Waste Pattern Scanning

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Full AWS deep audit (10+ check types) | `audit_aws_waste` | AWS |
| Pattern-based waste fingerprints (13 patterns) | `scan_waste_patterns` | AWS |
| Idle resource detection | `list_idle_resources` | AWS |
| Idle resource cleanup | `cleanup_idle_resources` | AWS |

## Cost Attribution and Reporting

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Cost by team (tag attribution) | `get_costs_by_team` | AWS, Azure, GCP |
| Team scorecards | `get_team_scorecards`, `get_efficiency_scorecard` | AWS, Azure, GCP |
| Per-resource cost breakdown (CUR/Athena) | `get_resource_cost_breakdown_aws` | AWS |
| Per-resource Azure cost detail | `get_resource_cost_breakdown_azure` | Azure |
| Tag cost breakdown (CUR) | `get_tag_cost_breakdown_cur` | AWS |
| Org-wide multi-account rollup | `get_org_cost_summary` | AWS |
| OU cost breakdown | `get_ou_cost_breakdown` | AWS |

## Forecasting and Benchmarking

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Cost forecast (Holt-Winters time-series) | `forecast_costs` | AWS |
| Peer group benchmarking | `benchmark_costs` | AWS |
| Unit economics (cost per customer, % MRR) | `get_unit_economics` | All |
| Business KPIs | `get_ai_kpis`, `get_business_metrics` | All |
| Cost change explanation | `explain_cost_change` | All |

## Savings Tracking

| Type | MCP Tool | Providers |
|------|----------|-----------|
| Open recommendations dashboard | `list_savings_recommendations` | All |
| Mark recommendation acted on | `mark_recommendation_acted_on` | All |
| Dismiss recommendation | `dismiss_recommendation` | All |
| Verify realized savings | `verify_savings` | AWS |
| Savings summary (open/acted/verified) | `get_savings_summary` | All |

---

## Provider Summary

| Provider | Categories Covered |
|----------|--------------------|
| AWS | Compute, Database, Storage, Networking, Commitments, Observability, Kubernetes, AI/LLM, Anomaly |
| Azure | Commitments (reservations), Resource costs, Kubernetes |
| GCP | Kubernetes, Cost queries |
| Databricks | Compute efficiency, Job costs, DBU breakdown |
| Langfuse | LLM model costs, Trace volume |
| OpenAI / Anthropic / Bedrock / Vertex AI | LLM spend by model |
| Snowflake, Datadog, GitHub, Stripe, Twilio, Vercel, Cloudflare, PagerDuty, New Relic, MongoDB Atlas | Cost queries, anomaly detection |
