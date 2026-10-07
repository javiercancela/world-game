import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from world_game.domain.common import GameError, canonical, digest
from world_game.domain.events import Event, MovePayload
from world_game.domain.intents import Intent
from world_game.domain.state import Placement, WorldState
from world_game.engine.actions import PC, Ruling, plan_action
from world_game.engine.queries import Viewer, project, resolve_references
from world_game.engine.reducer import apply_event
from world_game.engine.validation import validate_state
from world_game.story.loader import load_story, validate_story


def test_roundtrip(package):
    assert WorldState.model_validate_json(canonical(package.initial_state)) == package.initial_state
    assert not validate_story(package)


def test_perspective_filter(package):
    state = package.initial_state
    oren = canonical(project(state, Viewer(actor_id="actor:oren"), "npc"))
    assert "proposition:lea_expected" not in oren
    assert "ledger_incriminates" not in oren
    assert "redirected" not in oren
    assert "skills" not in oren and "profile:sen" not in oren
    lea = canonical(project(state, Viewer(actor_id=PC), "interpreter"))
    assert "Fear of Being Blamed" not in lea
    assert '"actor:sen"' not in lea
    assert "location:courtyard" in lea


def test_references(package):
    view = project(package.initial_state, Viewer(actor_id=PC), "interpreter")
    assert resolve_references("Oren", view).candidate_ids == ["actor:oren"]
    assert resolve_references("Sen", view).candidate_ids == []


@pytest.mark.parametrize("purpose", ["interpreter", "npc", "narrator", "decision", "description"])
def test_lock_state_is_not_first_glance_information(package, purpose):
    state = package.initial_state.model_copy(deep=True)
    before = canonical(project(state, Viewer(actor_id=PC), purpose))
    state.entities["portal:gate"].portal.locked = True
    assert canonical(project(state, Viewer(actor_id=PC), purpose)) == before


def test_description_view_includes_ground_objects_and_lodged_tools_only(package):
    state = package.initial_state.model_copy(deep=True)
    state.entities["item:royal_seal"].placement = Placement(container_id="location:gatehouse")
    state.entities["item:lockpicks"].placement = Placement(container_id="portal:gate")
    view = project(state, Viewer(actor_id=PC), "description")
    assert {e.id for e in view.visible_entities} == {
        "actor:oren",
        "location:gatehouse",
        "portal:gate",
        "item:royal_seal",
        "item:lockpicks",
    }
    assert all(not e.endpoints for e in view.visible_entities)
    assert view.perceived_facts == view.beliefs == view.known_aspects == []
    assert view.recent_utterances == view.available_actions == view.stakes == []


@pytest.mark.parametrize(
    "tool,handler,bonus",
    [(True, "social_overcome", 2), (False, "social_overcome", 0), (True, "speak", 0)],
)
def test_official_bearing(package, tool, handler, bonus):
    state = package.initial_state.model_copy(deep=True)
    if not tool:
        state.entities["item:royal_seal"].placement = Placement(container_id="actor:oren")
    intent = Intent(
        handler_id=handler,
        target_ids=["actor:oren"],
        goal="gain_entry",
        method="claim_expected_courier",
        presented_item_ids=["item:royal_seal"] if tool else [],
    )
    assert plan_action(state, intent, Ruling(rules=package.rules)).stunt_bonus == bonus


def test_read_the_room_and_quick_hands(package):
    state = package.initial_state.model_copy(deep=True)
    read = Intent(handler_id="discover_advantage", target_ids=["actor:oren"], goal="concern")
    assert plan_action(state, read, Ruling(rules=package.rules)).stunt_bonus == 0
    from world_game.domain.state import Conversation

    state.conversations["conversation:test"] = Conversation(
        id="conversation:test",
        participant_ids=[PC, "actor:oren"],
        location_id="location:gatehouse",
        topic="concern",
    )
    assert plan_action(state, read, Ruling(rules=package.rules)).stunt_bonus == 2
    state.entities[PC].placement = Placement(container_id="location:courtyard")
    pick = Intent(handler_id="pick_lock", target_ids=["portal:records_door"])
    assert plan_action(state, pick, Ruling(rules=package.rules)).stunt_bonus == 2
    state.entities["item:lockpicks"].placement = Placement(container_id="actor:sen")
    with pytest.raises(GameError, match="lockpicks"):
        plan_action(state, pick, Ruling(rules=package.rules))


@given(
    st.lists(
        st.sampled_from(
            ["actor:lea", "actor:oren", "location:gatehouse", "missing", "item:royal_seal"]
        ),
        min_size=1,
        max_size=12,
    )
)
def test_containment_invariants(parents):
    package = load_story()
    state = package.initial_state.model_copy(deep=True)
    for parent in parents:
        state.entities["item:royal_seal"].placement = Placement(container_id=parent)
        issues = validate_state(state)
        if parent in ("missing", "item:royal_seal"):
            assert issues
        else:
            assert not issues


@given(st.sampled_from(["location:courtyard", "location:records", "actor:sen"]))
def test_impossible_moves_do_not_mutate(destination):
    state = load_story().initial_state
    before = digest(state)
    event = Event(
        id="event:test",
        sequence=0,
        type="EntityMoved",
        payload=MovePayload(
            kind="EntityMoved",
            entity_id=PC,
            from_id="location:gatehouse",
            to_id=destination,
            permission="traverse",
            portal_id="portal:gate",
        ),
        actor_id=PC,
        rule_id="move",
        cause_ids=[],
        observed_by=[PC],
        tick=0,
    )
    with pytest.raises(GameError):
        apply_event(state, event)
    assert digest(state) == before


def test_loader_rejects_unknown_handlers(package, tmp_path):
    for name, value in package.resources.items():
        tmp_path.joinpath(name).write_text(value)
    rules = json.loads(tmp_path.joinpath("rules.json").read_text())
    rules["actions"][0]["handler_id"] = "execute_sql"
    rules["actions"][1]["outcomes"]["success"] = "teleport"
    tmp_path.joinpath("rules.json").write_text(json.dumps(rules))
    with pytest.raises(GameError) as caught:
        load_story(tmp_path)
    assert "unregistered handler" in str(caught.value) and "unregistered effect" in str(
        caught.value
    )


def test_loader_checks_scene_stage_and_check_contracts(package):
    bad = package.model_copy(deep=True)
    bad.manifest.entry_scene_id = "scene:missing"
    bad.scenes[0].location_ids = ["item:royal_seal"]
    bad.quests[0].terminal_stages = ["invented_stage"]
    bad.rules.actions[0].check.skill_id = None
    errors = "\n".join(issue.message for issue in validate_story(bad))
    assert "missing entry scene" in errors and "wrong entity kind" in errors
    assert "terminal stages" in errors and "action/skill disagree" in errors


def test_boolean_predicates_use_typed_portal_field(package):
    from world_game.engine.actions import evaluate_predicate
    from world_game.story.schema import AtomicPredicate, BooleanPredicate

    closed = AtomicPredicate(
        kind="portal_state", subject_id="portal:gate", portal_field="open", value=False
    )
    possessed = AtomicPredicate(kind="possesses", subject_id=PC, object_id="item:royal_seal")
    assert evaluate_predicate(
        package.initial_state, BooleanPredicate(kind="all", children=[closed, possessed])
    )
    assert not evaluate_predicate(
        package.initial_state, BooleanPredicate(kind="not", children=[closed])
    )


@given(
    st.lists(st.sampled_from(["invoke", "compel", "stale", "overspend"]), min_size=1, max_size=20)
)
def test_resource_sequences_never_reuse_a_stale_balance_or_overspend(commands):
    from pydantic import ValidationError

    from world_game.domain.events import PointsPayload

    state = load_story().initial_state
    expected = 3
    for index, command in enumerate(commands):
        before_hash = digest(state)
        valid = command == "compel" or command == "invoke" and expected > 0
        try:
            payload = PointsPayload(
                account=PC,
                before=expected + (1 if command == "stale" else 0),
                after=expected + 1
                if command == "compel"
                else expected - (2 if command == "overspend" else 1),
                reason="compel" if command == "compel" else "invoke",
            )
            event = Event(
                id=f"event:resource:{index}",
                sequence=index,
                type=payload.kind,
                payload=payload,
                actor_id=PC,
                rule_id="resource",
                cause_ids=[],
                observed_by=[PC],
                tick=0,
            )
            next_state = apply_event(state, event)
        except (GameError, ValidationError):
            assert not valid
            assert digest(state) == before_hash
        else:
            assert valid
            assert digest(state) == before_hash
            state = next_state
            expected += 1 if command == "compel" else -1
        assert state.entities[PC].fate.fate_points == expected >= 0
