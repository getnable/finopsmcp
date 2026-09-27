# SPDX-License-Identifier: Apache-2.0
"""An example nable code pack: a CSV cost connector and a CSV owners adapter.

Both entry points take a finops.packs.sdk.Context first. They read their
input path from a declared secret (the org sets it in nable's vault or its
environment), so the pack itself names no file on anyone's machine.
"""
