import pytest

from world_game.domain.common import GameError, digest
from world_game.domain.events import AspectPayload
from world_game.domain.fate import ComponentGrounding
from world_game.domain.state import Placement
from world_game.engine.actions import PC, add_aspect
from world_game.engine.scenes import (
    close_scene,
    start_scene,
    start_session,
)
from world_game.engine.turns import Coordinator
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SQLiteRepository


def passes(c):
    for _ in range(10):
        p = c.repository.pending()
        if p is None or p.current_choice is None or p.current_choice.kind != "invoke":
            return
        c.choose("pass")
    raise AssertionError("passes")


def begin(c):
    c.submit("Attack Oren")
    passes(c)
    assert c.repository.load().conflict.active_actor_id == "actor:oren"
    c.ensure_boundary()
    assert c.repository.pending().current_choice.kind == "pre_roll"


def test_attack_tie_boost_once(game):
    c = game([[0, 0, 1, 1], [0] * 4])
    c.submit("Attack Oren")
    passes(c)
    s = c.repository.load()
    boosts = [a for a in s.aspects.values() if a.kind == "boost"]
    assert len(boosts) == 1 and boosts[0].subject_id == PC
    assert s.entities["actor:oren"].fate.physical_stress_used == 0
    assert s.clock.tick == 0 and s.clock.player_turn == 1
    assert s.scene.gm_fate_pool == 1


def test_npc_attack_harm_reload_and_mixed_absorption(game):
    c = game([[-1] * 4, [0] * 4, [1] * 4, [0] * 4])
    begin(c)
    c.choose("continue:athletics")
    passes(c)
    assert c.repository.pending().current_choice.kind == "cost"
    c.choose("keep")
    p = c.repository.pending()
    assert p.current_choice.kind == "harm" and p.damage == 4
    repo = SQLiteRepository(c.repository.path)
    resumed = Coordinator(repo, ScriptedModel(), c.dice)
    assert resumed.describe() == c.describe()
    selected = next(
        o for o in p.current_choice.options if o.command.stress == 2 and o.command.slots == ["mild"]
    )
    resumed.choose(selected.id)
    s = repo.load()
    assert s.entities[PC].fate.physical_stress_used == 2
    ref = s.entities[PC].fate.consequence_slots.mild
    assert s.aspects[ref].free_invokes["actor:oren"] == 1
    assert s.aspects[ref].recovery.severity == 2
    assert s.conflict.consequences_taken[PC] == 1
    assert s.conflict.active_actor_id == PC and s.conflict.exchange == 2
    assert s.clock.tick == 1 and s.clock.player_turn == 1
    assert s.world_version == 2
    assert digest(repo.replay()) == digest(s)
    repo.close()


def test_capture_taken_out_and_no_concession_after_roll(game):
    c = game([[-1] * 4, [0] * 4, [1] * 4, [-1] * 4])
    begin(c)
    c.choose("continue:athletics")
    with pytest.raises(GameError, match="after the roll"):
        c.offer_concession()
    passes(c)
    c.choose("keep")
    c.choose("taken_out")
    s = c.repository.load()
    assert s.quests["recover_ledger"].terminal_result == "failure"
    assert s.entities[PC].actor.status == "taken_out"
    assert s.conflict is None and s.clock.tick == 1
    assert len(s.session.completed_scene_ids) == 1
    assert s.session.finished
    assert "death" not in " ".join(c.repository.history(False)).lower()
    assert digest(c.repository.replay()) == digest(s)


@pytest.mark.parametrize("terms", ["custody", "abandon"])
def test_pre_roll_concession_terms_and_award(game, terms):
    c = game([[-1] * 4, [0] * 4])
    begin(c)
    c.choose("concede")
    assert c.repository.pending().current_choice.kind == "concession"
    c.choose(terms)
    s = c.repository.load()
    assert s.entities[PC].actor.status == "conceded"
    assert s.entities[PC].fate.fate_points == 4
    assert s.quests["recover_ledger"].terminal_result == "failure"
    assert s.conflict is None and s.clock.tick == 1
    assert len(s.session.completed_scene_ids) == 1
    assert digest(c.repository.replay()) == digest(s)


@pytest.mark.parametrize("opponent", ["Oren", "Sen"])
def test_take_out_npc_persistent_and_colocated_key(game, opponent):
    def change(s):
        if opponent == "Sen":
            s.entities[PC].placement = Placement(container_id="location:courtyard")
            s.scene.definition_ref = "scene:courtyard"
            s.scene.participant_ids = [PC, "actor:sen"]

    c = game([[1] * 4, [-1] * 4], change=change)
    c.submit("Attack " + opponent)
    passes(c)
    c.choose("keep")
    s = c.repository.load()
    id = "actor:" + opponent.lower()
    assert s.entities[id].actor.status == "taken_out"
    assert s.conflict is None and s.clock.tick == 1
    assert s.clock.player_turn == 1
    if opponent == "Sen":
        assert s.entities["item:records_key"].placement.container_id == PC
    else:
        assert s.entities["portal:gate"].portal.open
        c.submit("Go to the courtyard")
        assert c.repository.load().entities[id].actor.status == "taken_out"
    assert digest(c.repository.replay()) == digest(c.repository.load())


def test_scene_boost_stress_and_persistent_grounding(game):
    c = game([])
    p = c.fresh("boundary", phase="validating")
    c.persist(p)
    b = c.builder(p)
    boost = add_aspect(b, PC, "momentary_opening")
    persistent = add_aspect(
        b,
        PC,
        "jammed_portal",
        kind="situation",
        grounding=ComponentGrounding(id="item:royal_seal", predicate="possessed", value_id=PC),
    )
    close_scene(b, "test.close")
    assert boost not in b.state.aspects and persistent in b.state.aspects
    start_scene(b, "scene:courtyard", "test.start")
    assert b.state.scene.gm_fate_pool == 1 and b.state.session.sequence == 1


def test_recovery_requires_complete_later_boundary_and_session_refresh(game):
    c = game([[-1] * 4, [0] * 4, [1] * 4, [0] * 4])
    begin(c)
    c.choose("continue:athletics")
    passes(c)
    c.choose("keep")
    p = c.repository.pending()
    choice = next(
        o for o in p.current_choice.options if o.command.slots == ["mild"] and o.command.stress == 2
    )
    c.choose(choice.id)
    ref = c.repository.load().entities[PC].fate.consequence_slots.mild
    p = c.fresh("treatment-boundaries", phase="validating")
    c.persist(p)
    b = c.builder(p)
    old = b.state.aspects[ref]
    after = old.model_copy(deep=True)
    after.recovery.treated = True
    after.recovery.treatment_scene = b.state.scene.sequence
    after.recovery.treatment_session = b.state.session.sequence
    b.emit(AspectPayload(kind="AspectChanged", id=ref, before=old, after=after))
    close_scene(b, "scene:treatment:end")
    assert ref in b.state.aspects
    start_scene(b, "scene:courtyard", "scene:next")
    close_scene(b, "scene:subsequent:end")
    assert ref not in b.state.aspects and b.state.entities[PC].fate.consequence_slots.mild is None
    # Loading never refreshes. Only an authored second-session handler does.
    from world_game.domain.events import PointsPayload

    sheet = b.state.entities[PC].fate
    b.emit(PointsPayload(account=PC, before=sheet.fate_points, after=2, reason="invoke"))
    b.emit(PointsPayload(account=PC, before=2, after=1, reason="invoke"))
    start_session(b, "session:2:start")
    assert b.state.entities[PC].fate.fate_points == 3
    version = b.state.session.sequence
    start_session(b, "session:2:start")
    assert b.state.session.sequence == version
