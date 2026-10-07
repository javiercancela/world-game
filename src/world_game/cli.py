"""CLI and injectable REPL streams; scene descriptions use a read-only agent."""

import json
import sys
import uuid
from contextlib import nullcontext
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, TextIO

import typer
from pydantic import ValidationError
from rich.console import Console

from world_game.config import Settings
from world_game.domain.common import GameError, canonical, digest
from world_game.domain.intents import PendingResolution
from world_game.engine.actions import PC
from world_game.engine.fate_rules import stress_capacity
from world_game.engine.queries import Viewer, inventory, project
from world_game.engine.randomness import ScriptedDice
from world_game.engine.turns import Coordinator
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SaveArchive, SQLiteRepository
from world_game.story.loader import export_schemas, load_story, new_state, package_hash

app = typer.Typer(
    help="Gate at Dusk: a conversation RPG. Use credits for Fate attribution.", no_args_is_help=True
)
NoColor = Annotated[bool, typer.Option("--no-color", help="Disable terminal colors.")]


def emit(text: str, output: TextIO = sys.stdout, no_color: bool = False) -> None:
    Console(
        file=output, no_color=no_color or not output.isatty(), markup=False, highlight=False
    ).print(text)


def read_command(coordinator: Coordinator, command: str) -> str | None:
    if command == "/look":
        return coordinator.look()
    repo = coordinator.repository
    state = repo.load()
    view = project(state, Viewer(actor_id=PC), "interpreter", utterances=repo.utterances(PC))
    sheet = state.entities[PC].fate
    assert sheet
    if command == "/help":
        return "/look /inventory /sheet /aspects /journal /history /save /choose <id|number> /invoke <aspect-id> <bonus|reroll> /pass /concede /cancel /retry /quit\nUse free-form English for actions. A pending choice blocks a new action."
    if command == "/inventory":
        return "Inventory: " + (", ".join(e.identity.name for e in inventory(state, PC)) or "empty")
    if command == "/sheet":
        return (
            f"Lea — Fate {sheet.fate_points}, refresh {sheet.refresh}\n"
            + ", ".join(f"{k} {v}" for k, v in sheet.skills.items())
            + f"\nPhysical stress {sheet.physical_stress_used}/{stress_capacity(sheet, 'physical')}; mental {sheet.mental_stress_used}/{stress_capacity(sheet, 'mental')}.\nConsequences: {canonical(sheet.consequence_slots)}\nStunts: {', '.join(sheet.stunt_ids)}"
        )
    if command == "/aspects":
        return "\n".join(
            f"{a.text} [{a.id}] — {a.free_invokes} controlled free invoke(s)"
            for a in view.known_aspects
        )
    if command == "/journal":
        quests = [f"Ledger mission: {q.stage}" for q in state.quests.values()]
        obligations = [
            f"Promise: {state.entities[c.fulfillment_predicate.item_id].identity.name} to {state.entities[c.fulfillment_predicate.recipient_id].identity.name} — {c.status}"
            for c in state.commitments.values()
            if PC in (c.debtor_id, c.creditor_id)
        ]
        known = [
            f"You learned: {f.description} = {f.value}"
            for f in view.perceived_facts
            if f.id == "proposition:ledger_incriminates_deputy"
        ]
        return "\n".join(quests + obligations + known)
    if command == "/history":
        return "\n\n".join(repo.history()) or "No delivered history yet."
    if command == "/save":
        return f"Saved world version {state.world_version}, tick {state.clock.tick}" + (
            ", including the pending interaction." if repo.pending() else "."
        )
    return None


def execute(coordinator: Coordinator, text: str) -> str:
    text = text.strip()
    read = read_command(coordinator, text)
    if read is not None:
        pending = coordinator.repository.pending()
        return read + (
            "\n\n" + coordinator.describe(pending)
            if pending and text not in ("/help", "/history")
            else ""
        )
    if text.startswith("/choose "):
        return coordinator.choose(text.removeprefix("/choose ").strip())
    if text == "/pass":
        return coordinator.choose("pass")
    if text == "/retry":
        return coordinator.resume()
    if text == "/cancel":
        return coordinator.cancel()
    if text == "/concede":
        return coordinator.offer_concession()
    if text.startswith("/invoke "):
        args = text.split()
        if len(args) != 3 or args[2] not in ("bonus", "reroll"):
            raise GameError("Use /invoke <aspect-id> <bonus|reroll>.")
        pending = coordinator.repository.pending()
        if (
            pending is None
            or pending.current_choice is None
            or pending.current_choice.kind != "invoke"
        ):
            raise GameError("Invokes require a pending roll.")
        candidates = [
            o
            for o in pending.current_choice.options
            if o.command.kind == "invoke"
            and o.command.aspect_id == args[1]
            and o.command.mode == args[2]
        ]
        if not candidates:
            raise GameError("That invoke is unavailable.")
        chosen = sorted(candidates, key=lambda o: o.command.payment != "free")[0]
        return coordinator.choose(chosen.id)
    if text.startswith("/"):
        raise GameError("Unknown command. Use /help.")
    return coordinator.submit(text)


def repl(
    coordinator: Coordinator,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    no_color: bool = False,
) -> None:
    repo = coordinator.repository
    for batch_id, text in repo.undelivered():
        emit(text, output_stream, no_color)
        repo.delivered(batch_id)
    emit("Gate at Dusk. /help lists commands; /quit saves and exits.", output_stream, no_color)
    console = Console(file=output_stream, no_color=no_color)
    with (
        console.status("Describing…")
        if repo.provider != "scripted" and output_stream.isatty()
        else nullcontext()
    ):
        initial_scene = read_command(coordinator, "/look") or ""
    emit(initial_scene, output_stream, no_color)
    while True:
        boundary = coordinator.ensure_boundary()
        if boundary:
            emit(coordinator.describe(boundary), output_stream, no_color)
        if output_stream.isatty():
            output_stream.write("> ")
            output_stream.flush()
        try:
            line = input_stream.readline()
            if not line or line.strip() == "/quit":
                emit("Saved. Goodbye.", output_stream, no_color)
                return
            if not line.strip():
                continue
            if (
                repo.provider != "scripted"
                and output_stream.isatty()
                and (
                    not line.startswith("/")
                    or line.split()[0]
                    in ("/look", "/choose", "/pass", "/retry", "/invoke", "/concede")
                )
            ):
                with console.status("Describing…" if line.strip() == "/look" else "Interpreting…"):
                    result = execute(coordinator, line)
            else:
                result = execute(coordinator, line)
            emit(result, output_stream, no_color)
            for batch_id, _ in repo.undelivered():
                repo.delivered(batch_id)
        except (GameError, ValidationError) as error:
            emit(str(error), output_stream, no_color)
        except KeyboardInterrupt:
            emit(
                "Interrupted; the pending interaction is saved. /retry or /quit.",
                output_stream,
                no_color,
            )


def command_error(error: Exception) -> None:
    typer.echo(str(error), err=True)
    raise typer.Exit(1)


@app.command()
def new(
    name: Annotated[str, typer.Option()],
    story: str = "gate-at-dusk",
    provider: str | None = None,
    no_color: NoColor = False,
) -> None:
    """Validate the package/configuration, then create a fresh campaign."""
    try:
        if story != "gate-at-dusk":
            raise GameError("Only gate-at-dusk is packaged in v0.1.")
        settings = Settings.environment(provider)
        settings.build_provider()
        package = load_story()
        path = settings.campaign_path(name)
        repo = SQLiteRepository.create(
            path, package, new_state(package, str(uuid.uuid4())), settings.provider
        )
        repo.close()
        emit(f"Created {name}. Run: world-game play {name}", no_color=no_color)
    except (GameError, ValidationError, ValueError, OSError) as error:
        command_error(error)


@app.command()
def play(campaign: str, no_color: NoColor = False) -> None:
    """Play or resume a campaign, including any pending choice."""
    repo = None
    try:
        settings = Settings.environment()
        repo = SQLiteRepository(settings.campaign_path(campaign))
        settings = Settings.environment(repo.provider)
        coordinator = Coordinator(
            repo,
            settings.build_provider(),
            call_budget=settings.call_budget,
            deadline=settings.provider_deadline,
        )
        repl(coordinator, no_color=no_color)
    except (GameError, ValueError, ValidationError, OSError) as error:
        command_error(error)
    finally:
        if repo:
            repo.close()


@app.command()
def replay(campaign: str, verify: bool = True, no_color: NoColor = False) -> None:
    """Replay the committed event journal without contacting any model."""
    try:
        repo = SQLiteRepository(Settings.environment().campaign_path(campaign))
        try:
            state = repo.replay(verify)
            emit(
                f"Verified {state.world_version} batches. State SHA-256: {digest(state)}",
                no_color=no_color,
            )
        finally:
            repo.close()
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command("export")
def export_campaign(
    campaign: str, output: Annotated[Path, typer.Option()], overwrite: bool = False
) -> None:
    """Export the story, history, world, RNG, and pending choices as JSON."""
    try:
        repo = SQLiteRepository(Settings.environment().campaign_path(campaign))
        try:
            repo.export(output, overwrite)
            typer.echo(f"Exported to {output}.")
        finally:
            repo.close()
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command("import")
def import_campaign(source: Path, name: Annotated[str, typer.Option()]) -> None:
    """Validate portable history and restore into a new database."""
    try:
        archive = SaveArchive.model_validate_json(source.read_text())
        repo = SQLiteRepository.restore(Settings.environment().campaign_path(name), archive)
        repo.close()
        typer.echo(f"Imported as {name}.")
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command("validate-story")
def validate_story_command(directory: Path) -> None:
    """Check typed resources and all story references."""
    try:
        package = load_story(directory)
        typer.echo(
            f"Valid {package.manifest.id}@{package.manifest.version}: {package_hash(package)}"
        )
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command()
def inspect(campaign: str, debug: bool = False) -> None:
    """Inspect a filtered campaign; --debug explicitly includes secrets."""
    try:
        repo = SQLiteRepository(Settings.environment().campaign_path(campaign))
        try:
            state = repo.load()
            typer.echo(
                canonical(state if debug else project(state, Viewer(actor_id=PC), "interpreter"))
            )
        finally:
            repo.close()
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command()
def recover(campaign: str, name: Annotated[str, typer.Option()]) -> None:
    """Rebuild verified journal history into a new database; keep the source."""
    try:
        settings = Settings.environment()
        repo = SQLiteRepository(settings.campaign_path(campaign))
        try:
            recovered = repo.recover(settings.campaign_path(name))
            recovered.close()
            typer.echo(f"Recovered into {name}.")
        finally:
            repo.close()
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command()
def schemas(output: Annotated[Path, typer.Option()] = Path("schemas")) -> None:
    """Export JSON Schemas from the installed Pydantic models."""
    export_schemas(output)
    typer.echo(f"Schemas exported to {output}.")


@app.command()
def credits() -> None:
    """Display Fate attribution and profile changes."""
    typer.echo(files("world_game").joinpath("licenses/fate-condensed-NOTICE.txt").read_text())


def run_demo(
    directory: Path, output: TextIO = sys.stdout, no_color: bool = False
) -> tuple[Path, Path]:
    fixture = json.loads(
        files("world_game").joinpath("data/stories/gate_at_dusk/fixtures/demo.json").read_text()
    )
    paths = []
    package = load_story()
    for branch in ("success", "failure"):
        path = directory / f"demo-{branch}.sqlite3"
        repo = SQLiteRepository.create(
            path, package, new_state(package, "demo-" + branch, "00" * 32)
        )
        dice = ScriptedDice(fixture[branch]["dice"])
        c = Coordinator(repo, ScriptedModel(), dice)
        emit(f"Gate at Dusk — offline {branch} demonstration", output, no_color)
        for line in fixture[branch]["inputs"]:
            if line == "@reload":
                pending: PendingResolution | None = repo.pending()
                assert pending and pending.actor_dice
                repo.close()
                repo = SQLiteRepository(path)
                c = Coordinator(repo, ScriptedModel(), dice)
                emit("[Saved and reloaded at the post-roll choice.]", output, no_color)
                emit(c.describe(), output, no_color)
                continue
            c.ensure_boundary()
            emit("> " + line, output, no_color)
            emit(execute(c, line), output, no_color)
            for batch_id, _ in repo.undelivered():
                repo.delivered(batch_id)
        state = repo.replay()
        expected = "success" if branch == "success" else "failure"
        if state.quests["recover_ledger"].terminal_result != expected:
            raise GameError(f"Demo {branch} did not reach its expected ending.")
        emit(
            f"Replay verified: {state.world_version} committed batches; ending={expected}.",
            output,
            no_color,
        )
        repo.close()
        paths.append(path)
    return paths[0], paths[1]


@app.command()
def demo(
    save_dir: Annotated[Path | None, typer.Option()] = None, no_color: NoColor = False
) -> None:
    """Play actual SQLite campaigns with scripted contracts and a roll reload."""
    try:
        if save_dir:
            run_demo(save_dir, no_color=no_color)
        else:
            with TemporaryDirectory(prefix="world-game-demo-") as directory:
                run_demo(Path(directory), no_color=no_color)
    except (GameError, ValidationError, OSError) as error:
        command_error(error)


@app.command("eval-live")
def eval_live(output: Annotated[Path, typer.Option()], provider: str | None = None) -> None:
    """Explicit opt-in interpretation corpus against the configured live provider."""
    from world_game.models.evaluation import evaluate

    try:
        settings = Settings.environment(provider)
        if settings.provider == "scripted":
            raise GameError("eval-live requires --provider local or --provider openai.")
        report = evaluate(settings.build_provider())
        output.write_text(canonical(report) + "\n")
        typer.echo(f"Live evaluation written to {output}; passed={report.passed}.")
    except (GameError, ValidationError, ValueError, OSError) as error:
        command_error(error)


if __name__ == "__main__":
    app()
