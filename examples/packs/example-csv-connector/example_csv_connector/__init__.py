# SPDX-License-Identifier: Apache-2.0
"""An example nable code pack: a CSV cost connector and a CSV owners adapter.

Both entry points take a finops.packs.sdk.Context first. They read their
input path from a declared secret, which the org stores for this pack with
`nable pack secret set com.example/example-csv-connector EXAMPLE_COSTS_CSV`
(the broker passes a pack only its own vault entries, never nable's
environment), so the pack itself names no file on anyone's machine.
"""
