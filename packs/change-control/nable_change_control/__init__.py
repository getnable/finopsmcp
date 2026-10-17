# SPDX-License-Identifier: Apache-2.0
"""Code for the change-control pack: one org-context adapter (approvals.py).

nable never imports this package in its own process. The broker runs it in
a subprocess with no network, no secrets and no data scopes.
"""
