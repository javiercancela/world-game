# world-game

A playable conversation RPG with a deterministic, event-sourced Fate Condensed engine. In **Gate at Dusk**, Lea must recover a ledger and bring it outside the gate before tick 20. NPC beliefs, secrets, offers, inventory, and combat are state records; model prose cannot change them.

- [World architecture](docs/world-architecture.md)
- [Implementation specification](docs/implementation-plan.md)
- [Story data guide](docs/story-data-guide.html)

## Install and try it

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync --frozen
uv run world-game demo --no-color
```

The project uses Python 3.12. The offline demo needs no credentials or network. It plays two actual SQLite campaigns, prints scripted inputs, saves and reloads a post-roll decision, reaches victory and deadline failure, and verifies both journals. [Success](tests/fixtures/demo-success.txt) and [failure](tests/fixtures/demo-failure.txt) transcripts are checked in.

To keep demo databases, choose a new directory:

```bash
uv run world-game demo --save-dir /tmp/gate-demo
```

For an interactive offline campaign:

```bash
uv run world-game new --name lea --provider scripted
uv run world-game play lea
```

The scripted interpreter recognizes the authored adventure vocabulary. Try “I show Oren my seal and say the court expects me”, “I need to see Sen”, “I read Oren's concern”, “Go to the courtyard”, or “Attack Oren”. The live mode handles broader free-form English through typed model proposals.

## Local LLM play

Export variables in your shell; `.env` files are not loaded automatically. [.env.example](.env.example) lists the settings.

```bash
uv run world-game new --name live-lea
uv run world-game play live-lea
```

New campaigns default to the existing Bonsai 2 27B server at `http://127.0.0.1:8080/v1`. The game connects to an already-running server; it does not load GGUF files or start the server. The reference model ID is `/home/xavi/bonsai/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf`, as advertised by this server's `/v1/models` endpoint. `/home/xavi/bonsai/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` is the accompanying vision projector, not the text model ID.

To use another local model/server, set:

```bash
export WORLD_GAME_PROVIDER=local
export WORLD_GAME_BASE_URL='http://127.0.0.1:8080'
export WORLD_GAME_MODEL='your-server-model-id-or-alias'
# export WORLD_GAME_API_KEY='optional-server-key'
```

`WORLD_GAME_BASE_URL` accepts a server root or a URL ending in `/v1`, including a reverse-proxy prefix. The server must support [Chat Completions with JSON Schema response formats](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api). Each game role receives its existing filtered context and a strict response schema; returned JSON is validated before engine admission. No key is required by default. Local calls use only `WORLD_GAME_API_KEY` when configured.

`WORLD_GAME_REASONING_EFFORT=none` disables Bonsai thinking for bounded JSON generation. It can be `low`, `medium`, or `high`; set it to an empty string to omit the parameter for servers that do not support it. `WORLD_GAME_MAX_OUTPUT_TOKENS` sets the local response limit (default 1,800). `WORLD_GAME_PROVIDER=scripted|local|openai` selects the default for new campaigns. A campaign retains its provider choice across saves, export/import, and recovery; the endpoint/model settings come from the current environment. `WORLD_GAME_SAVE_DIR` changes the save directory.

Defaults are a 30-second request timeout, eight total attempts and 120 seconds of provider work per action, an 8,000-token conservative context allowance, one transient retry, and one semantic repair. Adjust `WORLD_GAME_TIMEOUT`, `WORLD_GAME_PROVIDER_DEADLINE`, and `WORLD_GAME_CONTEXT_TOKENS` for your server. Waiting for player input consumes no provider budget. Successful calls are cached by interaction, context, prompt, schema, and model; local cache identities also include endpoint and generation settings. `/retry` starts a new bounded attempt while retaining successful results and recorded dice. Narration falls back to the committed factual outcome when unavailable.

Run the opt-in 30-case corpus against the configured server:

```bash
uv run world-game eval-live --output live-evaluation.json
```

The report records interpretation correctness, invalid proposals, initial-response disclosures, clarification rate, latency, usage, and actual engine admission/replay checks. Reactions in this corpus are scripted; it does not measure live NPC or narration quality. Ordinary tests never contact a model.

## OpenAI cloud play

The existing Responses API provider remains available:

```bash
export OPENAI_API_KEY='your-key'
export WORLD_GAME_MODEL='your-structured-outputs-model'
uv run world-game new --name cloud-lea --provider openai
uv run world-game play cloud-lea
# uv run world-game eval-live --provider openai --output cloud-evaluation.json
```

Both a key and an explicit model supporting Responses API Structured Outputs are required for OpenAI play. There is no silent provider or model substitution. Offline play remains available through `--provider scripted`.

## Play and durable choices

Fresh campaigns open with a brief introduction to Lea, her surroundings, the ledger mission, and the dusk deadline before describing the scene. Resuming after an action or with a pending choice skips the introduction.

`/help` lists the commands. Read-only commands are `/look`, `/inventory`, `/sheet`, `/aspects`, `/journal`, `/history`, and `/save`. They cost no game time. `/look` and the opening scene use a dedicated description agent, with a cached result for an unchanged view and factual prose as a fallback. Its context contains only the current surroundings, nearby people, visible objects, and open/closed doors; it excludes the player's inventory, remote room details, knowledge, and hidden lock state. `/inventory` lists carried items separately. The other read-only commands make no provider calls. Descriptions leave pending choices and action budgets untouched.

When a roll is pending, use `/pass`, `/choose <number|option-id>`, or `/invoke <aspect-id> <bonus|reroll>`. The invoke wrapper spends a controlled free use first when available. Exact, unique option labels also work as ordinary input. `/concede` offers authored surrender terms before the next conflict roll.

Every pending choice stores its locked plan, dice, provisional resources, and legal options. `/quit`, EOF, or interruption preserves it; the next `play` resumes the same choice. `/cancel` is available before dice or a resource-changing choice is accepted. It cannot erase a revealed roll. `/retry` addresses technical failures; a purchased reroll is a distinct Fate action.

The adventure can be completed peacefully with bluffing or an honest collateral offer, Sen's key loan, the records door, and returning outside with the ledger. Discovering Oren's concern offers another approach. Locks, promises, verification, the dusk deadline, and nonlethal capture are implemented consequences. A conflict charges one tick per exchange and preserves its scene when actors change zones.

## Saves, replay, and diagnostics

Saves default to `~/.local/share/world-game/campaigns/<name>.sqlite3`. SQLite commits the event batch, snapshot, resource changes, interaction completion, and fallback output atomically. Starting the application never refreshes Fate points or heals consequences. Campaigns pin the exact story/rules resources and supported schema/reducer versions.

```bash
uv run world-game replay lea --verify
uv run world-game export lea --output lea.json
uv run world-game import lea.json --name restored-lea
uv run world-game inspect lea
uv run world-game recover lea --name recovered-lea
uv run world-game validate-story src/world_game/data/stories/gate_at_dusk
uv run world-game schemas --output /tmp/world-game-schemas
uv run world-game credits
```

Replay is offline and compares journal results with saved hashes. Export/import preserves package resources, dialogue, history, pending choices, and RNG continuation. Saves, imports, and exports refuse accidental overwrite; export alone supports `--overwrite`. Recovery creates a new database and leaves the source intact. `inspect --debug` deliberately includes canonical secrets. Portable exports also contain the full story and canonical world, but exclude API credentials and model-call logs.

## Fate profile

This is the bounded `fate-condensed-cli-v1` profile. It implements four Fate actions, four Fate dice, active/static opposition, aspects and boosts, paid/free invokes, rerolls, delayed hostile-invoke awards, event compels, stress, consequences, recovery, and concessions.

Authored differences include nine merged skills and a shortened allocation, three passive +2 stunts, Athletics-based physical capacity, finite advantage/consequence templates, deterministic NPC spending through a scene GM pool, static checks without NPC invokes, event compels only, and explicit authored scene/session transitions. NPC defeat and PC capture are nonlethal. Only registered handlers and grounded templates are supported; unavailable abilities or objects remain unavailable.

Original code and adventure text use the [MIT license](LICENSE). Fate-derived rules use [CC BY 3.0](licenses/CC-BY-3.0.txt); the official SRD attribution and profile changes are in [the Fate notice](licenses/fate-condensed-NOTICE.txt), also shipped in the installed package and displayed by `credits`. No artwork or logos are included.

## Development checks

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src/world_game
uv run pytest
uv run world-game demo --no-color
uv build
```

Tests exercise the actual coordinator, reducer, SQLite repository, and CLI with scripted providers/dice, including crash checkpoints, pending import, invoke economics, harm choices, conflict order, recovery, visibility, and both adventure endings. Story, prompts, migrations, and notices are packaged resources; installed commands work outside this checkout.
