# Org bootstrap

Two org-context adapters for `nable org init`. They read where your org
already writes down who owns what, a Backstage software catalog and GitHub
teams, and propose owner and team facts. They never confirm anything: every
fact lands as a proposal, and a person says yes or no in the week-one
interview below.

## Quickstart: the week-one interview

1. Install a signed release of the pack. Releases are signed with nable's
   first-party key, and that signature is what lets its code run: no
   allowlist entry is needed. The copy in this repository is unsigned, and
   install refuses it (see Signing below).

   ```
   nable pack install <a signed release of io.github.getnable/org-bootstrap>
   ```

2. Give the GitHub adapter its token (a credential: read from a prompt,
   not echoed, or stdin, never the command line) and its organization (a
   setting: it may be given on the command line). Both are stored in the
   pack's own vault entries:

   ```
   nable pack secret set io.github.getnable/org-bootstrap GITHUB_TOKEN
   nable pack setting set io.github.getnable/org-bootstrap GITHUB_ORG your-org
   ```

   The Backstage adapter needs nothing to read the `catalog-info.yaml` files
   in your repos.

3. Run the interview from inside a repo (add `--repo PATH` for others):

   ```
   nable org init
   ```

   nable runs its own adapters (CODEOWNERS, Terraform, AWS Organizations,
   tags, workload), then this pack's, and asks at most ten questions, most
   dollars first. It reads like this:

   ```
   adapters (they only propose; nothing here is confirmed until you say so):
     codeowners: 3 proposed (3 new)
     pack:io.github.getnable/org-bootstrap/backstage: 6 proposed (6 new)
       service:checkout is owned by team payments  (confidence 0.80)
       service:checkout-api is owned by team payments  (confidence 0.80)
       repo_path:shop//services/checkout is owned by team payments  (confidence 0.70)
     pack:io.github.getnable/org-bootstrap/github-teams: 4 proposed (4 new)
   2 question(s), most dollars first. Answering as alice@example.com.

   1. payments owns these 4 subjects: repo_path:shop//services/checkout,
      service:checkout, service:checkout-api, team payments, people alice, bob.
      From io.github.getnable/org-bootstrap backstage.
          405ab9256e  repo_path:shop//services/checkout is owned by team payments
          2497380a9c  service:checkout is owned by team payments
          0f0cf940d0  service:checkout-api is owned by team payments
          9a1bc25c4e  team payments exists, people alice, bob
        yes / no / edit, one by one [Y/n/e/q]

   2. Which team owns aws_account:111122223333? ($3,400/mo) Nothing says yet.
        Team (blank to skip, q to stop):
   ```

   Answer each one. A yes to a grouped question confirms every fact it lists
   (and only those: the command is bound to a digest of them), a no rejects
   them so they are not proposed again, and `e` walks through them one at a
   time. Without a terminal, `nable org init` prints each question with the
   exact `nable org confirm` and `nable org reject` command that answers it.

4. Through the week, `nable org questions` asks what is still open and
   `nable org review` lists every proposal with its source. Anything this
   pack proposed has a source that starts with
   `pack:io.github.getnable/org-bootstrap:`.

## What it proposes

| Adapter | From | Proposes | Confidence |
|---|---|---|---|
| `backstage` | Component, System and API entities | owner of `service:<name>` from `spec.owner` | 0.8 |
| `backstage` | a local `catalog-info.yaml` whose components share an owner | owner of the repo path that holds it | 0.7 |
| `backstage` | Group entities | `team:<name>` with display name, parent and members | 0.7 |
| `github-teams` | `GET /orgs/{org}/teams` and each team's members | `team:<slug>` with name, parent and members (`@login`) | 0.7 |
| `github-teams` | each team's repos and its permission on them | owner of `repo_path:github.com/<org>/<repo>//.` for the team with admin (maintain if none has admin) | 0.6 (0.5), less 0.15 when several teams tie |

A Backstage owner that is a user, not a group, stands in as the team at 0.6
times the confidence, for a person to correct. Write, triage and read access
on GitHub propose nothing: access is not ownership. Archived repos are
skipped. Entities outside Backstage's default namespace keep it
(`service:retail/search`).

## What it reads

- `repo.files` (declared in `read_data`, with `repo_files = ["catalog-info.yaml",
  "catalog-info.yml"]`, so nable refuses a request for any other file):
  nable itself finds the files named
  `catalog-info.yaml` or `catalog-info.yml` in the repos `nable org init`
  reads, skipping `.git`, `node_modules`, vendored and build directories and
  every symlink, and hands their text to the adapter. The pack is not told
  where those repos are on your machine (like every adapter, it is told the
  directory nable ran in) and asks for files by name only.
- The Backstage catalog API, only when `BACKSTAGE_URL` is set:
  `GET <BACKSTAGE_URL>/api/catalog/entities/by-query`, filtered to
  components, systems, APIs and groups, following `pageInfo.nextCursor`.
- The GitHub REST API, only when `GITHUB_TOKEN` and `GITHUB_ORG` are set: the
  organization's teams, each team's members and each team's repos. A
  fine-grained token with read access to the organization's members (or a
  classic token with `read:org`) is enough.

## Secrets and settings

All optional, each from the pack's own vault entry, never from your
environment. The two tokens are credentials (`secrets`): set with `nable pack
secret set io.github.getnable/org-bootstrap NAME`, the value read from a
prompt or stdin. The rest are settings (`settings`): configuration, not
credentials, set with `nable pack setting set
io.github.getnable/org-bootstrap NAME VALUE`.

| Name | Declared as | Used for |
|---|---|---|
| `GITHUB_TOKEN` | secret | the GitHub API, in the Authorization header only |
| `BACKSTAGE_TOKEN` | secret | a Backstage service token, when the instance needs one |
| `GITHUB_ORG` | setting | the organization to read; it appears in the subjects and sources it proposes |
| `GITHUB_API_URL` | setting | GitHub Enterprise Server (`https://<host>/api/v3`) |
| `BACKSTAGE_URL` | setting | the Backstage instance's base URL |

A token is sent only in the Authorization header, over https, to a host the
pack declares. The adapters refuse redirects (a redirect could carry the
token elsewhere) and use no proxy. No token appears in a proposal, a source
or the pack's log: the broker redacts each credential's value from the log,
and refuses any proposal that carries one, in any field.

## Network

`api.github.com`, and nothing else as shipped. The Backstage adapter reads
local files with no network at all.

A Backstage instance or a GitHub Enterprise Server is on your org's own host,
which the pack cannot know. To use one, add its host to `network` in
`nable-pack.toml` (for example `network = ["api.github.com",
"backstage.example.internal"]`), validate the pack again, sign your changed
copy with your org key or allowlist its digest (see Signing), and approve the
update: nable shows the new host as an added capability. Until then the
adapter logs that the host is not declared and connects to nothing.

## Signing

Releases are signed with nable's first-party key (`support =
"first-party"`). A first-party claim is honoured only with a signature from
that key, and code from the pack runs because of it. The copy in this
repository is unsigned; `nable pack validate packs/org-bootstrap` says so,
and install refuses it until a release is signed.

A copy your org changes (another network host, say) is no longer the signed
release. Declare `support = "community"` in it, then either sign it with a
key in `packs.trusted_keys` or allowlist its exact digest, which `nable pack
validate` prints:

```yaml
packs:
  allow_unsigned_code:
    - io.github.getnable/org-bootstrap@<digest>
```
