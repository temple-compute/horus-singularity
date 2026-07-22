# horus-singularity

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [horus-runtime](https://github.com/temple-compute/horus-runtime) plugin that runs tasks inside Singularity/Apptainer containers.

---

## Overview

**horus-singularity** contributes a `SingularityExecutor` to the `horus.executor` entry point group. Once installed, any `HorusContext` that calls `HorusContext.boot()` will automatically discover and register the executor — no manual wiring required.

The executor accepts a `CommandRuntime`-produced shell command and runs it via `/bin/sh -c` inside a Singularity (or Apptainer) container, giving you the same shell semantics (pipes, globbing, `&&`, …) as the local shell executor.

It is the HPC counterpart of [horus-docker](https://github.com/temple-compute/horus-docker): most clusters forbid the Docker daemon but ship Singularity/Apptainer, so this plugin is a drop-in swap for the same workflows.

---

## Repository structure

```
src/
└── horus_singularity/
    ├── __init__.py
    ├── i18n.py                  # plugin-scoped gettext wrapper
    ├── locale/
    │   └── messages.pot         # translatable strings template
    └── executor/
        ├── __init__.py
        └── singularity.py       # SingularityExecutor implementation
tests/
├── __init__.py
├── conftest.py                  # shared fixtures (registry, HorusContext)
└── unit/
    ├── __init__.py
    └── test_singularity_executor.py
babel.cfg
Makefile
pyproject.toml
```

---

## SingularityExecutor

Registered under `kind = "singularity"`. Only accepted when the task's runtime is a `CommandRuntime`.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `image` | `str` | required | Path to the `.sif` image on the target (e.g. `/opt/images/boltz.sif`) |
| `exe` | `str` | `"singularity"` | Container CLI to invoke — `singularity` or `apptainer` |
| `binds` | `dict[str, str]` | `{}` | Bind mounts as `host_path → container_path` |
| `nv` | `bool` | `False` | Add `--nv` to expose the NVIDIA driver stack inside the container |
| `env` | `dict[str, str]` | `{}` | Environment variables set with `--env NAME=value` |
| `working_dir` | `str \| None` | `None` | Working directory inside the container (`--pwd`) |
| `extra_args` | `list[str]` | `[]` | Extra flags passed verbatim before the image path |

### Behaviour

1. The task's `CommandRuntime` prepares the shell command via `setup_runtime()`.
2. The executor builds:
   ```
   <exe> exec [--nv] [--env K=V]… [--bind host:container]… [--pwd dir] [extra_args…] <image> /bin/sh -c <command>
   ```
   Every interpolated value is passed through `shlex.quote`.
3. The parent directory of every task input/output artifact is auto-bound at the same path inside the container (`host_dir → host_dir`). Explicit `binds` entries win on conflict.
4. The command runs on the task target via `target.run_command(...)`; stdout is logged at `INFO`, stderr at `WARNING`.
5. A non-zero exit code raises `TaskExecutionError`.
6. `cancel_execution()` kills the still-running process (Singularity has no daemon-side container name to `stop`), and is a safe no-op otherwise.

---

## Development

### Requirements

- Python ≥ 3.13
- `singularity` or `apptainer` available on the target
- `horus-runtime` ≥ 0.2.0

### Setup

```bash
# Install dependencies (creates .venv automatically)
uv sync

# Install pre-commit hooks
uv run pre-commit install
```

### Common commands

| Command | Description |
|---|---|
| `make test` | Run the full test suite with coverage |
| `make lint` | ruff + mypy |
| `make format` | Auto-fix with ruff |
| `make type-check` | mypy only |
| `make babel-extract` | Update `messages.pot` |
| `make babel-add LANG=es` | Add a new language |
| `make babel-check` | Verify all strings are translated |
| `make clean` | Remove build artefacts and caches |

---

## Internationalization (i18n)

Each plugin maintains its **own** gettext domain and locale directory, independent of the runtime's translations.

`src/horus_singularity/i18n.py` wraps Python's `gettext` module, looking for compiled `.mo` files in `src/horus_singularity/locale/<lang>/LC_MESSAGES/horus_singularity.mo`. If no catalog exists for the detected locale, it falls back to the original string.

Import the wrapper as `_` (required by Babel's extractor) in any module with user-visible strings. Use `make babel-extract` → edit `.po` → `make babel-check` to update translations. The pre-commit hook prevents committing incomplete catalogs.

> Full i18n workflow and plural-form reference: [docs.templecompute.com](https://docs.templecompute.com/docs/sdk/i18n).

---

## License

MIT — see [LICENSE](LICENSE).
