from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

try:
    from research_integrity import InterProcessFileLock, file_sha256
except ModuleNotFoundError:  # package-style imports in tests
    from scripts.research_integrity import InterProcessFileLock, file_sha256


PRICE_CACHE_LOCK_FILENAME = ".fund-flow-exploratory-settlement-price.lock"


def price_cache_lock_path(price_dir: Path) -> Path:
    return price_dir.resolve().parent / PRICE_CACHE_LOCK_FILENAME


def price_cache_lock(price_dir: Path, *, timeout: float = 30.0) -> InterProcessFileLock:
    return InterProcessFileLock(price_cache_lock_path(price_dir), timeout=timeout)


def _manifest_digest(rows: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, sha256 in rows:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def price_cache_snapshot(price_dir: Path) -> dict[str, Any]:
    files = sorted(price_dir.glob("*.csv")) if price_dir.is_dir() else []
    rows = [(path.name, file_sha256(path)) for path in files]
    return {
        "directory_exists": price_dir.is_dir(),
        "csv_file_count": len(files),
        "aggregate_sha256": _manifest_digest(rows),
    }


def price_universe_snapshot(price_dir: Path, codes: Iterable[str]) -> dict[str, Any]:
    normalized_codes = sorted(dict.fromkeys(str(code) for code in codes))
    rows: list[tuple[str, str]] = []
    missing_codes: list[str] = []
    for code in normalized_codes:
        path = price_dir / f"{code}.csv"
        if not path.is_file():
            missing_codes.append(code)
            continue
        rows.append((path.name, file_sha256(path)))
    return {
        "expected_file_count": len(normalized_codes),
        "observed_file_count": len(rows),
        "missing_industry_codes": missing_codes,
        "aggregate_sha256": _manifest_digest(rows),
    }


__all__ = [
    "PRICE_CACHE_LOCK_FILENAME",
    "price_cache_lock",
    "price_cache_lock_path",
    "price_cache_snapshot",
    "price_universe_snapshot",
]
