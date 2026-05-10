"""
Idle resource detection and opt-in cleanup.

Opt-in is controlled by FINOPS_CLEANUP_ENABLED=true in the environment
(set during `finops setup`). If not enabled, list_idle_resources still
works but no action tools are registered.

Protected tags: resources tagged with any key in FINOPS_PROTECTED_TAGS
(comma-separated, default: "env=prod,protected=true,do-not-delete=true")
are NEVER touched regardless of instruction.
"""
