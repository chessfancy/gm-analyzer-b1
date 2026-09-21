"""Provider-neutral local worker materialization and command specifications."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .bundles import validate_job_bundle
from .profiles import ProviderProfile, get_provider_profile


@dataclass(frozen=True)
class WorkerCommand:
    """A command a provider wrapper may execute later.

    Building this object never starts a subprocess, provider API, notebook, or
    Stockfish process.
    """

    profile: str
    job_id: str
    workdir: Path
    command: tuple[str, ...]
    environment: Mapping[str, str]
    invoked: bool = False

    @property
    def cwd(self) -> Path:
        return self.workdir


class WorkerContract(Protocol):
    """Minimal provider-neutral contract implemented by local/platform wrappers."""

    def prepare(self, job_bundle: str | Path) -> WorkerCommand:
        """Materialize a verified job and return, but do not execute, a command."""
        ...



def build_worker_command(
    profile: str | ProviderProfile,
    *,
    job_id: str,
    workdir: str | Path,
    engine_command: Sequence[str] = ("cgm-analyze",),
) -> WorkerCommand:
    """Build a frozen B1 command specification from runtime profile metadata."""

    resolved = get_provider_profile(profile)
    workdir_path = Path(workdir).resolve()
    input_path = workdir_path / "input.pgn"
    cgm_home = workdir_path / "cgm-home"
    cgm_home.mkdir(parents=True, exist_ok=True)
    command = tuple(str(part) for part in engine_command) + (
        "--workers",
        str(resolved.workers),
        "--threads",
        str(resolved.threads),
        "--hash-mb",
        str(resolved.hash_mb),
        "--depth",
        str(resolved.depth),
        str(input_path),
    )
    return WorkerCommand(
        profile=resolved.name,
        job_id=job_id,
        workdir=workdir_path,
        command=command,
        environment={"CGM_HOME": str(cgm_home)},
    )


class LocalWorker:
    """Materialize a job into an isolated temporary workdir without executing it."""

    def __init__(
        self,
        profile: str | ProviderProfile,
        *,
        work_root: str | Path | None = None,
        engine_command: Sequence[str] = ("cgm-analyze",),
    ) -> None:
        self.profile = get_provider_profile(profile)
        self.work_root = Path(work_root).resolve() if work_root is not None else None
        self.engine_command = tuple(str(part) for part in engine_command)
        if self.work_root is not None:
            self.work_root.mkdir(parents=True, exist_ok=True)

    def prepare(self, job_bundle: str | Path) -> WorkerCommand:
        """Validate and copy a job bundle into a new isolated work directory."""

        validated = validate_job_bundle(job_bundle)
        workdir = Path(
            tempfile.mkdtemp(
                prefix=f"cgm-{validated.job_id}-",
                dir=str(self.work_root) if self.work_root is not None else None,
            )
        ).resolve()
        try:
            for source in validated.path.iterdir():
                destination = workdir / source.name
                if source.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
            (workdir / "outputs").mkdir()
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        return build_worker_command(
            self.profile,
            job_id=validated.job_id,
            workdir=workdir,
            engine_command=self.engine_command,
        )

    materialize = prepare
    command_spec = prepare

    @staticmethod
    def cleanup(command: WorkerCommand) -> None:
        """Remove a prepared workdir when a caller no longer needs it."""

        shutil.rmtree(command.workdir, ignore_errors=True)


__all__ = ["LocalWorker", "WorkerCommand", "WorkerContract", "build_worker_command"]
