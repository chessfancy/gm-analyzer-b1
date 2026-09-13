import multiprocessing as mp
import queue
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .engine_worker import LucasEngineWorker
from .uci_telemetry import UciEventArchive


def _job_game_key(job):
    if job.get("game_id") is not None:
        return job["game_id"]
    if job.get("source_game_index") is not None:
        return ("source_game_index", job["source_game_index"])
    if job.get("game_index") is not None:
        return ("game_index", job["game_index"])
    return ("job", job.get("job_id"))


def _stable_key(value):
    return (type(value).__name__, str(value))


def group_jobs_by_game(jobs):
    """Group jobs by stable database game identity and sort by ply."""
    groups = {}
    for job in jobs:
        groups.setdefault(_job_game_key(job), []).append(job)

    for game_jobs in groups.values():
        game_jobs.sort(
            key=lambda job: (
                job.get("ply", 0),
                str(job.get("job_id", "")),
            )
        )
    return groups


def assign_games_to_workers(jobs, workers):
    """Assign complete games with deterministic largest-first balancing."""
    workers = int(workers)
    if workers <= 0:
        raise ValueError("workers must be positive")

    groups = group_jobs_by_game(jobs)
    ordered_games = sorted(
        groups.items(),
        key=lambda item: (-len(item[1]), _stable_key(item[0])),
    )

    assignments = [[] for _ in range(workers)]
    assigned_counts = [0 for _ in range(workers)]

    for _, game_jobs in ordered_games:
        worker_id = min(
            range(workers),
            key=lambda index: (assigned_counts[index], index),
        )
        assignments[worker_id].extend(game_jobs)
        assigned_counts[worker_id] += len(game_jobs)

    return assignments


def _worker_loop(
    worker_id,
    engine_path,
    config,
    job_queue,
    result_queue,
    archive_root=None,
    run_id=None,
    execution_id=None,
    archive_enabled=False,
):
    worker = None
    archive = None
    initialized = False
    fatal = None
    engine_session_id = str(uuid4())

    try:
        if archive_enabled:
            archive_path = (
                Path(archive_root)
                / "uci"
                / f"run_{run_id}"
                / f"execution_{execution_id}"
                / f"worker_{worker_id}_{engine_session_id}.jsonl.gz"
            )
            archive = UciEventArchive(
                archive_path,
                archive_root=archive_root,
            )

        worker = LucasEngineWorker(
            engine_path,
            threads=config["threads"],
            hash_mb=config["hash_mb"],
            multipv=config["multipv"],
            depth=config["depth"],
            time_sec=config["time_sec"],
            nodes=config.get("nodes", 0),
            snapshot_depths=config["snapshot_depths"],
            execution_id=execution_id,
            worker_id=worker_id,
            engine_session_id=engine_session_id,
            archive=archive,
        )
        initialized = True

        game_tokens = {}

        while True:
            job = job_queue.get()

            if job is None:
                break

            started_at = datetime.now(timezone.utc).isoformat()
            game_key = _job_game_key(job)
            game_token = game_tokens.setdefault(
                game_key,
                f"run:{run_id}:game:{game_key}",
            )
            context = {
                "run_id": run_id,
                "execution_id": execution_id,
                "worker_id": worker_id,
                "engine_session_id": engine_session_id,
                "game_id": job.get("game_id"),
                "source_game_index": job.get(
                    "source_game_index",
                    job.get("game_index"),
                ),
                "move_id": job.get("move_id"),
                "ply": job.get("ply"),
            }

            try:
                result = worker.analyze_move(
                    job["fen_before"],
                    job["played_uci"],
                    game_token=game_token,
                    telemetry_context=context,
                )
                finished_at = datetime.now(timezone.utc).isoformat()

                result_queue.put({
                    "type": "job_result",
                    "ok": True,
                    "execution_id": execution_id,
                    "worker_id": worker_id,
                    "engine_session_id": engine_session_id,
                    "job_id": job["job_id"],
                    "move_id": job.get("move_id"),
                    "game_id": job.get("game_id"),
                    "source_game_index": job.get(
                        "source_game_index",
                        job.get("game_index"),
                    ),
                    "ply": job.get("ply"),
                    "san": job.get("san"),
                    "played_uci": job["played_uci"],
                    "started_at": started_at,
                    "finished_at": finished_at,

                    "played_rank": result.played_rank,
                    "lucas_loss": result.lucas_loss,
                    "category": result.category,
                    "nag": result.nag,
                    "second_search": result.second_search,

                    "responses": [
                        asdict(response)
                        for response in result.responses
                    ],
                    "depth_snapshots": [
                        asdict(snapshot)
                        for snapshot in result.depth_snapshots
                    ],
                })

            except Exception as exc:
                from .uci_telemetry import UciArchiveError

                if isinstance(exc, UciArchiveError):
                    raise

                finished_at = datetime.now(timezone.utc).isoformat()

                result_queue.put({
                    "type": "job_result",
                    "ok": False,
                    "execution_id": execution_id,
                    "worker_id": worker_id,
                    "engine_session_id": engine_session_id,
                    "job_id": job["job_id"],
                    "move_id": job.get("move_id"),
                    "game_id": job.get("game_id"),
                    "source_game_index": job.get(
                        "source_game_index",
                        job.get("game_index"),
                    ),
                    "ply": job.get("ply"),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                })

    except Exception as exc:
        fatal = {
            "type": "worker_fatal",
            "ok": False,
            "execution_id": execution_id,
            "worker_id": worker_id,
            "engine_session_id": engine_session_id,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }

    finally:
        if worker is not None:
            try:
                worker.close()
            except Exception as exc:
                if fatal is None:
                    fatal = {
                        "type": "worker_fatal",
                        "ok": False,
                        "execution_id": execution_id,
                        "worker_id": worker_id,
                        "engine_session_id": engine_session_id,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    }

        manifest = None
        if archive is not None:
            try:
                manifest = archive.close()
            except Exception as exc:
                if fatal is None:
                    fatal = {
                        "type": "worker_fatal",
                        "ok": False,
                        "execution_id": execution_id,
                        "worker_id": worker_id,
                        "engine_session_id": engine_session_id,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    }

        if fatal is not None:
            result_queue.put(fatal)
        elif initialized:
            result_queue.put({
                "type": "worker_finished",
                "ok": True,
                "execution_id": execution_id,
                "worker_id": worker_id,
                "engine_session_id": engine_session_id,
                "archive_manifest": manifest,
            })


class ParallelLucasRunner:

    def __init__(
        self,
        engine_path,
        workers=2,
        threads=1,
        hash_mb=256,
        multipv=1,
        depth=18,
        time_sec=0.0,
        nodes=0,
        snapshot_depths=(12, 14, 16, 18, 19),
        *,
        run_id=None,
        execution_id=None,
        archive_root=None,
        archive_enabled=None,
    ):
        if workers <= 0:
            raise ValueError("workers must be positive")
        if depth <= 0:
            raise ValueError("depth must be positive")

        self.engine_path = str(engine_path)
        self.num_workers = int(workers)
        self.run_id = run_id
        self.execution_id = execution_id or str(uuid4())
        self.archive_root = (
            Path(archive_root).resolve()
            if archive_root is not None
            else None
        )
        self.archive_enabled = (
            self.archive_root is not None
            if archive_enabled is None
            else bool(archive_enabled)
        )

        self.config = {
            "threads": threads,
            "hash_mb": hash_mb,
            "multipv": multipv,
            "depth": depth,
            "time_sec": time_sec,
            "nodes": nodes,
            "snapshot_depths": tuple(snapshot_depths),
        }

        start_methods = mp.get_all_start_methods()
        method = "fork" if "fork" in start_methods else start_methods[0]
        self.ctx = mp.get_context(method)

        self.job_queues = []
        self.result_queue = self.ctx.Queue(maxsize=max(2, self.num_workers * 2))
        self.processes = []
        self.assignments = []
        self.archive_manifests = []
        self._sentinels_sent = False
        self._dispatch_started = False

    def start(self):
        if self.processes:
            return

        for worker_id in range(self.num_workers):
            job_queue = self.ctx.Queue(maxsize=2)
            process = self.ctx.Process(
                target=_worker_loop,
                args=(
                    worker_id,
                    self.engine_path,
                    self.config,
                    job_queue,
                    self.result_queue,
                    self.archive_root,
                    self.run_id,
                    self.execution_id,
                    self.archive_enabled,
                ),
            )

            process.start()
            self.job_queues.append(job_queue)
            self.processes.append(process)

    def iter_analyze(self, jobs):
        """Yield completed job results while workers retain game affinity."""
        if self._dispatch_started:
            raise RuntimeError("runner dispatch can only start once")

        self._dispatch_started = True
        jobs = list(jobs)
        self.assignments = assign_games_to_workers(
            jobs,
            self.num_workers,
        )
        self.start()

        received = 0
        finished_workers = 0
        pending = [None for _ in self.assignments]
        iterators = [iter(worker_jobs) for worker_jobs in self.assignments]
        sentinels = [False for _ in self.assignments]

        def handle_message(message):
            nonlocal received, finished_workers
            message_type = message.get("type", "job_result")
            if message_type == "worker_fatal":
                raise RuntimeError(
                    "Stockfish worker failed: "
                    f"{message.get('error')}\n"
                    f"{message.get('traceback', '')}"
                )

            if message_type == "worker_finished":
                finished_workers += 1
                manifest = message.get("archive_manifest")
                if manifest is not None:
                    self.archive_manifests.append({
                        "run_id": self.run_id,
                        "execution_id": self.execution_id,
                        "worker_id": message.get("worker_id"),
                        "engine_session_id": message.get(
                            "engine_session_id"
                        ),
                        **manifest,
                    })
                return None

            received += 1
            return message

        while received < len(jobs) or finished_workers < self.num_workers:
            made_progress = False

            for worker_id, job_iterator in enumerate(iterators):
                if sentinels[worker_id]:
                    continue

                if pending[worker_id] is None:
                    try:
                        pending[worker_id] = next(job_iterator)
                    except StopIteration:
                        pending[worker_id] = False

                try:
                    self.job_queues[worker_id].put_nowait(
                        None if pending[worker_id] is False else pending[worker_id]
                    )
                except queue.Full:
                    continue

                made_progress = True
                if pending[worker_id] is False:
                    sentinels[worker_id] = True
                pending[worker_id] = None

            while True:
                try:
                    message = self.result_queue.get_nowait()
                except queue.Empty:
                    break
                result = handle_message(message)
                if result is not None:
                    yield result

            self._sentinels_sent = all(sentinels)
            if made_progress:
                continue

            try:
                message = self.result_queue.get(timeout=1)
            except queue.Empty:
                if (
                    self.processes
                    and all(not process.is_alive() for process in self.processes)
                ):
                    raise RuntimeError(
                        "Stockfish worker exited before reporting all results"
                    )
                continue

            result = handle_message(message)
            if result is not None:
                yield result

        if received != len(jobs):
            raise RuntimeError(
                f"Expected {len(jobs)} results, received {received}"
            )

    def analyze(self, jobs):
        """Compatibility helper returning results in job order."""
        results = list(self.iter_analyze(jobs))
        results.sort(key=lambda result: result["job_id"])
        return results

    def close(self):
        if not self.processes:
            return

        if not self._sentinels_sent:
            for job_queue in self.job_queues:
                try:
                    job_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._sentinels_sent = True

        for process in self.processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join()

        self.processes = []
        self.job_queues = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
