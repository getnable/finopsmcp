"""Which MCP tool calls mutate infrastructure, kept as data.

The shell guard only ever saw the Bash tool. An agent with the HashiCorp
Terraform MCP server, the AWS API MCP server or a Kubernetes MCP server
connected can destroy a workspace, terminate instances or delete a deployment
without touching Bash at all, and the guard was never consulted. This table
turns those tool calls into the shell command they amount to, so they go
through the same classifier, the same prices and the same policy as the
command an agent would have typed.

Matching rules, and why:

  - Claude Code names an MCP tool `mcp__<server>__<tool>`, where <server> is
    whatever key the user gave the server in their own config. The same
    HashiCorp server is "terraform" on one machine and "tfc" on the next, so
    rules match the TOOL part only, exactly and case-sensitively.
  - Some tool names are generic enough to exist on unrelated servers
    (create_run, delete_resource). Those rules also require the argument shape
    only the intended server sends (a workspace_name, an "AWS::" resource
    type), so an unrelated server's tool of the same name passes through.
  - A tool that matches no rule yields no actions and the guard says nothing.
    Asking about MCP tools nable does not understand would be friction with no
    information in it, and friction is how a guard gets uninstalled.
  - Read-shaped calls on a recognised tool (plan_only runs, `read`
    operations, a scale with no replica count) also yield nothing.

Every name below was read from the server's own source or published schema;
`source` on each rule says where, so a renamed tool can be re-checked.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

ONE_WAY_DELETE = ("one_way", "delete_resource")
TWO_WAY_APPLY = ("two_way", "infra_apply")


@dataclass(frozen=True)
class McpAction:
    """One infrastructure action an MCP tool call amounts to.

    command  the shell form, fed to the same classifier and price table as a
             Bash command (so `aws ec2 run-instances ...` from call_aws is
             priced exactly as if typed)
    summary  what the call does, as a verb phrase ("delete HCP Terraform
             workspace net"); "" when the shell form says it best
    hit      a fixed (door, action_type) for calls with no faithful shell form;
             None means "classify `command` like any shell command"
    """
    command: str
    summary: str = ""
    hit: tuple[str, str] | None = None


@dataclass(frozen=True)
class McpRule:
    names: tuple[str, ...]
    translate: Callable[[dict[str, Any]], list[McpAction]]
    source: str
    requires: tuple[str, ...] = ()
    family: str = field(default="")


# ── argument helpers ──────────────────────────────────────────────────────────

def _s(args: dict[str, Any], key: str) -> str:
    """A scalar argument as a string, "" when absent or not a scalar."""
    v = args.get(key)
    if isinstance(v, bool):
        return "true" if v else ""
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    return ""


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def _ns(args: dict[str, Any]) -> str:
    ns = _s(args, "namespace")
    return f"-n {ns}" if ns else ""


def _kebab(op: str) -> str:
    """`TerminateInstances` / `terminate_instances` -> `terminate-instances`,
    the CLI spelling the classifier and price table are written against."""
    op = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", op.strip())
    return op.replace("_", "-").lower()


def _cli_flags(params: Any) -> str:
    """AWS CLI flags from a use_aws `parameters` object."""
    if not isinstance(params, dict):
        return ""
    out = []
    for k, v in params.items():
        flag = "--" + str(k).lstrip("-")
        if v is True:
            out.append(flag)
        elif v is False or v is None:
            continue
        elif isinstance(v, list):
            out.append(_join(flag, *(json.dumps(x) if isinstance(x, (dict, list)) else str(x)
                                     for x in v)))
        elif isinstance(v, dict):
            out.append(f"{flag} {json.dumps(v, separators=(',', ':'))}")
        else:
            out.append(f"{flag} {v}")
    return " ".join(out)


# ── translations ──────────────────────────────────────────────────────────────

def _aws_cli(args: dict[str, Any]) -> list[McpAction]:
    """call_aws takes one CLI command or a batch of them in `cli_command`. The
    managed AWS MCP Server's schema is not published in a form we could read,
    so any string argument that is itself an `aws ...` command is taken too:
    over-reading an argument costs a question, missing a terminate does not."""
    raw = args.get("cli_command")
    cmds = raw if isinstance(raw, list) else [raw]
    found = [c for c in cmds if isinstance(c, str) and c.strip()]
    if not found:
        for v in args.values():
            for c in (v if isinstance(v, list) else [v]):
                if isinstance(c, str) and c.lstrip().startswith("aws "):
                    found.append(c)
    return [McpAction(c.strip()) for c in found]


def _use_aws(args: dict[str, Any]) -> list[McpAction]:
    svc, op = _s(args, "service_name"), _s(args, "operation_name")
    if not (svc and op):
        return []
    cmd = _join("aws", svc, _kebab(op), _cli_flags(args.get("parameters")))
    if _s(args, "region"):
        cmd += f" --region {_s(args, 'region')}"
    return [McpAction(cmd)]


def _tfc_create_run(args: dict[str, Any]) -> list[McpAction]:
    ws = _s(args, "workspace_name")
    run_type = _s(args, "run_type") or "plan_and_apply"   # the server's default
    where = f"HCP Terraform workspace {ws}"
    if run_type == "is_destroy":
        return [McpAction("terraform destroy", f"start a destroy run on {where}")]
    if run_type in ("plan_only", "refresh_state"):
        return []
    return [McpAction("terraform apply", f"start a {run_type} run on {where}")]


def _tfc_action_run(args: dict[str, Any]) -> list[McpAction]:
    if _s(args, "run_action") != "apply":
        return []                      # discard / cancel stop a change, not make one
    return [McpAction("terraform apply", f"apply HCP Terraform run {_s(args, 'run_id')}")]


def _tfc_delete_workspace(args: dict[str, Any]) -> list[McpAction]:
    ws = _s(args, "workspace_name")
    return [McpAction(f"terraform workspace delete {ws}",
                      f"delete HCP Terraform workspace {ws}", ONE_WAY_DELETE)]


def _tf_execute(tool: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        verb = _s(args, "command")
        where = _s(args, "working_directory")
        if tool == "terragrunt" and verb == "run-all":
            verb = "run-all apply"     # what the server actually runs for run-all
        if verb not in ("apply", "destroy", "run-all apply"):
            return []
        cmd = f"{tool} {verb}"
        return [McpAction(cmd, _join(f"run `{cmd}`", f"in {where}" if where else ""))]
    return translate


def _k8s_delete(kind_key: str, fixed_kind: str = "") -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        kind = fixed_kind or _s(args, kind_key)
        return [McpAction(_join("kubectl delete", kind, _s(args, "name"), _ns(args)))]
    return translate


def _k8s_scale(kind_key: str, replicas_key: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        replicas = _s(args, replicas_key)
        if not replicas:
            return []                  # containers' resources_scale reads when no scale is given
        target = f"{_s(args, kind_key)}/{_s(args, 'name')}".strip("/")
        return [McpAction(_join("kubectl scale", target, f"--replicas={replicas}", _ns(args)))]
    return translate


def _k8s_apply(what: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        src = _s(args, "filename") or _s(args, "yaml_path")
        cmd = _join("kubectl apply", f"-f {src}" if src else "", _ns(args))
        return [McpAction(cmd, what)]
    return translate


def _k8s_mutate(verb: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    """kubectl verbs the shell classifier has no pattern for (create, patch):
    still a reversible change, so they carry the two-way hit directly."""
    def translate(args: dict[str, Any]) -> list[McpAction]:
        cmd = _join("kubectl", verb, _s(args, "resourceType"), _s(args, "name"), _ns(args))
        return [McpAction(cmd, hit=TWO_WAY_APPLY)]
    return translate


def _k8s_rollout(args: dict[str, Any]) -> list[McpAction]:
    sub = _s(args, "subCommand") or "status"
    if sub in ("status", "history"):
        return []
    cmd = _join("kubectl rollout", sub, f"{_s(args, 'resourceType')}/{_s(args, 'name')}", _ns(args))
    return [McpAction(cmd, hit=TWO_WAY_APPLY)]


def _k8s_generic(args: dict[str, Any]) -> list[McpAction]:
    extra = args.get("args")
    tail = " ".join(str(a) for a in extra) if isinstance(extra, list) else ""
    return [McpAction(_join("kubectl", _s(args, "command"), _s(args, "subCommand"),
                            _s(args, "resourceType"), _s(args, "name"), tail, _ns(args)))]


def _helm(verb: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        return [McpAction(_join("helm", verb, _s(args, "name"),
                                "" if verb == "uninstall" else _s(args, "chart"), _ns(args)))]
    return translate


def _eks_resource(args: dict[str, Any]) -> list[McpAction]:
    op = _s(args, "operation")
    kind, name = _s(args, "kind"), _s(args, "name")
    where = _join(kind, name, _ns(args), f"on EKS cluster {_s(args, 'cluster_name')}")
    if op == "delete":
        return [McpAction(_join("kubectl delete", kind, name, _ns(args)), f"delete {where}")]
    if op in ("create", "replace", "patch"):
        return [McpAction(_join("kubectl", op, kind, name, _ns(args)), f"{op} {where}",
                          TWO_WAY_APPLY)]
    return []


def _eks_stack(args: dict[str, Any]) -> list[McpAction]:
    op, cluster = _s(args, "operation"), _s(args, "cluster_name")
    if op == "delete":
        return [McpAction(f"aws cloudformation delete-stack ({cluster})",
                          f"delete the CloudFormation stack for EKS cluster {cluster}",
                          ONE_WAY_DELETE)]
    if op == "deploy":
        return [McpAction(f"aws cloudformation deploy ({cluster})",
                          f"deploy the CloudFormation stack for EKS cluster {cluster}",
                          TWO_WAY_APPLY)]
    return []


def _ccapi(verb: str) -> Callable[[dict[str, Any]], list[McpAction]]:
    def translate(args: dict[str, Any]) -> list[McpAction]:
        rtype = _s(args, "resource_type")
        if not rtype.startswith("AWS::"):
            return []                  # not Cloud Control's argument shape: not ours to judge
        target = _join(rtype, _s(args, "identifier"))
        hit = ONE_WAY_DELETE if verb == "delete" else TWO_WAY_APPLY
        return [McpAction(f"cloudcontrol {verb} {target}",
                          f"{verb} {target} through the Cloud Control API", hit)]
    return translate


# ── the table ─────────────────────────────────────────────────────────────────

_AWS_API = "awslabs/mcp aws-api-mcp-server 1.5.5 (PyPI), server.py: call_aws(cli_command)"
_AWS_MANAGED = ("awslabs/mcp src/aws-api-mcp-server/MIGRATION.md: the AWS MCP Server "
                "replaces call_aws with aws___call_aws")
_Q_CLI = ("aws/amazon-q-developer-cli crates/chat-cli/src/cli/chat/tools/tool_index.json: "
          "use_aws(service_name, operation_name, parameters, region)")
_HASHI = "hashicorp/terraform-mcp-server pkg/tools/factories.go and pkg/tools/tfe/{}.go"
_AWS_TF = ("awslabs.terraform-mcp-server 1.0.18 (PyPI, deprecated for HashiCorp's), "
           "server.py: {}(command, working_directory)")
_FLUX = "mcp-server-kubernetes 4.1.7 (npm, Flux159), dist/tools/{}.js"
_CONTAINERS = "containers/kubernetes-mcp-server README.md tool reference"
_EKS = "awslabs.eks-mcp-server 0.2.1 (PyPI), {}"
_CCAPI = "awslabs.ccapi-mcp-server 1.0.18 (PyPI), server.py: {}_resource(resource_type, ...)"

MCP_RULES: tuple[McpRule, ...] = (
    # AWS: the tool carries a CLI command; the shell classifier reads it as-is.
    McpRule(("call_aws",), _aws_cli, _AWS_API, family="aws"),
    McpRule(("aws___call_aws",), _aws_cli, _AWS_MANAGED, family="aws"),
    McpRule(("use_aws",), _use_aws, _Q_CLI, ("service_name", "operation_name"), family="aws"),
    McpRule(("create_resource",), _ccapi("create"), _CCAPI.format("create"),
            ("resource_type",), family="aws"),
    McpRule(("update_resource",), _ccapi("update"), _CCAPI.format("update"),
            ("resource_type",), family="aws"),
    McpRule(("delete_resource",), _ccapi("delete"), _CCAPI.format("delete"),
            ("resource_type",), family="aws"),

    # Terraform: HashiCorp's HCP Terraform / TFE tools, and the deprecated
    # awslabs server that runs terraform and terragrunt locally.
    McpRule(("create_run",), _tfc_create_run, _HASHI.format("create_run"),
            ("workspace_name",), family="terraform"),
    McpRule(("action_run",), _tfc_action_run, _HASHI.format("action_run"),
            ("run_id", "run_action"), family="terraform"),
    McpRule(("delete_workspace_safely",), _tfc_delete_workspace,
            _HASHI.format("delete_workspace_safely"), ("workspace_name",), family="terraform"),
    McpRule(("ExecuteTerraformCommand",), _tf_execute("terraform"),
            _AWS_TF.format("ExecuteTerraformCommand"), ("command",), family="terraform"),
    McpRule(("ExecuteTerragruntCommand",), _tf_execute("terragrunt"),
            _AWS_TF.format("ExecuteTerragruntCommand"), ("command",), family="terraform"),

    # Kubernetes: Flux159's kubectl-shaped server.
    McpRule(("kubectl_delete",), _k8s_delete("resourceType"), _FLUX.format("kubectl-delete"),
            family="kubernetes"),
    McpRule(("kubectl_apply",), _k8s_apply(""), _FLUX.format("kubectl-apply"),
            family="kubernetes"),
    McpRule(("kubectl_scale",), _k8s_scale("resourceType", "replicas"),
            _FLUX.format("kubectl-scale"), ("name",), family="kubernetes"),
    McpRule(("kubectl_create",), _k8s_mutate("create"), _FLUX.format("kubectl-create"),
            family="kubernetes"),
    McpRule(("kubectl_patch",), _k8s_mutate("patch"), _FLUX.format("kubectl-patch"),
            family="kubernetes"),
    McpRule(("kubectl_rollout",), _k8s_rollout, _FLUX.format("kubectl-rollout"),
            family="kubernetes"),
    McpRule(("kubectl_generic",), _k8s_generic, _FLUX.format("kubectl-generic"),
            ("command",), family="kubernetes"),
    McpRule(("install_helm_chart",), _helm("install"), _FLUX.format("helm-operations"),
            family="kubernetes"),
    McpRule(("upgrade_helm_chart",), _helm("upgrade"), _FLUX.format("helm-operations"),
            family="kubernetes"),
    McpRule(("uninstall_helm_chart",), _helm("uninstall"), _FLUX.format("helm-operations"),
            family="kubernetes"),

    # Kubernetes: the containers (Red Hat) server's resource-shaped tools.
    McpRule(("resources_delete",), _k8s_delete("kind"), _CONTAINERS, ("kind", "name"),
            family="kubernetes"),
    McpRule(("pods_delete",), _k8s_delete("", fixed_kind="pod"), _CONTAINERS, ("name",),
            family="kubernetes"),
    McpRule(("resources_create_or_update",), _k8s_apply("create or update a resource"),
            _CONTAINERS, ("resource",), family="kubernetes"),
    McpRule(("resources_scale",), _k8s_scale("kind", "scale"), _CONTAINERS, ("kind", "name"),
            family="kubernetes"),
    McpRule(("helm_install",), _helm("install"), _CONTAINERS, ("chart",), family="kubernetes"),
    McpRule(("helm_uninstall",), _helm("uninstall"), _CONTAINERS, ("name",), family="kubernetes"),

    # Kubernetes on EKS: awslabs' operation-shaped tools.
    McpRule(("manage_k8s_resource",), _eks_resource, _EKS.format("k8s_handler.py"),
            ("operation", "kind"), family="kubernetes"),
    McpRule(("apply_yaml",), _k8s_apply("apply a manifest to an EKS cluster"),
            _EKS.format("k8s_handler.py"), ("yaml_path",), family="kubernetes"),
    McpRule(("manage_eks_stacks",), _eks_stack, _EKS.format("eks_stack_handler.py"),
            ("operation",), family="kubernetes"),
)

_BY_NAME: dict[str, McpRule] = {n: r for r in MCP_RULES for n in r.names}


def split_tool_name(tool_name: str) -> tuple[str, str] | None:
    """`mcp__<server>__<tool>` -> (server, tool) for a tool this table knows.

    Tried at every `__` rather than the first: the managed AWS server's tool is
    itself `aws___call_aws`, and a server key may contain `__` too. None when
    the name is not an MCP tool or no split names a known tool."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[5:]
    for m in re.finditer("__", rest):
        tool = rest[m.end():]
        if tool in _BY_NAME:
            return rest[:m.start()], tool
    return None


def translate(tool_name: str, arguments: Any) -> list[McpAction]:
    """The infrastructure actions an MCP tool call amounts to; [] for anything
    this table does not recognise, including a known name with the wrong
    argument shape."""
    split = split_tool_name(tool_name)
    if split is None or not isinstance(arguments, dict):
        return []
    rule = _BY_NAME[split[1]]
    if any(k not in arguments for k in rule.requires):
        return []
    try:
        return [a for a in rule.translate(arguments) if a.command]
    except Exception:
        return []                      # a malformed argument is not a reason to ask


def argument_text(arguments: Any, limit: int = 4000) -> str:
    """Every scalar argument value, flattened, for production-context detection
    (a namespace, workspace or cluster called prod counts, as a --profile prod
    does on the shell)."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if sum(len(s) for s in out) > limit:
            return
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out.append(str(v))

    walk(arguments)
    return " ".join(out)[:limit]
