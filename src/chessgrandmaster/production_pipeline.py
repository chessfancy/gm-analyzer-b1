
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
from .pgn_export import LucasPGNExporter


PIPELINE_VERSION = 1


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
    """
    Resolve Stockfish in this order:

    1. Explicit argument
    2. CGM_STOCKFISH environment variable
    3. `stockfish` available in PATH
    """

    candidates = []

    if value:
        candidates.append(str(value))

    env_value = os.environ.get("CGM_STOCKFISH")

    if env_value:
        candidates.append(env_value)


    for candidate in candidates:

        path = Path(candidate).expanduser()

        if path.is_file():
            return path.resolve()

        found = shutil.which(candidate)

        if found:
            return Path(found).resolve()


    found = shutil.which("stockfish")

    if found:
        return Path(found).resolve()


    raise FileNotFoundError(
        "Stockfish binary not found. "
        "Pass engine_binary=..., set CGM_STOCKFISH, "
        "or install stockfish in PATH."
    )


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


    CREATE INDEX IF NOT EXISTS idx_moves_game
        ON moves(game_id);


    CREATE INDEX IF NOT EXISTS idx_ma_run
        ON move_analysis(run_id);


    CREATE INDEX IF NOT EXISTS idx_ma_category
        ON move_analysis(run_id, category);


    CREATE INDEX IF NOT EXISTS idx_er_analysis
        ON engine_responses(analysis_id);
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

    con = sqlite3.connect(
        db_path
    )

    rows = con.execute("""
        SELECT
            id,
            config_json

        FROM analysis_runs

        ORDER BY id DESC
    """).fetchall()


    source_count = con.execute("""
        SELECT COUNT(*)
        FROM source_files
    """).fetchone()[0]

    con.close()


    desired_scope = {
        "purpose":
            "production_tournament_full",

        "pipeline_version":
            PIPELINE_VERSION,

        "source_sha256":
            source_sha,

        "engine":
            config,
    }


    # Exact modern pipeline match.
    for run_id, config_json in rows:

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


    # Compatibility with our original
    # production run #5:
    # old scope only contained purpose.
    if source_count == 1:

        for run_id, config_json in rows:

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
                and
                scope.get("purpose")
                ==
                "production_tournament_full"
            ):
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
    time_sec=3.0,
):

    root = (
        Path(root).expanduser().resolve()
        if root is not None
        else default_workspace_root()
    )

    engine_binary = resolve_engine_binary(
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
        engine_label="Stockfish 19",
        configured_time_sec=time_sec,
    )


    mistake_path = (
        output_dir /
        f"Mistakes_{slug}_SF19.pgn"
    )

    blunder_path = (
        output_dir /
        f"Blunders_{slug}_SF19.pgn"
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

        "mistakes":
            mistakes,

        "blunders":
            blunders,

        "mistakes_audit":
            audit_m,

        "blunders_audit":
            audit_b,
    }
