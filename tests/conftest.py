import pytest

from world_game.engine.randomness import ScriptedDice
from world_game.engine.turns import Coordinator
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SQLiteRepository
from world_game.story.loader import load_story, new_state


@pytest.fixture
def package():
    return load_story()


@pytest.fixture
def game(tmp_path, package):
    repos = []

    def make(rolls=None, change=None, provider=None, fault=None):
        state = new_state(package, f"test-{len(repos)}", "00" * 32)
        if change:
            change(state)
        repo = SQLiteRepository.create(tmp_path / f"test-{len(repos)}.sqlite3", package, state)
        repo.fault = fault or (lambda stage: None)
        repos.append(repo)
        c = Coordinator(
            repo, provider or ScriptedModel(), ScriptedDice(rolls) if rolls is not None else None
        )
        return c

    yield make
    for repo in repos:
        try:
            repo.close()
        except Exception:
            pass


def complete(c, text):
    result = c.submit(text)
    for _ in range(10):
        p = c.repository.pending()
        if p is None or p.current_choice is None or p.current_choice.kind != "invoke":
            return result
        result = c.choose("pass")
    raise AssertionError("invoke procedure did not finish")
