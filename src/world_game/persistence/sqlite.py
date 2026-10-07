import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from world_game.domain.common import GameError, Value, canonical, digest
from world_game.domain.events import EventBatch, UtterancePayload
from world_game.domain.intents import PendingResolution
from world_game.domain.state import WorldState
from world_game.engine.queries import VisibleUtterance
from world_game.engine.reducer import REDUCER_VERSION, apply_batch
from world_game.engine.validation import validate_causality, validate_state
from world_game.models.interfaces import ProviderName
from world_game.story.loader import load_story, package_hash
from world_game.story.schema import StoryPackage


class Presentation(Value):
    batch_id: str
    fallback: str
    narration: str | None = None
    delivered: bool = False


class ChoiceResponse(Value):
    id: str
    interaction_id: str
    revision: int
    option_id: str
    result: PendingResolution


class SaveArchive(Value):
    format_version: Literal[1] = 1
    reducer_version: Literal["world-game-reducer-v1"] = "world-game-reducer-v1"
    package: StoryPackage
    package_hash: str
    provider: ProviderName
    initial_state: WorldState
    current_state: WorldState
    batches: list[EventBatch]
    pending: PendingResolution | None
    presentations: list[Presentation]
    choice_responses: list[ChoiceResponse] = Field(default_factory=list)


class SQLiteRepository:
    def __init__(self, path: Path, fault: Callable[[str], None] | None = None) -> None:
        if not path.is_file():
            raise GameError(f"Campaign not found: {path.name}")
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.fault = fault or (lambda stage: None)
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            self.close()
            raise GameError("Unsupported save schema; use the matching engine or migrate a copy.")

    @classmethod
    def create(
        cls, path: Path, package: StoryPackage, initial: WorldState, provider: str = "scripted"
    ) -> "SQLiteRepository":
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb"):
                pass
        except FileExistsError as error:
            raise GameError("Campaign already exists; choose a new name.") from error
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.executescript(
                files("world_game").joinpath("persistence/migrations/001_initial.sql").read_text()
            )
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO campaign VALUES (?,?,?,?,?,?,?)",
                (
                    initial.campaign_id,
                    initial.world_version,
                    digest(initial),
                    package_hash(package),
                    1,
                    REDUCER_VERSION,
                    provider,
                ),
            )
            db.execute(
                "INSERT INTO story_package VALUES (?,?)",
                (package_hash(package), canonical(package)),
            )
            db.execute(
                "INSERT INTO snapshot VALUES (?,?,?)",
                (initial.world_version, canonical(initial), digest(initial)),
            )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()
            path.unlink(missing_ok=True)
            raise
        db.close()
        return cls(path)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @property
    def provider(self) -> str:
        return str(self.db.execute("SELECT provider FROM campaign").fetchone()[0])

    def package(self) -> StoryPackage:
        row = self.db.execute("SELECT hash,json FROM story_package").fetchone()
        package = StoryPackage.model_validate_json(row["json"])
        checked = load_story(resources=package.resources)
        if digest(checked) != digest(package) or package_hash(package) != row["hash"]:
            raise GameError("Story package integrity check failed.")
        return package

    def load(self) -> WorldState:
        campaign = self.db.execute("SELECT * FROM campaign").fetchone()
        if campaign["schema_version"] != 1 or campaign["reducer_version"] != REDUCER_VERSION:
            raise GameError("Unsupported schema/reducer; migrate a copy with a compatible engine.")
        package = self.package()
        if package_hash(package) != campaign["package_hash"]:
            raise GameError("Pinned package hash does not match.")
        row = self.db.execute(
            "SELECT json,hash FROM snapshot WHERE version=?", (campaign["version"],)
        ).fetchone()
        if row is None:
            raise GameError("Latest snapshot is missing. Recover into a new campaign.")
        state = WorldState.model_validate_json(row["json"])
        if (
            digest(state) != row["hash"]
            or row["hash"] != campaign["state_hash"]
            or state.world_version != campaign["version"]
        ):
            raise GameError("Save integrity check failed; recover into a new campaign.")
        if validate_state(state):
            raise GameError("Save contains invalid world references.")
        return state

    def pending(self) -> PendingResolution | None:
        row = self.db.execute("SELECT json FROM interaction WHERE status='active'").fetchone()
        if row is None:
            return None
        pending = PendingResolution.model_validate_json(row[0])
        if (
            pending.base_world_version
            != self.db.execute("SELECT version FROM campaign").fetchone()[0]
        ):
            raise GameError("Pending interaction has a stale base version.")
        from world_game.engine.choices import validate_pending

        validate_pending(self.load(), pending)
        return pending

    def response(self, response_id: str) -> PendingResolution | None:
        row = self.db.execute(
            "SELECT result_json FROM choice_response WHERE id=?", (response_id,)
        ).fetchone()
        return PendingResolution.model_validate_json(row[0]) if row else None

    def save_pending(
        self,
        pending: PendingResolution,
        expected_revision: int | None = None,
        response_id: str | None = None,
        option_id: str | None = None,
    ) -> None:
        with self.transaction():
            if response_id and self.response(response_id) is not None:
                raise GameError("Choice response already accepted.")
            version = self.db.execute("SELECT version FROM campaign").fetchone()[0]
            if version != pending.base_world_version:
                raise GameError("World changed while resolving this input.")
            old = self.db.execute(
                "SELECT revision FROM interaction WHERE id=?", (pending.interaction_id,)
            ).fetchone()
            if old is None:
                if expected_revision is not None:
                    raise GameError("Interaction no longer exists.")
                self.db.execute(
                    "INSERT INTO interaction VALUES (?,?,?,?,?)",
                    (
                        pending.interaction_id,
                        "active",
                        pending.base_world_version,
                        pending.choice_revision,
                        canonical(pending),
                    ),
                )
            else:
                if expected_revision is not None and old[0] != expected_revision:
                    raise GameError("Choice changed; reload its current options.")
                self.db.execute(
                    "UPDATE interaction SET revision=?,json=? WHERE id=? AND status='active'",
                    (pending.choice_revision, canonical(pending), pending.interaction_id),
                )
            if response_id:
                self.db.execute(
                    "INSERT INTO choice_response VALUES (?,?,?,?,?)",
                    (
                        response_id,
                        pending.interaction_id,
                        expected_revision,
                        option_id,
                        canonical(pending),
                    ),
                )

    def cancel(self, pending: PendingResolution) -> None:
        if pending.recorded_rolls or pending.completed_choices:
            raise GameError(
                "A revealed roll or accepted resource choice cannot be canceled; quit to save."
            )
        with self.transaction():
            self.db.execute(
                "UPDATE interaction SET status='canceled' WHERE id=? AND status='active'",
                (pending.interaction_id,),
            )

    def committed(self, input_id: str) -> EventBatch | None:
        row = self.db.execute(
            "SELECT json FROM event_batch WHERE input_id=?", (input_id,)
        ).fetchone()
        return EventBatch.model_validate_json(row[0]) if row else None

    def commit(self, batch: EventBatch, pending: PendingResolution, fallback: str) -> WorldState:
        existing = self.committed(batch.input_id)
        if existing:
            return self.load()
        self.fault("before")
        with self.transaction():
            old = self.load()
            validate_causality(
                batch.events, {row[0] for row in self.db.execute("SELECT id FROM event")}
            )
            next_state = apply_batch(old, batch)
            row = self.db.execute(
                "SELECT status,revision FROM interaction WHERE id=?", (pending.interaction_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or row["revision"] != pending.choice_revision
            ):
                raise GameError("Interaction changed before commit.")
            self.db.execute(
                "INSERT INTO event_batch VALUES (?,?,?,?,?)",
                (
                    batch.batch_id,
                    batch.input_id,
                    batch.result_world_version,
                    canonical(batch),
                    batch.state_hash_after,
                ),
            )
            for e in batch.events:
                self.db.execute(
                    "INSERT INTO event VALUES (?,?,?,?)",
                    (e.id, batch.batch_id, e.sequence, canonical(e)),
                )
            self.fault("inside")
            self.db.execute(
                "INSERT INTO snapshot VALUES (?,?,?)",
                (next_state.world_version, canonical(next_state), digest(next_state)),
            )
            self.db.execute(
                "UPDATE campaign SET version=?,state_hash=?",
                (next_state.world_version, digest(next_state)),
            )
            self.db.execute(
                "UPDATE interaction SET status='committed' WHERE id=?", (pending.interaction_id,)
            )
            self.db.execute(
                "INSERT INTO presentation(batch_id,fallback) VALUES (?,?)",
                (batch.batch_id, fallback),
            )
        self.fault("after")
        return next_state

    def batches(self) -> list[EventBatch]:
        return [
            EventBatch.model_validate_json(row[0])
            for row in self.db.execute("SELECT json FROM event_batch ORDER BY version")
        ]

    def replay(self, verify: bool = True) -> WorldState:
        row = self.db.execute("SELECT json,hash FROM snapshot WHERE version=0").fetchone()
        if row is None:
            raise GameError("Initial checkpoint is missing.")
        state = WorldState.model_validate_json(row[0])
        if digest(state) != row[1]:
            raise GameError("Initial checkpoint integrity failure.")
        committed_ids: set[str] = set()
        for batch in self.batches():
            validate_causality(batch.events, committed_ids)
            state = apply_batch(state, batch)
            committed_ids.update(e.id for e in batch.events)
            if verify:
                checkpoint = self.db.execute(
                    "SELECT hash,json FROM snapshot WHERE version=?", (state.world_version,)
                ).fetchone()
                if (
                    checkpoint is None
                    or digest(state) != checkpoint[0]
                    or digest(WorldState.model_validate_json(checkpoint[1])) != checkpoint[0]
                ):
                    raise GameError(f"Replay mismatch at version {state.world_version}.")
        if verify and digest(state) != digest(self.load()):
            raise GameError("Replay differs from latest snapshot.")
        return state

    def history(self, delivered_only: bool = True) -> list[str]:
        sql = "SELECT COALESCE(narration,fallback) FROM presentation"
        if delivered_only:
            sql += " WHERE delivered=1"
        sql += " ORDER BY rowid"
        return [row[0] for row in self.db.execute(sql)]

    def present(self, batch_id: str, narration: str | None = None) -> str:
        if narration:
            with self.transaction():
                self.db.execute(
                    "UPDATE presentation SET narration=? WHERE batch_id=?", (narration, batch_id)
                )
        row = self.db.execute(
            "SELECT COALESCE(narration,fallback) FROM presentation WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return str(row[0])

    def delivered(self, batch_id: str) -> None:
        self.db.execute("UPDATE presentation SET delivered=1 WHERE batch_id=?", (batch_id,))

    def undelivered(self) -> list[tuple[str, str]]:
        return [
            (row[0], row[1])
            for row in self.db.execute(
                "SELECT batch_id,COALESCE(narration,fallback) FROM presentation WHERE delivered=0 ORDER BY rowid"
            )
        ]

    def utterances(self, actor_id: str) -> list[VisibleUtterance]:
        result = []
        for batch in self.batches():
            for e in batch.events:
                p = e.payload
                if isinstance(p, UtterancePayload) and (
                    actor_id in p.audience_ids or actor_id == p.speaker_id
                ):
                    result.append(VisibleUtterance(id=e.id, speaker_id=p.speaker_id, text=p.text))
        return result[-12:]

    def record_call(self, key: str, record: Value) -> None:
        self.db.execute("INSERT OR REPLACE INTO model_call VALUES (?,?)", (key, canonical(record)))

    def cached_call(self, key: str) -> str | None:
        row = self.db.execute("SELECT json FROM model_call WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def archive(self) -> SaveArchive:
        self.replay()
        return SaveArchive(
            package=self.package(),
            package_hash=package_hash(self.package()),
            provider=cast(ProviderName, self.provider),
            initial_state=WorldState.model_validate_json(
                self.db.execute("SELECT json FROM snapshot WHERE version=0").fetchone()[0]
            ),
            current_state=self.load(),
            batches=self.batches(),
            pending=self.pending(),
            presentations=[
                Presentation(batch_id=r[0], fallback=r[1], narration=r[2], delivered=bool(r[3]))
                for r in self.db.execute("SELECT * FROM presentation ORDER BY rowid")
            ],
            choice_responses=[
                ChoiceResponse(
                    id=r[0],
                    interaction_id=r[1],
                    revision=r[2],
                    option_id=r[3],
                    result=PendingResolution.model_validate_json(r[4]),
                )
                for r in self.db.execute("SELECT * FROM choice_response")
            ],
        )

    def export(self, path: Path, overwrite: bool = False) -> None:
        text = canonical(self.archive()) + "\n"
        with path.open("w" if overwrite else "x", encoding="utf-8") as out:
            out.write(text)

    @classmethod
    def restore(cls, path: Path, archive: SaveArchive) -> "SQLiteRepository":
        package = load_story(resources=archive.package.resources)
        if (
            digest(package) != digest(archive.package)
            or package_hash(package) != archive.package_hash
        ):
            raise GameError("Export package integrity failure.")
        state = archive.initial_state
        if state.world_version != 0:
            raise GameError("Export initial state must be version zero.")
        committed_ids: set[str] = set()
        for batch in archive.batches:
            validate_causality(batch.events, committed_ids)
            state = apply_batch(state, batch)
            committed_ids.update(e.id for e in batch.events)
        if digest(state) != digest(archive.current_state):
            raise GameError("Export replay does not match snapshot.")
        if archive.pending:
            validate_causality(archive.pending.staged_events, committed_ids)
            from world_game.engine.reducer import apply_staged_events

            if archive.pending.base_world_version != state.world_version:
                raise GameError("Export pending choice has stale world version.")
            apply_staged_events(state, archive.pending.staged_events)
            from world_game.engine.choices import validate_pending

            validate_pending(state, archive.pending)
        repo = cls.create(path, package, archive.initial_state, archive.provider)
        try:
            for batch in archive.batches:
                pending = PendingResolution(
                    interaction_id=batch.input_id,
                    input_id=batch.input_id,
                    base_world_version=batch.base_world_version,
                    phase="validating",
                    actor_id="actor:lea",
                    raw_input="imported",
                    staged_rng=state.rng,
                )
                repo.save_pending(pending)
                presentation = next(
                    (p for p in archive.presentations if p.batch_id == batch.batch_id), None
                )
                if presentation is None:
                    raise GameError("Export presentation is missing.")
                repo.commit(batch, pending, presentation.fallback)
                if presentation.narration:
                    repo.present(batch.batch_id, presentation.narration)
                if presentation.delivered:
                    repo.delivered(batch.batch_id)
            if archive.pending:
                repo.save_pending(archive.pending)
                for response in archive.choice_responses:
                    if response.interaction_id == archive.pending.interaction_id:
                        repo.db.execute(
                            "INSERT INTO choice_response VALUES (?,?,?,?,?)",
                            (
                                response.id,
                                response.interaction_id,
                                response.revision,
                                response.option_id,
                                canonical(response.result),
                            ),
                        )
            return repo
        except BaseException:
            repo.close()
            path.unlink(missing_ok=True)
            raise

    def recover(self, path: Path) -> "SQLiteRepository":
        # Verify against journal, never mutate a damaged source database.
        initial = WorldState.model_validate_json(
            self.db.execute("SELECT json FROM snapshot WHERE version=0").fetchone()[0]
        )
        state = self.replay(verify=False)
        presentations = [
            Presentation(batch_id=r[0], fallback=r[1], narration=r[2], delivered=bool(r[3]))
            for r in self.db.execute("SELECT * FROM presentation")
        ]
        archive = SaveArchive(
            package=self.package(),
            package_hash=package_hash(self.package()),
            provider=cast(ProviderName, self.provider),
            initial_state=initial,
            current_state=state,
            batches=self.batches(),
            pending=self.pending(),
            presentations=presentations,
        )
        return self.restore(path, archive)
