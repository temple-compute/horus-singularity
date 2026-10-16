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

### ApptainerExecutor

Registered under `kind = "apptainer"`. Same fields and behaviour, with `exe` defaulting to `"apptainer"`. Use it for new workflows: SingularityCE reached end of life on 2026-10-25 and Apptainer is the maintained successor. `kind: singularity` keeps working, since Apptainer also ships a `singularity` command.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `image` | `str` | required | Path to the `.sif` image on the target (e.g. `/opt/images/boltz.sif`); accepts `${artifact}` placeholders |
| `exe` | `str` | `"singularity"` | Container CLI to invoke — `singularity` or `apptainer` |
| `binds` | `dict[str, str]` | `{}` | Bind mounts as `host_path → container_path`; both sides accept `${artifact}` placeholders |
| `nv` | `bool` | `False` | Add `--nv` to expose the NVIDIA driver stack inside the container |
| `env` | `dict[str, str]` | `{}` | Environment variables set with `--env NAME=value`; accepts `${artifact}` placeholders |
| `working_dir` | `str \| None` | `None` | Working directory inside the container (`--pwd`); accepts `${artifact}` placeholders |
| `extra_args` | `list[str]` | `[]` | Extra flags passed verbatim before the image path |

`image`, `binds`, `env`, and `working_dir` are rendered with the same `$id` /
`${id}` / `${id.attr}` / `${task.attr}` substitution as a `command` string (see
`horus_builtin.runtime.substitution`), so an explicit bind can reference the
task's own artifacts:

```yaml
binds:
  "${protein}": /data/protein.pdb
```

`image` accepting placeholders is the notable difference from `horus-docker`,
whose `image` is a registry tag with nothing to substitute. A Singularity image
is an ordinary file, so it can be declared as a workflow artifact and referenced
by id:

```yaml
artifacts:
  - id: drugflow_sif
    kind: file
    path: /scratch/me/drugflow.sif   # the one site-specific path
# ...
executor:
  kind: apptainer
  image: ${drugflow_sif}
```

That keeps the absolute, site-specific path in the workflow's artifacts block —
next to every other path an operator has to set — instead of buried in an
executor field.

### Behaviour

1. The task's `CommandRuntime` prepares the shell command via `setup_runtime()`.
2. The executor builds:
   ```
   <exe> exec [--nv] [--env K=V]… [--bind host:container]… [--pwd dir] [extra_args…] <image> /bin/sh -c <command>
   ```
   Every interpolated value is passed through `shlex.quote`.
3. The parent directory of every task input/output artifact is auto-bound at the same path inside the container (`host_dir → host_dir`), as is `task.side_artifacts_dir`. The directory is the artifact's location **on the task's target** (`target.path_on_target`), which for a remapping target (SSH, Slurm over SSH) is not the orchestrator's run directory. Explicit `binds` entries win on conflict.
4. `HORUS_SIDE_ARTIFACTS_DIR` is set both inside the container and on the host process, so scripts can drop side products where the runtime collects them.
5. The command runs on the task target via `target.run_command(...)`; stdout is logged at `INFO`, stderr at `WARNING`.
6. A non-zero exit code raises `TaskExecutionError`.
7. `cancel_execution()` kills the still-running process (Singularity has no daemon-side container name to `stop`), and is a safe no-op otherwise.

Singularity performs **no path translation**: a host path is visible inside the
container at the same location unless you bind it elsewhere. That is why the
identity auto-binds let a command use `${artifact}` unchanged — and why porting
a workflow from `horus-docker` usually means *deleting* the path-rewriting, not
translating it.

### Resource measurement

`SingularityExecutor` deliberately does **not** override
`BaseExecutor.resource_scope`, and contributes no `horus.resource_probe` entry
point. `horus-docker` has to, because `docker run` is a thin client and the
workload is reparented under the daemon. `singularity exec` *is* the workload's
process, so the inherited `ProcessTreeScope` already measures the right tree.

---

## Development

### Requirements

- Python ≥ 3.13
- `singularity` or `apptainer` available on the target
- `horus-runtime` ≥ 0.3.0

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
