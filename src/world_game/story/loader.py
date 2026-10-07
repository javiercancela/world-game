import json
import secrets
from importlib.resources import files
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from world_game.domain.common import GameError, ValidationIssue, canonical, digest
from world_game.domain.fate import Aspect, RngState
from world_game.domain.state import WorldState
from world_game.engine.intents_registry import REGISTERED_EFFECTS
from world_game.engine.validation import validate_state
from world_game.story.schema import (
    BooleanPredicate,
    Dialogue,
    Manifest,
    Predicate,
    Profile,
    QuestDefinition,
    Rules,
    SceneDefinition,
    StoryPackage,
)


def validate_story(package: StoryPackage) -> list[ValidationIssue]:
    from world_game.domain.intents import HANDLERS

    issues = validate_state(package.initial_state)

    def issue(path: str, message: str) -> None:
        issues.append(ValidationIssue(path=path, message=message))

    ids: set[str] = set()

    def predicates(nodes: list[Predicate], depth: int = 1) -> None:
        if depth > 4:
            issue("rules.preconditions", "predicate depth exceeds four")
            return
        for node in nodes:
            if isinstance(node, BooleanPredicate):
                if node.kind == "not" and len(node.children) != 1:
                    issue("rules.preconditions", "not requires one child")
                predicates(node.children, depth + 1)
            else:
                references = {
                    **package.initial_state.entities,
                    **package.initial_state.aspects,
                    **package.initial_state.beliefs,
                    **package.initial_state.propositions,
                    **package.initial_state.relationships,
                    **package.initial_state.conversations,
                    **package.initial_state.quests,
                }
                if node.subject_id not in references or (
                    node.object_id and node.object_id not in references
                ):
                    issue("rules.preconditions", "missing predicate reference")

    for name, definitions in (
        ("profiles", package.profiles),
        ("scenes", package.scenes),
        ("quests", package.quests),
        ("stunts", package.rules.stunts),
    ):
        definition_ids = [definition.id for definition in definitions]
        if len(definition_ids) != len(set(definition_ids)):
            issue(name, "duplicate definition id")
    scenes = {scene.id: scene for scene in package.scenes}
    if package.manifest.entry_scene_id not in scenes:
        issue("manifest.entry_scene_id", "missing entry scene")
    if package.initial_state.scene.definition_ref not in scenes:
        issue("entities.scene.definition_ref", "missing scene definition")
    for scene in package.scenes:
        if not scene.location_ids or any(
            id not in package.initial_state.entities
            or package.initial_state.entities[id].location is None
            for id in scene.location_ids
        ):
            issue(f"scenes.{scene.id}.location_ids", "missing location or wrong entity kind")
    for definition in package.quests:
        if len(definition.stages) != len(set(definition.stages)) or not set(
            definition.terminal_stages
        ) <= set(definition.stages):
            issue(f"quests.{definition.id}", "invalid stages or terminal stages")
    if {stunt.id for stunt in package.rules.stunts} != {
        "official_bearing",
        "read_the_room",
        "quick_hands",
    }:
        issue("rules.stunts", "the three authored stunts are required")
    if set(package.rules.consequence_catalog) != {"physical", "mental"} or any(
        len(names) != 3 for names in package.rules.consequence_catalog.values()
    ):
        issue("rules.consequence_catalog", "three finite descriptions required for each harm track")

    for rule in package.rules.actions:
        if rule.id in ids:
            issue(f"rules.{rule.id}", "duplicate rule id")
        ids.add(rule.id)
        if rule.handler_id not in HANDLERS:
            issue(f"rules.{rule.id}.handler_id", "unregistered handler")
        if rule.check.difficulty not in (0, 2, 4, 6):
            issue(f"rules.{rule.id}.check.difficulty", "difficulty must be 0, 2, 4, or authored 6")
        if (
            rule.check.kind == "none"
            and (rule.check.action is not None or rule.check.skill_id is not None)
        ) or (
            rule.check.kind != "none" and (rule.check.action is None or rule.check.skill_id is None)
        ):
            issue(f"rules.{rule.id}.check", "check kind and action/skill disagree")
        if (
            rule.argument_schema_id != "Intent"
            or rule.visibility_policy_id != "perception"
            or rule.reaction_policy_id != "authored"
        ):
            issue(f"rules.{rule.id}", "unsupported argument schema or policy")
        for template in rule.outcomes.model_dump().values():
            if template is not None and template not in REGISTERED_EFFECTS:
                issue(f"rules.{rule.id}.outcomes", f"unregistered effect template {template}")
        if not set(rule.permitted_stunt_ids) <= {s.id for s in package.rules.stunts}:
            issue(f"rules.{rule.id}.permitted_stunt_ids", "missing stunt")
        predicates(rule.preconditions)
    required_rules = {
        "gate.bluff",
        "gate.concern",
        "gate.exit",
        "read.concern",
        "lock.pick",
        "advantage.new",
        "attack.physical",
        "treat",
    } | {
        "action." + handler
        for handler in HANDLERS
        if handler
        not in (
            "social_overcome",
            "discover_advantage",
            "create_advantage",
            "pick_lock",
            "attack",
            "treat_consequence",
        )
    }
    if not required_rules <= ids:
        issue(
            "rules.actions",
            "missing authored action rules: " + ", ".join(sorted(required_rules - ids)),
        )
    for profile in package.profiles:
        if profile.actor_id not in package.initial_state.entities or profile.private_known_by != [
            profile.actor_id
        ]:
            issue(f"profiles.{profile.id}", "invalid actor/profile visibility")
    for e in package.initial_state.entities.values():
        if e.actor and e.actor.profile_ref not in {p.id for p in package.profiles}:
            issue(f"entities.{e.id}.actor.profile_ref", "missing profile")
    for q in package.initial_state.quests.values():
        if q.definition_ref not in {d.id for d in package.quests}:
            issue(f"quests.{q.id}.definition_ref", "missing quest definition")
        else:
            definition = next(d for d in package.quests if d.id == q.definition_ref)
            if q.stage not in definition.stages:
                issue(f"quests.{q.id}.stage", "stage absent from quest definition")
    sheet = package.initial_state.entities[package.manifest.player_id].fate
    assert sheet
    if sorted(sheet.skills.values()) != [0, 1, 1, 2, 2, 2, 3, 3, 4] or len(sheet.stunt_ids) != 3:
        issue("entities.actor:lea.fate", "incorrect authored skill allocation or stunts")
    if set(sheet.stunt_ids) != {stunt.id for stunt in package.rules.stunts}:
        issue("entities.actor:lea.fate.stunt_ids", "missing authored stunt references")
    if (
        len(
            [
                a
                for a in package.initial_state.aspects.values()
                if a.kind == "character" and a.subject_id == "actor:lea"
            ]
        )
        != 5
    ):
        issue("aspects", "PC requires five character aspects")
    return issues


def load_story(
    directory: Path | None = None, resources: dict[str, str] | None = None
) -> StoryPackage:
    root = directory or files("world_game").joinpath("data/stories/gate_at_dusk")
    try:
        raw = resources or {
            name: root.joinpath(name).read_text(encoding="utf-8")
            for name in (
                "manifest.json",
                "entities.json",
                "profiles.json",
                "rules.json",
                "aspects.json",
                "quests.json",
                "scenes.json",
                "dialogue.json",
            )
        }
        manifest = Manifest.model_validate_json(raw["manifest.json"])
        if set(manifest.resources) != set(raw) - {"manifest.json"}:
            raise GameError("manifest.resources: unexpected or missing resource files")
        state = WorldState.model_validate_json(raw["entities.json"])
        state.aspects = TypeAdapter(dict[str, Aspect]).validate_json(
            raw["aspects.json"], strict=True
        )
        package = StoryPackage(
            manifest=manifest,
            initial_state=state,
            profiles=TypeAdapter(list[Profile]).validate_json(raw["profiles.json"], strict=True),
            rules=Rules.model_validate_json(raw["rules.json"]),
            quests=TypeAdapter(list[QuestDefinition]).validate_json(
                raw["quests.json"], strict=True
            ),
            scenes=TypeAdapter(list[SceneDefinition]).validate_json(
                raw["scenes.json"], strict=True
            ),
            dialogue=Dialogue.model_validate_json(raw["dialogue.json"]),
            resources=raw,
        )
    except (ValidationError, OSError, KeyError, json.JSONDecodeError) as error:
        raise GameError(f"Invalid story package: {error}") from error
    issues = validate_story(package)
    if issues:
        raise GameError("\n".join(f"{i.path}: {i.message}" for i in issues))
    return package


def package_hash(package: StoryPackage) -> str:
    # Hash normalized complete resources, preserving originals separately.
    return digest({name: json.loads(text) for name, text in package.resources.items()})


def new_state(package: StoryPackage, campaign_id: str, seed: str | None = None) -> WorldState:
    state = package.initial_state.model_copy(deep=True)
    state.campaign_id = campaign_id
    state.story_ref = f"{package.manifest.id}@{package_hash(package)}"
    state.ruleset_ref = f"{package.rules.id}@{digest(package.rules)}"
    state.rng = RngState(seed=seed or secrets.token_hex(32))
    return state


def export_schemas(directory: Path) -> None:
    from world_game.domain.events import EventBatch
    from world_game.domain.intents import IntentResult, PendingResolution

    directory.mkdir(parents=True, exist_ok=True)
    for model in (WorldState, EventBatch, IntentResult, PendingResolution, StoryPackage):
        directory.joinpath(f"{model.__name__}.schema.json").write_text(
            canonical(model.model_json_schema()) + "\n", encoding="utf-8"
        )
