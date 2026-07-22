#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "markdown_link_audit"
VERSION = "markdown-link-audit-1.0"

EXTERNAL_SCHEMES = {
    "app",
    "ftp",
    "ftps",
    "http",
    "https",
    "mailto",
    "news",
    "slack",
    "tel",
}
INLINE_LINK_START = re.compile(r"!?\[[^\]\n]*\]\(")
REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|([^\s]+))"
)
INLINE_CODE = re.compile(r"(`+)(.*?)\1")
FENCE_START = re.compile(r"^[ \t]*(`{3,}|~{3,})")
WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")

CSV_FIELDS = [
    "source_file",
    "line",
    "link_target",
    "link_kind",
    "normalized_target",
    "target_exists",
    "git_tracked",
    "git_ignored",
    "absolute_local_path",
    "portable",
    "status",
    "issue_code",
    "detail",
]


@dataclass(frozen=True)
class DiscoveredLink:
    source_file: str
    line: int
    target: str


@dataclass(frozen=True)
class LinkRecord:
    source_file: str
    line: int
    link_target: str
    link_kind: str
    normalized_target: str
    target_exists: bool
    git_tracked: bool
    git_ignored: bool
    absolute_local_path: bool
    portable: bool
    status: str
    issue_code: str
    detail: str


@dataclass(frozen=True)
class AuditResult:
    root: Path
    tracked_markdown_files: tuple[str, ...]
    records: tuple[LinkRecord, ...]
    git_inventory_ok: bool

    @property
    def audit_passed(self) -> bool:
        return self.git_inventory_ok and all(row.status != "fail" for row in self.records)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed audit of local links in Git-tracked Markdown files."
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="Git repository root.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory; defaults to <root>/outputs/audit/markdown_link_audit.",
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    root = args.root.resolve()
    output = args.output.resolve() if args.output else root / "outputs" / "audit" / "markdown_link_audit"
    result = audit_repository(root)
    summary = write_outputs(result, output)
    print(f"tracked_markdown_count={summary['tracked_markdown_count']}")
    print(f"discovered_link_count={summary['discovered_link_count']}")
    print(f"failure_count={summary['failure_count']}")
    print(f"audit_passed={str(summary['audit_passed']).lower()}")
    return 0 if result.audit_passed else 1


def audit_repository(root: Path) -> AuditResult:
    root = root.resolve()
    try:
        tracked_paths = git_tracked_paths(root)
    except (OSError, RuntimeError) as exc:
        return AuditResult(
            root=root,
            tracked_markdown_files=(),
            records=(infrastructure_failure("git_inventory_failed", str(exc)),),
            git_inventory_ok=False,
        )

    markdown_paths = tuple(
        path for path in sorted(tracked_paths) if path.lower().endswith((".md", ".markdown"))
    )
    records: list[LinkRecord] = []
    inventory_ok = True

    for source_rel in markdown_paths:
        source_path = root / Path(PurePosixPath(source_rel))
        if not source_path.is_file():
            records.append(
                LinkRecord(
                    source_file=source_rel,
                    line=0,
                    link_target="",
                    link_kind="audit_error",
                    normalized_target=source_rel,
                    target_exists=False,
                    git_tracked=True,
                    git_ignored=False,
                    absolute_local_path=False,
                    portable=False,
                    status="fail",
                    issue_code="tracked_markdown_missing",
                    detail="Git-tracked Markdown source is absent from the worktree.",
                )
            )
            continue
        try:
            text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            records.append(
                LinkRecord(
                    source_file=source_rel,
                    line=0,
                    link_target="",
                    link_kind="audit_error",
                    normalized_target=source_rel,
                    target_exists=True,
                    git_tracked=True,
                    git_ignored=False,
                    absolute_local_path=False,
                    portable=False,
                    status="fail",
                    issue_code="markdown_read_failed",
                    detail=f"Unable to read tracked Markdown as UTF-8: {exc}",
                )
            )
            continue

        for link in discover_links(text, source_rel):
            records.append(evaluate_link(link, root, tracked_paths))

    candidate_paths = {
        row.normalized_target
        for row in records
        if row.link_kind == "relative_local" and row.normalized_target and not row.git_tracked
    }
    try:
        ignored = git_ignored_paths(root, candidate_paths)
    except (OSError, RuntimeError) as exc:
        inventory_ok = False
        records.append(infrastructure_failure("git_ignore_check_failed", str(exc)))
        ignored = set()

    if ignored:
        updated: list[LinkRecord] = []
        for row in records:
            if row.normalized_target in ignored and not row.git_tracked:
                updated.append(
                    replace(
                        row,
                        git_ignored=True,
                        portable=False,
                        status="fail",
                        issue_code="ignored_untracked_target",
                        detail=(
                            "Target is ignored and not Git-tracked; local existence cannot prove "
                            "portable link validity."
                        ),
                    )
                )
            else:
                updated.append(row)
        records = updated

    return AuditResult(
        root=root,
        tracked_markdown_files=markdown_paths,
        records=tuple(records),
        git_inventory_ok=inventory_ok,
    )


def git_tracked_paths(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed ({completed.returncode}): {stderr}")
    return {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in completed.stdout.split(b"\0")
        if item
    }


def git_ignored_paths(root: Path, paths: Iterable[str]) -> set[str]:
    normalized = sorted({path.replace("\\", "/") for path in paths if path})
    if not normalized:
        return set()
    payload = b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in normalized) + b"\0"
    completed = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git check-ignore failed ({completed.returncode}): {stderr}")
    return {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in completed.stdout.split(b"\0")
        if item
    }


def discover_links(text: str, source_file: str) -> list[DiscoveredLink]:
    links: list[DiscoveredLink] = []
    fence_character = ""
    fence_length = 0

    for line_number, original_line in enumerate(text.splitlines(), start=1):
        fence = FENCE_START.match(original_line)
        if fence:
            marker = fence.group(1)
            if not fence_character:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = ""
                fence_length = 0
            continue
        if fence_character:
            continue

        line = INLINE_CODE.sub(lambda match: " " * len(match.group(0)), original_line)
        reference = REFERENCE_DEFINITION.match(line)
        if reference:
            target = reference.group(1) if reference.group(1) is not None else reference.group(2)
            links.append(DiscoveredLink(source_file, line_number, target or ""))

        links.extend(_discover_inline_links(line, source_file, line_number))
    return links


def _discover_inline_links(line: str, source_file: str, line_number: int) -> list[DiscoveredLink]:
    links: list[DiscoveredLink] = []
    cursor = 0
    while True:
        match = INLINE_LINK_START.search(line, cursor)
        if match is None:
            break
        position = match.end()
        while position < len(line) and line[position].isspace():
            position += 1

        if position < len(line) and line[position] == "<":
            end = line.find(">", position + 1)
            if end == -1:
                cursor = match.end()
                continue
            target = line[position + 1 : end]
            close = line.find(")", end + 1)
            cursor = close + 1 if close >= 0 else end + 1
            links.append(DiscoveredLink(source_file, line_number, target))
            continue

        target_chars: list[str] = []
        depth = 0
        escaped = False
        while position < len(line):
            character = line[position]
            if escaped:
                target_chars.extend(("\\", character))
                escaped = False
                position += 1
                continue
            if character == "\\":
                escaped = True
                position += 1
                continue
            if character == "(":
                depth += 1
                target_chars.append(character)
                position += 1
                continue
            if character == ")":
                if depth == 0:
                    break
                depth -= 1
                target_chars.append(character)
                position += 1
                continue
            if character.isspace() and depth == 0:
                break
            target_chars.append(character)
            position += 1

        links.append(DiscoveredLink(source_file, line_number, "".join(target_chars)))
        cursor = max(position + 1, match.end())
    return links


def evaluate_link(link: DiscoveredLink, root: Path, tracked_paths: set[str]) -> LinkRecord:
    raw = html.unescape(link.target.strip())
    if not raw:
        return failed_record(link, "empty", "empty_target", "Markdown link target is empty.")

    if raw.startswith("#"):
        return LinkRecord(
            source_file=link.source_file,
            line=link.line,
            link_target=link.target,
            link_kind="anchor",
            normalized_target=link.source_file,
            target_exists=True,
            git_tracked=True,
            git_ignored=False,
            absolute_local_path=False,
            portable=True,
            status="pass",
            issue_code="anchor_only",
            detail="Document-local anchor does not require a filesystem target check.",
        )

    decoded = unquote(raw)
    if is_absolute_local_target(decoded):
        return LinkRecord(
            source_file=link.source_file,
            line=link.line,
            link_target=link.target,
            link_kind="absolute_local",
            normalized_target=decoded,
            target_exists=absolute_target_exists(decoded),
            git_tracked=False,
            git_ignored=False,
            absolute_local_path=True,
            portable=False,
            status="fail",
            issue_code="absolute_local_path",
            detail="Absolute local paths are machine-specific and forbidden in portable documentation.",
        )

    parsed = urlsplit(decoded)
    if decoded.startswith("//") or (parsed.scheme and parsed.scheme.lower() != "file"):
        scheme = parsed.scheme.lower()
        kind = "external_url" if scheme in EXTERNAL_SCHEMES or decoded.startswith("//") else "external_scheme"
        return LinkRecord(
            source_file=link.source_file,
            line=link.line,
            link_target=link.target,
            link_kind=kind,
            normalized_target=decoded,
            target_exists=False,
            git_tracked=False,
            git_ignored=False,
            absolute_local_path=False,
            portable=True,
            status="skip",
            issue_code="external_target",
            detail="External target is outside the local portability audit.",
        )

    path_part = decoded.split("#", 1)[0].split("?", 1)[0]
    path_part = markdown_unescape(path_part)
    if not path_part:
        return failed_record(link, "empty", "empty_target", "Local path is empty after removing query and fragment.")

    source_path = root / Path(PurePosixPath(link.source_file))
    portable_path = path_part.replace("\\", "/")
    candidate = source_path.parent.joinpath(*PurePosixPath(portable_path).parts)
    candidate = Path(os.path.abspath(candidate))
    root_absolute = Path(os.path.abspath(root))
    try:
        relative = candidate.relative_to(root_absolute)
    except ValueError:
        return LinkRecord(
            source_file=link.source_file,
            line=link.line,
            link_target=link.target,
            link_kind="relative_local",
            normalized_target=candidate.as_posix(),
            target_exists=candidate.exists(),
            git_tracked=False,
            git_ignored=False,
            absolute_local_path=False,
            portable=False,
            status="fail",
            issue_code="outside_repository",
            detail="Relative link resolves outside the repository root.",
        )

    relative_posix = relative.as_posix()
    exists = candidate.exists()
    tracked = is_tracked_target(relative_posix, candidate, tracked_paths)
    if not exists:
        status, issue, detail = "fail", "target_missing", "Local target does not exist in the worktree."
    elif not tracked:
        status, issue, detail = (
            "fail",
            "untracked_target",
            "Local target exists but is not Git-tracked, so a clean clone cannot rely on it.",
        )
    else:
        status, issue, detail = "pass", "portable_local_target", "Local target exists and is Git-tracked."

    return LinkRecord(
        source_file=link.source_file,
        line=link.line,
        link_target=link.target,
        link_kind="relative_local",
        normalized_target=relative_posix,
        target_exists=exists,
        git_tracked=tracked,
        git_ignored=False,
        absolute_local_path=False,
        portable=status == "pass",
        status=status,
        issue_code=issue,
        detail=detail,
    )


def markdown_unescape(value: str) -> str:
    return re.sub(r"\\([ !\"#$%&'()*+,./:;<=>?@\[\]^_`{|}~-])", r"\1", value)


def is_absolute_local_target(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.lower()
    return bool(
        WINDOWS_DRIVE_PATH.match(normalized)
        or normalized.startswith("\\")
        or (normalized.startswith("/") and not normalized.startswith("//"))
        or lowered.startswith("file:")
        or normalized.startswith("~/")
        or normalized.startswith("~\\")
    )


def absolute_target_exists(value: str) -> bool:
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        path_text = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:", path_text):
            path_text = path_text[1:]
        if parsed.netloc:
            path_text = f"//{parsed.netloc}{path_text}"
        return Path(path_text).exists()
    if value.startswith("~/") or value.startswith("~\\"):
        return Path(value).expanduser().exists()
    return Path(value).exists()


def is_tracked_target(relative_posix: str, candidate: Path, tracked_paths: set[str]) -> bool:
    if relative_posix in tracked_paths:
        return True
    if candidate.is_dir():
        prefix = relative_posix.rstrip("/")
        if prefix:
            prefix += "/"
        return any(path.startswith(prefix) for path in tracked_paths)
    return False


def failed_record(link: DiscoveredLink, kind: str, issue: str, detail: str) -> LinkRecord:
    return LinkRecord(
        source_file=link.source_file,
        line=link.line,
        link_target=link.target,
        link_kind=kind,
        normalized_target="",
        target_exists=False,
        git_tracked=False,
        git_ignored=False,
        absolute_local_path=False,
        portable=False,
        status="fail",
        issue_code=issue,
        detail=detail,
    )


def infrastructure_failure(issue: str, detail: str) -> LinkRecord:
    return LinkRecord(
        source_file="",
        line=0,
        link_target="",
        link_kind="audit_error",
        normalized_target="",
        target_exists=False,
        git_tracked=False,
        git_ignored=False,
        absolute_local_path=False,
        portable=False,
        status="fail",
        issue_code=issue,
        detail=detail,
    )


def build_summary(result: AuditResult) -> dict[str, object]:
    records = result.records
    failures = [row for row in records if row.status == "fail"]
    local_rows = [row for row in records if row.link_kind in {"relative_local", "absolute_local"}]
    return {
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": "fail_closed",
        "scope": "Git-tracked Markdown files",
        "audit_passed": result.audit_passed,
        "git_inventory_ok": result.git_inventory_ok,
        "tracked_markdown_count": len(result.tracked_markdown_files),
        "discovered_link_count": len(records),
        "local_path_link_count": len(local_rows),
        "portable_local_link_count": sum(row.portable for row in local_rows),
        "anchor_link_count": sum(row.link_kind == "anchor" for row in records),
        "external_link_count": sum(row.status == "skip" for row in records),
        "failure_count": len(failures),
        "missing_target_count": sum(row.issue_code == "target_missing" for row in records),
        "untracked_target_count": sum(
            row.issue_code in {"untracked_target", "ignored_untracked_target"} for row in records
        ),
        "ignored_target_count": sum(row.git_ignored for row in records),
        "absolute_local_path_count": sum(row.absolute_local_path for row in records),
        "portable_contract": {
            "relative_local_target_must_exist": True,
            "relative_local_target_must_be_git_tracked": True,
            "absolute_local_paths_allowed": False,
            "ignored_outputs_count_as_portable": False,
            "anchor_only_links_require_path_check": False,
            "http_and_mailto_are_external": True,
        },
    }


def write_outputs(result: AuditResult, output: Path) -> dict[str, object]:
    debug = output / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary = build_summary(result)
    ordered = sorted(
        result.records,
        key=lambda row: (row.status != "fail", row.source_file, row.line, row.link_target),
    )
    rows = [asdict(row) for row in ordered]

    (output / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(output / "top_candidates.csv", rows)
    write_csv(debug / "link_audit.csv", [asdict(row) for row in result.records])
    (debug / "tracked_markdown_files.txt").write_text(
        "\n".join(result.tracked_markdown_files) + ("\n" if result.tracked_markdown_files else ""),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(summary, ordered), encoding="utf-8")
    return summary


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def render_report(summary: dict[str, object], records: Sequence[LinkRecord]) -> str:
    failures = [row for row in records if row.status == "fail"]
    lines = [
        "# Git tracked Markdown 本地链接审计",
        "",
        f"- 审计结论：`{'PASS' if summary['audit_passed'] else 'FAIL'}`",
        f"- Git tracked Markdown：{summary['tracked_markdown_count']} 份",
        f"- 发现链接：{summary['discovered_link_count']} 条",
        f"- 本地路径链接：{summary['local_path_link_count']} 条",
        f"- 失败项：{summary['failure_count']} 条",
        "",
        "## 判定边界",
        "",
        "相对本地链接只有在目标存在、位于仓库内且受 Git 跟踪时才通过。Windows 或 Unix "
        "绝对本地路径直接失败；被 `.gitignore` 排除的输出即使当前机器上存在，也不具备干净克隆后的可移植性。"
        "纯锚点与 HTTP、HTTPS、mailto 等外部目标不执行本地文件检查。",
        "",
        "## 失败项",
        "",
    ]
    if not failures:
        lines.append("无。")
    else:
        lines.extend(
            [
                "| 来源 | 行 | 目标 | 问题 |",
                "|---|---:|---|---|",
            ]
        )
        for row in failures[:100]:
            source = markdown_cell(row.source_file)
            target = markdown_cell(row.link_target or row.normalized_target)
            issue = markdown_cell(row.issue_code)
            lines.append(f"| {source} | {row.line} | {target} | {issue} |")
        if len(failures) > 100:
            lines.append("")
            lines.append(f"另有 {len(failures) - 100} 条失败项，详见 `debug/link_audit.csv`。")
    lines.append("")
    return "\n".join(lines)


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="markdown-link-audit-") as directory:
        root = Path(directory)
        (root / "docs").mkdir()
        (root / "outputs").mkdir()
        (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        (root / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        (root / "outputs" / "report.md").write_text("ignored\n", encoding="utf-8")
        (root / ".gitignore").write_text("outputs/\n", encoding="utf-8")
        (root / "README.md").write_text(
            "\n".join(
                [
                    "# Fixture",
                    "[valid](docs/guide.md#guide)",
                    "[anchor](#fixture)",
                    "[missing](docs/missing.md)",
                    "[absolute](C:/machine-only/report.md)",
                    "[untracked](scratch.txt)",
                    "[ignored](outputs/report.md)",
                    "[web](https://example.invalid)",
                    "[mail](mailto:test@example.invalid)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        run_git(root, "init", "-q")
        run_git(root, "add", "README.md", "docs/guide.md", ".gitignore")

        result = audit_repository(root)
        by_target = {row.link_target: row for row in result.records}
        assert by_target["docs/guide.md#guide"].status == "pass"
        assert by_target["#fixture"].issue_code == "anchor_only"
        assert by_target["docs/missing.md"].issue_code == "target_missing"
        assert by_target["C:/machine-only/report.md"].issue_code == "absolute_local_path"
        assert by_target["scratch.txt"].issue_code == "untracked_target"
        assert by_target["outputs/report.md"].issue_code == "ignored_untracked_target"
        assert by_target["outputs/report.md"].portable is False
        assert by_target["https://example.invalid"].status == "skip"
        assert by_target["mailto:test@example.invalid"].status == "skip"
        assert result.audit_passed is False
    print("self_check=pass")


def run_git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")


if __name__ == "__main__":
    raise SystemExit(main())
