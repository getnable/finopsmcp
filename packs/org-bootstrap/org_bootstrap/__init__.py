# SPDX-License-Identifier: Apache-2.0
"""Org bootstrap: org-context adapters that propose owners and teams.

    backstage      catalog-info.yaml files in the repos `nable org init` reads
                   (through the repo.files data scope: no network), and a
                   Backstage catalog API when BACKSTAGE_URL is set and its host
                   is declared in the pack's network
    github-teams   GitHub teams, their members and the repos they administer
                   (GITHUB_TOKEN, GITHUB_ORG; api.github.com)

nable never imports this package: the broker runs each entry point in its own
process with only what nable-pack.toml declares, and whatever an adapter
returns is written as a proposal a person confirms.
"""
