"""Python version preflight, stdlib only.

Every console-script entry point imports and calls require_python() before its
own heavy imports, so a too-old interpreter fails with a clear message instead
of a cryptic crash. pip already refuses to install below requires-python (3.10),
so this mainly guards source checkouts and forced-interpreter launches.

Keep this module free of third-party imports and any syntax newer than 3.8, or
the guard will fail to load on the exact old interpreters it exists to catch.
"""
import sys

MIN_PYTHON = (3, 10)


def require_python(minimum=MIN_PYTHON):
    """Exit with a readable message when the running Python is too old."""
    v = sys.version_info
    if (v[0], v[1]) < minimum:
        have = "%d.%d.%d" % (v[0], v[1], v[2])
        want = "%d.%d" % (minimum[0], minimum[1])
        sys.stderr.write(
            "nable requires Python %s or newer. You are on Python %s at %s.\n"
            "Reinstall on a newer Python, for example:\n"
            "  uvx --python 3.12 --from finops-mcp finops welcome\n"
            "  # or: conda create -n nable python=3.12 && conda activate nable "
            "&& pip install finops-mcp\n"
            % (want, have, sys.executable)
        )
        raise SystemExit(1)
