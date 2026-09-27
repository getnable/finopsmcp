# Packs: signing and code

Most packs are data: policies, guard rules, playbooks, price books, report
templates and skills. nable validates them and nothing in them runs. Three
kinds carry code: **connectors** (a cost source, returning FOCUS rows),
**org-context adapters** (proposing owners and environments) and **action
sinks** (delivering a ticket or a PR proposal). nable never imports that code.
It runs out of process, behind a broker in the core, and gets only the data,
secrets and network hosts its manifest declares.

This page covers signing, what data packs may and may not do to the guard,
and the broker. `nable pack --help` covers the rest.

## Signing

A signature is a detached Ed25519 signature over the pack's **content digest**,
the `digest` that `nable pack validate` prints: one sha256 over every file's
path and sha256, with the signature file left out. It lives in
`nable-pack.sig`, inside the pack or next to a tarball (`<tarball>.sig`).

```
nable pack keygen --out ~/secure/org-packs.pem --name acme-platform
nable pack sign ./my-pack --key ~/secure/org-packs.pem
nable pack sign ./my-pack-1.0.0.tar.gz --key ~/secure/org-packs.pem
```

`keygen` writes the private key with mode 0600 (never over an existing file;
`--encrypt` asks for a passphrase) and prints the public key as a snippet for
the org policy. `sign` validates the pack first, and never prints or logs the
key.

nable trusts two kinds of key, and nothing else:

| Key | Where it comes from | What it lets a pack do |
|---|---|---|
| nable's first-party key | built into nable | claim `support = "first-party"` or `"verified"` |
| an org key | `packs.trusted_keys` in the org policy file | pass `packs.require_signed`; run code |

- A `first-party` or `verified` claim is honoured only when nable's first-party
  key signed the pack. Otherwise the install is refused, with or without an
  org policy.
- A signature that does not hold (a file changed after signing, or the
  signature does not verify) is refused everywhere.
- Under `packs.require_signed: true`, a pack must be signed by the first-party
  key or an org-trusted key. `--yes` stays refused under it.
- Audit, the runtime and the broker verify again from the installed files
  against today's keys. A pack that was approved with a trusted signature must
  keep one: removing its key from `trusted_keys` stops it loading, data packs
  included, until the key is restored or the pack is approved again.

The manifest's `[integrity].attestation` (a PEP 740 or Sigstore bundle
reference) is parsed and shown, and **not verified yet**. Nothing relies on it.

## The org policy

In `nable.policy.yaml` (in nable's data directory or `FINOPS_POLICY_FILE`, never
the working directory):

```yaml
packs:
  require_signed: true
  trusted_keys:
    - name: acme-platform
      key: <base64 Ed25519 public key from `nable pack keygen`>
  allow_unsigned_code:
    - io.github.acme/internal-connector@<content digest from `nable pack validate`>
```

A malformed key or pack id fails closed: every install is refused until it is
fixed.

`allow_unsigned_code` entries name exact files: `id@<content digest>` holds
only while the installed files hash to that digest, so an update needs a new
entry. A bare `id` would name whatever pack claims that namespace, which
nothing verifies for an unsigned pack; it is honoured only when
`allowed_sources` is also set, so the org has pinned where packs may come from.

The index (`<data dir>/packs/index.json`) records what was approved. Every
entry's namespace, name and version are checked when it is read, and the
capabilities and tier nable enforces are read from the installed manifest,
which the index's hashes pin. An entry's `source` is shown and matched against
`allowed_sources` as recorded: it is only as trustworthy as the index file.

## What data packs can do to the guard

**Guard rules** only tighten: allow to ask, ask to deny, never the reverse.
Their patterns (and policy `regex` conditions) are checked when the pack is
validated: a repeat inside a repeat (`(a+)+`, `(\S+\s*)*`), backreferences,
lookarounds, and possessive or atomic constructs are refused, because they can
backtrack for hours on a short command. In the guard's hook each pattern also
runs under a 50 ms timer (POSIX, main thread); a pattern that runs out of time
counts as a match that asks, with a reason that names the rule. Where no
timer is available (Windows) only the validation-time check applies.

**Price books** need `pricing = ["override"]` in `[capabilities]`, which is
shown at install, diffed on update, and limited by
`allowed_capabilities.pricing`. A price book informs the estimates nable
shows ("at your price book rate"). It never makes a change look cheaper to
anything that allows, asks or denies: the guard's thresholds, velocity cap
and budget checks, and the cost preflight's budget verdict, judge at the
higher of the list price and the book rate (for a Terraform plan, the higher
for what is added and the lower for what is removed). A book rate below list
is shown beside the list figure the guard judges by. A book rate of 0 is a
rate, not "no price".

## Running code

```
nable pack run com.example/example-csv-connector csv-costs --start 2026-09-01 --end 2026-10-01
```

Code runs only from a pack signed by the first-party key or an org-trusted
key, or one the org allowlists by id in `packs.allow_unsigned_code`. For each
call the broker:

1. re-checks the installed pack: files match what was approved, the org policy
   allows it, the signature holds;
2. copies the pack's files into a private temporary directory, hashing each
   against what was approved as it copies, and runs the pack from the copy (a
   file swapped in the packs root after the check is not the file that runs);
   starts `python -I -B` running `finops.packs.host` in a new process group,
   in a throwaway HOME, with only `PATH`, `HOME`, `LANG` and the declared
   secrets in its environment. No `FINOPS_*`, `NABLE_*` or cloud credentials
   cross;
3. speaks JSON-RPC 2.0 over stdin and stdout with a per-call timeout that
   holds even when the pack stops reading (writes to it are bound by the same
   deadline, and the process is killed when it passes), at most 64 of the
   pack's requests unanswered at once, and an output cap; stderr goes, truncated and with
   secret values redacted, to `<data dir>/packs/logs/<namespace>/<name>.log`;
4. answers `data.read` only for declared `read_data` scopes (`focus.cost`,
   `org.owners` and `org.environments` today; the other scopes say they are not
   available yet);
5. checks what comes back: FOCUS rows against nable's schema (invalid rows are
   dropped and reported), org facts as proposals whose source starts with the
   pack id, sink deliveries against the declared `act` kinds and
   `max_autonomy` (checked before the process starts).

Code must live in the pack directory, where its signature covers it. An entry
point that resolves to an installed Python distribution instead runs only for
a pack the org allowlists.

`read_cloud` is shown and approved at install but not brokered: the broker
hands a pack no cloud credentials. A connector that needs an API key declares
it in `secrets`.

### Secrets

A pack's secrets come only from its own entries in nable's vault, stored under
`pack:<namespace>/<name>:<NAME>`:

```
nable pack secret set com.example/example-csv-connector EXAMPLE_COSTS_CSV
echo "$VALUE" | nable pack secret set com.example/example-csv-connector EXAMPLE_COSTS_CSV
nable pack secret remove com.example/example-csv-connector EXAMPLE_COSTS_CSV
```

The value is read from a prompt (not echoed) or from stdin, never from the
command line. A declared secret is never read from nable's environment or its
provider keys, so declaring `AWS_SECRET_ACCESS_KEY` does not hand a pack the
keys nable itself uses. Cloud credential names are refused at validation for
every pack that is not first-party: `AWS_*`, `GOOGLE_*`, `CLOUDSDK_*`,
`AZURE_*`, `ARM_*`, `KUBECONFIG`, and anything ending in `_SECRET_ACCESS_KEY`
or `_SESSION_TOKEN`. Each either is a cloud credential or points a cloud SDK at
one; a connector that needs cloud data waits for `read_cloud`.

## What the laptop sandbox does not do

This is a seatbelt, not a security boundary.

- **Network.** On Linux, a pack that declares no network runs in its own empty
  network namespace when the kernel allows unprivileged user namespaces, and
  then reaches nothing. Otherwise, and for any pack that declares hosts, egress
  is audited, not isolated: an audit hook inside the pack's process refuses
  connections and name lookups for undeclared hosts, refuses unix sockets,
  starting programs and loading native libraries through ctypes, and reports
  every connection it sees to the log. A hook that runs inside the process it
  watches can be bypassed by a determined pack (through the garbage collector,
  or a C extension that calls `connect()` directly), and it cannot stop a
  program started through `_posixsubprocess.fork_exec`, which raises no audit
  event of its own. That is why unsigned code does not run.
- **Code.** A pack ships source only: a `__pycache__` directory or a `.pyc` or
  `.pyo` file is refused from every source, and the host never reads bytecode
  beside the source (a fresh, empty `pycache_prefix`). Native libraries
  (`.so`, `.pyd`, `.dylib`) are refused for every pack that is not
  first-party.
- **Files.** Not sandboxed. A pack runs as your user and can read what you can
  read, including, on Linux, other processes' environments under `/proc` that
  your user may read. The scrubbed environment keeps secrets out of the pack's
  own environment, not out of reach of a pack that goes looking.
- **Resources.** A call is stopped at its timeout and at its output cap. CPU
  and memory are not otherwise limited.

Where egress has to be enforced, run nable in the hosted or self-hosted
runner, where container network policy does it.

## Writing a code pack

See `examples/packs/example-csv-connector` and `finops/packs/sdk.py`. An entry
point is one function:

```python
def fetch(ctx, start, end):          # connector: FOCUS rows
def propose(ctx, context):           # adapter: org facts, always proposals
def deliver(ctx, payload):           # sink: a receipt dict
```

`ctx.secret(name)` and `ctx.read_data(scope, query)` refuse anything the
manifest does not declare. Print freely: stdout is not the protocol channel.
`sdk.Context.for_testing(...)` lets a pack's own tests call its entry points
without the broker.

## First-party packs: AI spend and org bootstrap

Two packs in `packs/`, under nable's namespace `io.github.getnable`. Each
README says what the pack reads, its secrets and its network.

| Pack | Kind | What it adds |
|---|---|---|
| `packs/ai-spend` | data | a policy that flags AI spend without `feature:` or `customer:` request tags; guard rules that make every GPU or accelerator launch ask (AWS p, g, trn, inf and dl families, GCP a2, a3, a4, g2 and TPU types, Azure N-series; tighten-only); the skill `check-ai-budget` for coding agents; a report of AI spend by vendor, model, feature and customer |
| `packs/org-bootstrap` | code: two adapters | `backstage` proposes service owners, repo path owners and teams from `catalog-info.yaml` files (no network) and, when configured, a Backstage catalog API; `github-teams` proposes teams, members and repo owners from GitHub (`api.github.com`) |

Both ship as `support = "community"` until nable's first-party key is
published: a first-party claim needs that key's signature. The org-bootstrap
code runs only when an org signs it or allowlists its digest.

Two small additions to the pack API came with them:

- The `repo.files` data scope. An adapter that declares it gets, in its
  context, each repo `nable org init` reads (a name, a label and the
  `repo_path` subject prefix, never a local path), and can ask nable for
  files by plain name (`{"names": ["catalog-info.yaml"]}`). nable walks the
  repos itself, skips `.git`, `node_modules`, vendored and build trees and
  every symlink, and caps the count and size of what it hands over.
- `nable pack report <ns/name> [<report>] [--days N]` fills an installed
  pack's report template from data nable already has. Each top-level
  placeholder name is a source in `finops/packs/reports.py` (`ai` today: AI
  spend by vendor, model and request tag), and a source runs only when the
  pack declares the `read_data` scope it reads (`focus.cost` for `ai`).
