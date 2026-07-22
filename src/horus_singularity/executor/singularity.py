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
from horus_runtime.core.executor.base import BaseExecutor, RuntimeFilterType
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.logging import horus_logger
from pydantic import Field, PrivateAttr

from horus_singularity.i18n import tr as _

if TYPE_CHECKING:
    from horus_runtime.core.task.base import BaseTask


class SingularityExecutor(BaseExecutor):
    """
    Runs the task's command inside a Singularity/Apptainer container.
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
    """

    exe: str = "singularity"
    """
    Container CLI to invoke — ``singularity`` or ``apptainer``.
    """

    binds: dict[str, str] = Field(default_factory=dict)
    """
    Bind mounts as ``host_path -> container_path`` (``--bind`` flag).
    """

    nv: bool = False
    """
    Add ``--nv`` so the NVIDIA driver stack is exposed inside the container.
    """

    env: dict[str, str] = Field(default_factory=dict)
    """
    Environment variables to set inside the container, as ``NAME -> value``.
    """

    working_dir: str | None = None
    """
    Working directory inside the container (``--pwd`` flag).
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

    def _singularity_exec_cmd(
        self, prepared_command: str, task: "BaseTask | None" = None
    ) -> str:
        """Return the full ``singularity exec`` CLI command string."""
        # ponytail: auto-mount artifact parent dirs; explicit binds win
        auto_mounts: dict[str, str] = {}
        if task is not None:
            for artifact in (*task.inputs, *task.outputs):
                host_dir = str(artifact.path.parent)
                auto_mounts[host_dir] = host_dir
        merged_binds = {**auto_mounts, **self.binds}

        parts = [shlex.quote(self.exe), "exec"]
        if self.nv:
            parts.append("--nv")
        for k, v in self.env.items():
            parts += ["--env", shlex.quote(f"{k}={v}")]
        for host, container in merged_binds.items():
            parts += ["--bind", shlex.quote(f"{host}:{container}")]
        if self.working_dir:
            parts += ["--pwd", shlex.quote(self.working_dir)]
        parts += [shlex.quote(arg) for arg in self.extra_args]
        parts += [
            shlex.quote(self.image),
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
                "image": self.image,
                "command": prepared_command,
            }
        )

        proc = await task.target.run_command(exec_cmd, cwd=task.working_dir)
        self._proc = proc

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

        try:
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
