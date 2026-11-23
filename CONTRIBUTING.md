# Contributing to JoltBot

Thanks for helping. This guide covers local setup, style, branching, and how to
add features. Architecture details live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
operator setup is in the [README](README.md).

## Prerequisites

- Python **3.12** recommended (3.11+ works; see `requires-python` in `pyproject.toml`)
- A Discord application with **Server Members** and **Message Content** intents
- Prefer a **separate test server and bot app** so production (e.g. King of Chaos) is untouched

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.lock
copy .env.example .env                 # macOS/Linux: cp .env.example .env
# fill DISCORD_* and SESSION_SECRET; leave PUBLIC_BASE_URL=http://localhost:8000
alembic upgrade head
```

Run both processes (two terminals):

```powershell
python -m joltbot.bot
python -m joltbot.web --reload
```

Open <http://localhost:8000>.

## Checks before you open a PR

```powershell
pytest
ruff check .
ruff format .          # CI runs `ruff format --check .`
mypy                   # type check; settings are in pyproject.toml
```

GitHub Actions (`.github/workflows/ci.yml`) runs lint, format check, type check,
tests, and a Docker image build that hits `/health`.

`tests/test_contract.py` pins what Discord and the servers already depend on: the
slash commands and the `custom_id` of every button and menu. If it fails, the
change breaks messages that are already posted. Change the expected values there
only when that is what you mean to do.

## Code style

- **Formatter / linter:** Ruff (`line-length` 120, target `py311`). Imports at
  the top of the file — no inline imports unless a circular dependency forces it
  and the reason is commented.
- **Type hints:** Prefer modern annotations (`X | None`, `list[str]`). Match
  surrounding code.
- **Async:** Keep Discord and DB I/O async; short DB sessions (open, work, close)
  rather than long-lived connections across Discord calls.
- **Docstrings:** Module header describing responsibility and how the file fits
  the bot/dashboard. Public functions and classes use a one-line summary, then
  Google-style `Args` / `Returns` / `Raises` when the signature is not obvious.
  Prefer **why** over restating the code. Slash commands should document who can
  run them, example usage, and any cooldowns.
- **Comments:** Explain non-obvious Discord quirks, race handling, and security
  checks. Skip “increment the counter” style comments.
- **Secrets:** Never commit `.env`, tokens, or real `SESSION_SECRET` values.
  Use placeholders in docs and `.env.example` only.

## Branching and pull requests

1. Branch from the default branch with a short topic name (`docs/…`, `fix/…`, `feat/…`).
2. Keep commits focused; prefer small reviewable PRs.
3. Do not change behavior in a “docs only” PR, and do not mix drive-by refactors
   with feature work.
4. Ensure CI is green. Describe **why** the change exists in the PR body; link
   issues when relevant.
5. For schema changes: edit `joltbot/core/models.py`, run
   `alembic revision --autogenerate -m "…"`, review the new file under
   `migrations/versions/`, then commit it. A test fails if models and migrations disagree.

## Adding a new module

The README has a worked example with all the code
([section 11, "Adding a module"](README.md#adding-a-module-a-new-cog)); the same
module lives in `tests/example_module/` and is run by the tests. In short:

1. Create `joltbot/modules/<name>/` with at least `settings.py` (a
   `ModuleSettingsModel` subclass with defaults for every field).
2. Optionally add `cog.py` (bot), `web.py` (FastAPI `router`), and
   `templates/<name>/`.
3. Register one `ModuleInfo` in [`joltbot/modules/registry.py`](joltbot/modules/registry.py).
   The bot loads the cog; the dashboard mounts the router and adds a sidebar
   link when the module has a `web` page. A bot-only module still gets its
   on/off switch on the General page.
4. Add tests under `tests/` for settings validation, permissions, or web access
   as appropriate.
5. Update the README command/feature list if the module exposes Discord commands
   or user-facing dashboard pages.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how settings load, how
guild access is checked, and how tickets are structured as a reference module.

## Changing dependencies

1. Edit the pin in `requirements.txt` (or `requirements-dev.txt` for a test, lint or type-check tool).
2. Regenerate both lock files (they carry a checksum for every package file):

   ```powershell
   pip install uv
   uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes -o requirements.lock
   uv pip compile requirements-dev.txt --universal --python-version 3.11 --generate-hashes -c requirements.lock -o requirements-dev.lock
   pip install --require-hashes -r requirements-dev.lock
   pytest
   ```

3. Commit the file you edited plus `requirements.lock` and `requirements-dev.lock`.
   `tests/test_dependencies.py` fails if the three disagree or a checksum is missing.

## Reporting issues

Include: process (bot vs web), relevant log lines (redact tokens), guild setup
symptoms, and whether you can reproduce on a clean test server. For permission
problems, run `/tickets diagnostics` and note role order relative to the JoltBot role.
