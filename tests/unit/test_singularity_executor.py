#
# horus_singularity
# Copyright (c) 2026 Temple Compute
#
# MIT License
#
"""Unit tests for SingularityExecutor."""

import asyncio
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from horus_builtin.artifact.file import FileArtifact
from horus_builtin.runtime.command import CommandRuntime
from horus_builtin.task.horus_task import HorusTask
from horus_runtime.context import HorusContext
from horus_runtime.core.executor.base import BaseExecutor
from horus_runtime.core.task.exceptions import TaskExecutionError
from horus_runtime.settings import runtime_settings

from horus_singularity.executor.singularity import SingularityExecutor

_IMAGE = "/opt/images/boltz.sif"
_WORKDIR = "/work"


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
    target.resolved_working_directory = _WORKDIR
    # Mirror LocalTarget.path_on_target so `${id}` substitution (used by the
    # binds/env/working_dir tests) resolves to the artifact's real path
    # instead of a MagicMock repr.
    target.path_on_target = lambda artifact: str(artifact.path)
    return target


def _make_remapping_target(proc: AsyncMock | None = None) -> MagicMock:
    """
    Return a mock target that relocates artifacts, like a remote one.

    Mirrors ``SSHTarget.path_on_target``: artifacts are copied to
    ``<working_directory>/<name>`` on the far host, so the orchestrator's own
    path for an artifact says nothing about where it lives on the target.
    """
    target = _make_mock_target(proc)
    target.path_on_target = lambda artifact: (
        f"{_WORKDIR}/{Path(artifact.path).name}"
    )
    return target


@pytest.mark.unit
class TestSingularityExecutorRegistration:
    """Verify SingularityExecutor registers via the horus.executor group."""

    def test_registered_under_kind(self) -> None:
        """SingularityExecutor must appear in BaseExecutor.registry."""
        assert "singularity" in BaseExecutor.registry
        assert BaseExecutor.registry["singularity"] is SingularityExecutor

    def test_kind_field(self) -> None:
        """The kind field must equal 'singularity'."""
        assert SingularityExecutor(image=_IMAGE).kind == "singularity"

    def test_runtimes_filter(self) -> None:
        """CommandRuntime must be the accepted runtime type."""
        assert CommandRuntime in SingularityExecutor.runtimes


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
    async def test_auto_binds_use_target_paths_not_orchestrator_paths(
        self, horus_context: HorusContext
    ) -> None:
        """
        On a remapping target, auto-binds must name the target's directory.

        Outputs are never transferred before execution, so their path is
        still the orchestrator's run directory. Binding that path made
        Singularity abort with `mount source ... doesn't exist` on the
        compute node.
        """
        del horus_context
        run_dir = "/tmp/tc-orchestrator/d53a38af"
        executor = SingularityExecutor(image=_IMAGE)
        task = HorusTask(
            id="test-task",
            name="test_task",
            executor=executor,
            runtime=CommandRuntime(command="true"),
            inputs=[FileArtifact(id="inp", path=Path(f"{run_dir}/in.sdf"))],
            outputs=[FileArtifact(id="out", path=Path(f"{run_dir}/out.sdf"))],
        )
        mock_target = _make_remapping_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        assert f"{_WORKDIR}:{_WORKDIR}" in cmd
        assert run_dir not in cmd

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
    async def test_placeholders_substituted_in_binds_env_and_pwd(
        self, horus_context: HorusContext
    ) -> None:
        """binds, env and working_dir must render ${artifact} placeholders."""
        del horus_context
        executor = SingularityExecutor(
            image=_IMAGE,
            binds={"${inp}": "/data/protein.pdb"},
            env={"OUT_PATH": "${out}"},
            working_dir="${inp.path}",
        )
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        assert "/data/results/in.pdb:/data/protein.pdb" in cmd
        assert "OUT_PATH=/data/results/out.pdb" in cmd
        assert "--pwd /data/results/in.pdb" in cmd

    @pytest.mark.asyncio
    async def test_image_path_is_substituted(
        self, horus_context: HorusContext
    ) -> None:
        """
        `image` must render placeholders: a Singularity image is a file, so it
        can be declared as a workflow artifact (unlike a Docker registry tag).
        """
        del horus_context
        executor = SingularityExecutor(image="${inp}")
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        parts = shlex.split(cmd)
        assert parts[-4] == "/data/results/in.pdb"
        assert "${inp}" not in cmd

    def test_literal_image_path_is_untouched(self) -> None:
        """A plain path with no placeholder must pass through unchanged."""
        cmd = SingularityExecutor(image=_IMAGE)._singularity_exec_cmd("true")
        assert shlex.split(cmd)[-4] == _IMAGE

    @pytest.mark.asyncio
    async def test_side_artifacts_dir_bound_and_exported(
        self, horus_context: HorusContext
    ) -> None:
        """
        The side-artifacts dir must be bound at its own path and exported
        inside the container, or the runtime collects nothing.
        """
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
            # Read inside the patch: side_artifacts_dir derives from the
            # target's working directory, so it changes once the patch exits.
            side_dir = task.side_artifacts_dir
        cmd = mock_target.run_command.call_args[0][0]
        assert f"{side_dir}:{side_dir}" in cmd
        assert f"{runtime_settings.SIDE_ARTIFACTS_DIR_ENV}={side_dir}" in cmd

    @pytest.mark.asyncio
    async def test_side_artifacts_env_passed_to_run_command(
        self, horus_context: HorusContext
    ) -> None:
        """The host process must also receive the side-artifacts env var."""
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
            side_dir = task.side_artifacts_dir
        env = mock_target.run_command.call_args.kwargs["env"]
        assert env[runtime_settings.SIDE_ARTIFACTS_DIR_ENV] == side_dir

    @pytest.mark.asyncio
    async def test_explicit_env_wins_over_side_artifacts_default(
        self, horus_context: HorusContext
    ) -> None:
        """A user-set side-artifacts env var must not be overwritten."""
        del horus_context
        executor = SingularityExecutor(
            image=_IMAGE,
            env={runtime_settings.SIDE_ARTIFACTS_DIR_ENV: "/custom/side"},
        )
        task = self._make_task(executor, command="true", artifacts=True)
        mock_target = _make_mock_target()
        with patch.object(task, "target", mock_target):
            await executor._execute(task)
        cmd = mock_target.run_command.call_args[0][0]
        assert f"{runtime_settings.SIDE_ARTIFACTS_DIR_ENV}=/custom/side" in cmd

    @pytest.mark.asyncio
    async def test_non_command_runtime_rejected(
        self, horus_context: HorusContext
    ) -> None:
        """A runtime that is not a CommandRuntime must be refused."""
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="true")
        with patch.object(task, "runtime", MagicMock()):
            with pytest.raises(
                TaskExecutionError, match="only supports CommandRuntime"
            ):
                await executor._execute(task)

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_are_logged(
        self, horus_context: HorusContext
    ) -> None:
        """Container stdout goes to INFO and stderr to WARNING."""
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="true")
        mock_target = _make_mock_target(
            _make_mock_proc(stdout=b"hello\n", stderr=b"careful\n")
        )
        with patch.object(task, "target", mock_target):
            with patch(
                "horus_singularity.executor.singularity.horus_logger"
            ) as logger:
                await executor._execute(task)
        logger.log.info.assert_called_once_with("hello")
        logger.log.warning.assert_called_once_with("careful")

    @pytest.mark.asyncio
    async def test_cancelled_execution_kills_and_clears_proc(
        self, horus_context: HorusContext
    ) -> None:
        """
        A cancellation must kill the process and still clear ``_proc``.

        Regression guard: the try/finally originally wrapped only the
        return-code check, leaving a dead handle behind on this path.
        """
        del horus_context
        executor = SingularityExecutor(image=_IMAGE)
        task = self._make_task(executor, command="true")
        proc = _make_mock_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        mock_target = _make_mock_target(proc)
        with patch.object(task, "target", mock_target):
            with pytest.raises(asyncio.CancelledError):
                await executor._execute(task)
        proc.kill.assert_called_once()
        assert executor._proc is None

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
