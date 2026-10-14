"""The guard against a corpus of commands an advanced engineer's agent runs.

Each entry is a realistic command (or MCP call) with the verdict an expert
would expect from a cost guard under the default policy:

  allow   silent, or a warn that lets it run (read-only, or cheap and
          reversible, or a change the guard cannot price)
  ask$    an ask carrying a dollar figure over the auto threshold
  door    a one-way door (a delete, a destroy, a commitment): always asks
  ask     another ask the policy calls for (a production context, a saved
          plan nobody could read)
  deny    denied by policy

"allow|door" accepts either: the expert answer depends on something the
command does not say (a bare pod or one a controller recreates).

Priced entries also carry the figure the guard must show, from the AWS Price
List (AmazonEC2 and AmazonRDS us-east-1 offer files, version
20261012193925 / 20261006), with the SKU it came from, so a price table
that drifts or a pricer that miscounts fails here by name.

The corpus is the guard's false-positive and false-negative budget: an ask
on read-only work is a false alarm an expert learns to click through, and a
priced launch or a destroy that passes silently is the miss the guard exists
to prevent. Add to it, never loosen it.
"""
from __future__ import annotations

import pytest

from finops import guard

H = 730.0   # hours in AWS's pricing month

# (command, expected, priced monthly figure or None, Price List SKU or note)
CORPUS: list[tuple[str, str, float | None, str]] = [
    # ── terraform and tofu ────────────────────────────────────────────────
    ("terraform init -upgrade", "allow", None, ""),
    ("terraform validate", "allow", None, ""),
    ("terraform fmt -recursive", "allow", None, ""),
    ("terraform plan", "allow", None, ""),
    ("terraform plan -out=tfplan", "allow", None, ""),
    ("terraform plan -destroy -out=destroy.tfplan", "allow", None, "a plan, not a destroy"),
    ("terraform show -json tfplan | jq '.resource_changes[] | .change.actions'", "allow", None, ""),
    ("terraform state list", "allow", None, ""),
    ("terraform state show aws_instance.web", "allow", None, ""),
    ("terraform output -json", "allow", None, ""),
    ("terraform workspace list", "allow", None, ""),
    ("terraform workspace select staging", "allow", None, ""),
    ("terraform workspace new feature-x", "allow", None, ""),
    ("terraform workspace delete feature-x", "door", None, "orphans what it managed"),
    ("terraform apply", "allow", None, "unpriced: no saved plan"),
    ("terraform apply -auto-approve", "allow", None, ""),
    ("terraform apply -target=module.vpc -auto-approve", "allow", None, ""),
    ("terraform apply -var-file=staging.tfvars -auto-approve", "allow", None, ""),
    ("terraform apply -refresh-only -auto-approve", "allow", None, ""),
    ("terraform apply -replace=aws_instance.web -auto-approve", "allow", None,
     "replacements are routine"),
    ("terraform -chdir=infra/staging apply -auto-approve", "allow", None, ""),
    ("terraform -chdir=infra/prod apply -auto-approve", "ask", None, "production context"),
    ("terraform apply tfplan", "ask", None, "a saved plan the guard cannot read"),
    ("terraform destroy", "door", None, ""),
    ("terraform destroy -auto-approve", "door", None, ""),
    ("terraform destroy -target=aws_instance.bastion -auto-approve", "door", None, ""),
    ("terraform -chdir=infra/dev destroy -auto-approve", "door", None, ""),
    ("terraform apply -destroy -auto-approve", "door", None, ""),
    ('TF_CLI_ARGS_apply="-destroy" terraform apply -auto-approve', "door", None, ""),
    ("terraform state rm aws_instance.legacy", "allow|door", None,
     "orphans one resource; terraform import undoes it"),
    ("terraform state mv aws_instance.a aws_instance.b", "allow", None, ""),
    ("terraform import aws_s3_bucket.logs my-logs-bucket", "allow", None, ""),
    ("terraform taint aws_instance.web", "allow", None, ""),
    ("terraform force-unlock -force 6f1c2d3e", "allow", None, ""),
    ("terraform providers lock -platform=linux_amd64 -platform=darwin_arm64", "allow", None, ""),
    ("terraform graph | dot -Tpng > graph.png", "allow", None, ""),
    ("terraform refresh", "allow", None, ""),
    ("cd infra/networking && terraform init && terraform plan", "allow", None, ""),
    ("cd infra && terraform destroy -target=module.bastion", "door", None, ""),
    ("tofu init", "allow", None, ""),
    ("tofu plan -out plan.bin", "allow", None, ""),
    ("tofu apply -auto-approve", "allow", None, ""),
    ("tofu destroy -auto-approve", "door", None, ""),
    ("tofu workspace delete old", "door", None, ""),

    # ── terragrunt, pulumi, cdk, sam ──────────────────────────────────────
    ("terragrunt plan", "allow", None, ""),
    ("terragrunt run-all plan", "allow", None, ""),
    ("terragrunt run-all apply --terragrunt-non-interactive", "allow", None, ""),
    ("terragrunt apply", "allow", None, ""),
    ("terragrunt hclfmt", "allow", None, ""),
    ("terragrunt output -json", "allow", None, ""),
    ("terragrunt run-all destroy", "door", None, ""),
    ("terragrunt destroy -auto-approve", "door", None, ""),
    ("terragrunt run --all destroy", "door", None, ""),
    ("terragrunt destroy-all", "door", None, ""),
    ("pulumi preview", "allow", None, ""),
    ("pulumi preview --diff -s dev", "allow", None, ""),
    ("pulumi up --yes -s dev", "allow", None, ""),
    ("pulumi up --yes --stack prod", "ask", None,
     "a production context, as `terraform apply` in prod asks"),
    ("pulumi refresh --yes", "allow", None, ""),
    ("pulumi stack ls", "allow", None, ""),
    ("pulumi stack select prod", "allow", None, "selects, changes nothing"),
    ("pulumi config set aws:region us-west-2", "allow", None, ""),
    ("pulumi destroy --yes", "door", None, ""),
    ("pulumi destroy --yes -s dev", "door", None, ""),
    ("pulumi down -y", "door", None, ""),
    ("npx cdk synth", "allow", None, ""),
    ("cdk diff", "allow", None, ""),
    ("npx cdk deploy --all --require-approval never", "allow", None, ""),
    ("npx cdk deploy app-prod --require-approval never", "ask", None, "production context"),
    ("npx cdk destroy --force", "door", None, ""),
    ("cdk destroy MyStack-dev -f", "door", None, ""),
    ("sam build && sam deploy --no-confirm-changeset --stack-name api-dev", "allow", None, ""),
    ("sam deploy --config-env prod --no-confirm-changeset", "ask", None, "production context"),
    ("sam delete --stack-name api-dev --no-prompts", "door", None, ""),
    ("sam local invoke Fn -e event.json", "allow", None, ""),
    ("sam validate --lint", "allow", None, ""),
    ("sam logs -n Fn --stack-name api --tail", "allow", None, ""),

    # ── kubectl, kustomize ────────────────────────────────────────────────
    ("kubectl get pods -A", "allow", None, ""),
    ("kubectl get deploy -n payments -o wide", "allow", None, ""),
    ("kubectl describe node ip-10-0-1-23.ec2.internal", "allow", None, ""),
    ("kubectl logs -f deploy/api -n prod --since=10m", "allow", None, "reads prod"),
    ("kubectl top pods -n prod", "allow", None, ""),
    ("kubectl get events --sort-by=.lastTimestamp -n staging", "allow", None, ""),
    ("kubectl port-forward svc/grafana 3000:80 -n monitoring", "allow", None, ""),
    ("kubectl exec -it deploy/api -n staging -- sh", "allow", None, ""),
    ("kubectl config use-context staging", "allow", None, ""),
    ("kubectl config get-contexts", "allow", None, ""),
    ("kubectl auth can-i delete pods -n prod", "allow", None,
     "a permission check: `delete` is the verb asked about"),
    ("kubectl auth can-i delete namespaces --as system:serviceaccount:ci:deployer", "allow",
     None, ""),
    ("kubectl explain deployment.spec.strategy", "allow", None, ""),
    ("kubectl diff -f k8s/", "allow", None, ""),
    ("kubectl wait --for=condition=available deploy/api --timeout=120s", "allow", None, ""),
    ("kubectl cp db/postgres-0:/tmp/dump.sql ./dump.sql", "allow", None, ""),
    ("kubectl --context prod-us-east-1 get pods", "allow", None, ""),
    ("kubectl get pods -n prod | grep -v Running", "allow", None, ""),
    ("kubectl get secret db -o jsonpath='{.data.password}' | base64 -d", "allow", None,
     "decoded to the terminal, not into a shell"),
    ("kubectl apply -f k8s/deployment.yaml", "allow", None, ""),
    ("kubectl apply -k overlays/staging", "allow", None, ""),
    ("kubectl apply -f k8s/ -n prod", "ask", None, "production context"),
    ("kubectl apply --dry-run=server -f k8s/", "allow", None, ""),
    ("kubectl apply --dry-run=client -f k8s/ -n prod", "allow", None,
     "a dry run persists nothing"),
    ("kubectl delete pod api-7d9f8-xk2p -n staging", "allow|door", None,
     "recreated by its ReplicaSet, or gone if it was a bare pod"),
    ("kubectl -n kube-system delete pod coredns-5d78c9869d-abcde", "allow|door", None, ""),
    ("kubectl delete deployment api -n staging", "door", None, ""),
    ("kubectl delete namespace feature-123", "door", None, ""),
    ("kubectl delete -f k8s/old/", "door", None, ""),
    ("kubectl delete pvc data-postgres-0 -n db", "door", None, ""),
    ("kubectl delete -f k8s/old/ --dry-run=client -o yaml", "allow", None,
     "a dry run persists nothing"),
    ("kubectl delete ns feature-123 --dry-run=server", "allow", None, ""),
    ("kubectl delete ns feature-123 --dry-run", "allow", None, "bare --dry-run is client"),
    ("kubectl delete ns feature-123 --dry-run=none", "door", None, "none is a real delete"),
    ("kubectl delete ns feature-123 --dry-run=client && kubectl delete ns feature-123", "door",
     None, "the second command is real"),
    ("kubectl scale deploy/api --replicas=10 -n staging", "allow", None, ""),
    ("kubectl scale deploy/api --replicas=0 -n prod", "ask", None, "production context"),
    ("kubectl rollout restart deploy/api -n staging", "allow", None, ""),
    ("kubectl rollout status deploy/api -n prod", "allow", None, ""),
    ("kubectl rollout undo deploy/api -n staging", "allow", None, ""),
    ("kubectl patch deploy api -n staging -p '{\"spec\":{\"replicas\":3}}'", "allow", None, ""),
    ("kubectl set image deploy/api api=repo/api:v2 -n staging", "allow", None, ""),
    ("kubectl drain ip-10-0-1-23.ec2.internal --ignore-daemonsets --delete-emptydir-data",
     "door", None, ""),
    ("kubectl drain ip-10-0-1-23.ec2.internal --ignore-daemonsets --dry-run=client", "allow",
     None, "a dry run evicts nothing"),
    ("kubectl cordon ip-10-0-1-23.ec2.internal", "allow", None, ""),
    ("kubectl uncordon ip-10-0-1-23.ec2.internal", "allow", None, ""),
    ("kubectl label nodes ip-10-0-1-23.ec2.internal disktype=ssd", "allow", None, ""),
    ("kubectl annotate deploy api -n staging owner=platform", "allow", None, ""),
    ("kubectl replace --force -f pod.yaml", "door", None, ""),
    ("kubectl replace -f deploy.yaml", "allow", None, ""),
    ("kubectl create namespace feature-x", "allow", None, ""),
    ("kubectl create job --from=cronjob/backup backup-manual -n db", "allow", None, ""),
    ("kubectl get pods -n ci -o name | grep Evicted | xargs kubectl delete -n ci", "door", None,
     ""),
    ("kubectl get pods --field-selector=status.phase=Failed -o name | xargs -r kubectl delete",
     "door", None, ""),
    ("kustomize build overlays/staging | kubectl apply -f -", "allow", None, ""),
    ("kustomize build overlays/prod | kubectl apply -f -", "ask", None, "production context"),
    ("kustomize build overlays/prod > rendered.yaml", "allow", None, "renders, applies nothing"),
    ("kubectl kustomize overlays/dev", "allow", None, ""),

    # ── helm ──────────────────────────────────────────────────────────────
    ("helm list -A", "allow", None, ""),
    ("helm status api -n staging", "allow", None, ""),
    ("helm history api -n prod", "allow", None, ""),
    ("helm get values api -n prod", "allow", None, ""),
    ("helm template api ./chart -f values-prod.yaml", "allow", None, "renders locally"),
    ("helm lint ./chart", "allow", None, ""),
    ("helm repo add bitnami https://charts.bitnami.com/bitnami && helm repo update", "allow",
     None, ""),
    ("helm dependency update ./chart", "allow", None, ""),
    ("helm search repo bitnami/postgresql", "allow", None, ""),
    ("helm install redis bitnami/redis -n cache --create-namespace", "allow", None, ""),
    ("helm upgrade --install api ./chart -n staging -f values-staging.yaml", "allow", None, ""),
    ("helm upgrade --install api ./chart -n prod -f values-prod.yaml --atomic", "ask", None,
     "production context"),
    ("helm upgrade api ./chart --dry-run --debug -n prod", "allow", None,
     "a dry run installs nothing"),
    ("helm install api ./chart --dry-run=server -n prod", "allow", None, ""),
    ("helm diff upgrade api ./chart -n prod -f values-prod.yaml", "allow", None,
     "the helm-diff plugin only shows a diff"),
    ("helm plugin install https://github.com/databus23/helm-diff", "allow", None, ""),
    ("helm plugin uninstall diff", "allow", None, "removes a local plugin, not a release"),
    ("helm rollback api 3 -n staging", "allow", None, ""),
    ("helm uninstall api -n staging", "door", None, ""),
    ("helm uninstall api -n staging --dry-run", "allow", None, "simulates the uninstall"),
    ("helm delete legacy -n tools", "door", None, ""),

    # ── eksctl ────────────────────────────────────────────────────────────
    ("eksctl get clusters", "allow", None, ""),
    ("eksctl utils describe-stacks --cluster dev", "allow", None, ""),
    ("eksctl create cluster -f cluster.yaml", "allow", None, "the config file is not read"),
    ("eksctl create nodegroup --cluster dev --name ng-2 --node-type m5.xlarge --nodes 2",
     "allow", 2 * 0.192 * H, "m5.xlarge 5G4TA8Z4MUKE6MJB"),
    ("eksctl create cluster --name train --node-type p4d.24xlarge --nodes 4", "ask$",
     (4 * 21.957642 + 0.10) * H, "p4d.24xlarge H7NGEAC6UEHNTKSJ, EKS ZYWMR684YSMFHWEU"),
    ("eksctl create nodegroup --cluster prod --name gpu --node-type g5.12xlarge --nodes 6 "
     "--nodes-max 10", "ask$", 6 * 5.672 * H, "g5.12xlarge UH59G55UJ94HZTJJ"),
    ("eksctl scale nodegroup --cluster dev --name ng-1 --nodes 50", "allow", None,
     "the node type is not in the command"),
    ("eksctl upgrade cluster --name dev --approve", "allow", None, ""),
    ("eksctl delete cluster --name dev", "door", None, ""),
    ("eksctl delete nodegroup --cluster dev --name ng-old", "door", None, ""),

    # ── aws: ec2 ──────────────────────────────────────────────────────────
    ("aws sts get-caller-identity", "allow", None, ""),
    ("aws configure list-profiles", "allow", None, ""),
    ("aws ec2 describe-instances --filters Name=instance-state-name,Values=running "
     "--query 'Reservations[].Instances[].InstanceId' --output text", "allow", None, ""),
    ("aws ec2 describe-instances --profile prod --region eu-west-1 --output table", "allow",
     None, ""),
    ("aws ec2 describe-instances --query \"Reservations[].Instances[?State.Name=='running']"
     ".[InstanceId, InstanceType]\" --output table", "allow", None, ""),
    ("aws ec2 describe-instance-types --instance-types p5.48xlarge", "allow", None, ""),
    ("aws ec2 describe-spot-price-history --instance-types p4d.24xlarge --max-items 5", "allow",
     None, ""),
    ("aws ec2 run-instances --image-id ami-0abc --instance-type t3.micro --count 1", "allow",
     0.0104 * H, "t3.micro CRAJUW7BTXFMT2UJ"),
    ("aws ec2 run-instances --image-id ami-0abc --instance-type m5.large --count 2 "
     "--subnet-id subnet-1", "allow", 2 * 0.096 * H, "m5.large 6C86BEPQVG73ZGGR"),
    ("aws ec2 run-instances --image-id ami-0abc --instance-type m5.2xlarge --count 2", "ask$",
     2 * 0.384 * H, "m5.2xlarge ZH8KU2QB7FHAJJXW"),
    ("aws ec2 run-instances --image-id ami-0abc --instance-type p4d.24xlarge --count 8", "ask$",
     8 * 21.957642 * H, "p4d.24xlarge H7NGEAC6UEHNTKSJ"),
    ("aws --profile ml --region us-east-1 ec2 run-instances --instance-type p5.48xlarge "
     "--count 2 --image-id ami-1", "ask$", 2 * 55.04 * H, "p5.48xlarge 3D4V8UAYEMB38GU2"),
    ("aws ec2 run-instances --instance-type g5.xlarge --image-id ami-1", "ask$", 1.006 * H,
     "g5.xlarge 79BRGEEZC6TARVWJ"),
    ("aws ec2 run-instances --instance-type m5.large --min-count 10 --max-count 20 "
     "--image-id ami-1", "ask$", 20 * 0.096 * H, "m5.large 6C86BEPQVG73ZGGR; the max is what is authorised"),
    ("aws ec2 run-instances --instance-type c6i.32xlarge --count 4 --image-id ami-1", "ask$",
     4 * 5.44 * H, "c6i.32xlarge WES4FF4FTRGSBS7W, scaled"),
    ("aws ec2 run-instances --instance-type m7i.48xlarge --image-id ami-1", "ask$",
     9.6768 * H, "m7i.48xlarge BQE6XQ9SJ9ZWTHDX, scaled"),
    ("aws ec2 run-instances --instance-type r6i.16xlarge --count 2 --image-id ami-1", "ask$",
     2 * 4.032 * H, "r6i.16xlarge J4EBNS2ZR7N8TB8T, scaled"),
    ("aws ec2 run-instances --dry-run --instance-type p4d.24xlarge --count 8 --image-id ami-1",
     "allow", None, "--dry-run launches nothing"),
    ("aws ec2 run-instances --launch-template LaunchTemplateName=web,Version=3 --count 4",
     "allow", None, "the type is in the template"),
    ("AWS_PROFILE=dev AWS_REGION=us-east-1 aws ec2 run-instances --instance-type p4d.24xlarge "
     "--image-id ami-1", "ask$", 21.957642 * H, "p4d.24xlarge H7NGEAC6UEHNTKSJ"),
    ("aws ec2 terminate-instances --instance-ids i-0123456789abcdef0", "door", None, ""),
    ("aws --profile prod ec2 terminate-instances --instance-ids i-1", "door", None, ""),
    ("aws ec2 --region us-west-2 terminate-instances --instance-ids i-1", "door", None, ""),
    ("aws ec2 terminate-instances --instance-ids i-1 --dry-run", "allow", None,
     "--dry-run only checks permissions"),
    ("aws ec2 terminate-instances --dry-run --instance-ids i-1 || "
     "aws ec2 terminate-instances --instance-ids i-1", "door", None, "the second one is real"),
    ("aws ec2 terminate-instances --instance-ids i-1 --no-dry-run", "door", None, ""),
    ("aws ec2 stop-instances --instance-ids i-1", "allow", None, ""),
    ("aws ec2 start-instances --instance-ids i-1", "allow", None, ""),
    ("aws ec2 modify-instance-attribute --instance-id i-1 --instance-type "
     "'{\"Value\": \"m5.4xlarge\"}'", "ask$", 0.768 * H, "m5.4xlarge DW64VZC89TS9M2P2"),
    ("aws ec2 modify-instance-attribute --instance-id i-1 --instance-type Value=t3.large",
     "allow", 0.0832 * H, "t3.large HBVJM3Q9S8K6MNPJ"),
    ("aws ec2 create-snapshot --volume-id vol-1 --description nightly", "allow", None, ""),
    ("aws ec2 create-volume --size 100 --volume-type gp3 --availability-zone us-east-1a",
     "allow", None, ""),
    ("aws ec2 delete-snapshot --snapshot-id snap-1", "door", None, ""),
    ("aws ec2 delete-volume --volume-id vol-1", "door", None, ""),
    ("aws ec2 release-address --allocation-id eipalloc-1", "door", None, ""),
    ("aws ec2 deregister-image --image-id ami-1", "door", None, ""),
    ("aws ec2 request-spot-instances --instance-count 4 --launch-specification "
     "'{\"InstanceType\":\"g5.12xlarge\",\"ImageId\":\"ami-1\"}'", "ask$", 4 * 5.672 * H,
     "on-demand ceiling for a spot request"),
    ("aws ec2 create-fleet --target-capacity-specification "
     "TotalTargetCapacity=10,DefaultTargetCapacityType=on-demand --launch-template-configs "
     "'LaunchTemplateSpecification={LaunchTemplateName=web,Version=1},"
     "Overrides=[{InstanceType=c5.large}]'", "ask$", 10 * 0.085 * H, "c5.large 6U6GZ2DN4RFCJ7D9"),
    ("aws ec2 cancel-spot-fleet-requests --spot-fleet-request-ids sfr-1 "
     "--terminate-instances", "door", None, ""),
    ("aws ec2 cancel-spot-fleet-requests --spot-fleet-request-ids sfr-1 "
     "--no-terminate-instances", "allow", None, "keeps the instances"),
    ("aws ec2 purchase-reserved-instances-offering --reserved-instances-offering-id abc "
     "--instance-count 2 --limit-price Amount=5000,CurrencyCode=USD", "door", None, ""),
    ("aws savingsplans create-savings-plan --savings-plan-offering-id x --commitment 2.5",
     "door", None, ""),
    ("aws ec2 authorize-security-group-ingress --group-id sg-1 --protocol tcp --port 443 "
     "--cidr 10.0.0.0/8", "allow", None, ""),
    ("aws ec2 create-tags --resources i-1 --tags Key=env,Value=prod", "allow", None, ""),

    # ── aws: rds ──────────────────────────────────────────────────────────
    ("aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'", "allow",
     None, ""),
    ("aws rds create-db-instance --db-instance-identifier app-dev --db-instance-class "
     "db.t3.medium --engine postgres --allocated-storage 20 --master-username app "
     "--manage-master-user-password", "allow", 0.072 * H, "db.t3.medium PostgreSQL 2V4Z46C2HCZPQCUW"),
    ("aws rds create-db-instance --db-instance-identifier analytics --db-instance-class "
     "db.r6g.2xlarge --engine postgres --multi-az --allocated-storage 500", "ask$",
     2 * 0.899 * H, "db.r6g.2xlarge PostgreSQL 7RBZ4XN9PFHXR8J8; Multi-AZ FS5J8D2WNXYZZUBS $1.798"),
    ("aws rds create-db-instance --db-instance-identifier big --db-instance-class "
     "db.r6g.8xlarge --engine mysql --allocated-storage 1000", "ask$", 3.437 * H,
     "db.r6g.8xlarge MySQL JEUU8MQD5B9QGEDC, scaled to $3.44 (+0.09%)"),
    ("aws rds create-db-instance --db-instance-identifier c1-1 --db-cluster-identifier c1 "
     "--db-instance-class db.r6g.large --engine aurora-postgresql", "allow", None,
     "Aurora rates are not in the tables"),
    ("aws rds create-db-cluster --db-cluster-identifier c1 --engine aurora-postgresql "
     "--master-username app --manage-master-user-password", "allow", None, ""),
    ("aws rds modify-db-instance --db-instance-identifier app --db-instance-class "
     "db.r6g.2xlarge --apply-immediately", "ask$", 0.899 * H,
     "PostgreSQL 7RBZ4XN9PFHXR8J8, the higher of it and MySQL CU8B2P4CA8S2V2ZW $0.859"),
    ("aws rds modify-db-instance --db-instance-identifier app --backup-retention-period 7",
     "allow", None, ""),
    ("aws rds stop-db-instance --db-instance-identifier app-dev", "allow", None, ""),
    ("aws rds reboot-db-instance --db-instance-identifier app", "allow", None, ""),
    ("aws rds create-db-snapshot --db-instance-identifier app --db-snapshot-identifier pre-up",
     "allow", None, ""),
    ("aws rds delete-db-instance --db-instance-identifier app-dev --skip-final-snapshot",
     "door", None, ""),
    ("aws rds delete-db-cluster --db-cluster-identifier c1 --final-db-snapshot-identifier fin",
     "door", None, ""),
    ("aws rds purchase-reserved-db-instances-offering --reserved-db-instances-offering-id x",
     "door", None, ""),

    # ── aws: eks, ecs ─────────────────────────────────────────────────────
    ("aws eks describe-cluster --name dev", "allow", None, ""),
    ("aws eks update-kubeconfig --name dev --region us-east-1", "allow", None, ""),
    ("aws eks create-cluster --name dev --role-arn arn:aws:iam::1:role/eks "
     "--resources-vpc-config subnetIds=subnet-a,subnet-b", "allow", None,
     "a control plane is $73/mo"),
    ("aws eks create-nodegroup --cluster-name dev --nodegroup-name gpu --instance-types "
     "g5.2xlarge --scaling-config minSize=0,maxSize=8,desiredSize=4 --subnets subnet-1 "
     "--node-role arn:aws:iam::1:role/node", "ask$", 4 * 1.212 * H, "g5.2xlarge FX9TUYTJKEENVBSR"),
    ("aws eks update-nodegroup-config --cluster-name dev --nodegroup-name ng "
     "--scaling-config desiredSize=20,maxSize=30,minSize=1", "allow", None,
     "the node type is not in the command"),
    ("aws eks delete-cluster --name dev", "door", None, ""),
    ("aws eks delete-nodegroup --cluster-name dev --nodegroup-name ng", "door", None, ""),
    ("aws ecs list-services --cluster prod", "allow", None, ""),
    ("aws ecs describe-tasks --cluster prod --tasks abc", "allow", None, ""),
    ("aws ecs update-service --cluster staging --service api --force-new-deployment", "allow",
     None, ""),
    ("aws ecs update-service --cluster staging --service api --desired-count 4", "allow", None,
     ""),
    ("aws ecs run-task --cluster staging --task-definition migrate:12 --launch-type FARGATE",
     "allow", None, ""),
    ("aws ecs delete-service --cluster staging --service old --force", "door", None, ""),
    ("aws ecs delete-cluster --cluster old", "door", None, ""),

    # ── aws: s3, s3api ────────────────────────────────────────────────────
    ("aws s3 ls", "allow", None, ""),
    ("aws s3 ls s3://my-bucket/logs/ --recursive --human-readable --summarize", "allow", None,
     ""),
    ("aws s3 cp ./build s3://my-site/ --recursive", "allow", None, ""),
    ("aws s3 sync ./dist s3://my-site --cache-control max-age=300", "allow", None, ""),
    ("aws s3 mv ./report.csv s3://bucket/reports/", "allow", None, ""),
    ("aws s3 presign s3://bucket/key --expires-in 3600", "allow", None, ""),
    ("aws s3 sync ./dist s3://my-site --delete", "door", None, ""),
    ("aws s3 sync ./dist s3://my-site --delete --dryrun", "allow", None,
     "--dryrun only lists what it would do"),
    ("aws s3 rm s3://my-bucket/tmp/ --recursive", "door", None, ""),
    ("aws s3 rm s3://my-bucket/tmp/ --recursive --dryrun", "allow", None, ""),
    ("aws s3 rb s3://old-bucket --force", "door", None, ""),
    ("aws s3 mv s3://bucket/a/ s3://other-bucket/a/ --recursive", "door", None,
     "empties the source prefix"),
    ("aws s3api list-objects-v2 --bucket b --prefix logs/ --query 'Contents[].Key'", "allow",
     None, ""),
    ("aws s3api get-bucket-policy --bucket b", "allow", None, ""),
    ("aws s3api head-object --bucket b --key k", "allow", None, ""),
    ("aws s3api put-bucket-versioning --bucket b --versioning-configuration Status=Enabled",
     "allow", None, ""),
    ("aws s3api put-bucket-lifecycle-configuration --bucket b --lifecycle-configuration "
     "file://lc.json", "allow", None, ""),
    ("aws s3api delete-object --bucket b --key tmp/x.json", "door", None, ""),
    ("aws s3api delete-bucket --bucket b", "door", None, ""),

    # ── aws: lambda, iam, autoscaling, cloudformation ─────────────────────
    ("aws lambda list-functions", "allow", None, ""),
    ("aws lambda invoke --function-name f out.json", "allow", None, ""),
    ("aws lambda update-function-code --function-name f --zip-file fileb://f.zip", "allow",
     None, ""),
    ("aws lambda update-function-configuration --function-name f --memory-size 2048", "allow",
     None, ""),
    ("aws lambda put-provisioned-concurrency-config --function-name f --qualifier live "
     "--provisioned-concurrent-executions 100", "allow", None, "memory not in the command"),
    ("aws lambda delete-function --function-name f", "door", None, ""),
    ("aws logs tail /aws/lambda/f --follow", "allow", None, ""),
    ("aws iam list-roles", "allow", None, ""),
    ("aws iam get-role --role-name deployer", "allow", None, ""),
    ("aws iam create-role --role-name x --assume-role-policy-document file://trust.json",
     "allow", None, ""),
    ("aws iam attach-role-policy --role-name x --policy-arn "
     "arn:aws:iam::aws:policy/ReadOnlyAccess", "allow", None, ""),
    ("aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::1:role/x "
     "--action-names ec2:TerminateInstances", "allow", None, ""),
    ("aws iam delete-role --role-name old", "door", None, ""),
    ("aws iam delete-access-key --access-key-id AKIAEXAMPLEEXAMPLE --user-name bob", "door",
     None, ""),
    ("aws autoscaling describe-auto-scaling-groups", "allow", None, ""),
    ("aws autoscaling set-desired-capacity --auto-scaling-group-name web --desired-capacity 10",
     "allow", None, "the instance type is in the launch template"),
    ("aws autoscaling update-auto-scaling-group --auto-scaling-group-name web --max-size 50",
     "allow", None, ""),
    ("aws autoscaling create-auto-scaling-group --auto-scaling-group-name web "
     "--launch-template LaunchTemplateName=web --min-size 2 --max-size 10 "
     "--desired-capacity 4 --vpc-zone-identifier subnet-1", "allow", None, ""),
    ("aws autoscaling delete-auto-scaling-group --auto-scaling-group-name old --force-delete",
     "door", None, ""),
    ("aws cloudformation describe-stacks --stack-name app", "allow", None, ""),
    ("aws cloudformation validate-template --template-body file://t.yaml", "allow", None, ""),
    ("aws cloudformation deploy --template-file t.yaml --stack-name app-dev "
     "--capabilities CAPABILITY_IAM", "allow", None, ""),
    ("aws cloudformation deploy --template-file t.yaml --stack-name app-prod", "ask", None,
     "production context"),
    ("aws cloudformation create-change-set --stack-name app --change-set-name cs1 "
     "--template-body file://t.yaml", "allow", None, ""),
    ("aws cloudformation wait stack-delete-complete --stack-name app-dev", "allow", None, ""),
    ("aws cloudformation delete-stack --stack-name app-dev", "door", None, ""),

    # ── aws: sagemaker, elasticache, opensearch, dynamodb, route53, misc ──
    ("aws sagemaker list-endpoints", "allow", None, ""),
    ("aws sagemaker stop-notebook-instance --notebook-instance-name nb", "allow", None, ""),
    ("aws sagemaker delete-endpoint --endpoint-name old", "door", None, ""),
    ("aws elasticache describe-cache-clusters", "allow", None, ""),
    ("aws elasticache create-cache-cluster --cache-cluster-id c1 --cache-node-type "
     "cache.t4g.micro --engine redis --num-cache-nodes 1", "allow", None, ""),
    ("aws elasticache delete-replication-group --replication-group-id cache", "door", None, ""),
    ("aws opensearch list-domain-names", "allow", None, ""),
    ("aws opensearch delete-domain --domain-name logs", "door", None, ""),
    ("aws dynamodb scan --table-name t --max-items 10", "allow", None, ""),
    ("aws dynamodb query --table-name t --key-condition-expression 'pk = :v' "
     "--expression-attribute-values '{\":v\":{\"S\":\"x\"}}'", "allow", None, ""),
    ("aws dynamodb create-table --table-name t --billing-mode PAY_PER_REQUEST "
     "--attribute-definitions AttributeName=pk,AttributeType=S "
     "--key-schema AttributeName=pk,KeyType=HASH", "allow", None, ""),
    ("aws dynamodb delete-table --table-name t", "door", None, ""),
    ("aws route53 list-hosted-zones", "allow", None, ""),
    ("aws route53 change-resource-record-sets --hosted-zone-id Z1 --change-batch "
     "file://upsert.json", "allow", None, ""),
    ("aws route53 delete-hosted-zone --id Z1", "door", None, ""),
    ("aws kms schedule-key-deletion --key-id k --pending-window-in-days 7", "door", None, ""),
    ("aws organizations close-account --account-id 123456789012", "door", None, ""),
    ("aws logs delete-log-group --log-group-name /aws/lambda/old", "door", None, ""),
    ("aws ecr batch-delete-image --repository-name r --image-ids imageTag=old", "door", None,
     ""),
    ("aws secretsmanager get-secret-value --secret-id x --query SecretString --output text",
     "allow", None, ""),
    ("aws ssm start-session --target i-1", "allow", None, ""),
    ("aws cloudtrail lookup-events --lookup-attributes "
     "AttributeKey=EventName,AttributeValue=TerminateInstances", "allow", None, ""),
    ("docker run --rm -v ~/.aws:/root/.aws amazon/aws-cli ec2 terminate-instances "
     "--instance-ids i-1", "door", None, "the AWS CLI's own image runs aws"),
    ("docker run --rm -v $PWD:/w -w /w hashicorp/terraform:1.9 destroy -auto-approve", "door",
     None, "terraform's own image runs terraform"),

    # ── gcloud, gsutil ────────────────────────────────────────────────────
    ("gcloud config list", "allow", None, ""),
    ("gcloud auth list", "allow", None, ""),
    ("gcloud compute instances list --filter=status=RUNNING", "allow", None, ""),
    ("gcloud compute instances create vm-1 --machine-type e2-standard-2 --zone us-central1-a",
     "allow", 48.91, "nable's GCE table"),
    ("gcloud compute instances create train-1 train-2 --machine-type n2-standard-32 "
     "--zone us-central1-a", "ask$", 2 * 967.78, "nable's GCE table"),
    ("gcloud compute instances stop vm-1 --zone us-central1-a", "allow", None, ""),
    ("gcloud compute instances delete vm-1 --zone us-central1-a --quiet", "door", None, ""),
    ("gcloud container clusters list", "allow", None, ""),
    ("gcloud container clusters get-credentials dev --region us-central1", "allow", None, ""),
    ("gcloud container clusters resize dev --node-pool default-pool --num-nodes 20", "allow",
     None, ""),
    ("gcloud container clusters delete dev --region us-central1 --quiet", "door", None, ""),
    ("gcloud sql instances list", "allow", None, ""),
    ("gcloud sql instances delete db1", "door", None, ""),
    ("gcloud run deploy api --image gcr.io/p/api --region us-central1", "allow", None, ""),
    ("gcloud run services delete api --region us-central1", "door", None, ""),
    ("gcloud projects delete my-proj", "door", None, ""),
    ("gcloud logging read 'resource.type=gce_instance AND severity>=ERROR' --limit 20",
     "allow", None, ""),
    ("gcloud storage rm -r gs://bucket/tmp", "door", None, ""),
    ("gsutil -m rm -r gs://bucket/tmp", "door", None, ""),
    ("gsutil ls gs://bucket", "allow", None, ""),

    # ── az ────────────────────────────────────────────────────────────────
    ("az account show", "allow", None, ""),
    ("az vm list -o table", "allow", None, ""),
    ("az vm create -g rg -n vm1 --image Ubuntu2204 --size Standard_D2s_v5", "allow", 70.08,
     "nable's Azure table"),
    ("az vm create -g rg -n vm1 --image Ubuntu2204 --size Standard_D16s_v3 --count 2", "ask$",
     2 * 560.64, "nable's Azure table"),
    ("az vm deallocate -g rg -n vm1", "allow", None, ""),
    ("az vm delete -g rg -n vm1 --yes", "door", None, ""),
    ("az aks list", "allow", None, ""),
    ("az aks get-credentials -g rg -n aks1", "allow", None, ""),
    ("az aks scale -g rg -n aks1 --node-count 5", "allow", None, ""),
    ("az aks delete -g rg -n aks1 --yes --no-wait", "door", None, ""),
    ("az sql db delete -g rg -s srv -n db1 --yes", "door", None, ""),
    ("az group list --query \"[?location=='eastus']\"", "allow", None, ""),
    ("az group delete -n rg-feature-123 --yes --no-wait", "door", None, ""),
    ("az storage blob delete-batch --account-name sa --source c", "door", None, ""),
    ("az deployment group what-if -g rg --template-file main.bicep", "allow", None, ""),
    ("az deployment group create -g rg-dev --template-file main.bicep", "allow", None, ""),

    # ── docker, git, gh, make, package managers ───────────────────────────
    ("docker ps -a", "allow", None, ""),
    ("docker build -t api:dev .", "allow", None, ""),
    ("docker compose up -d", "allow", None, ""),
    ("docker compose down -v", "allow", None, "local containers and volumes"),
    ("docker system prune -af --volumes", "allow", None, ""),
    ("docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/api:v2", "allow", None, ""),
    ("docker run --rm hashicorp/terraform:1.9 version", "allow", None, ""),
    ("git status", "allow", None, ""),
    ("git log --oneline -20", "allow", None, ""),
    ("git diff HEAD~1 -- infra/", "allow", None, ""),
    ("git commit -m 'terraform destroy the old bastion module'", "allow", None,
     "a commit message"),
    ("git commit -am \"Remove kubectl delete from the runbook\"", "allow", None, ""),
    ("git push origin feature/x", "allow", None, ""),
    ("git push --force-with-lease origin feature/x", "allow", None, ""),
    ("git push -f origin main", "allow", None, "not a cloud change"),
    ("git reset --hard origin/main", "allow", None, ""),
    ("git grep -n 'aws ec2 terminate-instances'", "allow", None, "a search"),
    ("git log --oneline -S 'terraform destroy'", "allow", None, "a search"),
    ("gh pr create --title 'Destroy legacy env' --body 'Runs terraform destroy on legacy'",
     "allow", None, ""),
    ("gh pr view 123", "allow", None, ""),
    ("gh run list --workflow deploy.yml", "allow", None, ""),
    ("grep -rn 'terraform destroy' docs/", "allow", None, ""),
    ("rg 'kubectl delete' -g '*.md'", "allow", None, ""),
    ("grep -rn 'aws s3 rm' scripts/ | wc -l", "allow", None, ""),
    ("echo 'run terraform destroy to clean up'", "allow", None, ""),
    ("make test", "allow", None, ""),
    ("make plan", "allow", None, ""),
    ("npm ci", "allow", None, ""),
    ("npm install --save-dev aws-cdk", "allow", None, ""),
    ("npx cdk bootstrap aws://123456789012/us-east-1", "allow", None, ""),
    ("pip install -r requirements.txt", "allow", None, ""),
    ("pip install boto3 awscli", "allow", None, ""),
    ("uv pip install -e .", "allow", None, ""),
    ("pytest -q tests/", "allow", None, ""),

    # ── shell compositions ────────────────────────────────────────────────
    ("terraform plan -out tfplan && terraform show tfplan", "allow", None, ""),
    ("terraform fmt -check || terraform fmt", "allow", None, ""),
    ("kubectl get ns; kubectl delete ns feature-old", "door", None, ""),
    ("kubectl get pods | grep api", "allow", None, ""),
    ("aws ec2 describe-instances --query 'Reservations[].Instances[].InstanceId' --output text"
     " | xargs -n1 aws ec2 terminate-instances --instance-ids", "door", None, ""),
    ("aws ec2 describe-instances --output text --query 'Reservations[].Instances[].InstanceId'"
     " | xargs aws ec2 describe-instance-status --instance-ids", "allow", None, ""),
    ("(cd infra/dev && terraform destroy -auto-approve)", "door", None, ""),
    ("bash -c \"terraform destroy -auto-approve\"", "door", None, ""),
    ("sh -c 'kubectl delete ns staging'", "door", None, ""),
    ("env AWS_PROFILE=prod aws ec2 terminate-instances --instance-ids i-1", "door", None, ""),
    ("sudo terraform destroy", "door", None, ""),
    ("time terraform apply -auto-approve", "allow", None, ""),
    ("nohup terraform destroy -auto-approve > destroy.log 2>&1 &", "door", None, ""),
    ("timeout 600 terraform apply -auto-approve", "allow", None, ""),
    ("watch -n5 kubectl get pods", "allow", None, ""),
    ("for id in i-1 i-2; do aws ec2 terminate-instances --instance-ids $id; done", "door",
     None, ""),
    ("aws ec2 terminate-instances --instance-ids $(aws ec2 describe-instances --filters "
     "Name=tag:env,Values=dev --query 'Reservations[].Instances[].InstanceId' --output text)",
     "door", None, ""),
    ("terraform output -raw kubeconfig > ~/.kube/dev && kubectl get nodes", "allow", None, ""),
    ("echo yes | terraform destroy", "door", None, ""),
    ("yes | terraform destroy", "door", None, ""),
    ("t\\erraform de\\stroy", "door", None, "escapes the shell removes"),
    ("\"terraform\" \"destroy\"", "door", None, ""),
    ("TERRAFORM destroy", "door", None, "case-insensitive filesystems"),
    ("alias tf=terraform; tf destroy", "door", None, ""),
    ("aws s3 ls s3://b | awk '{print $4}' | xargs -I{} aws s3 rm s3://b/{}", "door", None, ""),
    ("cat ids.txt | parallel aws ec2 terminate-instances --instance-ids {}", "door", None, ""),
    ("echo 'terraform destroy -auto-approve' > cleanup.sh && bash cleanup.sh", "door", None,
     ""),
    ("printf 'kubectl delete ns x\\n' | sh", "door", None, ""),
    ("echo ZGVzdHJveQ== | base64 -d | sh", "door", None, "the guard cannot read it"),
    ("jq -r '.resources[] | select(.type==\"aws_instance\") | .name' terraform.tfstate",
     "allow", None, ""),
    ("aws ec2 describe-instances --profile prod | jq '.Reservations[].Instances[] | "
     "{id: .InstanceId, type: .InstanceType}'", "allow", None, ""),
    ("aws ec2 run-instances --image-id ami-1 --instance-type t3.micro && "
     "aws ec2 run-instances --image-id ami-1 --instance-type p4d.24xlarge --count 8", "ask$",
     (0.0104 + 8 * 21.957642) * H, "each command priced, added up"),

    # ── a verb in another command of the line, or in a message ───────────
    ("terraform plan -no-color | grep destroy", "allow", None, "a search of the plan"),
    ('terraform plan -no-color | grep -E "destroy|replace"', "allow", None, ""),
    ("terraform show tfplan | grep -c 'will be destroyed'", "allow", None, ""),
    ("kubectl get events -A | grep -i delete", "allow", None, ""),
    ("kubectl logs deploy/api -n staging | grep -i drain", "allow", None, ""),
    ("helm history api -n staging | grep uninstall", "allow", None, ""),
    ("kubectl get pods -n staging && echo 'next: kubectl delete the failed ones'", "allow",
     None, ""),
    ('git commit -m "Remove terraform destroy step from the staging env"', "allow", None,
     "env inside the message"),
    ('git commit -m "Drop kubectl delete from the node pool runbook"', "allow", None, ""),
    ('git commit -m "Document how to source creds before terraform destroy"', "allow", None,
     ""),
    ("aws ec2 describe-instances --output json | jq '.Reservations[] | "
     "select(.Instances[].State.Name == \"terminated\")'", "allow", None, ""),
    ("kubectl delete ns x --dry-run=client -o yaml | kubectl apply -f -", "allow", None, ""),
    ("bash -c 'kubectl delete ns x --dry-run=client; kubectl delete ns x'", "door", None,
     "the nested shell runs the second"),
    ("terraform $(echo plan; echo destroy)", "door", None, "a substitution is judged whole"),

    # ── production changes through other tools ───────────────────────────
    ("helm rollback api 3 -n prod", "ask", None, "production context"),
    ("kubectl rollout undo deploy/api -n prod", "ask", None, "production context"),
    ("kubectl set image deploy/api api=repo/api:v2 -n prod", "ask", None, ""),
    ("kubectl patch deploy api -n prod -p '{\"spec\":{\"replicas\":0}}'", "ask", None, ""),
    ("eksctl create nodegroup --cluster dev --name gpu -t g5.12xlarge -M 10", "ask$",
     2 * 5.672 * H, "eksctl's default 2 nodes, g5.12xlarge UH59G55UJ94HZTJJ"),
    ("terraform state rm aws_instance.legacy && terraform import aws_instance.new i-1",
     "allow|door", None, ""),
    ("pulumi stack rm dev --yes", "allow|door", None, "refuses while the stack has resources"),
    ("docker compose -f docker-compose.prod.yml up -d", "allow", None, "local containers"),
    ("aws ec2 run-instances --instance-type t2.micro --image-id ami-1", "allow", None,
     "$8/mo; t2 is not in the tables"),
    ("aws ec2 run-instances --region sa-east-1 --instance-type c5.4xlarge --image-id ami-1",
     "ask$", 0.68 * 1.529 * H,
     "judged at sa-east-1's least premium; the Price List says $1.048/hr, S8SHPWRKY2EMQG6C"),
    ("aws ec2 run-instances --region us-west-2 --instance-type m5.large --image-id ami-1",
     "allow", 0.096 * H, "us-west-2 lists m5.large at the us-east-1 rate"),
]

# ── Known misses, kept visible ───────────────────────────────────────────────
# Priced changes the guard cannot price yet, so they pass silently: a family
# the EC2 table does not hold, and services it holds no rates for. Each is a
# strict xfail: the day one is fixed it fails here, and it moves into CORPUS.
KNOWN_GAPS: list[tuple[str, str, str]] = [
    ("aws ec2 run-instances --instance-type m8g.48xlarge --count 2 --image-id ami-1", "ask$",
     "m8g is not in the EC2 table: $8.61696/hr, UUUSQDAUET9V3BR9, ~$12,581/mo"),
    ("aws ec2 run-instances --instance-type c7a.48xlarge --image-id ami-1", "ask$",
     "c7a is not in the EC2 table: $9.85344/hr, 4594CJSUXQVZXZD2, ~$7,193/mo"),
    ("aws rds create-db-instance --db-instance-identifier a1 --db-cluster-identifier a "
     "--db-instance-class db.r6g.16xlarge --engine aurora-postgresql", "ask$",
     "Aurora rates are not in the tables"),
    ("aws rds create-db-instance --db-instance-identifier s1 --db-instance-class "
     "db.m5.4xlarge --engine sqlserver-se --license-model license-included", "ask$",
     "licence-included engines are not in the tables"),
    ("aws ec2 create-volume --size 16384 --volume-type io2 --iops 64000 "
     "--availability-zone us-east-1a", "ask$", "volume creation is not classified"),
    ("aws elasticache create-replication-group --replication-group-id c "
     "--replication-group-description x --cache-node-type cache.r6g.xlarge "
     "--num-node-groups 3 --replicas-per-node-group 2 --engine redis", "ask$",
     "no ElastiCache pricer"),
    ("aws opensearch create-domain --domain-name logs --cluster-config "
     "InstanceType=r6g.2xlarge.search,InstanceCount=6", "ask$", "no OpenSearch pricer"),
    ("aws sagemaker create-notebook-instance --notebook-instance-name nb --instance-type "
     "ml.p3.2xlarge --role-arn arn:aws:iam::1:role/sm", "ask$", "no SageMaker rates"),
    ("aws dynamodb update-table --table-name t --billing-mode PROVISIONED "
     "--provisioned-throughput ReadCapacityUnits=40000,WriteCapacityUnits=40000", "ask$",
     "no DynamoDB capacity pricer"),
    ("gcloud container clusters create ml --region us-central1 --num-nodes 4 "
     "--machine-type n2-standard-32", "ask$", "GKE cluster creation is not classified"),
    ("az aks create -g rg -n aks1 --node-count 30 --node-vm-size Standard_D16s_v3", "ask$",
     "AKS cluster creation is not classified"),
    ("k delete ns staging", "door", "an alias defined outside the command line"),
]

# ── MCP tool calls, through the servers guard_mcp knows ──────────────────────
MCP_CORPUS: list[tuple[str, dict, str]] = [
    ("mcp__aws-api__call_aws", {"cli_command": "aws ec2 describe-instances --region us-east-1"},
     "allow"),
    ("mcp__aws-api__call_aws", {"cli_command": "aws ec2 terminate-instances --instance-ids i-1"},
     "door"),
    ("mcp__aws-api__call_aws",
     {"cli_command": "aws ec2 run-instances --instance-type p4d.24xlarge --count 8 "
                     "--image-id ami-1"}, "ask$"),
    ("mcp__aws-api__call_aws",
     {"cli_command": "aws ec2 run-instances --dry-run --instance-type p4d.24xlarge --count 8 "
                     "--image-id ami-1"}, "allow"),
    ("mcp__awsq__use_aws", {"service_name": "ec2", "operation_name": "DescribeInstances",
                            "parameters": {}, "region": "us-east-1"}, "allow"),
    ("mcp__awsq__use_aws", {"service_name": "ec2", "operation_name": "TerminateInstances",
                            "parameters": {"InstanceIds": ["i-1"]}, "region": "us-east-1"},
     "door"),
    ("mcp__awsq__use_aws", {"service_name": "ec2", "operation_name": "TerminateInstances",
                            "parameters": {"InstanceIds": ["i-1"], "DryRun": True},
                            "region": "us-east-1"}, "allow"),
    ("mcp__awsq__use_aws", {"service_name": "ec2", "operation_name": "RunInstances",
                            "parameters": {"InstanceType": "p4d.24xlarge", "MaxCount": 8,
                                           "MinCount": 8, "ImageId": "ami-1"},
                            "region": "us-east-1"}, "ask$"),
    ("mcp__terraform__create_run", {"workspace_name": "net", "run_type": "is_destroy"}, "door"),
    ("mcp__terraform__create_run", {"workspace_name": "net", "run_type": "plan_only"}, "allow"),
    ("mcp__terraform__create_run", {"workspace_name": "net"}, "allow"),
    ("mcp__terraform__action_run", {"run_id": "run-1", "run_action": "apply"}, "allow"),
    ("mcp__terraform__delete_workspace_safely", {"workspace_name": "old"}, "door"),
    ("mcp__kubernetes__kubectl_get", {"resourceType": "pods", "namespace": "prod"}, "allow"),
    ("mcp__kubernetes__kubectl_delete", {"resourceType": "namespace", "name": "staging"}, "door"),
    ("mcp__kubernetes__kubectl_scale", {"name": "api", "resourceType": "deployment",
                                        "replicas": 5, "namespace": "staging"}, "allow"),
    ("mcp__kubernetes__kubectl_scale", {"name": "api", "resourceType": "deployment",
                                        "replicas": 0, "namespace": "prod"}, "ask"),
    ("mcp__kubernetes__kubectl_apply", {"filename": "k8s/api.yaml", "namespace": "staging"},
     "allow"),
    ("mcp__kubernetes__install_helm_chart", {"name": "redis", "chart": "bitnami/redis",
                                             "namespace": "cache"}, "allow"),
    ("mcp__kubernetes__uninstall_helm_chart", {"name": "redis", "namespace": "cache"}, "door"),
    ("mcp__kubernetes__kubectl_generic", {"command": "auth", "subCommand": "can-i",
                                          "args": ["delete", "pods"], "namespace": "dev"},
     "allow"),
    ("mcp__k8s__resources_delete", {"apiVersion": "v1", "kind": "Namespace", "name": "dev"},
     "door"),
    ("mcp__k8s__pods_list", {"namespace": "dev"}, "allow"),
    ("mcp__ccapi__create_resource", {"resource_type": "AWS::EC2::Instance",
                                     "desired_state": {"InstanceType": "p5.48xlarge"}}, "ask$"),
    ("mcp__ccapi__delete_resource", {"resource_type": "AWS::S3::Bucket", "identifier": "b"},
     "door"),
    ("mcp__eks__manage_k8s_resource", {"operation": "delete", "kind": "Deployment",
                                       "name": "api", "namespace": "dev",
                                       "cluster_name": "c"}, "door"),
    ("mcp__eks__manage_k8s_resource", {"operation": "read", "kind": "Deployment",
                                       "name": "api", "namespace": "dev",
                                       "cluster_name": "c"}, "allow"),
    ("mcp__shell__run_command", {"command": "terraform destroy -auto-approve"}, "door"),
    ("mcp__shell__run_command", {"command": "ls -la"}, "allow"),
    ("mcp__github__create_issue", {"title": "cleanup",
                                   "body": "never run terraform destroy on prod"}, "allow"),
]

_ENV = ("FINOPS_GUARD_STRICT", "FINOPS_GUARD_PROD_PATTERNS", "FINOPS_POLICY_MAX_AUTO_USD",
        "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_POLICY_VELOCITY_CAP_USD",
        "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_GUARD_TEAM",
        "FINOPS_GUARD_ACCOUNT", "FINOPS_POLICY_FILE", "TERRAFORM_BIN")


def label(v: dict | None) -> str:
    """The corpus vocabulary for a gate verdict."""
    if v is None or v.get("decision") in ("allow", "warn"):
        return "allow"
    if v["decision"] == "deny":
        return "deny"
    if v.get("door") == "one_way":
        return "door"
    if v.get("estimate") and v.get("rule") == "threshold":
        return "ask$"
    return "ask"


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "no-policy.yaml"))
    return tmp_path


@pytest.mark.parametrize("command,expected,monthly,note", CORPUS,
                         ids=[c[0][:80] for c in CORPUS])
def test_corpus_command(clean, command, expected, monthly, note):
    v = guard.gate_command(command, record=False, cwd=str(clean))
    got = label(v)
    assert got in expected.split("|"), (command, got, (v or {}).get("reason"))
    if got in ("ask", "door", "deny"):
        reason = v["reason"]
        assert reason.startswith("nable guard"), reason
    if monthly is not None:
        est = guard.estimate_command_monthly_cost(command, cwd=str(clean))
        assert est is not None, command
        assert est["monthly_usd"] == pytest.approx(monthly, rel=1e-3), (command, est["line"])


@pytest.mark.parametrize("tool,args,expected", MCP_CORPUS,
                         ids=[f"{t}:{list(a.values())[0]}"[:80] for t, a, _ in MCP_CORPUS])
def test_corpus_mcp_call(clean, tool, args, expected):
    v = guard.gate_mcp_call(tool, args, record=False, cwd=str(clean))
    got = label(v)
    assert got in expected.split("|"), (tool, args, got, (v or {}).get("reason"))


@pytest.mark.xfail(strict=True, reason="a known miss: see KNOWN_GAPS")
@pytest.mark.parametrize("command,expected,why", KNOWN_GAPS,
                         ids=[c[0][:80] for c in KNOWN_GAPS])
def test_known_gap(clean, command, expected, why):
    v = guard.gate_command(command, record=False, cwd=str(clean))
    assert label(v) in expected.split("|"), (command, why)


def test_corpus_size():
    """The corpus is meant to stay broad: a cut shows up here."""
    assert len(CORPUS) + len(MCP_CORPUS) >= 250
