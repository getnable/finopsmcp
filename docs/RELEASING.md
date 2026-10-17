# Releasing nable

A release is a version bump merged to `main` and a `vX.Y.Z` tag pushed for
that commit. The tag is what publishes. Merging a bump without tagging it
publishes nothing, and that is not harmless: see "Why the tag cannot wait".

**A person pushes the tag.** An agent session can prepare the bump, the
changelog and the pull request, but its permission settings block it from
pushing a release tag or starting a publish workflow, on purpose: publishing
is a one-way door. So every release ends with a maintainer running step 5
from their own terminal. An agent that prepared a release says so in its
hand-off, with the exact tag command, rather than reporting the release done.

## Why the tag cannot wait

Claude Code installs the nable plugin from this repository's default branch
(`/plugin marketplace add getnable/finopsmcp`). The plugin runs the release
its files pin, `finops-mcp==X.Y.Z` in `plugins/nable/.claude-plugin/plugin.json`
and `plugins/nable/hooks/hooks.json`, so every plugin user moves to X.Y.Z when
the bump is merged, whether or not X.Y.Z is on PyPI.

If it is not, `uvx` cannot resolve the pin. The plugin's MCP server does not
start, and the guard hook, which ends in `; exit 0` so that a broken hook never
blocks the agent, objects to nothing and says nothing: every command runs
under Claude Code's ordinary permission settings, unchecked by nable. This has
happened: `main` carried 0.8.217 from 2026-09-25, then 0.9.0 and 0.10.0, while
PyPI's newest release stayed 0.8.216 because no tag was pushed. Measured on
2026-10-13 with the plugin's exact hook command:

```text
$ echo '{"tool_name":"Bash","tool_input":{"command":"terraform destroy -auto-approve"}}' \
    | sh -c 'uvx --python 3.12 --from finops-mcp==0.10.0 finops guard hook --via plugin; exit 0'
```

Standard output was empty and the exit status 0, which Claude Code reads as
"no objection": the destroy goes ahead unchecked. Standard error, which the agent does
not act on, ended with:

```text
  ╰─▶ Because there is no version of finops-mcp==0.10.0 and you require
      finops-mcp==0.10.0, we can conclude that your requirements are
      unsatisfiable.
```

Pointing the pin back at 0.8.216 does not help either: 0.8.216 rejects
`--via plugin` (`unrecognized arguments`). The only fix is to publish.

So tag in the same sitting as the merge, and the person who merges a bump is
the person who pushes its tag (or hands it, in that sitting, to someone who
will). `scripts/check_release.py` (below) reports when this has not happened.

## What runs on a tag

| Workflow | Trigger | What it publishes |
|---|---|---|
| `.github/workflows/publish.yml` | a pushed tag `v*.*.*`, or a manual run | **PyPI** (`build-and-publish`): `python -m build`, `twine check`, then upload through PyPI trusted publishing (OIDC, no token) with PEP 740 attestations and `skip-existing`. **GitHub Release** (`publish-github-release`, tag pushes only, after PyPI): `gh release create <tag> --generate-notes --verify-tag`, skipped when the release exists. **MCP Registry** (`publish-mcp-registry`, after PyPI): `mcp-publisher` v1.7.9, checksum-verified, logs in with GitHub OIDC and publishes `server.json`. The registry checks that the PyPI version exists, rejects a version it already has, and rejects a `description` over 100 characters. |
| `.github/workflows/image.yml` | the same `v*.*.*` tag, or a manual run with an `image_version` | **GHCR**: `ghcr.io/<owner>/finops:X.Y.Z` and `:latest` for linux/amd64 and linux/arm64, built from `Dockerfile`, then pulled anonymously to prove the package is public. The image's default command is `finops serve`, which in this package prints that the dashboard is part of hosted nable and exits; the image carries the CLI and the MCP server, not a long-running service. |
| `.github/workflows/publish-shim.yml` | a pushed tag `shim-v*`, or a manual run | **PyPI `nable`**: the brand alias in `shim/`, which depends on `finops-mcp>=0.8.181`. `uvx nable` resolves the newest finops-mcp without a shim release, so tag the shim only when `shim/` changes. |

`.github/workflows/test.yml` runs the suite on Python 3.11 and 3.12 for every
push to `main` and every pull request. It does not run on tags; a tag must
point at a commit that already passed on `main`.

## Steps

1. **Bump every carrier to X.Y.Z** in one commit with
   `python scripts/bump_version.py X.Y.Z`. It edits only the version fields
   below, writes nothing unless every one says the old version, regenerates
   the SBOM and refuses to change any other line of `pyproject.toml`. Do not
   find-and-replace the old version: the 0.11.0 bump did, and it raised the
   `pdfplumber` dependency floor with it. `tests/test_upgrade_pin.py`
   (`test_every_version_carrier_agrees_with_the_package`) fails until all of
   them agree:
   - `pyproject.toml` (`version`)
   - `src/finops/__init__.py` (`__version__`, which the CLI reports and which
     setup writes into editor configs)
   - `server.json` (`version` and `packages[0].version`)
   - `plugins/nable/.claude-plugin/plugin.json` (`version` and the
     `finops-mcp==X.Y.Z` argument)
   - `plugins/nable/hooks/hooks.json` (both hook commands)
   - `.claude-plugin/marketplace.json` (`plugins[0].version`)
   - `packaging/mcpb/manifest.json` (`version`)
   - `docs/sbom.json`: regenerate with `python scripts/generate_sbom.py`
2. **Add the CHANGELOG entry** for X.Y.Z at the top of `CHANGELOG.md`.
3. **Run the suite** on 3.11 and 3.12 (`pytest -q`, see
   [CONTRIBUTING](../.github/CONTRIBUTING.md)). Optionally build locally:
   `python -m build && twine check dist/*`.
4. **Merge to `main`** and wait for the Tests workflow to pass on the merge
   commit.
5. **A maintainer tags that commit and pushes the tag**, from their own
   terminal, in the same sitting (an agent session cannot do this step):

   ```bash
   git fetch origin
   git tag -a vX.Y.Z -m "vX.Y.Z" <merge-commit-sha>
   git push origin vX.Y.Z
   ```

   The tag must match `v*.*.*` and must name the commit whose carriers say
   X.Y.Z. A tag on any other commit publishes whatever version that commit
   carries.
6. **Watch the runs**: Publish (PyPI + MCP Registry) and Build container
   image (GHCR), both started by the tag.
7. **Verify** (next section).
8. **The shim**, only when `shim/` changed: bump `shim/pyproject.toml`, merge,
   then `git tag -a shim-vA.B.C -m "shim-vA.B.C" <sha> && git push origin shim-vA.B.C`.

## Verify

Run each from a clean shell, with no cloud credentials needed:

```bash
# PyPI has it, and the tag exists (exit 0 and "is on PyPI")
python scripts/check_release.py

# A cold install of exactly this release reports itself
UV_CACHE_DIR=$(mktemp -d) uvx --python 3.12 --from finops-mcp==X.Y.Z finops --version
#   nable (finops-mcp) X.Y.Z

# The plugin's guard hook answers: a destroy asks (non-empty JSON).
# Empty output means the pin did not resolve and the guard is off for plugin users.
echo '{"tool_name":"Bash","tool_input":{"command":"terraform destroy -auto-approve"}}' \
  | uvx --python 3.12 --from finops-mcp==X.Y.Z finops guard hook --via plugin

# The brand command resolves the new release through the shim
UV_CACHE_DIR=$(mktemp -d) uvx nable --version

# The GitHub Release exists
gh api repos/getnable/finopsmcp/releases/latest --jq .tag_name
```

The MCP Registry leg prints its result in the Publish run's log. The image
workflow already proves an anonymous `docker pull` of both tags.

## When a leg fails

- **Re-run, do not re-tag.** Publish has a manual trigger, which a maintainer
  starts (an agent session cannot). Run it from the
  tag (Actions, Publish, "Use workflow from: Tags: vX.Y.Z"), not from `main`:
  a manual run builds the ref you select, and if `main` has moved to another
  version it would publish that one. `skip-existing` makes the PyPI step a
  no-op when the upload already happened, and the release step skips a release
  that exists. This is how v0.8.216 shipped: its tag run published to PyPI and
  cut the release, the MCP Registry leg failed, and a manual run passed.
- **A bad release cannot be replaced.** PyPI never accepts the same version
  twice. Yank it on pypi.org if it must not be installed, and release X.Y.Z+1.
- **The image** has its own manual trigger with an `image_version` input, for
  building an already-tagged version again.

## The release-gap check

`scripts/check_release.py` reads every carrier, asks PyPI whether
`finops-mcp==X.Y.Z` exists and looks for the `vX.Y.Z` tag. It exits 0 when
the release is on PyPI, or when the bump is younger than `--grace-days`
(default 2: a merge waiting for its tag); 1 when the bump has gone untagged
for longer, or the carriers disagree; 2 when PyPI could not be asked.
`.github/workflows/release-gap.yml` runs it daily and on demand. It never
runs on a push or a pull request, so it cannot turn `main` red between a
merge and its tag, and it never publishes or tags anything.
`tests/test_release_gap.py` pins its decisions and its triggers without the
network.
