from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

import audit_markdown_links as audit


def make_repo(tmp_path: Path, readme: str, *, tracked: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(readme, encoding="utf-8")
    tracked_paths = ["README.md"]
    for relative, content in (tracked or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        tracked_paths.append(relative.replace("\\", "/"))
    run_git(root, "init", "-q")
    run_git(root, "add", *tracked_paths)
    return root


def run_git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def only_link(result: audit.AuditResult) -> audit.LinkRecord:
    assert len(result.records) == 1
    return result.records[0]


def test_legal_relative_target_exists_and_is_git_tracked(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        "[guide](docs/guide.md#usage)\n",
        tracked={"docs/guide.md": "# Usage\n"},
    )

    row = only_link(audit.audit_repository(root))

    assert row.status == "pass"
    assert row.issue_code == "portable_local_target"
    assert row.normalized_target == "docs/guide.md"
    assert row.target_exists is True
    assert row.git_tracked is True
    assert row.portable is True


def test_anchor_http_and_mailto_do_not_become_local_paths(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        "[section](#section)\n[web](https://example.invalid/x)\n[mail](mailto:a@example.invalid)\n",
    )

    result = audit.audit_repository(root)
    by_target = {row.link_target: row for row in result.records}

    assert by_target["#section"].status == "pass"
    assert by_target["#section"].issue_code == "anchor_only"
    assert by_target["https://example.invalid/x"].status == "skip"
    assert by_target["mailto:a@example.invalid"].status == "skip"
    assert result.audit_passed is True


def test_missing_relative_target_fails_closed(tmp_path: Path) -> None:
    root = make_repo(tmp_path, "[missing](docs/missing.md)\n")

    row = only_link(audit.audit_repository(root))

    assert row.status == "fail"
    assert row.issue_code == "target_missing"
    assert row.target_exists is False
    assert row.portable is False


def test_windows_absolute_target_is_nonportable(tmp_path: Path) -> None:
    root = make_repo(tmp_path, "[machine](E:/private/report.md)\n")

    row = only_link(audit.audit_repository(root))

    assert row.status == "fail"
    assert row.issue_code == "absolute_local_path"
    assert row.absolute_local_path is True
    assert row.portable is False


def test_existing_untracked_and_ignored_targets_do_not_pass(tmp_path: Path) -> None:
    root = make_repo(tmp_path, "[draft](draft.txt)\n[output](outputs/report.md)\n")
    (root / "draft.txt").write_text("local only\n", encoding="utf-8")
    (root / "outputs").mkdir()
    (root / "outputs" / "report.md").write_text("generated\n", encoding="utf-8")
    (root / ".gitignore").write_text("outputs/\n", encoding="utf-8")
    run_git(root, "add", ".gitignore")

    result = audit.audit_repository(root)
    by_target = {row.link_target: row for row in result.records}

    assert by_target["draft.txt"].issue_code == "untracked_target"
    assert by_target["draft.txt"].target_exists is True
    assert by_target["draft.txt"].git_tracked is False
    assert by_target["outputs/report.md"].issue_code == "ignored_untracked_target"
    assert by_target["outputs/report.md"].git_ignored is True
    assert by_target["outputs/report.md"].portable is False
    assert result.audit_passed is False


def test_standard_four_piece_outputs_are_written(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        "[guide](guide.md)\n",
        tracked={"guide.md": "# Guide\n"},
    )
    result = audit.audit_repository(root)
    output = tmp_path / "audit-output"

    summary = audit.write_outputs(result, output)

    assert summary["audit_passed"] is True
    assert (output / "report.md").is_file()
    assert (output / "run_summary.json").is_file()
    assert (output / "top_candidates.csv").is_file()
    assert (output / "debug").is_dir()
    assert (output / "debug" / "link_audit.csv").is_file()
    assert json.loads((output / "run_summary.json").read_text(encoding="utf-8"))["failure_count"] == 0
    with (output / "top_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["normalized_target"] == "guide.md"


def test_self_check_covers_required_negative_cases() -> None:
    audit.self_check()
