"""Fate rule: four-action arithmetic, stress, invokes, and outcome branches."""

from itertools import combinations
from typing import Literal

from world_game.domain.common import Value
from world_game.domain.fate import Action, FateSheet, Harm, Outcome, Severity, Slot


class ActionResult(Value):
    goal_achieved: bool = False
    cost: Literal["none", "minor", "major"] = "none"
    boost: bool = False
    aspect: bool = False
    reveal: bool = False
    free_invokes: int = 0
    enemy_free_invokes: int = 0
    hit: int = 0
    stopped: bool = False


def classify_margin(effort: int, opposition: int) -> Outcome:
    margin = effort - opposition
    return (
        "failure" if margin < 0 else "tie" if margin == 0 else "success" if margin < 3 else "style"
    )


def outcome_branch(
    action: Action,
    margin: int,
    variant: Literal["new", "known", "unknown"] = "new",
    major_cost: bool = False,
    reveal_on_failure: bool = False,
) -> ActionResult:
    result = classify_margin(margin, 0)
    if action == "overcome":
        return ActionResult(
            goal_achieved=margin >= 0 or major_cost,
            cost="major" if margin < 0 and major_cost else "minor" if margin == 0 else "none",
            boost=result == "style",
        )
    if action == "attack":
        return ActionResult(boost=margin == 0, hit=max(0, margin))
    if action == "defend":
        return ActionResult(stopped=margin > 0, boost=result == "style")
    if variant == "unknown":
        return ActionResult(
            reveal=margin > 0 or (margin < 0 and reveal_on_failure),
            boost=margin == 0,
            free_invokes=2 if margin >= 3 else 1 if margin > 0 else 0,
            enemy_free_invokes=int(margin < 0 and reveal_on_failure),
        )
    if variant == "known":
        return ActionResult(
            free_invokes=2 if margin >= 3 else 1 if margin >= 0 else 0,
            enemy_free_invokes=int(margin < 0),
        )
    return ActionResult(
        aspect=margin > 0,
        boost=margin == 0,
        free_invokes=2 if margin >= 3 else 1 if margin > 0 else 0,
    )


def stress_capacity(sheet: FateSheet, harm: Harm) -> int:
    if sheet.stress_capacity_override is not None:
        return sheet.stress_capacity_override
    # v0.1 policy: Athletics supplies the physical capacity.
    rating = sheet.skills.get("athletics" if harm == "physical" else "will", 0)
    return 3 if rating == 0 else 4 if rating < 3 else 6


class Absorption(Value):
    stress: int
    slots: list[Literal["mild", "moderate", "severe"]]
    severity_sum: int


SEVERITIES: dict[Slot, Severity] = {"mild": 2, "moderate": 4, "severe": 6}


def absorption_choices(sheet: FateSheet, harm: Harm, hit: int) -> list[Absorption]:
    used = sheet.physical_stress_used if harm == "physical" else sheet.mental_stress_used
    available: list[Literal["mild", "moderate", "severe"]] = [
        slot
        for slot in ("mild", "moderate", "severe")
        if sheet.has_consequences and getattr(sheet.consequence_slots, slot) is None
    ]
    choices = []
    for count in range(len(available) + 1):
        for slots in combinations(available, count):
            severity = sum(SEVERITIES[slot] for slot in slots)
            for stress in range(stress_capacity(sheet, harm) - used + 1):
                if stress + severity >= hit:
                    choices.append(
                        Absorption(stress=stress, slots=list(slots), severity_sum=severity)
                    )
    return sorted(choices, key=lambda c: (c.severity_sum, c.stress, c.slots))
