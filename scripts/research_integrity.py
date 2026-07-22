#!/usr/bin/env python
"""Shared integrity primitives for research calendars, files, and ledgers.

The module deliberately has no dependency on the versioned V5 scripts.  It is
safe to integrate incrementally: callers must opt in to every write operation.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import hmac
import io
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO


GENESIS_HASH = "GENESIS"


class TradingCalendarError(ValueError):
    """Raised when an A-share calendar is empty, malformed, or too short."""


class LockTimeoutError(TimeoutError):
    """Raised when another process keeps a file lock beyond the deadline."""


class HashChainError(ValueError):
    """Raised when an append-only ledger cannot be verified."""

    def __init__(self, message: str, *, line_number: int | None = None) -> None:
        self.line_number = line_number
        suffix = f"; line={line_number}" if line_number is not None else ""
        super().__init__(message + suffix)


class DuplicateRecordError(HashChainError):
    """Raised when an append violates an explicitly requested unique key."""


class HeadMismatchError(HashChainError):
    """Raised when a caller's expected ledger head is no longer current."""


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise TradingCalendarError("empty trading date")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError as exc:
        raise TradingCalendarError(f"invalid trading date: {value!r}") from exc


class AShareTradingCalendar:
    """Strict exchange-session calendar with no weekday-only fallback."""

    def __init__(self, dates: Iterable[Any], *, source: str = "provided") -> None:
        normalized = tuple(sorted({_as_date(value) for value in dates}))
        if not normalized:
            raise TradingCalendarError("empty A-share trading calendar")
        self._dates = normalized
        self._index = {value: index for index, value in enumerate(normalized)}
        self.source = source

    @classmethod
    def from_csv(
        cls,
        path: str | os.PathLike[str],
        *,
        date_column: str = "trade_date",
    ) -> "AShareTradingCalendar":
        source = Path(path)
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or date_column not in reader.fieldnames:
                raise TradingCalendarError(
                    f"calendar CSV missing column {date_column!r}: {source}"
                )
            values = [row.get(date_column, "") for row in reader]
        return cls(values, source=f"csv:{source}")

    @property
    def dates(self) -> tuple[date, ...]:
        return self._dates

    @property
    def first_date(self) -> date:
        return self._dates[0]

    @property
    def last_date(self) -> date:
        return self._dates[-1]

    def __len__(self) -> int:
        return len(self._dates)

    def __contains__(self, value: object) -> bool:
        try:
            parsed = _as_date(value)
        except (TradingCalendarError, TypeError):
            return False
        return parsed in self._index

    def is_trading_day(self, value: Any) -> bool:
        return value in self

    def require_coverage(self, through: Any) -> None:
        required = _as_date(through)
        if self.last_date < required:
            raise TradingCalendarError(
                f"calendar ends at {self.last_date.isoformat()}, required through "
                f"{required.isoformat()}"
            )

    def roll_forward(self, value: Any, *, include_current: bool = True) -> date:
        day = _as_date(value)
        position = bisect.bisect_left(self._dates, day)
        if not include_current and position < len(self._dates) and self._dates[position] == day:
            position += 1
        if position >= len(self._dates):
            raise TradingCalendarError(f"no trading day on or after {day.isoformat()}")
        return self._dates[position]

    def roll_backward(self, value: Any, *, include_current: bool = True) -> date:
        day = _as_date(value)
        position = bisect.bisect_right(self._dates, day) - 1
        if not include_current and position >= 0 and self._dates[position] == day:
            position -= 1
        if position < 0:
            raise TradingCalendarError(f"no trading day on or before {day.isoformat()}")
        return self._dates[position]

    def next_trading_day(self, value: Any, sessions: int = 1) -> date:
        if sessions < 1:
            raise ValueError("sessions must be at least 1")
        day = _as_date(value)
        position = bisect.bisect_right(self._dates, day) + sessions - 1
        if position >= len(self._dates):
            raise TradingCalendarError(f"calendar has no session {sessions} day(s) after {day}")
        return self._dates[position]

    def previous_trading_day(self, value: Any, sessions: int = 1) -> date:
        if sessions < 1:
            raise ValueError("sessions must be at least 1")
        day = _as_date(value)
        position = bisect.bisect_left(self._dates, day) - sessions
        if position < 0:
            raise TradingCalendarError(f"calendar has no session {sessions} day(s) before {day}")
        return self._dates[position]

    def shift(self, value: Any, sessions: int) -> date:
        day = _as_date(value)
        if day not in self._index:
            raise TradingCalendarError(f"shift origin is not a trading day: {day}")
        position = self._index[day] + sessions
        if position < 0 or position >= len(self._dates):
            raise TradingCalendarError(f"shift leaves calendar coverage: {day}, sessions={sessions}")
        return self._dates[position]

    def holding_exit(self, entry_date: Any, holding_sessions: int) -> date:
        """Return the exit session after an exact number of held sessions."""

        if holding_sessions < 1:
            raise ValueError("holding_sessions must be at least 1")
        return self.shift(entry_date, holding_sessions)

    def trading_days(
        self,
        start: Any,
        end: Any,
        *,
        inclusive: str = "both",
    ) -> tuple[date, ...]:
        first, last = _as_date(start), _as_date(end)
        if first > last:
            raise ValueError("start must not be after end")
        if inclusive not in {"both", "left", "right", "neither"}:
            raise ValueError("inclusive must be both, left, right, or neither")
        left = bisect.bisect_left(self._dates, first)
        right = bisect.bisect_right(self._dates, last)
        if inclusive in {"right", "neither"} and left < len(self._dates) and self._dates[left] == first:
            left += 1
        if inclusive in {"left", "neither"} and right > 0 and self._dates[right - 1] == last:
            right -= 1
        return self._dates[left:right]


def _akshare_trade_dates() -> Iterable[Any]:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    if "trade_date" not in frame:
        raise TradingCalendarError("AkShare trade calendar has no trade_date column")
    return frame["trade_date"].tolist()


def load_a_share_trading_calendar(
    cache_path: str | os.PathLike[str] | None = None,
    *,
    required_through: Any | None = None,
    fetcher: Callable[[], Iterable[Any]] | None = None,
) -> AShareTradingCalendar:
    """Load a cached calendar, fetching from AkShare only when cache is absent/short.

    A stale cache never silently satisfies ``required_through``.  Tests and
    offline callers can inject a deterministic ``fetcher``.
    """

    cache = Path(cache_path) if cache_path is not None else None
    cached: AShareTradingCalendar | None = None
    if cache is not None and cache.exists():
        cached = AShareTradingCalendar.from_csv(cache)
        if required_through is None or cached.last_date >= _as_date(required_through):
            return cached

    provider = fetcher or _akshare_trade_dates
    try:
        calendar = AShareTradingCalendar(provider(), source="injected" if fetcher else "akshare")
    except Exception as exc:
        requirement = "" if required_through is None else f" through {_as_date(required_through)}"
        raise TradingCalendarError(f"unable to refresh A-share trading calendar{requirement}") from exc
    if required_through is not None:
        calendar.require_coverage(required_through)
    if cache is not None:
        atomic_write_csv(cache, ({"trade_date": item.isoformat()} for item in calendar.dates), fieldnames=["trade_date"])
    return calendar


def _calendar_from(calendar_or_dates: AShareTradingCalendar | Iterable[Any]) -> AShareTradingCalendar:
    if isinstance(calendar_or_dates, AShareTradingCalendar):
        return calendar_or_dates
    return AShareTradingCalendar(calendar_or_dates)


def next_trading_day(
    calendar_or_dates: AShareTradingCalendar | Iterable[Any],
    value: Any,
    sessions: int = 1,
) -> date:
    """Functional wrapper for integration into versioned scripts."""

    return _calendar_from(calendar_or_dates).next_trading_day(value, sessions)


def holding_exit(
    calendar_or_dates: AShareTradingCalendar | Iterable[Any],
    entry_date: Any,
    holding_sessions: int,
) -> date:
    """Calculate an exit from explicit exchange sessions, never weekdays."""

    return _calendar_from(calendar_or_dates).holding_exit(entry_date, holding_sessions)


def lock_path_for(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    return target.with_name(target.name + ".lock")


class InterProcessFileLock:
    """Portable advisory lock released automatically when a process exits."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "InterProcessFileLock":
        if self._handle is not None:
            raise RuntimeError("lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                _lock_handle(handle)
                self._handle = handle
                return self
            except (BlockingIOError, OSError) as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    handle.close()
                    raise LockTimeoutError(f"timed out acquiring lock: {self.path}") from exc
                time.sleep(self.poll_interval)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock_handle(handle)
        finally:
            handle.close()

    def __enter__(self) -> "InterProcessFileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _lock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _json_ready(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {path}")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite decimal at {path}")
        return str(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path} must be str, got {type(key).__name__}")
            result[key] = _json_ready(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_ready(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"unsupported canonical JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _json_ready(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical CSV does not allow non-finite floats")
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical CSV does not allow non-finite decimals")
        return str(value)
    if isinstance(value, (Mapping, list, tuple)):
        return canonical_json_bytes(value).decode("utf-8")
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def canonical_csv_bytes(
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
    sort_rows_by: Sequence[str] | None = None,
) -> bytes:
    materialized = [dict(row) for row in rows]
    invalid_keys = sorted(
        {repr(key) for row in materialized for key in row if not isinstance(key, str)}
    )
    if invalid_keys:
        raise TypeError(f"CSV field names must be strings: {invalid_keys}")
    fields = list(fieldnames) if fieldnames is not None else sorted({key for row in materialized for key in row})
    if len(fields) != len(set(fields)):
        raise ValueError("fieldnames must be unique")
    unknown = sorted({key for row in materialized for key in row if key not in fields})
    if unknown:
        raise ValueError(f"CSV rows contain fields outside schema: {unknown}")
    normalized = [{field: _canonical_csv_value(row.get(field)) for field in fields} for row in materialized]
    if sort_rows_by is not None:
        missing = [field for field in sort_rows_by if field not in fields]
        if missing:
            raise ValueError(f"CSV sort fields are not in schema: {missing}")
        normalized.sort(key=lambda row: tuple(row[field] for field in sort_rows_by))
    if not fields:
        return b""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(normalized)
    return buffer.getvalue().encode("utf-8")


def _fingerprint(data: bytes, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    digest.update(data)
    return digest.hexdigest()


def json_fingerprint(value: Any, *, algorithm: str = "sha256") -> str:
    return _fingerprint(canonical_json_bytes(value), algorithm)


def csv_fingerprint(
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
    sort_rows_by: Sequence[str] | None = None,
    algorithm: str = "sha256",
) -> str:
    return _fingerprint(
        canonical_csv_bytes(rows, fieldnames=fieldnames, sort_rows_by=sort_rows_by),
        algorithm,
    )


def fingerprint_json_file(path: str | os.PathLike[str], *, algorithm: str = "sha256") -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return json_fingerprint(payload, algorithm=algorithm)


def fingerprint_csv_file(
    path: str | os.PathLike[str],
    *,
    fieldnames: Sequence[str] | None = None,
    sort_rows_by: Sequence[str] | None = None,
    algorithm: str = "sha256",
) -> str:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        schema = (
            list(fieldnames)
            if fieldnames is not None
            else sorted(reader.fieldnames or [])
        )
    return csv_fingerprint(rows, fieldnames=schema, sort_rows_by=sort_rows_by, algorithm=algorithm)


def file_sha256(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    mode: int | None = None,
) -> None:
    """Write in the destination directory, fsync, then atomically replace."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    inherited_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        final_mode = mode if mode is not None else inherited_mode
        if final_mode is not None:
            os.chmod(temporary, final_mode)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(
    path: str | os.PathLike[str],
    payload: Any,
    *,
    indent: int | None = 2,
    mode: int | None = None,
) -> None:
    normalized = _json_ready(payload)
    if indent is None:
        data = canonical_json_bytes(normalized) + b"\n"
    else:
        data = (
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                indent=indent,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    atomic_write_bytes(path, data, mode=mode)


def atomic_write_csv(
    path: str | os.PathLike[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
    sort_rows_by: Sequence[str] | None = None,
    utf8_bom: bool = True,
    mode: int | None = None,
) -> None:
    data = canonical_csv_bytes(rows, fieldnames=fieldnames, sort_rows_by=sort_rows_by)
    if utf8_bom:
        data = b"\xef\xbb\xbf" + data
    atomic_write_bytes(path, data, mode=mode)


def hash_chain_record(
    record: Mapping[str, Any],
    *,
    hash_field: str = "record_hash",
) -> str:
    payload = {key: value for key, value in record.items() if key != hash_field}
    return json_fingerprint(payload)


def canonical_row_hash(
    row: Mapping[str, Any],
    *,
    hash_field: str = "record_hash",
) -> str:
    """Canonical SHA-256 for a row, excluding its stored hash field."""

    return hash_chain_record(row, hash_field=hash_field)


@dataclass(frozen=True)
class HashChainVerification:
    records: tuple[dict[str, Any], ...]
    head_hash: str

    @property
    def record_count(self) -> int:
        return len(self.records)


def _read_hash_chain_source(
    source: str | os.PathLike[str] | Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(source, (str, os.PathLike)):
        return [dict(row) for row in source], 1
    path = Path(source)
    if not path.exists():
        return [], 1
    if path.suffix.lower() == ".csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise HashChainError("CSV ledger has no header")
                return [dict(row) for row in reader], 2
        except UnicodeDecodeError as exc:
            raise HashChainError("ledger is not valid UTF-8") from exc
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise HashChainError("ledger is not valid UTF-8") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise HashChainError("blank line inside ledger", line_number=line_number)
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HashChainError("invalid JSONL record", line_number=line_number) from exc
        if not isinstance(value, dict):
            raise HashChainError("ledger record must be a JSON object", line_number=line_number)
        records.append(value)
    return records, 1


def verify_hash_chain(
    source: str | os.PathLike[str] | Iterable[Mapping[str, Any]],
    *,
    expected_head: str | None = None,
    genesis: str = GENESIS_HASH,
    previous_hash_field: str = "previous_hash",
    hash_field: str = "record_hash",
) -> HashChainVerification:
    records, first_line = _read_hash_chain_source(source)
    verified_records: list[dict[str, Any]] = []
    previous = genesis
    for offset, value in enumerate(records):
        line_number = first_line + offset
        actual_previous = value.get(previous_hash_field)
        if actual_previous != previous:
            raise HashChainError("previous hash mismatch", line_number=line_number)
        stored = value.get(hash_field)
        computed = hash_chain_record(value, hash_field=hash_field)
        if not isinstance(stored, str) or not hmac.compare_digest(stored, computed):
            raise HashChainError("record hash mismatch", line_number=line_number)
        verified_records.append(value)
        previous = stored
    if expected_head is not None and not hmac.compare_digest(previous, expected_head):
        raise HeadMismatchError(
            f"ledger head mismatch: expected={expected_head}, actual={previous}"
        )
    return HashChainVerification(tuple(verified_records), previous)


def append_hash_chain_record(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    unique_fields: Sequence[str] = (),
    expected_head: str | None = None,
    lock_timeout: float | None = 10.0,
    genesis: str = GENESIS_HASH,
    previous_hash_field: str = "previous_hash",
    hash_field: str = "record_hash",
) -> dict[str, Any]:
    """Verify, append one record under a process lock, and atomically replace."""

    if previous_hash_field in payload or hash_field in payload:
        raise ValueError(f"payload must not provide {previous_hash_field!r} or {hash_field!r}")
    normalized_payload = dict(_json_ready(dict(payload)))
    missing_unique = [field for field in unique_fields if field not in normalized_payload]
    if missing_unique:
        raise ValueError(f"payload is missing unique fields: {missing_unique}")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with InterProcessFileLock(lock_path_for(target), timeout=lock_timeout):
        verified = verify_hash_chain(
            target,
            expected_head=expected_head,
            genesis=genesis,
            previous_hash_field=previous_hash_field,
            hash_field=hash_field,
        )
        if unique_fields:
            key = tuple(normalized_payload[field] for field in unique_fields)
            for existing in verified.records:
                if tuple(existing.get(field) for field in unique_fields) == key:
                    raise DuplicateRecordError(
                        f"duplicate ledger key {tuple(unique_fields)}={key}"
                    )
        record = normalized_payload
        record[previous_hash_field] = verified.head_hash
        record[hash_field] = hash_chain_record(record, hash_field=hash_field)
        existing_bytes = target.read_bytes() if target.exists() else b""
        separator = b"" if not existing_bytes or existing_bytes.endswith(b"\n") else b"\n"
        line = canonical_json_bytes(record) + b"\n"
        atomic_write_bytes(target, existing_bytes + separator + line)
        return record


def append_hash_chained_csv(
    path: str | os.PathLike[str],
    rows: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
    *,
    id_field: str = "event_id",
    expected_head: str | None = None,
    lock_timeout: float | None = 10.0,
    genesis: str = GENESIS_HASH,
    previous_hash_field: str = "previous_hash",
    hash_field: str = "record_hash",
    utf8_bom: bool = True,
) -> list[dict[str, str]]:
    """Append CSV rows as one locked, validated, atomic transaction.

    Existing bytes remain an exact prefix of the replacement.  The function
    rejects duplicate IDs both in the old ledger and in the incoming batch.
    """

    schema = list(fieldnames)
    if not schema or any(not isinstance(field, str) or not field for field in schema):
        raise ValueError("fieldnames must contain non-empty strings")
    if len(schema) != len(set(schema)):
        raise ValueError("fieldnames must be unique")
    if id_field not in schema:
        raise ValueError(f"id field is not in schema: {id_field}")
    for field in (previous_hash_field, hash_field):
        if field not in schema:
            schema.append(field)
    if isinstance(rows, Mapping):
        incoming = [dict(rows)]
    else:
        incoming = [dict(row) for row in rows]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with InterProcessFileLock(lock_path_for(target), timeout=lock_timeout):
        existing_records: list[dict[str, Any]] = []
        existing_bytes = b""
        if target.exists():
            existing_bytes = target.read_bytes()
            with target.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != schema:
                    raise HashChainError(
                        f"CSV ledger schema mismatch: expected={schema}, actual={reader.fieldnames}"
                    )
                existing_records = [dict(row) for row in reader]
        verified = verify_hash_chain(
            existing_records,
            expected_head=expected_head,
            genesis=genesis,
            previous_hash_field=previous_hash_field,
            hash_field=hash_field,
        )
        seen_ids: set[str] = set()
        for existing in existing_records:
            identifier = str(existing.get(id_field, ""))
            if not identifier:
                raise HashChainError(f"existing CSV ledger has blank {id_field}")
            if identifier in seen_ids:
                raise DuplicateRecordError(f"existing CSV ledger has duplicate {id_field}={identifier}")
            seen_ids.add(identifier)
        appended: list[dict[str, str]] = []
        previous = verified.head_hash
        allowed_payload_fields = set(schema) - {previous_hash_field, hash_field}
        for input_row in incoming:
            if previous_hash_field in input_row or hash_field in input_row:
                raise ValueError(
                    f"incoming CSV row must not provide {previous_hash_field!r} or {hash_field!r}"
                )
            unknown = sorted(set(input_row) - allowed_payload_fields)
            if unknown:
                raise ValueError(f"incoming CSV row contains fields outside schema: {unknown}")
            record = {
                field: _canonical_csv_value(input_row.get(field))
                for field in schema
                if field not in {previous_hash_field, hash_field}
            }
            identifier = record[id_field]
            if not identifier:
                raise ValueError(f"incoming CSV row has blank {id_field}")
            if identifier in seen_ids:
                raise DuplicateRecordError(f"duplicate CSV ledger {id_field}={identifier}")
            seen_ids.add(identifier)
            record[previous_hash_field] = previous
            record[hash_field] = canonical_row_hash(record, hash_field=hash_field)
            previous = record[hash_field]
            appended.append(record)
        if not appended:
            return []
        if existing_bytes:
            serialized = canonical_csv_bytes(appended, fieldnames=schema)
            _, separator, body = serialized.partition(b"\n")
            if not separator:
                raise AssertionError("canonical CSV append has no header separator")
            joiner = b"" if existing_bytes.endswith(b"\n") else b"\n"
            replacement = existing_bytes + joiner + body
        else:
            replacement = canonical_csv_bytes(appended, fieldnames=schema)
            if utf8_bom:
                replacement = b"\xef\xbb\xbf" + replacement
        atomic_write_bytes(target, replacement)
        return appended


class HashChainLedger:
    """Small object-oriented facade over the JSONL hash-chain functions."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        genesis: str = GENESIS_HASH,
        previous_hash_field: str = "previous_hash",
        hash_field: str = "record_hash",
        lock_timeout: float | None = 10.0,
    ) -> None:
        self.path = Path(path)
        self.genesis = genesis
        self.previous_hash_field = previous_hash_field
        self.hash_field = hash_field
        self.lock_timeout = lock_timeout

    def verify(self, *, expected_head: str | None = None) -> HashChainVerification:
        return verify_hash_chain(
            self.path,
            expected_head=expected_head,
            genesis=self.genesis,
            previous_hash_field=self.previous_hash_field,
            hash_field=self.hash_field,
        )

    def append(
        self,
        payload: Mapping[str, Any],
        *,
        unique_fields: Sequence[str] = (),
        expected_head: str | None = None,
    ) -> dict[str, Any]:
        return append_hash_chain_record(
            self.path,
            payload,
            unique_fields=unique_fields,
            expected_head=expected_head,
            lock_timeout=self.lock_timeout,
            genesis=self.genesis,
            previous_hash_field=self.previous_hash_field,
            hash_field=self.hash_field,
        )


__all__ = [
    "AShareTradingCalendar",
    "DuplicateRecordError",
    "GENESIS_HASH",
    "HashChainError",
    "HashChainLedger",
    "HashChainVerification",
    "HeadMismatchError",
    "InterProcessFileLock",
    "LockTimeoutError",
    "TradingCalendarError",
    "append_hash_chain_record",
    "append_hash_chained_csv",
    "atomic_write_bytes",
    "atomic_write_csv",
    "atomic_write_json",
    "atomic_write_text",
    "canonical_csv_bytes",
    "canonical_json_bytes",
    "canonical_row_hash",
    "csv_fingerprint",
    "file_sha256",
    "fingerprint_csv_file",
    "fingerprint_json_file",
    "hash_chain_record",
    "holding_exit",
    "json_fingerprint",
    "load_a_share_trading_calendar",
    "lock_path_for",
    "next_trading_day",
    "verify_hash_chain",
]
