import io
import json

import pytest
from pydantic import ValidationError

from world_game.cli import execute, repl
from world_game.domain.common import GameError, canonical, digest
from world_game.engine.turns import Coordinator
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SaveArchive, SQLiteRepository
from world_game.story.loader import new_state


@pytest.mark.parametrize("provider", ["scripted", "local", "openai"])
def test_provider_survives_export_import_and_recovery(package, tmp_path, provider):
    repo = SQLiteRepository.create(
        tmp_path / "source.db",
        package,
        new_state(package, "provider-roundtrip", "00" * 32),
        provider,
    )
    try:
        archive = SaveArchive.model_validate_json(canonical(repo.archive()))
        assert archive.provider == provider
        restored = SQLiteRepository.restore(tmp_path / "restored.db", archive)
        recovered = repo.recover(tmp_path / "recovered.db")
        try:
            assert restored.provider == recovered.provider == provider
            assert restored.load() == recovered.load() == repo.load()
        finally:
            restored.close()
            recovered.close()
    finally:
        repo.close()


def bluff(c):
    return c.submit("I show Oren the seal and say the court expects me")


def test_export_import_pending_paid_invoke(game, tmp_path):
    c = game([[-1, -1, 0, 0]])
    bluff(c)
    c.choose("invoke:aspect:lea:concept:paid:bonus", response_id="paid")
    archive = c.repository.archive()
    restored = SQLiteRepository.restore(
        tmp_path / "import.db", SaveArchive.model_validate_json(canonical(archive))
    )
    other = Coordinator(restored, ScriptedModel(), c.dice)
    assert other.describe() == c.describe()
    assert restored.load() == c.repository.load()
    assert "already accepted" in other.choose("pass", response_id="paid")
    other.choose("pass")
    c.choose("pass")
    assert restored.load() == c.repository.load()
    assert digest(restored.replay()) == digest(c.repository.replay())
    restored.close()


@pytest.mark.parametrize("stage", ["before", "inside", "after"])
def test_atomic_commit_failure(game, stage):
    def fault(at):
        if at == stage:
            raise RuntimeError("injected failure")

    c = game([[-1, -1, 0, 0]])
    bluff(c)
    before = digest(c.repository.load())
    c.repository.fault = fault
    with pytest.raises(RuntimeError, match="injected"):
        c.choose("pass")
    c.repository.fault = lambda at: None
    if stage == "after":
        assert c.repository.load().world_version == 1
        assert c.repository.pending() is None
        assert c.repository.undelivered()
    else:
        assert digest(c.repository.load()) == before
        assert c.repository.pending() is not None
        c.resume()
        assert c.repository.load().world_version == 1
    assert c.repository.load().rng.counter == 4
    assert digest(c.repository.replay()) == digest(c.repository.load())


def test_output_failure_does_not_undo_commit(game):
    c = game([])

    class FailingOutput(io.StringIO):
        def write(self, text):
            if "wait as dusk" in text:
                raise OSError("broken terminal")
            return super().write(text)

    with pytest.raises(OSError):
        repl(c, io.StringIO("wait\n"), FailingOutput(), True)
    assert c.repository.load().clock.tick == 1
    assert c.repository.undelivered()
    recovered = io.StringIO()
    repl(c, io.StringIO("/quit\n"), recovered, True)
    assert "wait as dusk" in recovered.getvalue()
    assert not c.repository.undelivered()


def test_stale_choice_cas_and_overwrite(game, tmp_path):
    c = game([[-1, -1, 0, 0]])
    bluff(c)
    p = c.repository.pending()
    c.choose("invoke:aspect:lea:concept:paid:bonus")
    with pytest.raises(GameError, match="Choice changed"):
        c.repository.save_pending(p, expected_revision=0, response_id="stale", option_id="pass")
    path = tmp_path / "portable.json"
    c.repository.export(path)
    with pytest.raises(FileExistsError):
        c.repository.export(path)
    c.repository.export(path, overwrite=True)
    with pytest.raises(GameError, match="already exists"):
        SQLiteRepository.restore(c.repository.path, c.repository.archive())


def test_tampered_history_and_new_schema_rejected(game, tmp_path):
    c = game([])
    c.submit("wait")
    data = c.repository.archive().model_dump(mode="json")
    data["current_state"]["clock"]["tick"] = 50
    with pytest.raises(GameError, match="replay"):
        SQLiteRepository.restore(
            tmp_path / "tampered.db", SaveArchive.model_validate_json(json.dumps(data))
        )
    assert not tmp_path.joinpath("tampered.db").exists()
    data["format_version"] = 2
    with pytest.raises(ValidationError):
        SaveArchive.model_validate_json(json.dumps(data))


def test_corrupt_snapshot_recover_to_new_database(game, tmp_path):
    c = game([])
    c.submit("wait")
    repo = c.repository
    original = repo.db.execute("SELECT json FROM snapshot WHERE version=1").fetchone()[0]
    corrupted = json.loads(original)
    corrupted["clock"]["tick"] = 90
    repo.db.execute("UPDATE snapshot SET json=? WHERE version=1", (json.dumps(corrupted),))
    with pytest.raises(GameError, match="integrity"):
        repo.load()
    recovered = repo.recover(tmp_path / "recovered.db")
    assert recovered.load().clock.tick == 1
    assert repo.db.execute("SELECT json FROM snapshot WHERE version=1").fetchone()[0] != original
    assert recovered.replay() == recovered.load()
    recovered.close()


def test_read_only_commands_do_not_call_models_or_advance(game):
    model = ScriptedModel()
    c = game([], provider=model)
    before = digest(c.repository.load())
    for command in [
        "/inventory",
        "/sheet",
        "/aspects",
        "/journal",
        "/history",
        "/save",
        "/help",
    ]:
        assert execute(c, command)
    assert model.calls == [] and digest(c.repository.load()) == before
