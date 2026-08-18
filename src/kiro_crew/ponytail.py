"""Native Ponytail coding-mode policy.

The policy is derived from Ponytail's MIT-licensed Kiro steering adapter:
https://github.com/DietrichGebert/ponytail (copyright DietrichGebert).
It is deliberately a small, prompt-only policy layer. It never grants tools,
changes approvals, or overrides an explicit user request.
"""

from __future__ import annotations

PONYTAIL_LEVELS: tuple[str, ...] = ("off", "lite", "full", "ultra")
PONYTAIL_DEFAULT = "full"
PONYTAIL_VALUES = frozenset(PONYTAIL_LEVELS)
PONYTAIL_OVERRIDE_VALUES = frozenset({"", *PONYTAIL_LEVELS})


def is_valid_ponytail(value: object, *, allow_empty: bool = False) -> bool:
    """Return whether *value* is an accepted mode or slot override."""
    if not isinstance(value, str):
        return False
    return value in (PONYTAIL_OVERRIDE_VALUES if allow_empty else PONYTAIL_VALUES)


def normalize_ponytail(value: object, *, default: str = PONYTAIL_DEFAULT) -> str:
    """Return a valid concrete mode, falling back safely on malformed input."""
    if isinstance(value, str) and value in PONYTAIL_VALUES:
        return value
    return default if default in PONYTAIL_VALUES else PONYTAIL_DEFAULT


def resolve_ponytail(override: object = "", *, default: object = PONYTAIL_DEFAULT) -> str:
    """Resolve a slot override against the live global default."""
    if isinstance(override, str) and override in PONYTAIL_VALUES:
        return override
    return normalize_ponytail(default)


def render_ponytail_mode(mode: object) -> str:
    """Render the trusted prompt block for a concrete Ponytail mode."""
    level = normalize_ponytail(mode)
    if level == "off":
        return ""

    intensity = {
        "lite": (
            "Build what the user asked for, then name the lazier alternative in one "
            "short line; the user decides."
        ),
        "full": (
            "Enforce the ladder: stop at the first rung that solves the real problem "
            "without cutting safety or correctness."
        ),
        "ultra": (
            "Use the YAGNI-extreme posture: challenge speculative requirements and "
            "prefer deletion or the smallest working change."
        ),
    }[level]
    return (
        "[PONYTAIL MODE]\n"
        f"## Ponytail coding mode: {level}\n\n"
        "This is an implementation-style preference, not a permission. Priority "
        "is always: Kiro-Crew safety rules, the user's explicit current request, "
        "and Autopilot workflow gates; this mode comes after them.\n\n"
        "Before writing code, stop at the first rung that holds:\n"
        "1. Does this need to exist? Skip speculative work (YAGNI).\n"
        "2. Does it already exist here? Reuse the existing helper or pattern.\n"
        "3. Can the standard library solve it?\n"
        "4. Can a native platform feature solve it?\n"
        "5. Can an already-installed dependency solve it?\n"
        "6. Can it be one line?\n"
        "7. Only then write the minimum code that works.\n\n"
        f"Intensity: {intensity}\n\n"
        "Never simplify away problem understanding, validation at trust boundaries, "
        "error handling that prevents data loss, security, accessibility, hardware "
        "calibration, or anything explicitly requested. Read and trace the real flow "
        "before choosing the smaller diff. Fix bugs at their shared root cause.\n"
        "[END PONYTAIL MODE]\n\n"
    )
