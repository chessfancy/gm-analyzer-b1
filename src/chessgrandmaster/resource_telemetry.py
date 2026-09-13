"""Best-effort runtime process and memory telemetry for an analysis run."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _as_int(value):
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_meminfo(text):
    """Parse Linux ``/proc/meminfo`` text into byte-valued fields."""
    if not isinstance(text, str):
        return {}

    result = {}
    multipliers = {
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
    }

    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.strip().split()
        if not key or not parts:
            continue
        number = _as_int(parts[0])
        if number is None or number < 0:
            continue
        if len(parts) > 1:
            multiplier = multipliers.get(parts[1].lower())
            if multiplier is None:
                continue
        else:
            multiplier = 1
        result[key.strip()] = number * multiplier

    return result


def parse_cgroup_value(value):
    """Parse a cgroup memory value, treating ``max`` as unavailable."""
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() == "max":
        return None
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _parse_status(text):
    values = {}
    if not text:
        return values
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    return values


def _rss_from_status(value):
    if not value:
        return None
    parts = value.split()
    number = _as_int(parts[0]) if parts else None
    if number is None or number < 0:
        return None
    if len(parts) > 1:
        multiplier = {
            "kb": 1024,
            "mb": 1024**2,
            "gb": 1024**3,
        }.get(parts[1].lower())
        if multiplier is None:
            return None
    else:
        multiplier = 1
    return number * multiplier


def _parse_stat(text):
    """Return ``(name, ppid, rss_bytes)`` from a Linux ``/proc/PID/stat`` row."""
    if not text or "(" not in text or ")" not in text:
        return None, None, None
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    try:
        name = text[open_paren + 1 : close_paren]
        fields = text[close_paren + 1 :].split()
        ppid = _as_int(fields[1])
        rss_pages = _as_int(fields[21])
        if rss_pages is None or rss_pages < 0:
            rss_bytes = None
        else:
            try:
                page_size = os.sysconf("SC_PAGE_SIZE")
            except (AttributeError, OSError, ValueError):
                page_size = 4096
            rss_bytes = rss_pages * page_size
        return name, ppid, rss_bytes
    except (IndexError, TypeError, ValueError):
        return name if "name" in locals() else None, None, None


def _process_row(process_dir):
    pid = _as_int(process_dir.name)
    if pid is None:
        return None

    status = _parse_status(_read_text(process_dir / "status"))
    stat_name, stat_ppid, stat_rss = _parse_stat(_read_text(process_dir / "stat"))

    name = status.get("Name") or stat_name
    ppid = _as_int(status.get("PPid"))
    if ppid is None:
        ppid = stat_ppid
    rss_bytes = _rss_from_status(status.get("VmRSS"))
    if rss_bytes is None:
        rss_bytes = stat_rss

    cmdline_text = _read_text(process_dir / "cmdline")
    if cmdline_text is not None:
        cmdline = " ".join(part for part in cmdline_text.split("\x00") if part)
    else:
        cmdline = None

    if name is None and cmdline:
        name = Path(cmdline.split()[0]).name

    if ppid is None or name is None:
        return None

    try:
        executable = os.readlink(process_dir / "exe")
    except (OSError, UnicodeError):
        executable = None

    return {
        "pid": pid,
        "ppid": ppid,
        "name": name,
        "cmdline": cmdline,
        "rss_bytes": rss_bytes,
        "exe": executable,
    }


def _read_processes():
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return None

    rows = []
    for entry in entries:
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        try:
            row = _process_row(entry)
        except OSError:
            row = None
        if row is not None:
            rows.append(row)
    return rows


def _is_stockfish(row):
    name = str(row.get("name") or "")
    cmdline = str(row.get("cmdline") or "")
    executable = str(row.get("exe") or "")
    candidates = [name, executable]
    if cmdline:
        candidates.append(cmdline.split()[0])
    for candidate in candidates:
        basename = os.path.basename(candidate.strip("[]"))
        if basename.lower().startswith("stockfish"):
            return True
    return False


def _tree_rows(rows, root_pid):
    root_pid = _as_int(root_pid)
    if root_pid is None:
        return None
    by_pid = {row["pid"]: row for row in rows}
    if root_pid not in by_pid:
        return None

    children = {}
    for row in rows:
        children.setdefault(row["ppid"], []).append(row["pid"])

    result = []
    seen = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = by_pid.get(pid)
        if row is None:
            continue
        result.append(row)
        pending.extend(children.get(pid, ()))
    return result


def collect_resource_sample(root_pid, configured_workers, configured_hash_mb_per_worker):
    """Collect one best-effort runtime sample.

    Individual platform fields are nullable.  Callers may persist this row even
    when the host does not expose Linux proc or cgroup data.
    """
    configured_workers = _as_int(configured_workers)
    configured_hash_mb_per_worker = _as_int(configured_hash_mb_per_worker)
    if configured_workers is None or configured_hash_mb_per_worker is None:
        configured_hash_total_bytes = None
    else:
        configured_hash_total_bytes = (
            configured_workers * configured_hash_mb_per_worker * 1024 * 1024
        )

    meminfo_text = _read_text(PROC_ROOT / "meminfo")
    meminfo = parse_meminfo(meminfo_text)

    cgroup_current = parse_cgroup_value(_read_text(CGROUP_ROOT / "memory.current"))
    cgroup_max = parse_cgroup_value(_read_text(CGROUP_ROOT / "memory.max"))

    rows = _read_processes()
    tree_rows = _tree_rows(rows, root_pid) if rows is not None else None
    if tree_rows is None:
        tree_rss = None
        stockfish_count = None
        stockfish_rss = None
    else:
        rss_values = [row["rss_bytes"] for row in tree_rows if row["rss_bytes"] is not None]
        tree_rss = sum(rss_values) if rss_values else None
        stockfish_rows = [row for row in tree_rows if _is_stockfish(row)]
        stockfish_count = len(stockfish_rows)
        stockfish_values = [
            row["rss_bytes"] for row in stockfish_rows if row["rss_bytes"] is not None
        ]
        stockfish_rss = sum(stockfish_values) if stockfish_values else None

    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "configured_workers": configured_workers,
        "configured_hash_mb_per_worker": configured_hash_mb_per_worker,
        "configured_hash_total_bytes": configured_hash_total_bytes,
        "cgroup_memory_current_bytes": cgroup_current,
        "cgroup_memory_max_bytes": cgroup_max,
        "mem_total_bytes": meminfo.get("MemTotal"),
        "mem_available_bytes": meminfo.get("MemAvailable"),
        "cgm_process_tree_rss_bytes": tree_rss,
        "stockfish_process_count": stockfish_count,
        "stockfish_rss_bytes": stockfish_rss,
    }


def persist_resource_sample(
    db_path,
    run_id,
    sample,
    completed_positions=None,
    total_positions=None,
):
    """Persist one collected sample, returning False for unavailable storage."""
    columns = (
        "run_id, sampled_at, completed_positions, total_positions, "
        "configured_workers, configured_hash_mb_per_worker, "
        "configured_hash_total_bytes, cgroup_memory_current_bytes, "
        "cgroup_memory_max_bytes, mem_total_bytes, mem_available_bytes, "
        "cgm_process_tree_rss_bytes, stockfish_process_count, stockfish_rss_bytes"
    )
    values = (
        run_id,
        sample.get("sampled_at"),
        completed_positions,
        total_positions,
        sample.get("configured_workers"),
        sample.get("configured_hash_mb_per_worker"),
        sample.get("configured_hash_total_bytes"),
        sample.get("cgroup_memory_current_bytes"),
        sample.get("cgroup_memory_max_bytes"),
        sample.get("mem_total_bytes"),
        sample.get("mem_available_bytes"),
        sample.get("cgm_process_tree_rss_bytes"),
        sample.get("stockfish_process_count"),
        sample.get("stockfish_rss_bytes"),
    )
    con = None
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            f"INSERT INTO runtime_resource_samples ({columns}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        con.commit()
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def _empty_summary():
    return {
        "sample_count": 0,
        "configured_hash_total_bytes": None,
        "peak_stockfish_rss_bytes": None,
        "peak_cgm_process_tree_rss_bytes": None,
        "peak_cgroup_memory_current_bytes": None,
        "cgroup_memory_max_bytes": None,
        "minimum_mem_available_bytes": None,
        "max_stockfish_process_count": None,
    }


def resource_summary(db_path, run_id):
    """Aggregate persisted resource samples for one analysis run."""
    summary = _empty_summary()
    con = None
    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            """
            SELECT
                COUNT(*),
                MAX(configured_hash_total_bytes),
                MAX(stockfish_rss_bytes),
                MAX(cgm_process_tree_rss_bytes),
                MAX(cgroup_memory_current_bytes),
                MAX(cgroup_memory_max_bytes),
                MIN(mem_available_bytes),
                MAX(stockfish_process_count)
            FROM runtime_resource_samples
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    except (OSError, sqlite3.Error):
        return summary
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    if row is None:
        return summary
    summary.update(
        {
            "sample_count": row[0] or 0,
            "configured_hash_total_bytes": row[1],
            "peak_stockfish_rss_bytes": row[2],
            "peak_cgm_process_tree_rss_bytes": row[3],
            "peak_cgroup_memory_current_bytes": row[4],
            "cgroup_memory_max_bytes": row[5],
            "minimum_mem_available_bytes": row[6],
            "max_stockfish_process_count": row[7],
        }
    )
    return summary
