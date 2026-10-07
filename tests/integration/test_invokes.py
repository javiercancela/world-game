import pytest

from world_game.domain.common import GameError, digest
from world_game.engine.actions import PC


def bluff(c):
    c.submit("I show Oren the seal and say I am expected")


def passes(c):
    for _ in range(10):
        p = c.repository.pending()
        if not p or not p.current_choice or p.current_choice.kind != "invoke":
            return
        c.choose("pass")
    raise AssertionError("passes")


def test_free_paid_stacking_paid_once_and_staged_budget(game):
    def free(s):
        s.aspects["aspect:lea:concept"].free_invokes[PC] = 2

    c = game([[-1, -1, 0, 0]], change=free)
    bluff(c)
    c.choose("invoke:aspect:lea:concept:free:bonus")
    c.choose("invoke:aspect:lea:concept:paid:bonus")
    c.choose("invoke:aspect:lea:concept:free:bonus")
    assert c.repository.pending().actor_bonus == 6
    assert "concept:" not in c.describe()
    with pytest.raises(GameError):
        c.choose("invoke:aspect:lea:concept:paid:bonus")
    assert c.repository.load().entities[PC].fate.fate_points == 3
    c.choose("pass")
    s = c.repository.load()
    assert s.entities[PC].fate.fate_points == 2
    assert s.aspects["aspect:lea:concept"].free_invokes[PC] == 0
    assert s.clock.tick == 1


def test_reroll_replaces_dice_keeps_skill_stunts_and_other_bonuses(game):
    def free(s):
        s.aspects["aspect:lea:concept"].free_invokes[PC] = 1

    c = game([[-1, -1, 0, 0], [-1] * 4], change=free)
    bluff(c)
    c.choose("invoke:aspect:lea:concept:free:bonus")
    c.choose("invoke:aspect:lea:concept:paid:reroll")
    p = c.repository.pending()
    assert p.actor_dice == [-1] * 4 and p.actor_bonus == 2
    assert p.staged_rng.counter == 8 and p.resolution_plan.stunt_bonus == 2
    assert "tie" in c.describe()
    c.choose("pass")
    s = c.repository.load()
    assert s.rng.counter == 8 and s.entities[PC].fate.fate_points == 2
    assert any(e.handler_id == "verify_invitation" for e in s.scheduled_effects.values())
    assert digest(c.repository.replay()) == digest(s)


def test_npc_spend_only_active_two_passes_and_hidden_name(game):
    c = game([[-1, -1, -1, 0], [0] * 4])
    c.submit("Read Oren's concern")
    result = c.choose("pass")
    assert c.repository.pending().pass_count == 0
    assert c.repository.pending().defender_bonus == 2
    assert c.repository.pending().provisional_resource_balances["gm"] == 0
    assert "Fear of Being Blamed" not in result
    result = c.choose("pass")
    assert c.repository.pending() is None
    assert "Fear of Being Blamed" not in result
    assert c.repository.load().scene.gm_fate_pool == 0
    assert PC not in c.repository.load().aspects["aspect:oren:blame"].known_by
    other = game([[-1, -1, 0, 0]])
    bluff(other)
    other.choose("pass")
    assert other.repository.load().scene.gm_fate_pool == 1


def test_hostile_paid_award_waits_for_scene_and_credit_consumed_once(game):
    def known(s):
        s.aspects["aspect:oren:blame"].known_by.append(PC)
        s.entities["portal:gate"].portal.open = True

    c = game([[0] * 4], change=known)
    c.submit("Address Oren's concern and reassure him")
    c.choose("invoke:aspect:oren:blame:paid:bonus")
    c.choose("pass")
    s = c.repository.load()
    assert len(s.scene.deferred_awards) == 1 and not s.session.npc_carryover_credits
    c.submit("Go to the courtyard")
    s = c.repository.load()
    assert s.session.npc_carryover_credits["actor:oren"] == 1
    assert s.scene.gm_fate_pool == 1
    c.submit("Go outside to the gatehouse")
    s = c.repository.load()
    assert s.scene.gm_fate_pool == 2 and "actor:oren" not in s.session.npc_carryover_credits
    c.submit("Go to the courtyard")
    c.submit("Go outside to the gatehouse")
    assert c.repository.load().scene.gm_fate_pool == 1


def test_hostile_free_awards_nothing(game):
    def known(s):
        s.aspects["aspect:oren:blame"].known_by.append(PC)
        s.aspects["aspect:oren:blame"].free_invokes[PC] = 1

    c = game([[0] * 4], change=known)
    c.submit("Address Oren's concern")
    c.choose("invoke:aspect:oren:blame:free:bonus")
    c.choose("pass")
    assert not c.repository.load().scene.deferred_awards


def test_boost_free_only_deleted_and_expired(game):
    c = game([[1] * 4, [0] * 4])
    bluff(c)
    c.choose("pass")
    s = c.repository.load()
    boost = next(a.id for a in s.aspects.values() if a.kind == "boost")
    c.submit("Address Oren's concern")
    # The undiscovered concern is blocked, with no resource loss.
    assert boost in c.repository.load().aspects
    c.submit("I show Oren the seal and say I am expected")
    assert f"invoke:{boost}:free:bonus" in c.describe()
    assert f"invoke:{boost}:paid" not in c.describe()
    c.choose(f"invoke:{boost}:free:bonus")
    assert boost not in c.builder(c.repository.pending()).state.aspects
    c.choose("pass")
    c.submit("Go to the courtyard")
    assert not any(a.kind == "boost" for a in c.repository.load().aspects.values())


def test_unknown_aspect_invoke_and_cancel_after_revealed_roll_rejected(game):
    c = game([[0] * 4])
    bluff(c)
    before = c.repository.pending()
    with pytest.raises(GameError):
        c.choose("invoke:aspect:oren:blame:paid:bonus")
    with pytest.raises(GameError, match="cannot be canceled"):
        c.cancel()
    assert c.repository.pending() == before
