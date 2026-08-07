#
# horus_singularity
# Copyright (c) 2026 Temple Compute
#
# MIT License
#
"""
Singularity/Apptainer executor implementation for Horus.
"""

import asyncio
import shlex
from typing import TYPE_CHECKING, Any, ClassVar

from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.runtime.substitution import substitute
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.logging import horus_logger
from horus_runtime.settings import runtime_settings
from pydantic import Field, PrivateAttr

from horus_singularity.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class SingularityExecutor(BaseExecutor):
    """
    Runs the task's command inside a Singularity/Apptainer container.

    :meth:`BaseExecutor.resource_scope` is deliberately **not** overridden.
    Docker has to override it because ``docker run`` is a thin client and the
    workload is reparented under the daemon, so the spawned process tree holds
    nothing worth measuring. ``singularity exec`` *is* the workload's process,
    so the inherited ``ProcessTreeScope`` measures the right thing and there is
    no ``horus.resource_probe`` entry point to contribute.
    """

    kind: str = "singularity"
    kind_name: ClassVar[str] = "Singularity Executor"
    kind_description: ClassVar[str] = _(
        "Executes a command inside a Singularity/Apptainer container on the "
        "task target."
    )

    runtimes: ClassVar[RuntimeFilterType] = (CommandRuntime,)

    image: str
    """
    Path to the ``.sif`` image file on the target (e.g.
    ``/opt/images/boltz.sif``).  Unlike Docker there is no build step: the
    image must already exist on the target.

    Supports artifact substitution (see :attr:`binds`). This is the important
    difference from ``horus-docker``'s ``image``, which is a registry tag and
    so has nothing to substitute: a Singularity image is an ordinary file, so
    it can be declared as a workflow artifact and referenced here::

        image: ${drugflow_sif}

    That keeps the site-specific absolute path in the workflow's artifacts
    block, next to every other path an operator has to set, instead of buried
    in an executor field.
    """

    exe: str = "singularity"
    """
    Container CLI to invoke — ``singularity`` or ``apptainer``.
    """

    binds: dict[str, str] = Field(default_factory=dict)
    """
    Bind mounts as ``host_path -> container_path`` (``--bind`` flag).

    Both sides support ``$id`` / ``${id}`` / ``${id.attr}`` / ``${task.attr}``
    placeholders, resolved the same way a ``command`` string is (see
    :mod:`horus_builtin.runtime.substitution`) — ``$id`` renders to the
    artifact's absolute path on the task's target. This lets an explicit bind
    reference a task's own input/output paths, e.g.::

        binds:
          "${protein}": /data/protein.pdb

    Unknown placeholders are left as-is. Explicit entries here take precedence
    over the auto-bound artifact parent dirs (see
    :meth:`_singularity_exec_cmd`).

    Note that Singularity performs **no path translation**: unless you bind a
    path somewhere else, host paths are visible inside the container at the
    same location, which is why the identity auto-binds let a command use
    ``${artifact}`` unchanged.
    """

    nv: bool = False
    """
    Add ``--nv`` so the NVIDIA driver stack is exposed inside the container.
    """

    env: dict[str, str] = Field(default_factory=dict)
    """
    Environment variables to set inside the container, as ``NAME -> value``.
    Keys and values support ``$id`` / ``${id}`` / ``${id.attr}`` /
    ``${task.attr}`` artifact substitution (see :attr:`binds`).
    """

    working_dir: str | None = None
    """
    Working directory inside the container (``--pwd`` flag). Supports artifact
    substitution, see :attr:`binds`.

    Distinct from ``task.working_dir``, which is the *host* directory the
    ``singularity`` process itself is launched from. Setting neither leaves the
    container's working directory at the host cwd, which is the usual choice
    when the command already starts with its own ``cd``.
    """

    extra_args: list[str] = Field(default_factory=list)
    """
    Extra flags passed verbatim to ``<exe> exec`` before the image path.
    """

    _proc: Any = PrivateAttr(default=None)
    """
    Reference to the process started in :meth:`_execute` so
    :meth:`cancel_execution` can kill it.
    """

    @staticmethod
    def _sub(value: str, task: "BaseTask | None") -> str:
        """Render ``$``/``${}`` artifact placeholders in *value*."""
        return substitute(value, task) if task is not None else value

    @classmethod
    def _sub_dict(
        cls, mapping: dict[str, str], task: "BaseTask | None"
    ) -> dict[str, str]:
        """Apply :meth:`_sub` to every key and value of *mapping*."""
        return {
            cls._sub(k, task): cls._sub(v, task) for k, v in mapping.items()
        }

    def _binds_and_env(
        self, task: "BaseTask | None"
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return the bind mounts and container environment for *task*."""
        # auto-bind artifact parent dirs; explicit binds win
        auto_binds: dict[str, str] = {}
        env = self._sub_dict(self.env, task)
        if task is not None:
            for artifact in (*task.inputs, *task.outputs):
                host_dir = str(artifact.path.parent)
                auto_binds[host_dir] = host_dir
            # The container is a different mount namespace: the side-artifacts
            # directory the runtime collects from must be reachable at the same
            # path inside it, or scripts write into a throwaway view and
            # collect_side_artifacts() finds an empty directory.
            side_dir = task.side_artifacts_dir
            auto_binds[side_dir] = side_dir
            env.setdefault(runtime_settings.SIDE_ARTIFACTS_DIR_ENV, side_dir)
        explicit_binds = self._sub_dict(self.binds, task)
        return {**auto_binds, **explicit_binds}, env

    def _singularity_exec_cmd(
        self, prepared_command: str, task: "BaseTask | None" = None
    ) -> str:
        """Return the full ``singularity exec`` CLI command string."""
        merged_binds, env = self._binds_and_env(task)
        working_dir = (
            self._sub(self.working_dir, task) if self.working_dir else None
        )
        image = self._sub(self.image, task)

        parts = [shlex.quote(self.exe), "exec"]
        if self.nv:
            parts.append("--nv")
        for k, v in env.items():
            parts += ["--env", shlex.quote(f"{k}={v}")]
        for host, container in merged_binds.items():
            parts += ["--bind", shlex.quote(f"{host}:{container}")]
        if working_dir:
            parts += ["--pwd", shlex.quote(working_dir)]
        parts += [shlex.quote(arg) for arg in self.extra_args]
        parts += [
            shlex.quote(image),
            "/bin/sh",
            "-c",
            shlex.quote(prepared_command),
        ]
        return " ".join(parts)

    async def _execute(self, task: "BaseTask") -> None:
        """
        Run the task's command inside a Singularity/Apptainer container on
        the target.
        """
        if not isinstance(task.runtime, CommandRuntime):
            raise TaskExecutionError(
                _("SingularityExecutor only supports CommandRuntime runtimes.")
            )
        prepared_command = await task.runtime.setup_runtime(task)

        exec_cmd = self._singularity_exec_cmd(prepared_command, task)
        horus_logger.log.debug(
            _(
                "Running task %(task_id)s in Singularity image %(image)s: "
                "%(command)s"
            )
            % {
                "task_id": task.id,
                # Resolved, not raw: logging "${drugflow_sif}" when the run
                # fails on a missing image helps nobody.
                "image": self._sub(self.image, task),
                "command": prepared_command,
            }
        )

        proc = await task.target.run_command(
            exec_cmd,
            cwd=task.working_dir,
            env={
                runtime_settings.SIDE_ARTIFACTS_DIR_ENV: (
                    task.side_artifacts_dir
                )
            },
        )
        self._proc = proc

        # One try/finally around everything after launch: an earlier version
        # only wrapped the return-code check, so a cancellation left a dead
        # handle in self._proc for cancel_execution() to find.
        try:
            try:
                stdout, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise

            out = stdout.decode(errors="replace").strip() if stdout else ""
            err = stderr.decode(errors="replace").strip() if stderr else ""
            if out:
                horus_logger.log.info(out)
            if err:
                horus_logger.log.warning(err)

            if proc.returncode != 0:
                horus_logger.log.error(
                    _(
                        "Container for task %(task_id)s exited with code "
                        "%(code)s. Output: %(out)s"
                    )
                    % {
                        "task_id": task.id,
                        "code": proc.returncode,
                        "out": (out or err).strip(),
                    }
                )

                raise TaskExecutionError(
                    _("Container exited with code %(code)s")
                    % {"code": proc.returncode}
                )
        finally:
            self._proc = None

    async def cancel_execution(self) -> None:
        """Kill the running container process so it is not orphaned.

        Singularity containers have no daemon-side name to stop, so the
        process started in :meth:`_execute` is killed directly.  If nothing
        is running (e.g. the task finished before the cancel arrived) this
        is a safe no-op.
        """
        proc = self._proc
        if proc is None:
            return
        self._proc = None  # clear before kill — idempotent
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
