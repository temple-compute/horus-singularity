#
# horus_singularity
# Copyright (c) 2026 Temple Compute
#
# MIT License
#
"""Unit tests for SingularityExecutor."""

import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.task.exceptions import TaskExecutionError

from horus_singularity.executor.singularity import SingularityExecutor

_IMAGE = "/opt/images/boltz.sif"


def _make_mock_proc(
    returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> AsyncMock:
    """Return an AsyncMock ChannelProcess."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _make_mock_target(proc: AsyncMock | None = None) -> MagicMock:
    """Return a mock target whose run_command returns *proc*."""
    target = MagicMock()
    target.run_command = AsyncMock(return_value=proc or _make_mock_proc())
    target.put_file = AsyncMock()
    target.mkdir = AsyncMock()
    return target


@pytest.mark.unit
class TestSingularityExecCmd:
    """Verify _singularity_exec_cmd() builds correct CLI strings."""

    def test_minimal_command(self) -> None:
        """
        A minimal executor yields '<exe> exec <image> /bin/sh -c <cmd>'.
        """
        cmd = SingularityExecutor(image=_IMAGE)._singularity_exec_cmd(
            "echo hello"
        )
        parts = shlex.split(cmd)
        assert parts[:2] == ["singularity", "exec"]
        assert parts[-4:] == [_IMAGE, "/bin/sh", "-c", "echo hello"]

    def test_nv_only_when_enabled(self) -> None:
        """--nv must appear only when nv=True."""
        assert "--nv" not in SingularityExecutor(
            image=_IMAGE
        )._singularity_exec_cmd("true")
        assert "--nv" in SingularityExecutor(
            image=_IMAGE, nv=True
        )._singularity_exec_cmd("true")

    def test_env_pwd_exe_and_extra_args(self) -> None:
        """exe, --env, --pwd and extra_args must all be rendered."""
        cmd = SingularityExecutor(
            image=_IMAGE,
            exe="apptainer",
            env={"FOO": "bar"},
            working_dir="/work",
            extra_args=["--cleanenv"],
        )._singularity_exec_cmd("true")
        parts = shlex.split(cmd)
        assert parts[0] == "apptainer"
        assert "--env" in parts
        assert "FOO=bar" in parts
        assert "--pwd" in parts
        assert "/work" in parts
        assert parts.index("--cleanenv") < parts.index(_IMAGE)


@pytest.mark.unit
class TestSingularityExecutorExecute:
    """Verify SingularityExecutor._execute() end-to-end behaviour."""

    def _make_task(
        self,
        executor: SingularityExecutor,
        command: str = "echo hello",
        artifacts: bool = False,
    ) -> HorusTask:
        return HorusTask(
            id="test-task",
            name="test_task",
            executor=executor,
            runtime=CommandRuntime(command=command),
            inputs=(
                [FileArtifact(id="inp", path=Path("/data/results/in.pdb"))]
                if artifacts
                else []
            ),
            outputs=(
                [FileArtifact(id="out", path=Path("/data/results/out.pdb"))]
                if artifacts
                else []
            ),
        )

    @pytest.mark.asyncio
    async def test_binds_and_auto_binds_both_present(
        self, horus_context: HorusContext
    ) -> None:
        """Explicit binds and artifact auto-binds must both be emitted."""
        del horus_context
        executor = SingularityExecutor(
            image=_IMAGE, binds={"/scratch": "/mnt/scratch"}
        )
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        assert "/data/results:/data/results" in cmd
        assert "/scratch:/mnt/scratch" in cmd

    @pytest.mark.asyncio
    async def test_explicit_bind_overrides_auto_bind(
        self, horus_context: HorusContext
    ) -> None:
        """An explicit bind wins over the auto-bind for the same host dir."""
        del horus_context
        executor = SingularityExecutor(
            image=_IMAGE, binds={"/data/results": "/mnt/custom"}
        )
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        assert "/data/results:/mnt/custom" in cmd
        assert "/data/results:/data/results" not in cmd

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_raises(
        self, horus_context: HorusContext
    ) -> None:
        """A non-zero container exit code must raise TaskExecutionError."""
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="exit 1")
        mock_target = _make_mock_target(
            _make_mock_proc(returncode=1, stderr=b"boom")
        )
        with patch.object(task, "target", mock_target):
            with pytest.raises(
                TaskExecutionError, match="Container exited with code 1"
            ):
                await executor._execute(task)

    @pytest.mark.asyncio
    async def test_cancel_execution_kills_running_process(
        self, horus_context: HorusContext
    ) -> None:
        """cancel_execution() kills a live process and no-ops otherwise."""
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        await executor.cancel_execution()  # no process — safe no-op

        proc = _make_mock_proc()
        proc.returncode = None
        executor._proc = proc
        await executor.cancel_execution()
        proc.kill.assert_called_once()
        assert executor._proc is None
