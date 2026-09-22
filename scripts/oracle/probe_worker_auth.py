#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

HOME = Path.home()
KAGGLE_TOKEN = HOME / ".kaggle" / "access_token"
DEEPNOTE_TOKEN = HOME / ".config" / "cgm" / "secrets" / "deepnote_api_key"
KAGGLE = HOME / ".local" / "bin" / "kaggle"


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def probe_kaggle() -> dict[str, object]:
    if not KAGGLE_TOKEN.is_file():
        return {"ok": False, "error": "missing ~/.kaggle/access_token"}
    if _mode(KAGGLE_TOKEN) != "0o600":
        return {"ok": False, "error": f"unsafe mode {_mode(KAGGLE_TOKEN)}"}
    proc = subprocess.run(
        [str(KAGGLE), "kernels", "list", "--mine", "--page-size", "1"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip()[-1000:],
        "stderr": proc.stderr.strip()[-1000:],
    }


def _deepnote_get(path: str) -> object:
    token = DEEPNOTE_TOKEN.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        "https://api.deepnote.com/v2" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ChessGrandmaster coordinator/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def probe_deepnote() -> dict[str, object]:
    if not DEEPNOTE_TOKEN.is_file():
        return {"ok": False, "error": "missing Deepnote key file"}
    if _mode(DEEPNOTE_TOKEN) != "0o600":
        return {"ok": False, "error": f"unsafe mode {_mode(DEEPNOTE_TOKEN)}"}
    try:
        me = _deepnote_get("/me")
        projects = _deepnote_get("/projects?pageSize=20")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        return {"ok": False, "status": exc.code, "error": body}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    workspace = me.get("workspace") if isinstance(me, dict) else None
    access = me.get("accessLevel") if isinstance(me, dict) else None
    project_rows = projects.get("projects", []) if isinstance(projects, dict) else []
    public_projects = []
    for item in project_rows:
        if not isinstance(item, dict):
            continue
        public_projects.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "projectType": item.get("projectType"),
        })
    return {
        "ok": True,
        "accessLevel": access,
        "workspace": workspace,
        "projects": public_projects,
    }


def main() -> int:
    result = {
        "kaggle": probe_kaggle(),
        "deepnote": probe_deepnote(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(value.get("ok") for value in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
