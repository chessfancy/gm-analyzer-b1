
import multiprocessing as mp
import traceback
from dataclasses import asdict
from datetime import datetime, timezone

from .engine_worker import LucasEngineWorker


def _worker_loop(
    worker_id,
    engine_path,
    config,
    job_queue,
    result_queue,
):
    worker = None

    try:
        worker = LucasEngineWorker(
            engine_path,
            threads=config["threads"],
            hash_mb=config["hash_mb"],
            multipv=config["multipv"],
            depth=config["depth"],
            time_sec=config["time_sec"],
            nodes=config.get("nodes", 0),
            snapshot_depths=config["snapshot_depths"],
        )

        while True:

            job = job_queue.get()

            if job is None:
                break

            started_at = datetime.now(timezone.utc).isoformat()

            try:
                result = worker.analyze_move(
                    job["fen_before"],
                    job["played_uci"],
                )
                finished_at = datetime.now(timezone.utc).isoformat()

                result_queue.put({
                    "ok": True,
                    "worker_id": worker_id,
                    "job_id": job["job_id"],
                    "move_id": job.get("move_id"),
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
                        asdict(r)
                        for r in result.responses
                    ],
                    "depth_snapshots": [
                        asdict(snapshot)
                        for snapshot in result.depth_snapshots
                    ],
                })

            except Exception as exc:
                finished_at = datetime.now(timezone.utc).isoformat()

                result_queue.put({
                    "ok": False,
                    "worker_id": worker_id,
                    "job_id": job["job_id"],
                    "move_id": job.get("move_id"),
                    "ply": job.get("ply"),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                })

    finally:
        if worker is not None:
            worker.close()


class ParallelLucasRunner:

    def __init__(
        self,
        engine_path,
        workers=2,
        threads=1,
        hash_mb=256,
        multipv=1,
        depth=18,
        time_sec=3.0,
        nodes=0,
        snapshot_depths=(12, 14, 16, 18, 19),
    ):
        self.engine_path = str(engine_path)
        self.num_workers = workers

        self.config = {
            "threads": threads,
            "hash_mb": hash_mb,
            "multipv": multipv,
            "depth": depth,
            "time_sec": time_sec,
            "nodes": nodes,
            "snapshot_depths": tuple(snapshot_depths),
        }

        # Deepnote/Linux:
        # fork works reliably from notebook when worker
        # entrypoint lives in a real Python module.
        self.ctx = mp.get_context("fork")

        self.job_queue = self.ctx.Queue()
        self.result_queue = self.ctx.Queue()

        self.processes = []


    def start(self):

        if self.processes:
            return

        for worker_id in range(self.num_workers):

            p = self.ctx.Process(
                target=_worker_loop,
                args=(
                    worker_id,
                    self.engine_path,
                    self.config,
                    self.job_queue,
                    self.result_queue,
                ),
            )

            p.start()

            self.processes.append(p)


    def analyze(self, jobs):

        self.start()

        jobs = list(jobs)

        for job in jobs:
            self.job_queue.put(job)

        results = []

        for _ in range(len(jobs)):
            results.append(
                self.result_queue.get()
            )

        # Parallel completion order is arbitrary.
        # Restore original job order for audit/output.
        results.sort(
            key=lambda x: x["job_id"]
        )

        return results


    def close(self):

        if not self.processes:
            return

        for _ in self.processes:
            self.job_queue.put(None)

        for p in self.processes:
            p.join(timeout=10)

            if p.is_alive():
                p.terminate()
                p.join()

        self.processes = []


    def __enter__(self):
        self.start()
        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
