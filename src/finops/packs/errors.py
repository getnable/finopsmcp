# SPDX-License-Identifier: Apache-2.0
"""What goes wrong with a pack, said precisely enough to fix.

Every refusal names the field and the reason, because "invalid pack" sends an
author back to guess and an admin back to the source. A validation run collects
every problem it finds rather than stopping at the first, so one `nable pack
validate` shows the whole list.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    """One thing wrong: where (a dotted manifest field or a file path) and why."""

    field: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: {self.reason}"


class PackError(Exception):
    """A pack could not be validated, installed, removed or loaded."""

    def __init__(self, message: str, problems: list[Problem] | tuple[Problem, ...] = ()):
        super().__init__(message)
        self.message = message
        self.problems: list[Problem] = list(problems)

    def __str__(self) -> str:
        if not self.problems:
            return self.message
        return self.message + "\n" + "\n".join(f"  {p}" for p in self.problems)


class ValidationError(PackError):
    """The manifest or a content file breaks the pack schema."""


class PolicyRefusal(PackError):
    """The org policy (packs: in nable.policy.yaml) does not allow this."""


class ApprovalRequired(PackError):
    """Nobody approved the pack's capabilities, so nothing was installed."""


class IntegrityError(PackError):
    """A file's sha256 is not the one it was pinned to."""


class RegistryError(PackError):
    """The registry index could not be read or does not list the pack."""
