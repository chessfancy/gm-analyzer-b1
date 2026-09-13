
import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import chess
import chess.pgn

from .analyzer import TournamentAnalyzer
from .resource_telemetry import resource_summary
from .pgn_export import LucasPGNExporter
from .engine_manifest import (
    resolve_installed_engine,
    verify_engine_binary,
)


PIPELINE_VERSION = 2


# ============================================================
# PATH / HASH
# ============================================================

def sha256_file(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)

    return h.hexdigest()


def safe_stem(name):
    stem = Path(name).stem

    stem = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        stem
    )

    return stem.strip("_")


def resolve_input_path(value):
    """
    Resolve an existing PGN path without knowing anything
    about the cloud/runtime hosting the analyzer.
    """

    path = Path(value).expanduser()

    if path.exists():
        return path.resolve()

    raise FileNotFoundError(
        f"Input PGN not found: {value}"
    )


def default_workspace_root():
    """
    Persistent working directory for databases, archived
    inputs, outputs and logs.

    CGM_HOME can be set by Deepnote, Kaggle, Codespaces,
    VPS or any other runtime.
    """

    value = os.environ.get("CGM_HOME")

    if value:
        return Path(value).expanduser().resolve()

    return (Path.cwd() / ".cgm").resolve()


def resolve_engine_binary(value=None):
    """Resolve Stockfish through the canonical portable resolver."""
    return resolve_installed_engine(value)


# ============================================================
# SQLITE SCHEMA
# ============================================================

def ensure_schema(db_path):

    con = sqlite3.connect(db_path)

    con.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;


    CREATE TABLE IF NOT EXISTS source_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        imported_at TEXT,
        game_count INTEGER DEFAULT 0
    );


    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        source_file_id INTEGER NOT NULL,
        source_game_index INTEGER NOT NULL,

        event TEXT,
        site TEXT,
        date TEXT,
        round TEXT,
        white TEXT,
        black TEXT,
        result TEXT,

        initial_fen TEXT NOT NULL,
        headers_json TEXT NOT NULL,
        game_hash TEXT,

        UNIQUE(
            source_file_id,
            source_game_index
        ),

        FOREIGN KEY(source_file_id)
            REFERENCES source_files(id)
    );


    CREATE TABLE IF NOT EXISTS moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        game_id INTEGER NOT NULL,
        ply INTEGER NOT NULL,

        move_number INTEGER,
        side TEXT,

        uci TEXT NOT NULL,
        san TEXT NOT NULL,

        fen_before TEXT NOT NULL,
        fen_after TEXT NOT NULL,

        UNIQUE(
            game_id,
            ply
        ),

        FOREIGN KEY(game_id)
            REFERENCES games(id)
    );


    CREATE TABLE IF NOT EXISTS analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        engine_name TEXT,
        engine_version TEXT,
        binary_sha256 TEXT,

        workers INTEGER,
        threads INTEGER,
        hash_mb INTEGER,
        multipv INTEGER,

        time_limit_ms INTEGER,
        depth_limit INTEGER,
        nodes_limit INTEGER,

        config_json TEXT,
        created_at TEXT
    );


    CREATE TABLE IF NOT EXISTS move_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        run_id INTEGER NOT NULL,
        move_id INTEGER NOT NULL,

        status TEXT,

        played_response_rank INTEGER,
        lucas_eval_loss REAL,

        category TEXT,
        nag INTEGER,

        started_at TEXT,
        finished_at TEXT,

        UNIQUE(
            run_id,
            move_id
        ),

        FOREIGN KEY(run_id)
            REFERENCES analysis_runs(id),

        FOREIGN KEY(move_id)
            REFERENCES moves(id)
    );


    CREATE TABLE IF NOT EXISTS engine_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        analysis_id INTEGER NOT NULL,

        rank INTEGER,
        is_played INTEGER,
        source_search TEXT,

        uci TEXT,

        cp INTEGER,
        mate INTEGER,

        depth INTEGER,
        seldepth INTEGER,

        nodes INTEGER,
        nps INTEGER,
        time_ms INTEGER,

        pv_uci TEXT,

        FOREIGN KEY(analysis_id)
            REFERENCES move_analysis(id)
    );


    CREATE TABLE IF NOT EXISTS engine_depth_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_id INTEGER NOT NULL,
        source_search TEXT NOT NULL,
        checkpoint_depth INTEGER NOT NULL,
        reported_depth INTEGER NOT NULL,
        uci TEXT,
        cp INTEGER,
        mate INTEGER,
        wdl_wins INTEGER,
        wdl_draws INTEGER,
        wdl_losses INTEGER,
        seldepth INTEGER,
        nodes INTEGER,
        nps INTEGER,
        time_ms INTEGER,
        pv_uci TEXT,
        UNIQUE(analysis_id, source_search, checkpoint_depth),
        FOREIGN KEY(analysis_id) REFERENCES move_analysis(id)
    );


    CREATE TABLE IF NOT EXISTS runtime_resource_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        sampled_at TEXT NOT NULL,
        completed_positions INTEGER,
        total_positions INTEGER,
        configured_workers INTEGER,
        configured_hash_mb_per_worker INTEGER,
        configured_hash_total_bytes INTEGER,
        cgroup_memory_current_bytes INTEGER,
        cgroup_memory_max_bytes INTEGER,
        mem_total_bytes INTEGER,
        mem_available_bytes INTEGER,
        cgm_process_tree_rss_bytes INTEGER,
        stockfish_process_count INTEGER,
        stockfish_rss_bytes INTEGER,
        FOREIGN KEY(run_id) REFERENCES analysis_runs(id)
    );


    CREATE INDEX IF NOT EXISTS idx_moves_game
        ON moves(game_id);


    CREATE INDEX IF NOT EXISTS idx_ma_run
        ON move_analysis(run_id);


    CREATE INDEX IF NOT EXISTS idx_ma_category
        ON move_analysis(run_id, category);


    CREATE INDEX IF NOT EXISTS idx_er_analysis
        ON engine_responses(analysis_id);


    CREATE INDEX IF NOT EXISTS idx_eds_analysis
        ON engine_depth_snapshots(analysis_id);


    CREATE INDEX IF NOT EXISTS idx_rrs_run
        ON runtime_resource_samples(run_id);
    """)

    con.commit()
    con.close()


# ============================================================
# DB DISCOVERY
# ============================================================

def db_contains_source(
    db_path,
    source_sha
):

    if not db_path.exists():
        return False

    try:
        con = sqlite3.connect(db_path)

        exists = con.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name='source_files'
        """).fetchone()

        if not exists:
            con.close()
            return False

        row = con.execute("""
            SELECT id
            FROM source_files
            WHERE sha256=?
        """, (
            source_sha,
        )).fetchone()

        con.close()

        return row is not None

    except sqlite3.Error:
        return False


def choose_database(
    db_dir,
    slug,
    source_sha
):

    db_dir = Path(db_dir)

    # First preserve compatibility with our
    # original golden analysis.sqlite.
    canonical = db_dir / "analysis.sqlite"

    if db_contains_source(
        canonical,
        source_sha
    ):
        return canonical


    # Future tournaments get their own isolated DB.
    job_db = (
        db_dir /
        f"analysis_{slug}_{source_sha[:8]}.sqlite"
    )

    if db_contains_source(
        job_db,
        source_sha
    ):
        return job_db

    return job_db


# ============================================================
# ARCHIVE INPUT
# ============================================================

def archive_input(
    source_path,
    archive_dir,
    source_sha
):

    archive_dir = Path(archive_dir)

    archive_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    target = (
        archive_dir /
        source_path.name
    )

    if target.exists():

        if sha256_file(target) == source_sha:
            return target

        target = (
            archive_dir /
            (
                f"{source_path.stem}_"
                f"{source_sha[:8]}"
                f"{source_path.suffix}"
            )
        )

    if not target.exists():

        shutil.copy2(
            source_path,
            target
        )

    return target


# ============================================================
# PGN IMPORT
# ============================================================

def import_pgn(
    pgn_path,
    db_path
):

    ensure_schema(db_path)

    source_sha = sha256_file(
        pgn_path
    )

    con = sqlite3.connect(db_path)
    con.execute(
        "PRAGMA foreign_keys=ON"
    )


    existing = con.execute("""
        SELECT
            id,
            game_count

        FROM source_files

        WHERE sha256=?
    """, (
        source_sha,
    )).fetchone()


    if existing:

        source_file_id = existing[0]

        moves = con.execute("""
            SELECT COUNT(*)

            FROM moves m

            JOIN games g
              ON g.id=m.game_id

            WHERE g.source_file_id=?
        """, (
            source_file_id,
        )).fetchone()[0]

        con.close()

        return {
            "existing": True,
            "source_file_id": source_file_id,
            "games": existing[1],
            "moves": moves,
            "sha256": source_sha,
        }


    try:

        cur = con.execute("""
            INSERT INTO source_files(
                filename,
                sha256,
                imported_at,
                game_count
            )
            VALUES (?, ?, ?, 0)
        """, (
            pgn_path.name,
            source_sha,
            datetime.now().isoformat(
                timespec="seconds"
            ),
        ))

        source_file_id = cur.lastrowid


        game_count = 0
        move_count = 0


        with open(
            pgn_path,
            encoding="utf-8-sig",
            errors="replace"
        ) as f:

            while True:

                game = chess.pgn.read_game(f)

                if game is None:
                    break

                game_count += 1


                if game.errors:

                    raise RuntimeError(
                        "PGN parser error in "
                        f"game #{game_count}: "
                        f"{game.errors}"
                    )


                headers = dict(
                    game.headers
                )

                headers_json = json.dumps(
                    headers,
                    ensure_ascii=False,
                    separators=(",", ":")
                )


                board = game.board()

                initial_fen = board.fen()


                li_moves = []

                for ply, move in enumerate(
                    game.mainline_moves(),
                    start=1
                ):

                    fen_before = board.fen()

                    move_number = (
                        board.fullmove_number
                    )

                    side = (
                        "white"
                        if board.turn
                        else "black"
                    )

                    san = board.san(move)
                    uci = move.uci()

                    board.push(move)

                    fen_after = board.fen()

                    li_moves.append((
                        ply,
                        move_number,
                        side,
                        uci,
                        san,
                        fen_before,
                        fen_after,
                    ))


                game_hash_data = (
                    headers_json
                    + "|"
                    + " ".join(
                        x[3]
                        for x in li_moves
                    )
                )

                game_hash = hashlib.sha256(
                    game_hash_data.encode(
                        "utf-8"
                    )
                ).hexdigest()


                cur = con.execute("""
                    INSERT INTO games(
                        source_file_id,
                        source_game_index,

                        event,
                        site,
                        date,
                        round,
                        white,
                        black,
                        result,

                        initial_fen,
                        headers_json,
                        game_hash
                    )
                    VALUES (
                        ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?
                    )
                """, (
                    source_file_id,
                    game_count,

                    headers.get("Event", ""),
                    headers.get("Site", ""),
                    headers.get("Date", ""),
                    headers.get("Round", ""),
                    headers.get("White", ""),
                    headers.get("Black", ""),
                    headers.get("Result", ""),

                    initial_fen,
                    headers_json,
                    game_hash,
                ))

                game_id = cur.lastrowid


                con.executemany("""
                    INSERT INTO moves(
                        game_id,
                        ply,
                        move_number,
                        side,
                        uci,
                        san,
                        fen_before,
                        fen_after
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, [
                    (
                        game_id,
                        ply,
                        move_number,
                        side,
                        uci,
                        san,
                        fen_before,
                        fen_after,
                    )
                    for (
                        ply,
                        move_number,
                        side,
                        uci,
                        san,
                        fen_before,
                        fen_after,
                    )
                    in li_moves
                ])


                move_count += len(
                    li_moves
                )


        con.execute("""
            UPDATE source_files
            SET game_count=?
            WHERE id=?
        """, (
            game_count,
            source_file_id,
        ))

        con.commit()


    except Exception:

        con.rollback()
        con.close()

        raise


    con.close()


    return {
        "existing": False,
        "source_file_id": source_file_id,
        "games": game_count,
        "moves": move_count,
        "sha256": source_sha,
    }


# ============================================================
# RUN RECOVERY
# ============================================================

def find_or_create_run(
    analyzer,
    db_path,
    source_sha,
    config
):

    def normalize_engine_config(value):
        if not isinstance(value, dict):
            return {}

        result = dict(value)

        # Runtime path is not part of engine identity.
        binary = result.pop("binary", None)

        if "snapshot_depths" in result:
            result["snapshot_depths"] = list(
                result["snapshot_depths"]
            )

        # When the binary exists, identify it by content instead.
        if (
            binary
            and "binary_sha256" not in result
        ):
            binary_path = Path(binary).expanduser()

            if binary_path.is_file():
                result["binary_sha256"] = (
                    sha256_file(binary_path)
                )

        return result


    desired_engine = normalize_engine_config(
        config
    )

    desired_scope = {
        "purpose":
            "production_tournament_full",

        "pipeline_version":
            PIPELINE_VERSION,

        "source_sha256":
            source_sha,

        "engine":
            desired_engine,
    }


    con = sqlite3.connect(
        db_path
    )

    rows = con.execute("""
        SELECT
            id,
            config_json,
            binary_sha256,
            workers,
            threads,
            hash_mb,
            multipv,
            time_limit_ms,
            depth_limit

        FROM analysis_runs

        ORDER BY id DESC
    """).fetchall()


    source_count = con.execute("""
        SELECT COUNT(*)
        FROM source_files
    """).fetchone()[0]

    con.close()


    # Exact modern pipeline match.
    for row in rows:

        run_id = row[0]
        config_json = row[1]

        try:
            data = json.loads(
                config_json or "{}"
            )
        except Exception:
            continue

        scope = data.get(
            "scope",
            {}
        )

        if (
            isinstance(scope, dict)
            and scope == desired_scope
        ):
            return run_id, False


    # Compatibility with older production runs.
    #
    # Old runs may have:
    #
    #     scope = {
    #         "purpose":
    #             "production_tournament_full"
    #     }
    #
    # Recover them only when the actual engine/search
    # configuration also matches.
    if source_count == 1:

        for row in rows:

            (
                run_id,
                config_json,
                binary_sha256,
                workers,
                threads,
                hash_mb,
                multipv,
                time_limit_ms,
                depth_limit,
            ) = row

            try:
                data = json.loads(
                    config_json or "{}"
                )
            except Exception:
                continue

            scope = data.get(
                "scope",
                {}
            )

            if not (
                isinstance(scope, dict)
                and
                scope.get("purpose")
                ==
                "production_tournament_full"
            ):
                continue


            # Transitional modern scope:
            # it may contain an engine config whose only
            # portability problem is the binary path.
            stored_engine = normalize_engine_config(
                scope.get("engine")
            )


            # Original legacy production run:
            # reconstruct engine identity from DB columns.
            if not stored_engine:

                stored_engine = {
                    "workers":
                        workers,

                    "threads":
                        threads,

                    "hash_mb":
                        hash_mb,

                    "multipv":
                        multipv,

                    "depth":
                        depth_limit,
                }

                if time_limit_ms is not None:
                    stored_engine["time_sec"] = (
                        time_limit_ms / 1000.0
                    )


            # analysis_runs has always stored the actual
            # engine binary digest. Add it when available.
            if binary_sha256:
                stored_engine["binary_sha256"] = (
                    binary_sha256
                )


            # Ignore absent legacy metadata, but never ignore
            # a conflicting value.
            compatible = True

            for key, desired_value in (
                desired_engine.items()
            ):

                if (
                    key in stored_engine
                    and
                    stored_engine[key]
                    != desired_value
                ):
                    compatible = False
                    break

                if (
                    key not in stored_engine
                    and
                    key != "binary_sha256"
                ):
                    compatible = False
                    break


            if compatible:
                return run_id, False


    run_id = analyzer.create_run(
        scope=desired_scope
    )

    return run_id, True



# ============================================================
# DB AUDIT
# ============================================================

def database_audit(
    db_path,
    run_id
):

    con = sqlite3.connect(
        db_path
    )


    total = con.execute("""
        SELECT COUNT(*)
        FROM move_analysis
        WHERE run_id=?
          AND status='completed'
    """, (
        run_id,
    )).fetchone()[0]


    failed = con.execute("""
        SELECT COUNT(*)
        FROM move_analysis
        WHERE run_id=?
          AND status='failed'
    """, (
        run_id,
    )).fetchone()[0]


    categories = dict(
        con.execute("""
            SELECT
                category,
                COUNT(*)

            FROM move_analysis

            WHERE run_id=?
              AND status='completed'

            GROUP BY category
        """, (
            run_id,
        )).fetchall()
    )


    responses = con.execute("""
        SELECT COUNT(*)

        FROM engine_responses er

        JOIN move_analysis ma
          ON ma.id=er.analysis_id

        WHERE ma.run_id=?
    """, (
        run_id,
    )).fetchone()[0]


    second = con.execute("""
        SELECT COUNT(*)

        FROM engine_responses er

        JOIN move_analysis ma
          ON ma.id=er.analysis_id

        WHERE ma.run_id=?
          AND er.source_search='post_move'
    """, (
        run_id,
    )).fetchone()[0]


    bad_nags = con.execute("""
        SELECT COUNT(*)

        FROM move_analysis

        WHERE run_id=?
          AND (
               (category='NO_RATING'
                    AND nag != 0)

            OR (category='INACCURACY'
                    AND nag != 6)

            OR (category='MISTAKE'
                    AND nag != 2)

            OR (category='BLUNDER'
                    AND nag != 4)
          )
    """, (
        run_id,
    )).fetchone()[0]


    con.close()


    return {
        "completed": total,
        "failed": failed,
        "responses": responses,
        "second_searches": second,
        "categories": categories,
        "bad_nags": bad_nags,
    }


def snapshot_audit(
    db_path,
    run_id,
    expected_depths,
):
    """Check depth snapshot coverage and final primary consistency.

    ``expected_depths`` contains the checkpoints required for each completed
    analysis.  A post-move search is required only for analyses that have a
    final ``post_move`` response.  The final primary response is compared with
    the snapshot at its final checkpoint on score and first PV move.
    """

    expected = tuple(dict.fromkeys(expected_depths))

    con = sqlite3.connect(db_path)

    try:
        completed_ids = [
            row[0]
            for row in con.execute(
                """
                SELECT id
                FROM move_analysis
                WHERE run_id=?
                  AND status='completed'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        ]

        snapshot_rows = con.execute(
            """
            SELECT eds.analysis_id, eds.source_search,
                   eds.checkpoint_depth, eds.cp, eds.mate,
                   eds.uci, eds.pv_uci
            FROM engine_depth_snapshots eds
            JOIN move_analysis ma
              ON ma.id=eds.analysis_id
            WHERE ma.run_id=?
              AND ma.status='completed'
            """,
            (run_id,),
        ).fetchall()

        snapshots = {}
        for (
            analysis_id,
            source_search,
            checkpoint_depth,
            cp,
            mate,
            uci,
            pv_uci,
        ) in snapshot_rows:
            snapshots.setdefault(
                (analysis_id, source_search),
                {},
            )[checkpoint_depth] = {
                "cp": cp,
                "mate": mate,
                "uci": uci,
                "pv_uci": pv_uci,
            }

        primary_missing = sum(
            depth not in snapshots.get((analysis_id, "primary"), {})
            for analysis_id in completed_ids
            for depth in expected
        )

        post_move_ids = [
            row[0]
            for row in con.execute(
                """
                SELECT DISTINCT ma.id
                FROM move_analysis ma
                JOIN engine_responses er
                  ON er.analysis_id=ma.id
                WHERE ma.run_id=?
                  AND ma.status='completed'
                  AND er.source_search='post_move'
                ORDER BY ma.id
                """,
                (run_id,),
            ).fetchall()
        ]

        post_move_missing = sum(
            depth not in snapshots.get((analysis_id, "post_move"), {})
            for analysis_id in post_move_ids
            for depth in expected
        )

        final_rows = con.execute(
            """
            SELECT ma.id, er.depth, er.cp, er.mate, er.uci, er.pv_uci
            FROM move_analysis ma
            LEFT JOIN engine_responses er
              ON er.analysis_id=ma.id
             AND er.source_search='primary'
             AND er.rank=0
            WHERE ma.run_id=?
              AND ma.status='completed'
            ORDER BY ma.id
            """,
            (run_id,),
        ).fetchall()
    finally:
        con.close()

    def first_pv_move(pv_uci, uci):
        if pv_uci:
            return pv_uci.split()[0]
        return uci or ""

    final_mismatches = 0
    final_snapshot_missing = 0

    for analysis_id, final_depth, final_cp, final_mate, final_uci, final_pv in final_rows:
        if final_depth not in expected:
            continue

        final_snapshot = snapshots.get(
            (analysis_id, "primary"),
            {},
        ).get(final_depth)

        if final_snapshot is None:
            final_snapshot_missing += 1
            final_mismatches += 1
            continue

        if (
            final_snapshot["cp"] != final_cp
            or final_snapshot["mate"] != final_mate
            or first_pv_move(
                final_snapshot["pv_uci"],
                final_snapshot["uci"],
            )
            != first_pv_move(final_pv, final_uci)
        ):
            final_mismatches += 1

    audit = {
        "completed": len(completed_ids),
        "expected_depths": list(expected),
        "primary_expected": len(completed_ids) * len(expected),
        "primary_present":
            len(completed_ids) * len(expected) - primary_missing,
        "primary_missing": primary_missing,
        "post_move_analyses": len(post_move_ids),
        "post_move_expected": len(post_move_ids) * len(expected),
        "post_move_present":
            len(post_move_ids) * len(expected) - post_move_missing,
        "post_move_missing": post_move_missing,
        "final_checked": len(final_rows),
        "final_snapshot_missing": final_snapshot_missing,
        "final_mismatches": final_mismatches,
    }

    print()
    print("=== DEPTH SNAPSHOT AUDIT ===")
    print("primary snapshot missing=", audit["primary_missing"])
    print("post_move snapshot missing=", audit["post_move_missing"])
    print("final snapshot mismatches=", audit["final_mismatches"])

    return audit


# ============================================================
# PGN AUDIT
# ============================================================

def audit_export(
    path
):

    total = 0
    fen_records = 0
    original_records = 0
    slices_with_variation = 0


    with open(
        path,
        encoding="utf-8",
        errors="replace"
    ) as f:

        while True:

            game = chess.pgn.read_game(
                f
            )

            if game is None:
                break

            total += 1


            if "FEN" in game.headers:

                fen_records += 1

                if (
                    len(game.variations)
                    >= 2
                ):
                    slices_with_variation += 1

            else:
                original_records += 1


    return {
        "total_records": total,
        "fen_slices": fen_records,
        "original_games":
            original_records,

        "slices_with_variation":
            slices_with_variation,
    }


# ============================================================
# EXPECTED EXPORT COUNTS
# ============================================================

def expected_category(
    db_path,
    run_id,
    category
):

    con = sqlite3.connect(
        db_path
    )


    slices = con.execute("""
        SELECT COUNT(*)

        FROM move_analysis

        WHERE run_id=?
          AND category=?
          AND status='completed'
    """, (
        run_id,
        category,
    )).fetchone()[0]


    games = con.execute("""
        SELECT COUNT(
            DISTINCT m.game_id
        )

        FROM move_analysis ma

        JOIN moves m
          ON m.id=ma.move_id

        WHERE ma.run_id=?
          AND ma.category=?
          AND ma.status='completed'
    """, (
        run_id,
        category,
    )).fetchone()[0]


    con.close()


    return {
        "slices": slices,
        "original_games": games,
        "records":
            slices + games,
    }


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_pipeline(
    pgn_input,

    root=None,

    engine_binary=None,

    workers=2,
    threads=1,
    hash_mb=256,
    multipv=1,
    depth=18,
    time_sec=0.0,
    snapshot_depths=(12, 14, 16, 18, 19),
):
    if depth <= 0:
        raise ValueError("depth must be positive")


    root = (
        Path(root).expanduser().resolve()
        if root is not None
        else default_workspace_root()
    )

    engine_binary = resolve_engine_binary(
        engine_binary
    )

    engine_meta = verify_engine_binary(
        engine_binary
    )

    db_dir = root / "db"
    input_dir = root / "input"
    output_dir = root / "output"

    db_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    input_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    # --------------------------------------------------------
    # Resolve PGN
    # --------------------------------------------------------

    pgn_path = resolve_input_path(
        pgn_input
    )

    source_sha = sha256_file(
        pgn_path
    )

    slug = safe_stem(
        pgn_path.name
    )


    print("=" * 70)
    print("CHESSGRANDMASTER PRODUCTION PIPELINE")
    print("=" * 70)

    print("Input :", pgn_path)
    print("SHA256:", source_sha)


    # --------------------------------------------------------
    # Archive raw PGN
    # --------------------------------------------------------

    archived = archive_input(
        pgn_path,
        input_dir,
        source_sha
    )

    print("Archive:", archived)


    # --------------------------------------------------------
    # Choose / recover database
    # --------------------------------------------------------

    db_path = choose_database(
        db_dir,
        slug,
        source_sha
    )

    print("DB     :", db_path)


    # --------------------------------------------------------
    # Import
    # --------------------------------------------------------

    imported = import_pgn(
        pgn_path,
        db_path
    )

    print()
    print("=== IMPORT ===")
    print("Existing :", imported["existing"])
    print("Games    :", imported["games"])
    print("Moves    :", imported["moves"])


    if imported["games"] <= 0:
        raise RuntimeError(
            "PGN contains no games"
        )

    if imported["moves"] <= 0:
        raise RuntimeError(
            "PGN contains no moves"
        )


    # --------------------------------------------------------
    # Analyzer
    # --------------------------------------------------------

    analyzer = TournamentAnalyzer(
        db_path,
        engine_binary,

        workers=workers,
        threads=threads,
        hash_mb=hash_mb,
        multipv=multipv,
        depth=depth,
        time_sec=time_sec,
        snapshot_depths=snapshot_depths,
        batch_size=100,
    )


    engine_config = {
        "binary":
            str(engine_binary),

        "workers":
            workers,

        "threads":
            threads,

        "hash_mb":
            hash_mb,

        "multipv":
            multipv,

        "depth":
            depth,

        "time_sec":
            time_sec,

        "depth_only":
            time_sec == 0.0,

        "snapshot_depths":
            list(snapshot_depths),
    }


    run_id, created = (
        find_or_create_run(
            analyzer,
            db_path,
            source_sha,
            engine_config
        )
    )


    print()
    print("=== ANALYSIS RUN ===")

    print(
        "Run ID :",
        run_id,
        "(new)"
        if created
        else "(recovered)"
    )


    before = analyzer.status(
        run_id
    )

    print("Before :", before)


    if (
        before["pending"] > 0
        or before["failed"] > 0
    ):

        result = analyzer.analyze(
            run_id
        )

        print("Result :", result)

    else:

        print(
            "Nothing pending. "
            "Engine analysis skipped."
        )


    after = analyzer.status(
        run_id
    )

    print("After  :", after)

    resource_audit = resource_summary(
        db_path,
        run_id,
    )

    print()
    print("=== RESOURCE AUDIT ===")
    print(
        "Samples                 :",
        resource_audit["sample_count"],
    )
    print(
        "Configured Hash total   :",
        resource_audit["configured_hash_total_bytes"],
    )
    print(
        "Peak Stockfish RSS      :",
        resource_audit["peak_stockfish_rss_bytes"],
    )
    print(
        "Peak CGM tree RSS       :",
        resource_audit["peak_cgm_process_tree_rss_bytes"],
    )
    print(
        "Peak cgroup usage       :",
        resource_audit["peak_cgroup_memory_current_bytes"],
    )
    print(
        "Cgroup limit            :",
        resource_audit["cgroup_memory_max_bytes"],
    )
    print(
        "Minimum MemAvailable    :",
        resource_audit["minimum_mem_available_bytes"],
    )
    print(
        "Max Stockfish processes :",
        resource_audit["max_stockfish_process_count"],
    )


    expected_moves = imported[
        "moves"
    ]


    if (
        after["total"]
        != expected_moves
    ):

        raise RuntimeError(
            "Analysis total mismatch: "
            f"{after['total']} "
            f"!= {expected_moves}"
        )


    if (
        after["completed"]
        != expected_moves
        or after["failed"] != 0
        or after["pending"] != 0
    ):

        raise RuntimeError(
            "Analysis incomplete: "
            f"{after}"
        )


    # --------------------------------------------------------
    # Analysis audit
    # --------------------------------------------------------

    audit = database_audit(
        db_path,
        run_id
    )


    if audit["bad_nags"] != 0:

        raise RuntimeError(
            "Bad NAG mappings: "
            f"{audit['bad_nags']}"
        )


    if (
        audit["responses"]
        !=
        audit["completed"]
        +
        audit["second_searches"]
    ):

        raise RuntimeError(
            "Response invariant failed"
        )


    print()
    print("=== ANALYSIS AUDIT ===")
    print(
        "Completed       :",
        audit["completed"]
    )
    print(
        "Responses       :",
        audit["responses"]
    )
    print(
        "Second searches :",
        audit["second_searches"]
    )
    print(
        "Bad NAGs        :",
        audit["bad_nags"]
    )
    print(
        "Categories      :",
        audit["categories"]
    )


    # --------------------------------------------------------
    # Depth snapshot audit
    # --------------------------------------------------------

    active_snapshot_depths = tuple(
        checkpoint
        for checkpoint in snapshot_depths
        if checkpoint <= depth
    )

    snapshot_audit_result = snapshot_audit(
        db_path,
        run_id,
        active_snapshot_depths,
    )

    if (
        snapshot_audit_result["primary_missing"] != 0
        or snapshot_audit_result["post_move_missing"] != 0
        or snapshot_audit_result["final_mismatches"] != 0
    ):
        raise RuntimeError(
            "Depth snapshot audit failed: "
            f"{snapshot_audit_result}"
        )


    # --------------------------------------------------------
    # Safety check for the cumulative StringExporter bug
    # --------------------------------------------------------

    exporter_source = inspect.getsource(
        LucasPGNExporter.export_category
    )

    if "render_game" not in exporter_source:

        raise RuntimeError(
            "pgn_export.py is not the "
            "fixed exporter. "
            "Cumulative StringExporter "
            "bug detected."
        )


    # --------------------------------------------------------
    # Export
    # --------------------------------------------------------

    exporter = LucasPGNExporter(
        db_path,
        run_id=run_id,
        engine_label=engine_meta["label"],
        configured_time_sec=time_sec,
    )


    mistake_path = (
        output_dir /
        f"Mistakes_{slug}_{engine_meta['output_tag']}.pgn"
    )

    blunder_path = (
        output_dir /
        f"Blunders_{slug}_{engine_meta['output_tag']}.pgn"
    )


    mistakes = exporter.export_category(
        "MISTAKE",
        mistake_path,
        include_original=True,
    )

    blunders = exporter.export_category(
        "BLUNDER",
        blunder_path,
        include_original=True,
    )


    # --------------------------------------------------------
    # Final export audit
    # --------------------------------------------------------

    expected_m = expected_category(
        db_path,
        run_id,
        "MISTAKE"
    )

    expected_b = expected_category(
        db_path,
        run_id,
        "BLUNDER"
    )


    audit_m = audit_export(
        mistake_path
    )

    audit_b = audit_export(
        blunder_path
    )


    def verify(
        name,
        expected,
        actual
    ):

        if (
            actual["total_records"]
            != expected["records"]
        ):
            raise RuntimeError(
                f"{name}: total records "
                "mismatch"
            )

        if (
            actual[
                "slices_with_variation"
            ]
            != expected["slices"]
        ):
            raise RuntimeError(
                f"{name}: variation "
                "count mismatch"
            )


    verify(
        "Mistakes",
        expected_m,
        audit_m
    )

    verify(
        "Blunders",
        expected_b,
        audit_b
    )


    print()
    print("=" * 70)
    print("PRODUCTION COMPLETE")
    print("=" * 70)

    print(
        "Mistakes:",
        mistakes
    )

    print(
        "Audit   :",
        audit_m
    )

    print()

    print(
        "Blunders:",
        blunders
    )

    print(
        "Audit   :",
        audit_b
    )

    print()
    print("Database:", db_path)
    print("Run ID  :", run_id)


    return {
        "input":
            str(pgn_path),

        "archive":
            str(archived),

        "database":
            str(db_path),

        "run_id":
            run_id,

        "games":
            imported["games"],

        "moves":
            imported["moves"],

        "categories":
            audit["categories"],

        "snapshot_audit":
            snapshot_audit_result,

        "mistakes":
            mistakes,

        "blunders":
            blunders,

        "mistakes_audit":
            audit_m,

        "blunders_audit":
            audit_b,
    }
