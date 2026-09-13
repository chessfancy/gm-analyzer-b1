import sqlite3

from chessgrandmaster import resource_telemetry
from chessgrandmaster.production_pipeline import ensure_schema


def _write_process(proc_root, pid, ppid, name, rss_kb, cmdline=None):
    process_dir = proc_root / str(pid)
    process_dir.mkdir()
    command = cmdline if cmdline is not None else name
    (process_dir / "status").write_text(
        f"Name:\t{name}\n"
        f"PPid:\t{ppid}\n"
        f"VmRSS:\t{rss_kb} kB\n",
        encoding="utf-8",
    )
    (process_dir / "cmdline").write_bytes(command.encode() + b"\0--uci\0")


def test_parse_meminfo_converts_kibibytes_to_bytes():
    parsed = resource_telemetry.parse_meminfo(
        "MemTotal:       8192 kB\n"
        "MemAvailable:   4096 kB\n"
        "BadLine\n"
    )

    assert parsed["MemTotal"] == 8192 * 1024
    assert parsed["MemAvailable"] == 4096 * 1024


def test_parse_meminfo_omits_unparseable_units():
    parsed = resource_telemetry.parse_meminfo("MemAvailable: 4096 bananas\n")

    assert parsed.get("MemAvailable") is None


def test_parse_cgroup_value_handles_integer_max_and_invalid_text():
    assert resource_telemetry.parse_cgroup_value("12345\n") == 12345
    assert resource_telemetry.parse_cgroup_value("max\n") is None
    assert resource_telemetry.parse_cgroup_value("not-a-number\n") is None


def test_stockfish_detection_uses_executable_basename():
    assert resource_telemetry._is_stockfish(
        {
            "name": "python",
            "cmdline": "python worker.py",
            "exe": "/usr/local/bin/stockfish-17",
        }
    )


def test_collect_resource_sample_sums_synthetic_process_tree_rss(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 100, 1, "python", 100)
    _write_process(proc_root, 101, 100, "stockfish", 200)
    _write_process(proc_root, 102, 101, "stockfish", 300)
    _write_process(proc_root, 900, 1, "stockfish", 999)
    (proc_root / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 500 kB\n",
        encoding="utf-8",
    )

    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    (cgroup_root / "memory.current").write_text("700\n", encoding="utf-8")
    (cgroup_root / "memory.max").write_text("1200\n", encoding="utf-8")

    monkeypatch.setattr(resource_telemetry, "PROC_ROOT", proc_root)
    monkeypatch.setattr(resource_telemetry, "CGROUP_ROOT", cgroup_root)

    sample = resource_telemetry.collect_resource_sample(100, 2, 256)

    assert sample["cgm_process_tree_rss_bytes"] == (100 + 200 + 300) * 1024
    assert sample["stockfish_process_count"] == 2
    assert sample["stockfish_rss_bytes"] == (200 + 300) * 1024
    assert sample["cgroup_memory_current_bytes"] == 700
    assert sample["cgroup_memory_max_bytes"] == 1200
    assert sample["mem_total_bytes"] == 1000 * 1024
    assert sample["mem_available_bytes"] == 500 * 1024
    assert sample["configured_hash_total_bytes"] == 2 * 256 * 1024 * 1024


def test_collect_resource_sample_counts_four_stockfish_descendants(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 10, 1, "python", 10)
    for pid in range(11, 15):
        _write_process(proc_root, pid, 10, "stockfish", pid)
    _write_process(proc_root, 99, 1, "stockfish", 500)
    (proc_root / "meminfo").write_text("MemAvailable: 1 kB\n", encoding="utf-8")

    monkeypatch.setattr(resource_telemetry, "PROC_ROOT", proc_root)
    monkeypatch.setattr(resource_telemetry, "CGROUP_ROOT", tmp_path / "missing-cgroup")

    sample = resource_telemetry.collect_resource_sample(10, 4, 512)

    assert sample["stockfish_process_count"] == 4
    assert sample["stockfish_rss_bytes"] == sum(range(11, 15)) * 1024


def test_resource_summary_aggregates_stored_samples(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    run_id = con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)", ("{}",)
    ).lastrowid
    rows = [
        (run_id, "2026-09-13T01:00:00+00:00", 2, 4, 2, 256,
         512 * 1024 * 1024, 100, 1000, 8000, 7000, 2 * 1024**3, 5 * 1024**3, 3),
        (run_id, "2026-09-13T01:01:00+00:00", 4, 4, 2, 256,
         512 * 1024 * 1024, 300, 1200, 8000, 6000, 3 * 1024**3, 7 * 1024**3, 4),
    ]
    con.executemany(
        """
        INSERT INTO runtime_resource_samples(
            run_id, sampled_at, completed_positions, total_positions,
            configured_workers, configured_hash_mb_per_worker,
            configured_hash_total_bytes, cgroup_memory_current_bytes,
            cgroup_memory_max_bytes, mem_total_bytes, mem_available_bytes,
            cgm_process_tree_rss_bytes, stockfish_rss_bytes,
            stockfish_process_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    con.close()

    assert resource_telemetry.resource_summary(db_path, run_id) == {
        "sample_count": 2,
        "configured_hash_total_bytes": 512 * 1024 * 1024,
        "peak_stockfish_rss_bytes": 7 * 1024**3,
        "peak_cgm_process_tree_rss_bytes": 3 * 1024**3,
        "peak_cgroup_memory_current_bytes": 300,
        "cgroup_memory_max_bytes": 1200,
        "minimum_mem_available_bytes": 6000,
        "max_stockfish_process_count": 4,
    }
