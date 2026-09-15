#!/usr/bin/env python3
"""Bootstrap the offline Vietnamese-player discovery artifacts from one FIDE list."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import unicodedata
from zipfile import ZipFile


DEFAULT_SOURCE_URL = "https://ratings.fide.com/download/standard_rating_list.zip"
DEFAULT_SOURCE_PAGE_URL = "https://ratings.fide.com/download_lists.phtml"

# These are intentionally small, explicit discovery hints rather than a linguistic model.
CURATED_SURNAMES = (
    "bui",
    "dang",
    "dinh",
    "do",
    "duong",
    "ho",
    "hoang",
    "huynh",
    "le",
    "ly",
    "mai",
    "ngo",
    "nguyen",
    "phan",
    "pham",
    "tran",
    "vo",
    "vu",
)
CURATED_NAME_TOKENS = (
    "an",
    "anh",
    "bach",
    "bao",
    "binh",
    "chau",
    "chi",
    "cong",
    "cuong",
    "dai",
    "dao",
    "dat",
    "duc",
    "dung",
    "duy",
    "gia",
    "giang",
    "ha",
    "hai",
    "han",
    "hang",
    "hieu",
    "hoa",
    "hoai",
    "hoang",
    "hong",
    "hung",
    "huong",
    "huu",
    "huy",
    "huyen",
    "khanh",
    "khang",
    "khoa",
    "khoi",
    "kien",
    "kiet",
    "kim",
    "lam",
    "linh",
    "loan",
    "long",
    "luong",
    "manh",
    "mai",
    "minh",
    "my",
    "nam",
    "ngan",
    "nghi",
    "nghia",
    "ngoc",
    "nhat",
    "nhi",
    "nhu",
    "phat",
    "phong",
    "phu",
    "phuc",
    "phuoc",
    "phuong",
    "quan",
    "quang",
    "quoc",
    "quynh",
    "son",
    "tai",
    "tam",
    "tan",
    "thai",
    "thang",
    "thanh",
    "thao",
    "the",
    "thi",
    "thien",
    "thinh",
    "thu",
    "thuy",
    "tien",
    "trang",
    "tri",
    "trinh",
    "trong",
    "trung",
    "truong",
    "tu",
    "tuan",
    "tung",
    "tuong",
    "uyen",
    "van",
    "viet",
    "vinh",
    "vuong",
    "vy",
    "xuan",
    "yen",
)
AMBIGUOUS_TOKENS = (
    "anh",
    "ho",
    "hong",
    "le",
    "ly",
    "mai",
    "nam",
    "son",
    "van",
)


def normalize_name_tokens(name: str) -> tuple[str, ...]:
    """Normalize a FIDE name using the same rules as the runtime filter."""
    if not isinstance(name, str):
        raise TypeError("player name must be a string")
    text = name.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = "".join(char if char.isalnum() else " " for char in text)
    return tuple(text.split())


def _source_lines(source: Path) -> list[str]:
    if source.suffix.casefold() == ".zip":
        with ZipFile(source) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.casefold().endswith("standard_rating_list.txt")
            ]
            if len(candidates) != 1:
                raise ValueError(
                    "expected exactly one standard_rating_list.txt in the archive"
                )
            raw = archive.read(candidates[0])
    else:
        raw = source.read_bytes()
    return raw.decode("utf-8-sig").splitlines()


def parse_rating_list(source: Path) -> list[tuple[int, str, str]]:
    """Parse the official fixed-width FIDE TXT list into ID/name/FED rows."""
    lines = _source_lines(Path(source))
    if not lines:
        raise ValueError("rating list is empty")
    header = lines[0]
    try:
        name_start = header.index("Name")
        fed_start = header.index("Fed")
    except ValueError as exc:
        raise ValueError("rating list header lacks ID Number, Name or Fed") from exc
    if not header.startswith("ID Number") or fed_start <= name_start:
        raise ValueError("unsupported FIDE rating list layout")

    rows: list[tuple[int, str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        raw_id = line[:name_start].strip()
        if not raw_id.isdigit():
            continue
        name = line[name_start:fed_start].strip()
        fed = line[fed_start : fed_start + 4].strip()
        rows.append((int(raw_id), name, fed))
    if not rows:
        raise ValueError("rating list contains no player rows")
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_artifacts(
    source: Path,
    output_dir: Path,
    *,
    source_url: str,
    source_page_url: str,
    list_period: str,
    snapshot_date: str,
    generation_date: str,
) -> dict[str, object]:
    rows = parse_rating_list(source)
    vie_rows = [row for row in rows if row[2] == "VIE"]
    vie_ids = sorted({row[0] for row in vie_rows})
    frequencies = Counter(
        token
        for _, name, _ in vie_rows
        for token in set(normalize_name_tokens(name))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "vie_fide_ids.txt").write_text(
        "".join(f"{fide_id}\n" for fide_id in vie_ids),
        encoding="utf-8",
    )
    (output_dir / "vie_name_tokens.json").write_text(
        json.dumps(
            {
                "surnames": sorted(set(CURATED_SURNAMES)),
                "name_tokens": sorted(set(CURATED_NAME_TOKENS)),
                "ambiguous_tokens": sorted(set(AMBIGUOUS_TOKENS)),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    metadata = {
        "dictionary_version": "vie-name-filter-v1",
        "source_url": source_url,
        "source_page_url": source_page_url,
        "list_period": list_period,
        "snapshot_date": snapshot_date,
        "download_sha256": _sha256(source),
        "download_bytes": source.stat().st_size,
        "generated_at": generation_date,
        "total_players_parsed": len(rows),
        "fed_vie_players": len(vie_rows),
        "static_vie_fide_ids": len(vie_ids),
        "surname_token_count": len(set(CURATED_SURNAMES)),
        "general_name_token_count": len(set(CURATED_NAME_TOKENS)),
        "ambiguous_token_count": len(set(AMBIGUOUS_TOKENS)),
        "token_frequencies": dict(sorted(frequencies.items())),
    }
    (output_dir / "bootstrap_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**metadata, "vie_fide_ids": vie_ids}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap offline VIE FIDE IDs and name-hint artifacts."
    )
    parser.add_argument("source", type=Path, help="official FIDE TXT or ZIP snapshot")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write static artifacts to this directory",
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--source-page-url", default=DEFAULT_SOURCE_PAGE_URL)
    parser.add_argument("--list-period", required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--generation-date", default=date.today().isoformat())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    metadata = build_artifacts(
        args.source,
        args.output_dir or Path("."),
        source_url=args.source_url,
        source_page_url=args.source_page_url,
        list_period=args.list_period,
        snapshot_date=args.snapshot_date,
        generation_date=args.generation_date,
    )
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
