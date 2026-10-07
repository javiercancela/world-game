import io

import pytest

from world_game.cli import execute, repl
from world_game.domain.common import canonical, digest
from world_game.domain.proposals import DescriptionResult
from world_game.models.interfaces import ProviderError
from world_game.models.scripted import ScriptedModel


def test_look_uses_dedicated_agent_and_keeps_inventory_separate(game):
    prose = "Dusk gathers at the royal gate. Oren watches beside the closed gate."
    model = ScriptedModel(
        {
            "describer": DescriptionResult(
                prose=prose,
                mentioned_entity_ids=["actor:oren", "portal:gate", "location:gatehouse"],
            )
        }
    )
    c = game([], provider=model)
    before = digest(c.repository.load())
    assert execute(c, "/look") == prose
    assert execute(c, "/look") == prose
    assert [role for role, _ in model.calls] == ["describer"]
    request = model.calls[0][1]
    assert request.context.purpose == "description"
    assert {e.id for e in request.context.visible_entities} == {
        "actor:oren",
        "location:gatehouse",
        "portal:gate",
    }
    wire = canonical(request)
    for hidden in ("lockpicks", "royal_seal", "locked", "courtyard", "lea_expected", "skills"):
        assert hidden not in wire
    assert execute(c, "/inventory") == "Inventory: Lockpicks, Royal Seal"
    assert len(model.calls) == 1
    assert digest(c.repository.load()) == before
    assert c.repository.pending() is None
    assert c.repository.batches() == [] and c.repository.history() == []


@pytest.mark.parametrize(
    "failure",
    [
        ProviderError("timeout"),
        KeyboardInterrupt(),
        "not JSON",
        DescriptionResult(prose="  ", mentioned_entity_ids=[]),
        DescriptionResult(prose="You see a ledger.", mentioned_entity_ids=["item:ledger"]),
        DescriptionResult(prose="You carry a seal.", mentioned_entity_ids=["item:royal_seal"]),
    ],
)
def test_failed_description_uses_visible_prose_without_creating_pending(game, failure):
    # ScriptedModel treats ordinary Exceptions as provider failures; interruption
    # remains a BaseException and needs to be raised by the provider itself.
    class Describer(ScriptedModel):
        def generate(self, **kwargs):
            if isinstance(failure, KeyboardInterrupt):
                raise failure
            return super().generate(**kwargs)

    model = Describer({"describer": failure} if not isinstance(failure, KeyboardInterrupt) else {})
    c = game([], provider=model)
    before = digest(c.repository.load())
    result = execute(c, "/look")
    assert "Oren" in result and "gate is closed" in result
    assert "locked" not in result and "Inventory" not in result and "Lea:" not in result
    assert "seal" not in result and "ledger" not in result and "Courtyard" not in result
    assert "\n" not in result
    assert c.repository.pending() is None and digest(c.repository.load()) == before


def test_look_preserves_a_pending_roll_and_its_provider_budget(game):
    model = ScriptedModel()
    c = game([[0] * 4], provider=model)
    c.submit("I show Oren the seal and say I am expected")
    pending = canonical(c.repository.pending())
    world = digest(c.repository.load())
    c.call_budget = 1  # The action has already used its only provider attempt.
    result = execute(c, "/look")
    assert "gate is closed" in result and "Pending interaction" in result
    assert model.calls[-1][0] == "describer"
    assert canonical(c.repository.pending()) == pending
    assert digest(c.repository.load()) == world


def test_description_updates_after_gate_opens_and_player_moves(game):
    model = ScriptedModel()
    c = game([[0] * 4], provider=model)
    assert "gate is closed" in execute(c, "/look")
    c.submit("I show Oren the seal and say I am expected")
    c.choose("pass")
    opened = execute(c, "/look")
    assert "gate is open" in opened and "closed" not in opened
    c.submit("Go to the courtyard")
    courtyard = execute(c, "/look")
    assert "Sen" in courtyard and "records door is closed" in courtyard
    assert "Shelves" not in courtyard and "locked" not in courtyard
    assert "Oren" not in courtyard and "Dusk gathers" not in courtyard
    assert sum(role == "describer" for role, _ in model.calls) == 3


def test_repl_starts_with_introduction_scene_prose_and_separate_inventory(game):
    c = game([])
    before = digest(c.repository.load())
    out = io.StringIO()
    repl(c, io.StringIO("/inventory\n/quit\n"), out, True)
    lines = out.getvalue().splitlines()
    scene = " ".join(out.getvalue().split("Inventory:")[0].split())
    assert "You are Lea, a courier wearing a borrowed official uniform." in scene
    assert "outside the royal gate" in scene
    assert "recover the ledger before nightfall (tick 20)" in scene
    assert "Sen, the archivist who once saved your life" in scene
    assert "your first challenge is to get past him" in scene
    assert "Actions advance time; /look and /inventory do not." in scene
    assert out.getvalue().index("You are Lea") < out.getvalue().index("Dusk gathers")
    assert "Oren" in scene and "gate is closed" in scene
    assert "Lockpicks" not in scene and "unlocked" not in scene
    assert "Inventory: Lockpicks, Royal Seal" in lines
    assert digest(c.repository.load()) == before
    assert c.repository.pending() is None
