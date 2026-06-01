# GitHub Actions examples

Copy-paste templates. These do NOT run in this repo, copy one into your own
repo at `.github/workflows/` and add the required secrets.

- `budget-check.yml` — fail a PR (or weekly check) when spend exceeds `budget.yml`.
- `cost-check.yml` — comment estimated cost change on every infra PR.

Both reference the nable budget/cost actions and need `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, and optionally `FINOPS_LICENSE_KEY` set as repo secrets.
