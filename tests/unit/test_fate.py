from collections import Counter
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from world_game.domain.fate import FateSheet, RngState
from world_game.engine.fate_rules import (
    absorption_choices,
    classify_margin,
    outcome_branch,
    stress_capacity,
)
from world_game.engine.randomness import CounterDice


def test_distribution():
    sums = Counter(map(sum, product([-1, 0, 1], repeat=4)))
    assert [sums[n] for n in range(-4, 5)] == [1, 4, 10, 16, 19, 16, 10, 4, 1]


@pytest.mark.parametrize(
    "margin,result",
    [
        (-4, "failure"),
        (-1, "failure"),
        (0, "tie"),
        (1, "success"),
        (2, "success"),
        (3, "style"),
        (7, "style"),
    ],
)
@pytest.mark.parametrize("action", ["overcome", "create_advantage", "attack", "defend"])
def test_outcomes(action, margin, result):
    assert classify_margin(margin + 4, 4) == result
    branch = outcome_branch(action, margin)
    if action == "overcome":
        assert branch.goal_achieved == (margin >= 0)
        assert branch.boost == (margin >= 3)
        assert branch.cost == ("minor" if margin == 0 else "none")
    elif action == "attack":
        assert branch.hit == max(0, margin)
        assert branch.boost == (margin == 0)
    elif action == "defend":
        assert branch.stopped == (margin > 0)
        assert branch.boost == (margin >= 3)
    else:
        assert branch.aspect == (margin > 0)
        assert branch.free_invokes == (2 if margin >= 3 else 1 if margin > 0 else 0)


@pytest.mark.parametrize("variant", ["new", "known", "unknown"])
@pytest.mark.parametrize("margin", [-1, 0, 1, 2, 3])
def test_advantage_branches(variant, margin):
    r = outcome_branch("create_advantage", margin, variant)
    if variant == "unknown":
        assert r.reveal == (margin > 0)
        assert r.enemy_free_invokes == 0
        if margin < 0:
            revealed = outcome_branch("create_advantage", margin, variant, reveal_on_failure=True)
            assert revealed.reveal and revealed.enemy_free_invokes == 1
    if variant == "known":
        assert r.free_invokes == (2 if margin >= 3 else 1 if margin >= 0 else 0)
        assert r.enemy_free_invokes == (1 if margin < 0 else 0)
        assert not r.boost
    else:
        assert r.boost == (margin == 0)


def test_major_cost_and_tie_once():
    assert outcome_branch("overcome", -1, major_cost=True).cost == "major"
    assert outcome_branch("attack", 0).boost
    assert not outcome_branch("defend", 0).boost


def test_reference_rng():
    dice = CounterDice()
    rng = RngState(seed="00" * 32)
    first, rng = dice.roll(rng, "check:a", "actor")
    second, rng = dice.roll(rng, "check:a", "defender")
    assert first.dice + second.dice == [-1, 0, 1, 0, 0, -1, 0, 1]
    assert rng.counter == 8
    assert first.before_counter == 0 and second.before_counter == 4


@given(st.binary(min_size=32, max_size=32), st.integers(min_value=0, max_value=1000))
def test_deterministic_dice(seed, counter):
    rng = RngState(seed=seed.hex(), counter=counter)
    a, after = CounterDice().roll(rng, "check", "actor")
    b, next_after = CounterDice().roll(rng, "check", "actor")
    assert a == b and after == next_after
    assert all(d in (-1, 0, 1) for d in a.dice)
    assert rng.counter == counter


def test_counter_exhaustion():
    with pytest.raises(Exception, match="exhausted"):
        CounterDice().roll(RngState(seed="00" * 32, counter=2**64), "check", "actor")


@pytest.mark.parametrize("rating,capacity", [(0, 3), (1, 4), (2, 4), (3, 6), (4, 6)])
def test_capacity(rating, capacity):
    assert stress_capacity(FateSheet(skills={"athletics": rating}), "physical") == capacity
    assert stress_capacity(FateSheet(skills={"will": rating}), "mental") == capacity


@given(st.integers(min_value=1, max_value=18), st.integers(min_value=0, max_value=4))
def test_absorption_covers_every_shift(hit, used):
    sheet = FateSheet(skills={"athletics": 2}, physical_stress_used=used)
    choices = absorption_choices(sheet, "physical", hit)
    for c in choices:
        assert c.stress + c.severity_sum >= hit
        assert c.stress + used <= 4
        assert len(c.slots) == len(set(c.slots))
    if hit > 16 - used:
        assert not choices


def test_npc_allocation_and_no_partial_hit():
    sheet = FateSheet(skills={}, stress_capacity_override=3, has_consequences=False)
    assert absorption_choices(sheet, "physical", 3)[0].stress == 3
    assert not absorption_choices(sheet, "physical", 4)


def test_strict_json_and_python():
    for value in [True, 2.0, "2"]:
        with pytest.raises(ValidationError):
            FateSheet(skills={"will": value})
    with pytest.raises(ValidationError):
        FateSheet.model_validate_json('{"skills":{"will":"2"}}')
    with pytest.raises(ValidationError):
        FateSheet.model_validate_json('{"skills":{},"unknown":2}')
