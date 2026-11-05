"""Normalize structured tool operation names for the security gate."""

import re as _re


def _normalize_to_kebab(name: str) -> str:
    """Convert PascalCase/camelCase to kebab-case for consistent deny matching.

    ``DeleteStack`` → ``delete-stack``, ``send-command`` → ``send-command``
    (already kebab, unchanged). This ensures the security deny globs (which are
    authored in kebab-case, e.g. ``*delete-stack*``) match regardless of whether
    kiro-cli or the LLM sends the AWS API PascalCase name or the CLI kebab name.

    The transform is injective within the space of valid AWS operation names
    (single-word PascalCase identifiers), so it cannot wrongly DENY a benign op
    by colliding with a destructive one: AWS operation names are globally unique
    per service, and the kebab form of each is equally unique.
    """
    # Insert hyphen before uppercase runs: "DeleteStack" → "Delete-Stack" → "delete-stack"
    s = _re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", s)
    return s.lower()
