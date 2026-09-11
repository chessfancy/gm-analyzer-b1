
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .parallel_runner import ParallelLucasRunner


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
        time_sec=3.0,
        nodes=0,
        batch_size=20,
    ):
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


    def _sha256_file(self, path):
        h = hashlib.sha256()

        with open(path, "rb") as f:
            for block in iter(
                lambda: f.read(1024 * 1024),
                b"",
            ):
                h.update(block)

        return h.hexdigest()


    def create_run(self, scope=None):

        config = {
            "engine": "Stockfish 19",
            "workers": self.workers,
            "threads_per_worker": self.threads,
            "hash_mb_per_worker": self.hash_mb,
            "multipv": self.multipv,
            "depth": self.depth,
            "time_ms": int(self.time_sec * 1000),
            "nodes": self.nodes,
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
                config_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Stockfish",
            "19",
            binary_sha,
            self.workers,
            self.threads,
            self.hash_mb,
            self.multipv,
            int(self.time_sec * 1000),
            self.depth or None,
            self.nodes or None,
            json.dumps(
                config,
                ensure_ascii=False
            ),
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
                game_no,
                ply,
                san,
                uci,
                fen_before,
            ) = row

            jobs.append({
                "job_id": job_id,
                "move_id": move_id,
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

            now = datetime.now(
                timezone.utc
            ).isoformat()


            # ----------------------------------
            # Failed engine job
            # ----------------------------------

            if not r["ok"]:

                existing = con.execute("""
                    SELECT id
                    FROM move_analysis
                    WHERE run_id=?
                      AND move_id=?
                """, (
                    run_id,
                    r["move_id"],
                )).fetchone()

                if existing:

                    analysis_id = existing[0]

                    con.execute("""
                        DELETE FROM engine_responses
                        WHERE analysis_id=?
                    """, (analysis_id,))

                    con.execute("""
                        UPDATE move_analysis
                        SET
                            status='failed',
                            played_response_rank=NULL,
                            lucas_eval_loss=NULL,
                            category=NULL,
                            nag=NULL,
                            finished_at=?
                        WHERE id=?
                    """, (
                        now,
                        analysis_id,
                    ))

                else:

                    con.execute("""
                        INSERT INTO move_analysis(
                            run_id,
                            move_id,
                            status,
                            finished_at
                        )
                        VALUES (?, ?, 'failed', ?)
                    """, (
                        run_id,
                        r["move_id"],
                        now,
                    ))

                failed += 1
                continue


            # ----------------------------------
            # Successful job
            # ----------------------------------

            existing = con.execute("""
                SELECT id
                FROM move_analysis
                WHERE run_id=?
                  AND move_id=?
            """, (
                run_id,
                r["move_id"],
            )).fetchone()


            if existing:

                analysis_id = existing[0]

                con.execute("""
                    DELETE FROM engine_responses
                    WHERE analysis_id=?
                """, (
                    analysis_id,
                ))

                con.execute("""
                    UPDATE move_analysis
                    SET
                        status='completed',
                        played_response_rank=?,
                        lucas_eval_loss=?,
                        category=?,
                        nag=?,
                        finished_at=?
                    WHERE id=?
                """, (
                    r["played_rank"],
                    r["lucas_loss"],
                    r["category"],
                    r["nag"],
                    now,
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
                        finished_at
                    )
                    VALUES (
                        ?, ?,
                        'completed',
                        ?, ?, ?, ?, ?
                    )
                """, (
                    run_id,
                    r["move_id"],
                    r["played_rank"],
                    r["lucas_loss"],
                    r["category"],
                    r["nag"],
                    now,
                ))

                analysis_id = cur.lastrowid


            for response in r["responses"]:

                con.execute("""
                    INSERT INTO engine_responses(
                        analysis_id,
                        rank,
                        is_played,
                        source_search,
                        uci,
                        cp,
                        mate,
                        depth,
                        seldepth,
                        nodes,
                        nps,
                        time_ms,
                        pv_uci
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                """, (
                    analysis_id,
                    response["rank"],
                    int(response["is_played"]),
                    response["source_search"],
                    response["uci"],
                    response["cp"],
                    response["mate"],
                    response["depth"],
                    response["seldepth"],
                    response["nodes"],
                    response["nps"],
                    response["time_ms"],
                    response["pv_uci"],
                ))

            completed += 1


        # CHECKPOINT:
        # one commit after every batch.
        con.commit()
        con.close()

        return completed, failed


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

        if total == 0:
            print("Nothing pending.")
            return {
                "completed": 0,
                "failed": 0,
                "pending_start": 0,
            }


        print("Pending positions:", total)

        completed_total = 0
        failed_total = 0


        with ParallelLucasRunner(
            self.engine_path,
            workers=self.workers,
            threads=self.threads,
            hash_mb=self.hash_mb,
            multipv=self.multipv,
            depth=self.depth,
            time_sec=self.time_sec,
            nodes=self.nodes,
        ) as runner:

            for start in range(
                0,
                total,
                self.batch_size,
            ):

                batch = jobs[
                    start:
                    start + self.batch_size
                ]

                # job_id only needs to be unique
                # within this runner call.
                results = runner.analyze(batch)

                completed, failed = (
                    self._write_results(
                        run_id,
                        results,
                    )
                )

                completed_total += completed
                failed_total += failed

                done = min(
                    start + len(batch),
                    total
                )

                print(
                    f"Checkpoint "
                    f"{done}/{total} | "
                    f"completed={completed_total} "
                    f"failed={failed_total}"
                )


        return {
            "completed": completed_total,
            "failed": failed_total,
            "pending_start": total,
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
