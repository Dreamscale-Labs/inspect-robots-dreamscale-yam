"""Small numbered-list and yes/no prompts shared by the interview commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dreamscale_yam.errors import UserFacingError


def select(
    label: str,
    candidates: list[str],
    used: set[str],
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    labels: Mapping[str, str] | None = None,
) -> str:
    """Ask for one unused candidate by its listed number."""
    available = [candidate for candidate in candidates if candidate not in used]
    if not available:
        raise UserFacingError(
            f"There is no unused device available for {label}",
            "Connect the missing device, then rerun ./setup.sh",
        )
    output(f"Assign {label}:")
    for index, candidate in enumerate(candidates, 1):
        suffix = " (already assigned)" if candidate in used else ""
        shown = (labels or {}).get(candidate, candidate)
        output(f"  {index}. {shown}{suffix}")
    while True:
        answer = input_fn(f"{label} [1-{len(candidates)}]: ").strip()
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if not 1 <= index <= len(candidates):
            output("Enter one listed number.")
            continue
        selected = candidates[index - 1]
        if selected in used:
            output("That device is already assigned; choose another.")
            continue
        used.add(selected)
        return selected


def yes_no(
    prompt: str,
    *,
    default: bool,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> bool:
    """Ask one yes/no question; an empty answer takes the default."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input_fn(f"{prompt} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        output("Please answer y or n.")
