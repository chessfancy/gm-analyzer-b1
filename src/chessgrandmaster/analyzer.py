
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .parallel_runner import ParallelLucasRunner
from .engine_manifest import configured_engine
from .resource_telemetry import (
    collect_resource_sample,
    persist_resource_sample,
)


def _stored_info_json(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class TournamentAnalyzer:

    def __init__(
        self,
        db_path,
        engine_path,
        workers=2,
        threads=1,
        hash_mb=256,
        multipv=1,
        depth=18,
        time_sec=0.0,
        nodes=0,
        batch_size=20,
        snapshot_depths=(12, 14, 16, 18, 19),
        *,
        archive_root=None,
        archive_enabled=None,
    ):
        if depth <= 0:
            raise ValueError("depth must be positive")

        self.db_path = str(db_path)
        self.engine_path = str(engine_path)

        self.workers = workers
        self.threads = threads
        self.hash_mb = hash_mb
        self.multipv = multipv
        self.depth = depth
        self.time_sec = time_sec
        self.nodes = nodes
        self.batch_size = batch_size
        self.snapshot_depths = tuple(snapshot_depths)
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


    def _sha256_file(self, path):
        h = hashlib.sha256()

        with open(path, "rb") as f:
            for block in iter(
                lambda: f.read(1024 * 1024),
                b"",
            ):
                h.update(block)

        return h.hexdigest()


    def _sample_resources(
        self,
        run_id,
        root_pid,
        completed_positions,
        total_positions,
    ):
        """Best-effort resource sampling that cannot interrupt analysis."""
        try:
            sample = collect_resource_sample(
                root_pid,
                self.workers,
                self.hash_mb,
            )
            persist_resource_sample(
                self.db_path,
                run_id,
                sample,
                completed_positions=completed_positions,
                total_positions=total_positions,
            )
            return sample
        except Exception:
            return None


    def create_run(self, scope=None):

        engine = configured_engine()

        config = {
            "engine": engine["label"],
            "pipeline_version": (scope or {}).get("pipeline_version", 3),
            "scheduler": "game_affinity_lpt",
            "game_affinity": True,
            "telemetry_archive": True,
            "workers": self.workers,
            "threads_per_worker": self.threads,
            "hash_mb_per_worker": self.hash_mb,
            "multipv": self.multipv,
            "depth": self.depth,
            "time_ms": int(self.time_sec * 1000),
            "time_sec": self.time_sec,
            "depth_only": self.time_sec == 0.0,
            "nodes": self.nodes,
            "snapshot_depths": list(self.snapshot_depths),
            "scope": scope or {},
        }

        binary_sha = self._sha256_file(
            self.engine_path
        )

        con = sqlite3.connect(self.db_path)

        cur = con.execute("""
            INSERT INTO analysis_runs(
                engine_name,
                engine_version,
                binary_sha256,
                workers,
                threads,
                hash_mb,
                multipv,
                time_limit_ms,
                depth_limit,
                nodes_limit,
                config_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            engine["name"],
            engine["version"],
            binary_sha,
            self.workers,
            self.threads,
            self.hash_mb,
            self.multipv,
            int(self.time_sec * 1000) if self.time_sec else None,
            self.depth or None,
            self.nodes or None,
            json.dumps(
                config,
                ensure_ascii=False
            ),
            datetime.now(timezone.utc).isoformat(),
        ))

        run_id = cur.lastrowid

        con.commit()
        con.close()

        return run_id


    def _load_jobs(
        self,
        run_id,
        game_index=None,
        ply_start=None,
        ply_end=None,
        max_positions=None,
    ):

        sql = """
            SELECT
                m.id,
                m.game_id,
                g.source_game_index,
                m.ply,
                m.san,
                m.uci,
                m.fen_before
            FROM moves m
            JOIN games g
              ON g.id = m.game_id

            LEFT JOIN move_analysis ma
              ON ma.move_id = m.id
             AND ma.run_id = ?

            WHERE (
                ma.id IS NULL
                OR ma.status != 'completed'
            )
        """

        params = [run_id]

        if game_index is not None:
            sql += """
                AND g.source_game_index = ?
            """
            params.append(game_index)

        if ply_start is not None:
            sql += """
                AND m.ply >= ?
            """
            params.append(ply_start)

        if ply_end is not None:
            sql += """
                AND m.ply <= ?
            """
            params.append(ply_end)

        sql += """
            ORDER BY
                g.source_game_index,
                m.ply
        """

        if max_positions is not None:
            sql += " LIMIT ?"
            params.append(max_positions)

        con = sqlite3.connect(self.db_path)

        rows = con.execute(
            sql,
            params
        ).fetchall()

        con.close()


        jobs = []

        for job_id, row in enumerate(rows):

            (
                move_id,
                game_id,
                game_no,
                ply,
                san,
                uci,
                fen_before,
            ) = row

            jobs.append({
                "job_id": job_id,
                "move_id": move_id,
                "game_id": game_id,
                "source_game_index": game_no,
                "game_index": game_no,
                "ply": ply,
                "san": san,
                "played_uci": uci,
                "fen_before": fen_before,
            })

        return jobs


    def _write_results(
        self,
        run_id,
        results,
    ):

        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA foreign_keys=ON")

        completed = 0
        failed = 0

        for r in results:
            execution_id = r.get("execution_id")
            worker_id = r.get("worker_id")
            engine_session_id = r.get("engine_session_id")

            existing = con.execute("""
                SELECT id
                FROM move_analysis
                WHERE run_id=?
                  AND move_id=?
            """, (
                run_id,
                r["move_id"],
            )).fetchone()

            analysis_id = existing[0] if existing else None
            if analysis_id is not None:
                con.execute("""
                    DELETE FROM engine_responses
                    WHERE analysis_id=?
                """, (analysis_id,))
                con.execute("""
                    DELETE FROM engine_depth_snapshots
                    WHERE analysis_id=?
                """, (analysis_id,))

            if not r["ok"]:
                if analysis_id is not None:
                    con.execute("""
                        UPDATE move_analysis
                        SET
                            status='failed',
                            played_response_rank=NULL,
                            lucas_eval_loss=NULL,
                            category=NULL,
                            nag=NULL,
                            started_at=?,
                            finished_at=?,
                            execution_id=?,
                            worker_id=?,
                            engine_session_id=?
                        WHERE id=?
                    """, (
                        r.get("started_at"),
                        r.get("finished_at"),
                        execution_id,
                        worker_id,
                        engine_session_id,
                        analysis_id,
                    ))
                else:
                    con.execute("""
                        INSERT INTO move_analysis(
                            run_id,
                            move_id,
                            status,
                            started_at,
                            finished_at,
                            execution_id,
                            worker_id,
                            engine_session_id
                        )
                        VALUES (?, ?, 'failed', ?, ?, ?, ?, ?)
                    """, (
                        run_id,
                        r["move_id"],
                        r.get("started_at"),
                        r.get("finished_at"),
                        execution_id,
                        worker_id,
                        engine_session_id,
                    ))
                failed += 1
                continue

            if analysis_id is not None:
                con.execute("""
                    UPDATE move_analysis
                    SET
                        status='completed',
                        played_response_rank=?,
                        lucas_eval_loss=?,
                        category=?,
                        nag=?,
                        started_at=?,
                        finished_at=?,
                        execution_id=?,
                        worker_id=?,
                        engine_session_id=?
                    WHERE id=?
                """, (
                    r["played_rank"],
                    r["lucas_loss"],
                    r["category"],
                    r["nag"],
                    r.get("started_at"),
                    r.get("finished_at"),
                    execution_id,
                    worker_id,
                    engine_session_id,
                    analysis_id,
                ))
            else:
                cur = con.execute("""
                    INSERT INTO move_analysis(
                        run_id,
                        move_id,
                        status,
                        played_response_rank,
                        lucas_eval_loss,
                        category,
                        nag,
                        started_at,
                        finished_at,
                        execution_id,
                        worker_id,
                        engine_session_id
                    )
                    VALUES (
                        ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, (
                    run_id,
                    r["move_id"],
                    r["played_rank"],
                    r["lucas_loss"],
                    r["category"],
                    r["nag"],
                    r.get("started_at"),
                    r.get("finished_at"),
                    execution_id,
                    worker_id,
                    engine_session_id,
                ))
                analysis_id = cur.lastrowid

            def value(item, key):
                return item[key] if key in item else r.get(key)

            for response in r.get("responses", []):
                con.execute("""
                    INSERT INTO engine_responses(
                        analysis_id, rank, is_played, source_search,
                        uci, cp, mate, depth, seldepth, nodes, nps,
                        time_ms, pv_uci, hashfull, tbhits, info_json,
                        execution_id, worker_id, engine_session_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    analysis_id,
                    response.get("rank"),
                    int(response.get("is_played", False)),
                    response.get("source_search"),
                    response.get("uci"),
                    response.get("cp"),
                    response.get("mate"),
                    response.get("depth"),
                    response.get("seldepth"),
                    response.get("nodes"),
                    response.get("nps"),
                    response.get("time_ms"),
                    response.get("pv_uci"),
                    response.get("hashfull"),
                    response.get("tbhits"),
                    _stored_info_json(response.get("info_json")),
                    value(response, "execution_id"),
                    value(response, "worker_id"),
                    value(response, "engine_session_id"),
                ))

            for snapshot in r.get("depth_snapshots", []):
                con.execute("""
                    INSERT INTO engine_depth_snapshots(
                        analysis_id, source_search, checkpoint_depth,
                        reported_depth, uci, cp, mate, wdl_wins,
                        wdl_draws, wdl_losses, seldepth, nodes, nps,
                        time_ms, pv_uci, hashfull, tbhits, info_json,
                        execution_id, worker_id, engine_session_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    analysis_id,
                    snapshot.get("source_search"),
                    snapshot.get("checkpoint_depth"),
                    snapshot.get("reported_depth"),
                    snapshot.get("uci"),
                    snapshot.get("cp"),
                    snapshot.get("mate"),
                    snapshot.get("wdl_wins"),
                    snapshot.get("wdl_draws"),
                    snapshot.get("wdl_losses"),
                    snapshot.get("seldepth"),
                    snapshot.get("nodes"),
                    snapshot.get("nps"),
                    snapshot.get("time_ms"),
                    snapshot.get("pv_uci"),
                    snapshot.get("hashfull"),
                    snapshot.get("tbhits"),
                    _stored_info_json(snapshot.get("info_json")),
                    value(snapshot, "execution_id"),
                    value(snapshot, "worker_id"),
                    value(snapshot, "engine_session_id"),
                ))

            completed += 1


        # CHECKPOINT:
        # one commit after every batch.
        con.commit()
        con.close()

        return completed, failed


    def _persist_archive_manifests(self, run_id, manifests):
        if not manifests:
            return

        con = sqlite3.connect(self.db_path)
        try:
            for manifest in manifests:
                if self.archive_enabled:
                    if self.archive_root is None:
                        raise RuntimeError("UCI archive root is not configured")
                    archive_path = self.archive_root / manifest["relative_path"]
                    if not archive_path.is_file():
                        raise RuntimeError(
                            f"UCI archive missing: {archive_path}"
                        )
                    if manifest.get("compression") != "gzip":
                        raise RuntimeError(
                            "Unsupported UCI archive compression: "
                            f"{manifest.get('compression')}"
                        )
                    if manifest.get("closed_at") is None:
                        raise RuntimeError(
                            f"UCI archive was not closed: {archive_path}"
                        )
                con.execute("""
                    INSERT OR IGNORE INTO uci_event_archives(
                        run_id,
                        execution_id,
                        worker_id,
                        engine_session_id,
                        relative_path,
                        compression,
                        event_count,
                        created_at,
                        closed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id,
                    manifest["execution_id"],
                    manifest["worker_id"],
                    manifest["engine_session_id"],
                    manifest["relative_path"],
                    manifest["compression"],
                    manifest["event_count"],
                    manifest.get("created_at"),
                    manifest.get("closed_at"),
                ))
            con.commit()
        finally:
            con.close()


    def analyze(
        self,
        run_id,
        game_index=None,
        ply_start=None,
        ply_end=None,
        max_positions=None,
    ):

        jobs = self._load_jobs(
            run_id,
            game_index=game_index,
            ply_start=ply_start,
            ply_end=ply_end,
            max_positions=max_positions,
        )

        total = len(jobs)
        execution_id = str(uuid4())

        if total == 0:
            print("Nothing pending.")
            return {
                "completed": 0,
                "failed": 0,
                "pending_start": 0,
                "execution_id": execution_id,
                "archive_manifests": [],
            }


        print("Pending positions:", total)

        completed_total = 0
        failed_total = 0
        processed_total = 0
        result_buffer = []


        with ParallelLucasRunner(
            self.engine_path,
            workers=self.workers,
            threads=self.threads,
            hash_mb=self.hash_mb,
            multipv=self.multipv,
            depth=self.depth,
            time_sec=self.time_sec,
            nodes=self.nodes,
            snapshot_depths=self.snapshot_depths,
            run_id=run_id,
            execution_id=execution_id,
            archive_root=self.archive_root,
            archive_enabled=self.archive_enabled,
        ) as runner:

            root_pid = os.getpid()
            self._sample_resources(
                run_id,
                root_pid,
                completed_positions=0,
                total_positions=total,
            )

            for result in runner.iter_analyze(jobs):
                result_buffer.append(result)

                if len(result_buffer) < self.batch_size:
                    continue

                completed, failed = self._write_results(
                    run_id,
                    result_buffer,
                )
                result_buffer = []
                completed_total += completed
                failed_total += failed
                processed_total += completed + failed

                self._sample_resources(
                    run_id,
                    root_pid,
                    completed_positions=processed_total,
                    total_positions=total,
                )

                print(
                    f"Checkpoint {processed_total}/{total} | "
                    f"completed={completed_total} failed={failed_total}"
                )

            if result_buffer:
                completed, failed = self._write_results(
                    run_id,
                    result_buffer,
                )
                completed_total += completed
                failed_total += failed
                processed_total += completed + failed

                self._sample_resources(
                    run_id,
                    root_pid,
                    completed_positions=processed_total,
                    total_positions=total,
                )

                print(
                    f"Checkpoint {processed_total}/{total} | "
                    f"completed={completed_total} failed={failed_total}"
                )

            self._sample_resources(
                run_id,
                root_pid,
                completed_positions=processed_total,
                total_positions=total,
            )

            archive_manifests = list(runner.archive_manifests)

        self._persist_archive_manifests(
            run_id,
            archive_manifests,
        )


        return {
            "completed": completed_total,
            "failed": failed_total,
            "pending_start": total,
            "execution_id": execution_id,
            "archive_manifests": archive_manifests,
        }


    def status(
        self,
        run_id,
        game_index=None,
        ply_start=None,
        ply_end=None,
    ):

        sql = """
            SELECT
                COUNT(m.id),
                SUM(
                    CASE
                    WHEN ma.status='completed'
                    THEN 1 ELSE 0
                    END
                ),
                SUM(
                    CASE
                    WHEN ma.status='failed'
                    THEN 1 ELSE 0
                    END
                )
            FROM moves m
            JOIN games g
              ON g.id=m.game_id

            LEFT JOIN move_analysis ma
              ON ma.move_id=m.id
             AND ma.run_id=?

            WHERE 1=1
        """

        params = [run_id]

        if game_index is not None:
            sql += """
                AND g.source_game_index=?
            """
            params.append(game_index)

        if ply_start is not None:
            sql += """
                AND m.ply>=?
            """
            params.append(ply_start)

        if ply_end is not None:
            sql += """
                AND m.ply<=?
            """
            params.append(ply_end)

        con = sqlite3.connect(self.db_path)

        total, completed, failed = con.execute(
            sql,
            params
        ).fetchone()

        con.close()

        completed = completed or 0
        failed = failed or 0

        pending = total - completed

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
        }
