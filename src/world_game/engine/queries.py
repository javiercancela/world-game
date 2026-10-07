from typing import Literal

from pydantic import Field

from world_game.domain.common import ID, Text, Value
from world_game.domain.fate import Aspect
from world_game.domain.intents import HANDLERS
from world_game.domain.state import Entity, WorldState


class Viewer(Value):
    actor_id: ID


ContextPurpose = Literal["interpreter", "npc", "narrator", "decision", "description"]


class VisibleEntity(Value):
    id: ID
    name: str
    aliases: list[str]
    description: Text
    kind: str
    container_id: ID | None = None
    # Open/closed is visible; mechanical lock state is deliberately absent.
    open: bool | None = None
    endpoints: list[ID] = Field(default_factory=list)


class PerceivedFact(Value):
    id: ID
    description: Text
    value: str | bool | int | None
    source: str
    acquired_tick: int
    stale: bool = False


class KnownAspect(Value):
    id: ID
    text: Text
    free_invokes: int


class VisibleUtterance(Value):
    id: ID
    speaker_id: ID
    text: Text


class ContextView(Value):
    world_version: int
    pending_revision: int
    viewer_id: ID
    purpose: ContextPurpose
    visible_entities: list[VisibleEntity]
    perceived_facts: list[PerceivedFact]
    beliefs: list[PerceivedFact]
    known_aspects: list[KnownAspect]
    recent_utterances: list[VisibleUtterance]
    available_actions: list[str]
    stakes: list[Text]


class ReferenceCandidates(Value):
    text: Text
    candidate_ids: list[ID]
    exact: bool


def container(state: WorldState, entity_id: str) -> str | None:
    placement = state.entities[entity_id].placement
    return placement.container_id if placement else None


def location_of(state: WorldState, entity_id: str) -> str | None:
    visited: set[str] = set()
    while entity_id in state.entities and entity_id not in visited:
        visited.add(entity_id)
        entity = state.entities[entity_id]
        if entity.kind == "location":
            return entity_id
        if entity.placement is None:
            return None
        entity_id = entity.placement.container_id
    return None


def possesses(state: WorldState, actor_id: str, item_id: str) -> bool:
    return item_id in state.entities and container(state, item_id) == actor_id


def co_located(state: WorldState, a: str, b: str) -> bool:
    loc = location_of(state, a)
    return loc is not None and loc == location_of(state, b)


def inventory(state: WorldState, actor_id: str) -> list[Entity]:
    return sorted(
        (
            e
            for e in state.entities.values()
            if e.placement and e.placement.container_id == actor_id
        ),
        key=lambda e: e.id,
    )


def visible_entity_ids(state: WorldState, actor_id: str) -> list[str]:
    """Available action references, including inventory and adjacent destinations."""
    loc = location_of(state, actor_id)
    nearby = [e.portal for e in state.entities.values() if e.portal and loc in e.portal.endpoints]
    destinations = {endpoint for portal in nearby for endpoint in portal.endpoints}
    visible = []
    for e in state.entities.values():
        holder = state.entities.get(e.placement.container_id) if e.placement else None
        lodged = bool(holder and holder.portal and loc in holder.portal.endpoints)
        if (
            (
                state.conflict is not None
                and e.id in {p.actor_id for p in state.conflict.participants}
            )
            or e.id in destinations
            or e.id == actor_id
            or (e.portal and loc in e.portal.endpoints)
            or (e.placement and e.placement.container_id in (actor_id, loc))
            or lodged
        ):
            visible.append(e.id)
    return sorted(visible)


def scene_entity_ids(state: WorldState, actor_id: str) -> list[str]:
    """First-glance surroundings, excluding possessions and remote scene details.

    Adjacency permits targeting a destination, but does not establish a sightline
    into it. Only the current location, its contents, and nearby portals qualify.
    """
    loc = location_of(state, actor_id)
    if loc is None:
        return []
    visible = []
    for e in state.entities.values():
        if e.id == actor_id:
            continue
        holder = state.entities.get(container(state, e.id) or "")
        lodged = bool(holder and holder.portal and loc in holder.portal.endpoints)
        if (
            e.id == loc
            or (e.portal and loc in e.portal.endpoints)
            or (e.placement and e.placement.container_id == loc)
            or lodged
        ):
            visible.append(e.id)
    return sorted(visible)


def grounding_holds(state: WorldState, aspect: Aspect) -> bool:
    g = aspect.grounding
    if g.kind == "authored_trait":
        return True
    if g.kind == "component_predicate":
        entity = state.entities.get(g.id)
        if not entity:
            return False
        if g.predicate in ("possessed", "lodged"):
            return container(state, g.id) == g.value_id
        if g.predicate == "open":
            return entity.portal is not None and entity.portal.open
        return bool(entity.item and g.predicate in entity.item.tags)
    if g.kind == "belief":
        return g.id in state.beliefs and state.beliefs[g.id].stance == "believes"
    if g.kind == "proposition":
        return g.id in state.propositions and state.propositions[g.id].truth == "true"
    return g.id in state.relationships


def project(
    state: WorldState,
    viewer: Viewer,
    purpose: ContextPurpose,
    pending_revision: int = 0,
    utterances: list[VisibleUtterance] | None = None,
) -> ContextView:
    actor_id = viewer.actor_id
    visible = (
        scene_entity_ids(state, actor_id)
        if purpose == "description"
        else visible_entity_ids(state, actor_id)
    )
    entities = []
    # Filter before retrieval; other actors' sheets/profiles are never serialized.
    for entity_id in visible:
        e = state.entities[entity_id]
        entities.append(
            VisibleEntity(
                id=e.id,
                name=e.identity.name,
                aliases=e.identity.aliases,
                description=e.identity.description,
                kind=e.kind,
                container_id=container(state, e.id),
                open=e.portal.open if e.portal else None,
                endpoints=e.portal.endpoints if e.portal and purpose != "description" else [],
            )
        )
    if purpose == "description":
        # Do not retrieve knowledge, inventory, dialogue, or mechanics for this role.
        return ContextView(
            world_version=state.world_version,
            pending_revision=pending_revision,
            viewer_id=actor_id,
            purpose=purpose,
            visible_entities=entities,
            perceived_facts=[],
            beliefs=[],
            known_aspects=[],
            recent_utterances=[],
            available_actions=[],
            stakes=[],
        )
    knowledge = state.knowledge[actor_id]
    facts = [
        PerceivedFact(
            id=o.record_id,
            description=o.field,
            value=o.value,
            source=o.source_event_id,
            acquired_tick=o.acquired_tick,
            stale=o.acquired_tick < state.clock.tick and o.record_id not in visible,
        )
        for o in knowledge.observations[-30:]
    ]
    # NPCs get their stance without canonical truth alongside it.
    beliefs = [
        PerceivedFact(
            id=b.id,
            description=state.propositions[b.proposition_id].predicate,
            value=b.stance,
            source=",".join(b.source_event_ids),
            acquired_tick=b.acquired_tick,
        )
        for b in state.beliefs.values()
        if b.holder_id == actor_id
    ]
    for ref in knowledge.known_record_ids:
        if ref in state.propositions and not (
            purpose == "npc"
            and any(
                b.holder_id == actor_id and b.proposition_id == ref for b in state.beliefs.values()
            )
        ):
            p = state.propositions[ref]
            facts.append(
                PerceivedFact(
                    id=p.id,
                    description=p.predicate,
                    value=p.truth,
                    source=p.source.id,
                    acquired_tick=0,
                )
            )
    aspects = [
        KnownAspect(
            id=a.id,
            text=(
                "You believe Lea is expected"
                if purpose == "npc" and a.grounding.kind == "belief"
                else a.text
            ),
            free_invokes=a.free_invokes.get(actor_id, 0),
        )
        for a in sorted(state.aspects.values(), key=lambda a: a.id)
        if actor_id in a.known_by and grounding_holds(state, a)
    ]
    return ContextView(
        world_version=state.world_version,
        pending_revision=pending_revision,
        viewer_id=actor_id,
        purpose=purpose,
        visible_entities=entities,
        perceived_facts=facts,
        beliefs=beliefs,
        known_aspects=aspects,
        recent_utterances=(utterances or [])[-12:],
        available_actions=list(HANDLERS),
        stakes=[
            "Recover the ledger and carry it outside the gate.",
            "Conflict defeat means capture; concession offers custody or abandoning the mission.",
        ]
        if actor_id == "actor:lea"
        else [],
    )


def resolve_references(text: str, view: ContextView) -> ReferenceCandidates:
    lowered = text.casefold().strip()
    exact = [
        e.id
        for e in view.visible_entities
        if lowered
        in [e.id.casefold(), e.name.casefold(), *(alias.casefold() for alias in e.aliases)]
    ]
    return ReferenceCandidates(text=text, candidate_ids=sorted(exact), exact=len(exact) == 1)
