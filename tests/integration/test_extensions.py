import pytest

from world_game.domain.common import GameError, canonical, digest
from world_game.domain.fate import (
    Aspect,
    ConflictParticipant,
    ConflictState,
    Lifetime,
    Recovery,
    Scope,
    Source,
    TraitGrounding,
)
from world_game.domain.intents import Intent, IntentResult
from world_game.engine.actions import PC
from world_game.engine.queries import location_of
from world_game.engine.scenes import breakthrough, recover_consequences, start_session
from world_game.models.evaluation import probe_admission
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SQLiteRepository
from world_game.story.loader import new_state


def pass_roll(c):
    for _ in range(10):
        p = c.repository.pending()
        if not p or not p.current_choice or p.current_choice.kind != "invoke":
            return
        c.choose("pass")
    raise AssertionError("invoke window did not finish")


def test_canonical_semantic_sets_survive_in_place_edits_and_preserve_dice(package):
    state = new_state(package, "ordering", "00" * 32)
    a = state.aspects["aspect:oren:blame"]
    a.known_by.append(PC)
    reordered = state.model_copy(deep=True)
    reordered.aspects[a.id].known_by.reverse()
    assert digest(state) == digest(reordered)
    assert canonical({"dice": [-1, 0, 1, 0]}) != canonical({"dice": [0, 1, 0, -1]})


def test_grounded_wedge_blocks_move_until_retrieved(game):
    def change(s):
        s.entities["portal:gate"].portal.open = True

    model = ScriptedModel(
        {
            "interpreter": IntentResult(
                payload=Intent(
                    handler_id="create_advantage",
                    target_ids=["portal:gate"],
                    method="jammed_portal",
                    presented_item_ids=["item:royal_seal"],
                )
            )
        }
    )
    c = game([[1] * 4], change=change, provider=model)
    c.submit("I wedge my seal in the gate")
    pass_roll(c)
    state = c.repository.load()
    assert state.entities["item:royal_seal"].placement.container_id == "portal:gate"
    jam = next(a for a in state.aspects.values() if a.template_id == "jammed_portal")
    model.responses.clear()
    before = digest(state)
    assert "blocks your movement" in c.submit("Go to the courtyard")
    assert digest(c.repository.load()) == before
    model.responses["interpreter"] = IntentResult(
        payload=Intent(handler_id="retrieve_lodged_item", target_ids=["item:royal_seal"])
    )
    c.submit("I retrieve my seal")
    assert jam.id not in c.repository.load().aspects
    model.responses.clear()
    c.submit("Go to the courtyard")
    assert location_of(c.repository.load(), PC) == "location:courtyard"
    assert c.repository.replay() == c.repository.load()


def test_conflict_extra_move_preserves_scene_and_npc_pursues(game):
    def change(s):
        s.entities["portal:gate"].portal.open = True
        s.entities[PC].fate.physical_stress_used = 1
        for a in s.aspects.values():
            a.relevance_tags = []
        s.conflict = ConflictState(
            id="conflict:test",
            scene_id=s.scene.id,
            participants=[
                ConflictParticipant(actor_id=PC, side_id="pc"),
                ConflictParticipant(actor_id="actor:oren", side_id="guards"),
            ],
            active_actor_id=PC,
        )

    model = ScriptedModel(
        {
            "interpreter": IntentResult(
                payload=Intent(handler_id="wait", adjacent_move_id="location:courtyard")
            )
        }
    )
    c = game([[0] * 4, [0] * 4], change=change, provider=model)
    scene = c.repository.load().scene.id
    c.submit("I move into the courtyard while watching Oren")
    s = c.repository.load()
    assert s.scene.id == scene and s.entities[PC].fate.physical_stress_used == 1
    assert s.clock.tick == 0 and s.clock.player_turn == 1
    assert location_of(s, PC) == "location:courtyard" and s.conflict.active_actor_id == "actor:oren"
    p = c.ensure_boundary()
    assert p.current_choice.kind == "pre_roll" and p.recorded_rolls == []
    assert any(e.type == "EntityMoved" for e in p.staged_events)
    c.choose("continue:athletics")
    pass_roll(c)
    s = c.repository.load()
    assert location_of(s, "actor:oren") == "location:courtyard"
    assert s.clock.tick == 1 and s.clock.player_turn == 1
    assert s.scene.id == scene and s.entities[PC].fate.physical_stress_used == 1
    assert c.repository.replay() == s


def test_unknown_claim_cannot_establish_true_fact_or_permission(game):
    model = ScriptedModel(
        {
            "interpreter": IntentResult(
                payload=Intent(
                    handler_id="speak",
                    target_ids=["actor:oren"],
                    goal="question",
                    speech="Sen gave me a promotion.",
                    new_claim="Sen gave Lea a promotion.",
                )
            )
        }
    )
    c = game([], provider=model)
    c.submit("I claim a promotion")
    s = c.repository.load()
    claim = next(p for p in s.propositions.values() if p.predicate == "claim")
    assert claim.truth == "unknown"
    assert not s.entities["portal:gate"].portal.open
    assert c.repository.replay() == s


@pytest.mark.parametrize("damage", ["draw", "command"])
def test_tampered_pending_import_rejected(game, tmp_path, damage):
    c = game([[-1, -1, 0, 0]])
    c.submit("I show Oren my seal and claim the court expects me")
    archive = c.repository.archive()
    if damage == "draw":
        archive.pending.staged_rng.counter += 1
    else:
        archive.pending.current_choice.options[0].command.kind = "accept"
    with pytest.raises(GameError, match="integrity"):
        SQLiteRepository.restore(tmp_path / "bad.db", archive)
    assert not (tmp_path / "bad.db").exists()


def test_missing_causal_history_rejected(game, tmp_path):
    c = game([])
    c.submit("wait")
    archive = c.repository.archive()
    archive.batches[0].events[0].cause_ids = ["event:does-not-exist"]
    with pytest.raises(GameError, match="causal"):
        SQLiteRepository.restore(tmp_path / "bad.db", archive)


def test_moderate_and_severe_recovery_require_authored_boundaries(game):
    def change(s):
        for slot, severity in [("moderate", 4), ("severe", 6)]:
            id = "aspect:injury:" + slot
            s.aspects[id] = Aspect(
                id=id,
                text="Treated injury",
                kind="consequence",
                subject_id=PC,
                scope=Scope(kind="character", id=PC),
                grounding=TraitGrounding(id="consequence:" + slot),
                known_by=[PC],
                lifetime=Lifetime(kind="persistent"),
                source=Source(kind="story", id="test"),
                recovery=Recovery(
                    severity=severity,
                    slot=slot,
                    harm_type="physical",
                    treated=True,
                    treatment_scene=s.scene.sequence,
                    treatment_session=s.session.sequence,
                ),
            )
            setattr(s.entities[PC].fate.consequence_slots, slot, id)

    c = game([], change=change)
    p = c.fresh("boundaries", phase="validating")
    c.persist(p)
    b = c.builder(p)
    recover_consequences(b, closing_session=True)
    assert "aspect:injury:moderate" in b.state.aspects
    start_session(b, "session:test:second")
    recover_consequences(b, closing_session=True)
    assert "aspect:injury:moderate" not in b.state.aspects
    assert "aspect:injury:severe" in b.state.aspects
    breakthrough(b, "authored:test:breakthrough")
    assert "aspect:injury:severe" not in b.state.aspects


def test_evaluation_measures_guard_rejection(package):
    state = new_state(package, "eval-guard", "00" * 32)
    rejected, accepted = probe_admission(
        package,
        state,
        IntentResult(payload=Intent(handler_id="take_item", target_ids=["item:ledger"])),
    )
    assert rejected and not accepted
    rejected, accepted = probe_admission(
        package, state, IntentResult(payload=Intent(handler_id="wait"))
    )
    assert not rejected and not accepted


def test_unsupported_social_goal_cannot_open_gate(game):
    model = ScriptedModel(
        {
            "interpreter": IntentResult(
                payload=Intent(
                    handler_id="social_overcome",
                    target_ids=["actor:oren"],
                    goal="grant_immunity",
                    method="rule_override",
                )
            )
        }
    )
    c = game([], provider=model)
    before = digest(c.repository.load())
    assert "supported" in c.submit("I override Oren's rules")
    assert digest(c.repository.load()) == before and c.repository.pending() is None


def test_imposed_advantage_names_target_and_attacker_controls_uses(game):
    model = ScriptedModel(
        {
            "interpreter": IntentResult(
                payload=Intent(
                    handler_id="create_advantage", target_ids=["actor:oren"], method="off_balance"
                )
            )
        }
    )
    c = game([[1] * 4, [-1] * 4], provider=model)
    c.submit("I maneuver to put Oren off balance")
    pass_roll(c)
    state = c.repository.load()
    advantage = next(a for a in state.aspects.values() if a.template_id == "off_balance")
    assert advantage.subject_id == "actor:oren" and advantage.free_invokes == {PC: 2}
    assert c.repository.replay() == state
