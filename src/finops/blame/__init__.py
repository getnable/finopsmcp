"""Cost-to-code blame: tie a resource's cost-driving sizing change to the git
commit / PR that made it, and draft a propose-only revert.

Public entry point: finops.blame.culprit.find_cost_culprit
"""
