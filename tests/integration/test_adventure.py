import io

import pytest

from world_game.cli import execute, repl, run_demo
from world_game.domain.common import canonical, digest
from world_game.domain.state import Placement
from world_game.engine.actions import PC
from world_game.engine.queries import Viewer, co_located, container, project
from world_game.engine.turns import Coordinator
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SQLiteRepository


def complete(c, text):
    result = execute(c, text)
    for _ in range(10):
        p = c.repository.pending()
        if not p or not p.current_choice or p.current_choice.kind != "invoke":
            return result
        result = c.choose("pass")
    raise AssertionError("invoke loop")


def courtyard(state):
    state.entities[PC].placement = Placement(container_id="location:courtyard")
    state.entities["portal:gate"].portal.open = True
    state.scene.definition_ref = "scene:courtyard"
    state.scene.participant_ids = [PC, "actor:sen"]


def loan(c, compel="object"):
    c.submit("Ask Sen for help with the key")
    c.ensure_boundary()
    c.choose("accept")
    c.ensure_boundary()
    return c.choose(compel)


def test_worked_bluff_reload_and_paid_choice(game):
    c = game([[-1, -1, 0, 0]])
    repo = c.repository
    before = digest(repo.load())
    initial = c.submit("I show Oren the seal and say the court expects me. Let me in.")
    assert "tie" in initial and "stunt +2" in initial
    p = repo.pending()
    assert p.staged_rng.counter == 4 and repo.load().rng.counter == 0
    assert digest(repo.load()) == before
    reopened = SQLiteRepository(repo.path)
    resumed = Coordinator(reopened, ScriptedModel(), c.dice)
    assert resumed.describe() == c.describe()
    resumed.choose("invoke:aspect:lea:concept:paid:bonus", response_id="response:once")
    after_choice = reopened.pending()
    assert after_choice.provisional_resource_balances[PC] == 2
    assert "concept:paid" not in resumed.describe()
    assert "already accepted" in resumed.choose(
        "invoke:aspect:lea:concept:paid:bonus", response_id="response:once"
    )
    assert reopened.pending() == after_choice
    reopened.close()
    c.choose("pass")
    state = repo.load()
    assert state.entities[PC].fate.fate_points == 2
    assert state.entities["portal:gate"].portal.open
    assert state.propositions["proposition:lea_expected"].truth == "false"
    assert state.beliefs["belief:oren:invitation"].stance == "believes"
    assert (
        container(state, PC) == "location:gatehouse" and container(state, "item:royal_seal") == PC
    )
    assert (state.clock.tick, state.clock.player_turn, state.world_version) == (1, 1, 1)
    assert not any(e.handler_id == "verify_invitation" for e in state.scheduled_effects.values())
    assert not any(a.free_invokes for a in state.aspects.values())
    assert digest(repo.replay()) == digest(state)


def test_bluff_tie_verification(game):
    c = game([[-1, -1, 0, 0]])
    complete(c, "I show Oren the seal and say I am expected")
    assert (
        next(
            e
            for e in c.repository.load().scheduled_effects.values()
            if e.handler_id == "verify_invitation"
        ).due_tick
        == 3
    )
    complete(c, "Go to the courtyard")
    complete(c, "wait")
    s = c.repository.load()
    assert s.clock.tick == 3
    assert not s.entities["portal:gate"].portal.open
    assert s.beliefs["belief:oren:invitation"].stance == "disbelieves"
    assert co_located(s, PC, "actor:oren")
    assert (
        "invitation"
        not in canonical(project(s, Viewer(actor_id="actor:oren"), "npc"))
        .lower()
        .split('"beliefs":')[0]
    )
    assert digest(c.repository.replay()) == digest(s)


def test_failed_bluff_cannot_fish_for_new_roll(game):
    c = game([[-1, -1, -1, -1]])
    complete(c, "Show the seal to Oren; the court expects me")
    s = c.repository.load()
    assert not s.entities["portal:gate"].portal.open
    result = c.submit("Show the seal to Oren; the court expects me")
    assert "unchanged claim" in result
    assert c.repository.load() == s and c.repository.pending() is None
    assert s.rng.counter == 4


def test_honest_offer_accept_decline_and_return_collateral(game):
    c = game([])
    c.submit("Oren, I need Sen")
    assert container(c.repository.load(), "item:royal_seal") == PC
    c.ensure_boundary()
    c.choose("decline")
    assert container(c.repository.load(), "item:royal_seal") == PC
    c.submit("Oren, I need Sen")
    c.ensure_boundary()
    c.choose("accept")
    assert container(c.repository.load(), "item:royal_seal") == "actor:oren"
    c.submit("Go to the courtyard")
    loan(c)
    c.submit("Use the key to unlock the records door")
    c.submit("Enter the records office")
    c.submit("Take the ledger")
    c.submit("Go to the courtyard")
    c.submit("Give Sen the key")
    c.submit("Go outside to the gatehouse")
    s = c.repository.load()
    assert s.quests["recover_ledger"].terminal_result == "success"
    assert container(s, "item:royal_seal") == PC
    assert container(s, "item:records_key") == "actor:sen"
    assert all(c.status == "fulfilled" for c in s.commitments.values())
    assert digest(c.repository.replay()) == digest(s)


@pytest.mark.parametrize(
    "actor_dice,defender_dice,revealed,uses,boost",
    [
        ([-1] * 4, [0] * 4, False, 0, False),
        ([-1, -1, -1, 0], [0] * 4, False, 0, True),
        ([-1, -1, 0, 0], [0] * 4, True, 1, False),
        ([1] * 4, [0] * 4, True, 2, False),
    ],
)
def test_discover_unknown_aspect(game, actor_dice, defender_dice, revealed, uses, boost):
    def no_pool(state):
        state.scene.gm_fate_pool = 0

    c = game([actor_dice, defender_dice], change=no_pool)
    c.submit("Read Oren's concern")
    assert "Fear of Being Blamed" not in c.describe()
    complete(c, "/pass")
    s = c.repository.load()
    a = s.aspects["aspect:oren:blame"]
    assert (PC in a.known_by) == revealed
    assert a.free_invokes.get(PC, 0) == uses
    assert any(a.kind == "boost" and a.subject_id == PC for a in s.aspects.values()) == boost
    if revealed:
        assert "Fear of Being Blamed" in execute(c, "/aspects")


def test_address_discovered_concern_does_not_assert_invitation(game):
    def known(state):
        state.aspects["aspect:oren:blame"].known_by.append(PC)

    c = game([[0] * 4], change=known)
    complete(c, "Reassure Oren and address his concern")
    s = c.repository.load()
    assert s.entities["portal:gate"].portal.open
    assert "belief:oren:invitation" not in s.beliefs
    assert s.propositions["proposition:lea_expected"].truth == "false"


@pytest.mark.parametrize(
    "dice,open,lodged,noisy",
    [
        ([-1] * 4, False, False, True),
        ([-1, 0, 0, 0], True, True, False),
        ([0] * 4, True, False, False),
        ([1] * 4, True, False, False),
    ],
)
def test_lockpick_outcomes(game, dice, open, lodged, noisy):
    c = game([dice], change=courtyard)
    complete(c, "Pick the records door lock")
    s = c.repository.load()
    assert s.entities["portal:records_door"].portal.open == open
    assert (container(s, "item:lockpicks") == "portal:records_door") == lodged
    assert (
        any(
            a.text == "Noisy Intrusion" and a.free_invokes.get("actor:sen") == 1
            for a in s.aspects.values()
        )
        == noisy
    )
    if lodged:
        c.submit("Retrieve my lockpicks")
        assert container(c.repository.load(), "item:lockpicks") == PC
        assert c.repository.load().clock.tick == 2
    assert digest(c.repository.replay()) == digest(c.repository.load())


def test_take_on_deadline_beats_collection(game):
    def change(s):
        s.entities[PC].placement = Placement(container_id="location:records")
        s.clock.tick = 19
        s.scene.definition_ref = "scene:records"

    c = game([], change=change)
    c.submit("Take the ledger")
    s = c.repository.load()
    assert s.clock.tick == 20 and container(s, "item:ledger") == PC
    assert s.scheduled_effects["schedule:deadline"].status == "skipped"
    assert s.quests["recover_ledger"].stage == "acquired"


def test_receipt_compel_zero_tick_once_and_promise_breach(game):
    c = game([], change=courtyard)
    c.submit("Ask Sen for help with the key")
    c.ensure_boundary()
    c.choose("accept")
    before = c.repository.load()
    c.ensure_boundary()
    assert "compel" in c.describe().lower()
    c.choose("accept")
    s = c.repository.load()
    assert s.clock == before.clock and s.entities[PC].fate.fate_points == 4
    assert container(s, "item:archive_receipt") == PC
    assert c.ensure_boundary() is None
    c.submit("Use the key to open the door")
    c.submit("Enter the records office")
    c.submit("Take the ledger")
    c.submit("Go to the courtyard")
    c.submit("Go outside to the gatehouse")
    s = c.repository.load()
    assert s.quests["recover_ledger"].stage == "completed"
    assert s.relationships["relationship:sen_lea"].value == 0
    assert any(
        o.status == "broken" and o.fulfillment_predicate.item_id == "item:records_key"
        for o in s.commitments.values()
    )


@pytest.mark.parametrize("choice,points", [("refuse", 2), ("object", 3)])
def test_compel_refusal_and_objection(game, choice, points):
    c = game([], change=courtyard)
    loan(c, choice)
    assert c.repository.load().entities[PC].fate.fate_points == points
    assert "item:archive_receipt" not in c.repository.load().entities


def test_no_refusal_with_zero_points(game):
    def change(s):
        courtyard(s)
        s.entities[PC].fate.fate_points = 0

    c = game([], change=change)
    c.submit("Ask Sen for help with the key")
    c.ensure_boundary()
    c.choose("accept")
    c.ensure_boundary()
    assert "refuse" not in [o.id for o in c.repository.pending().current_choice.options]
    c.choose("object")
    assert c.repository.load().entities[PC].fate.fate_points == 0


def test_real_cli_demos_and_eof_resume(tmp_path, game):
    out = io.StringIO()
    success, failure = run_demo(tmp_path, out, True)
    assert "Saved and reloaded" in out.getvalue()
    for path, expected in [(success, "success"), (failure, "failure")]:
        r = SQLiteRepository(path)
        assert r.replay().quests["recover_ledger"].terminal_result == expected
        r.close()
    c = game([[-1, -1, 0, 0]])
    out = io.StringIO()
    repl(c, io.StringIO("I show Oren the seal and say the court expects me\n/quit\n"), out, True)
    assert c.repository.pending().actor_dice == [-1, -1, 0, 0]
    assert c.repository.load().clock.tick == 0
    assert "Saved. Goodbye." in out.getvalue() and "\x1b[" not in out.getvalue()
