#!/usr/bin/env python
"""Replayable browser QA for the local research dashboard trust surface.

The script expects a Vite preview server to be running already.  It uses the
Python Playwright package with the system Chrome executable, so downloading a
Playwright-managed browser is not required.  Every synthetic state is injected
only in the browser through route interception; no project data is modified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:4175/"
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "dashboard_trust_remediation" / "debug"
DEFAULT_CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
EXPECTED_SCHEMA = "dashboard-data-v2"
RESULT_SCHEMA = "dashboard-trust-ui-qa-v1"
LONG_TEXT_SENTINEL = (
    "UI_QA_LONG_TEXT_"
    "这是一段用于验证窄屏换行与信息完整性的超长数据质量警告，"
    "任何字段都不得覆盖按钮、突破页面宽度或被误解成可以买入的信号："
    "ui_qa_unbroken_identifier_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ。"
)
FIXED_NOTICES = (
    {"code": "not_investment_advice", "text": "本页面不构成投资建议。"},
    {"code": "history_not_future", "text": "历史表现不代表未来结果。"},
    {"code": "data_may_lag", "text": "数据可能存在延迟。"},
    {"code": "manual_support_not_ready", "text": "人工决策支持当前未就绪。"},
    {"code": "auto_execution_disabled", "text": "自动执行永久关闭，当前不会自动下单。"},
)
FIXED_NOTICE_PATTERNS = (
    ("not_investment_advice", r"不构成.{0,6}投资建议"),
    ("history_not_future", r"历史.{0,8}不代表.{0,8}未来"),
    ("data_may_lag", r"数据.{0,8}(?:延迟|滞后)"),
    ("manual_support_not_ready", r"人工.{0,10}(?:未就绪|不具备|尚未准备)"),
    ("auto_execution_disabled", r"自动执行.{0,8}(?:关闭|禁用|不允许)"),
)
CURRENT_BUY_RE = re.compile(r"\bBUY(?:_CANDIDATE)?\b|当前.{0,8}(?:建议)?买入|建议买入", re.IGNORECASE)


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class Scenario:
    name: str
    intercept: bool
    expect_error: bool = False
    expect_focus: bool = False
    load_history: bool = False


VIEWPORTS = (
    Viewport("desktop_1440", 1440, 1000),
    Viewport("mobile_390", 390, 844),
)
SCENARIOS = (
    Scenario("normal", False),
    Scenario("history_loaded", False, load_history=True),
    Scenario("stale", True),
    Scenario("warning", True),
    Scenario("contract_error", True, expect_error=True),
    Scenario("empty_candidates", True),
    Scenario("no_action", True),
    Scenario("long_text_focus", True, expect_focus=True),
)


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def relative_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def fetch_bytes(url: str, timeout_seconds: float) -> tuple[int, bytes, str]:
    request = Request(url, headers={"User-Agent": "dashboard-trust-ui-qa/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - explicitly local preview URL
        return int(response.status), response.read(), str(response.headers.get("Content-Type", ""))


def fetch_json(url: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    status, body, _ = fetch_bytes(url, timeout_seconds)
    value = json.loads(body.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("dashboard data endpoint must return a JSON object")
    return status, value


def ensure_mapping(parent: MutableMapping[str, Any], key: str) -> MutableMapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def ensure_list(parent: MutableMapping[str, Any], key: str) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        value = []
        parent[key] = value
    return value


def ensure_fixed_notices(data: MutableMapping[str, Any]) -> list[dict[str, str]]:
    notices = data.get("fixed_notices")
    required_codes = {str(item["code"]) for item in FIXED_NOTICES}
    if isinstance(notices, list) and all(isinstance(item, dict) for item in notices):
        present_codes = {str(item.get("code", "")) for item in notices}
        if required_codes.issubset(present_codes):
            return notices
    replacement = [dict(item) for item in FIXED_NOTICES]
    data["fixed_notices"] = replacement
    return replacement


def ensure_ui_qa_evidence(data: MutableMapping[str, Any]) -> str:
    """Register the browser-only fixture evidence used by injected rows."""

    evidence_id = "ui_qa.fixture"
    catalog = ensure_list(data, "evidence_catalog")
    if not any(isinstance(item, dict) and item.get("evidence_id") == evidence_id for item in catalog):
        catalog.append(
            {
                "evidence_id": evidence_id,
                "path": "tests/fixtures/dashboard_data.valid.json",
                "local_generated": True,
                "linkable": False,
            }
        )
    return evidence_id


def add_warning(
    data: MutableMapping[str, Any],
    *,
    code: str,
    severity: str,
    message: str,
    source: str = "ui_qa_fixture",
    evidence_id: str = "ui_qa.fixture",
) -> None:
    warnings = ensure_list(data, "data_quality_warnings")
    warnings.append(
        {
            "code": code,
            "severity": severity,
            "source": source,
            "message": message,
            "evidence_id": evidence_id,
        }
    )


def recursively_disable_auto_execution(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "auto_execution_allowed":
                value[key] = False
            else:
                recursively_disable_auto_execution(item)
    elif isinstance(value, list):
        for item in value:
            recursively_disable_auto_execution(item)


def set_safe_current_recommendation(data: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    recommendation = ensure_mapping(data, "current_recommendation")
    recommendation.update(
        {
            "action": "NO_ACTION",
            "candidates": [],
            "human_confirmation_required": True,
            "auto_execution_allowed": False,
        }
    )
    return recommendation


def build_variant(base: Mapping[str, Any], scenario: str) -> dict[str, Any]:
    """Create a dashboard-data-v2 browser-only fixture for one QA scenario."""

    if scenario not in {item.name for item in SCENARIOS if item.intercept}:
        raise ValueError(f"unsupported intercepted scenario: {scenario}")
    data = copy.deepcopy(dict(base))
    if scenario == "contract_error":
        data["schema_version"] = "dashboard-data-unsupported-ui-qa"
        return data

    data["schema_version"] = EXPECTED_SCHEMA
    ensure_fixed_notices(data)
    fixture_evidence_id = ensure_ui_qa_evidence(data)

    decision_as_of = str(
        data.get("decision_as_of_date")
        or ensure_mapping(data, "trust_summary").get("decision_as_of_date")
        or datetime.now().date().isoformat()
    )
    generated_at = str(data.get("generated_at") or iso_now())
    data["decision_as_of_date"] = decision_as_of
    data["generated_at"] = generated_at

    trust = ensure_mapping(data, "trust_summary")
    trust.update(
        {
            "research_state": "research_only / NO_ACTION",
            "policy_status": "research_only",
            "current_action": "NO_ACTION",
            "decision_as_of_date": decision_as_of,
            "manual_support_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        }
    )
    recommendation = set_safe_current_recommendation(data)

    source_freshness = ensure_list(data, "source_freshness")
    if not source_freshness or not isinstance(source_freshness[0], dict):
        source_freshness[:] = [
            {
                "source": "行业行情",
                "source_id": "industry_history",
                "cutoff_date": decision_as_of,
                "lag_days": 0,
                "required": True,
                "status": "fresh",
                "detail": "UI QA fixture",
                "evidence_id": fixture_evidence_id,
            }
        ]

    if scenario == "stale":
        message = "UI QA fixture：行业行情已陈旧 9 天，当前动作必须保持 NO_ACTION。"
        add_warning(data, code="ui_qa_stale", severity="warning", message=message)
        stale_source = next(
            (
                item
                for item in source_freshness
                if isinstance(item, dict) and item.get("required") is False and item.get("status") == "fresh"
            ),
            None,
        )
        if stale_source is None:
            stale_source = {
                "source": "UI QA 可选行情源",
                "source_id": "ui_qa_optional_source",
                "cutoff_date": decision_as_of,
                "lag_days": 0,
                "required": False,
                "status": "fresh",
                "detail": "浏览器内注入的可选源，仅用于 stale 状态验收。",
                "evidence_id": fixture_evidence_id,
            }
            source_freshness.append(stale_source)
        try:
            stale_cutoff = (datetime.fromisoformat(decision_as_of).date() - timedelta(days=9)).isoformat()
        except ValueError:
            stale_cutoff = decision_as_of
        stale_source.update(
            {
                "cutoff_date": stale_cutoff,
                "lag_days": 9,
                "required": False,
                "status": "stale_optional",
                "detail": "可选行情源已陈旧 9 天，当前结果降级为研究观察。",
            }
        )
    elif scenario == "warning":
        message = "UI QA fixture：估值发布时间仍待核验，结果仅供研究观察。"
        add_warning(data, code="ui_qa_warning", severity="warning", message=message)
    elif scenario == "empty_candidates":
        recommendation.update({"action": "NO_ACTION", "candidates": []})
        trust["current_action"] = "NO_ACTION"
        valuation = ensure_mapping(data, "valuation_snapshot")
        valuation.update({"candidate_count": 0, "candidates": []})
    elif scenario == "no_action":
        recommendation.update({"action": "NO_ACTION", "candidates": [], "risk_vetoes": ["goal_evidence"]})
        trust["current_action"] = "NO_ACTION"
    elif scenario == "long_text_focus":
        add_warning(
            data,
            code="ui_qa_long_text",
            severity="warning",
            message=LONG_TEXT_SENTINEL,
        )

    recursively_disable_auto_execution(data)
    return data


def testid_probe(page: Any, testid: str) -> dict[str, Any]:
    locator = page.locator(f'[data-testid="{testid}"]')
    count = locator.count()
    if count == 0:
        return {"count": 0, "visible": False, "text": ""}
    first = locator.first
    try:
        visible = bool(first.is_visible())
        text = first.inner_text(timeout=2_000).strip() if visible else ""
    except Exception as exc:  # noqa: BLE001 - individual selector failure belongs in the audit result
        return {"count": count, "visible": False, "text": "", "error": str(exc)}
    return {"count": count, "visible": visible, "text": text[:2_000]}


def fixed_notice_check(text: str) -> tuple[bool, list[str]]:
    missing = [code for code, pattern in FIXED_NOTICE_PATTERNS if re.search(pattern, text) is None]
    return not missing, missing


def matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def current_buy_copy(text: str) -> list[str]:
    return sorted(set(match.group(0) for match in CURRENT_BUY_RE.finditer(text)))


def focus_with_keyboard(page: Any, preferred_testid: str, max_tabs: int = 30) -> dict[str, Any]:
    page.locator("body").click(position={"x": 2, "y": 2})
    target_seen = False
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        state = page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el) return null;
              const style = getComputedStyle(el);
              const visible = el.matches(':focus-visible');
              const painted = (style.outlineStyle !== 'none' && style.outlineWidth !== '0px') ||
                style.boxShadow !== 'none';
              return {
                tag: el.tagName.toLowerCase(),
                testid: el.getAttribute('data-testid') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                text: (el.textContent || '').trim().slice(0, 200),
                focusVisible: visible,
                focusPainted: painted,
                outline: `${style.outlineWidth} ${style.outlineStyle} ${style.outlineColor}`,
                boxShadow: style.boxShadow,
              };
            }"""
        )
        if isinstance(state, dict) and state.get("testid") == preferred_testid:
            target_seen = True
            break
    if not isinstance(state, dict):
        return {"checked": True, "passed": False, "reason": "no focusable element reached"}
    passed = target_seen and bool(state.get("focusVisible")) and bool(state.get("focusPainted"))
    return {
        "checked": True,
        "preferred_testid": preferred_testid,
        "preferred_target_reached": target_seen,
        "passed": passed,
        "active_element": state,
    }


def classify_link(base_url: str, href: str) -> tuple[str, str]:
    href = href.strip()
    if not href:
        return "empty", ""
    parsed = urlparse(href)
    if parsed.scheme in {"mailto", "tel", "javascript", "data"}:
        return "special", href
    absolute = urljoin(base_url, href)
    if urlparse(absolute).netloc == urlparse(base_url).netloc:
        return "same_origin", absolute
    return "external", absolute


def audit_links(page: Any, context: Any, timeout_ms: int) -> dict[str, Any]:
    hrefs = page.locator("a[href]").evaluate_all("els => els.map(el => el.getAttribute('href') || '')")
    rows: list[dict[str, Any]] = []
    for href in sorted(set(str(item) for item in hrefs)):
        kind, target = classify_link(page.url, href)
        row: dict[str, Any] = {"href": href, "kind": kind, "target": target}
        if kind == "same_origin":
            try:
                response = context.request.get(target, timeout=timeout_ms, fail_on_status_code=False)
                row["status"] = response.status
                row["ok"] = response.status == 200
            except Exception as exc:  # noqa: BLE001 - network failures are audit evidence
                row.update({"status": None, "ok": False, "error": str(exc)})
        elif kind == "external":
            row.update({"status": None, "ok": None, "note": "recorded_only_not_visited"})
        else:
            row.update({"status": None, "ok": None})
        rows.append(row)
    same_origin = [row for row in rows if row["kind"] == "same_origin"]
    return {
        "items": rows,
        "same_origin_count": len(same_origin),
        "same_origin_all_ok": all(row.get("ok") is True for row in same_origin),
        "external_count": sum(row["kind"] == "external" for row in rows),
    }


def mobile_contract(selectors: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    valuation = selectors.get("valuation-mobile", {})
    trust = selectors.get("trust-summary", {})
    freshness = selectors.get("source-freshness", {})
    valuation_text = str(valuation.get("text", ""))
    trust_text = str(trust.get("text", ""))
    freshness_text = str(freshness.get("text", ""))
    fields = {
        "valuation_date": bool(re.search(r"估值.{0,8}(?:20\d{2}[-/.年]\d{1,2}|日期)", valuation_text)),
        "pe": bool(re.search(r"\bPE\b", valuation_text, flags=re.IGNORECASE)),
        "pb": bool(re.search(r"\bPB\b", valuation_text, flags=re.IGNORECASE)),
        "dividend_yield": "股息" in valuation_text,
        "current_action": matches_any(trust_text, (r"NO_ACTION", r"暂不买入", r"当前动作")),
        "key_gate": matches_any(trust_text + "\n" + freshness_text, (r"门禁", r"gate", r"阻断", r"cohort")),
    }
    return {
        "checked": True,
        "valuation_mobile_visible": valuation.get("visible") is True,
        "fields": fields,
        "passed": valuation.get("visible") is True and all(fields.values()),
    }


def scenario_expectations(
    scenario: Scenario,
    viewport: Viewport,
    body_text: str,
    selectors: Mapping[str, Mapping[str, Any]],
    schema_version: str,
) -> list[str]:
    failures: list[str] = []
    if scenario.expect_error:
        if not matches_any(body_text, (r"数据读取失败", r"contract", r"schema", r"契约")):
            failures.append("contract error copy is not visible")
        return failures

    if schema_version != EXPECTED_SCHEMA:
        failures.append(f"baseline schema is {schema_version!r}, expected {EXPECTED_SCHEMA!r}")
    required = ["reload-local-data", "trust-summary", "source-freshness", "fixed-notices"]
    if not scenario.load_history:
        required.append("history-load")
    if viewport.name == "mobile_390":
        required.append("valuation-mobile")
    for testid in required:
        if selectors.get(testid, {}).get("visible") is not True:
            failures.append(f"required visible data-testid missing: {testid}")

    trust_text = str(selectors.get("trust-summary", {}).get("text", ""))
    buy_copy = current_buy_copy(trust_text)
    if buy_copy:
        failures.append("current trust summary contains BUY copy: " + ",".join(buy_copy))

    notices_text = str(selectors.get("fixed-notices", {}).get("text", ""))
    notices_ok, missing_notices = fixed_notice_check(notices_text)
    if not notices_ok:
        failures.append("fixed notices missing: " + ",".join(missing_notices))

    freshness_text = str(selectors.get("source-freshness", {}).get("text", ""))
    if scenario.name == "stale" and not matches_any(freshness_text + body_text, (r"stale", r"陈旧", r"过期")):
        failures.append("stale state is not visibly labelled")
    if scenario.name == "warning" and not matches_any(body_text, (r"warning", r"警告", r"待核验", r"降级")):
        failures.append("warning state is not visibly labelled")
    if scenario.name == "empty_candidates" and not matches_any(body_text, (r"暂无.{0,6}候选", r"没有.{0,6}候选", r"空候选", r"empty")):
        failures.append("empty candidate copy is not visible")
    if scenario.name == "no_action" and not matches_any(trust_text, (r"NO_ACTION", r"暂不买入", r"当前动作")):
        failures.append("NO_ACTION is not visible in the trust summary")
    if scenario.name == "long_text_focus" and LONG_TEXT_SENTINEL not in body_text:
        failures.append("long-text sentinel is not visible")
    if scenario.load_history and selectors.get("history-loaded", {}).get("visible") is not True:
        failures.append("validated historical details did not become visible")
    return failures


def capture_case(
    browser: Any,
    preview_url: str,
    data_url: str,
    output_dir: Path,
    base_payload: Mapping[str, Any],
    scenario: Scenario,
    viewport: Viewport,
    timeout_ms: int,
) -> dict[str, Any]:
    case_id = f"{scenario.name}__{viewport.name}"
    screenshot = output_dir / f"{case_id}.png"
    context = browser.new_context(viewport={"width": viewport.width, "height": viewport.height})
    page = context.new_page()
    console_errors: list[str] = []
    console_warnings: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[dict[str, Any]] = []

    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else console_warnings.append(message.text) if message.type == "warning" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append(f"{request.method} {request.url}: {request.failure}"))
    page.on(
        "response",
        lambda response: bad_responses.append({"url": response.url, "status": response.status})
        if response.status >= 400
        else None,
    )

    payload = dict(base_payload)
    if scenario.intercept:
        payload = build_variant(base_payload, scenario.name)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)

        def fulfill(route: Any) -> None:
            route.fulfill(status=200, content_type="application/json; charset=utf-8", body=encoded)

        route_base = urlparse(data_url)._replace(query="", fragment="").geturl()
        context.route(re.compile(rf"^{re.escape(route_base)}(?:\?.*)?$"), fulfill)

    result: dict[str, Any] = {
        "case_id": case_id,
        "scenario": scenario.name,
        "viewport": {"name": viewport.name, "width": viewport.width, "height": viewport.height},
        "injected": scenario.intercept,
        "intercepted_data_url": data_url if scenario.intercept else None,
        "schema_version": payload.get("schema_version"),
        "screenshot": relative_path(screenshot),
        "passed": False,
        "failure_reasons": [],
    }
    try:
        response = page.goto(preview_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(800)
        result["navigation"] = {"url": page.url, "status": response.status if response else None}

        if scenario.load_history:
            load_button = page.get_by_test_id("history-load")
            load_button.click(timeout=timeout_ms)
            page.locator(".details-verified").wait_for(state="visible", timeout=timeout_ms)
            page.wait_for_timeout(500)

        selector_ids = (
            "reload-local-data",
            "trust-summary",
            "source-freshness",
            "valuation-mobile",
            "history-load",
            "fixed-notices",
        )
        selectors = {testid: testid_probe(page, testid) for testid in selector_ids}
        history_loaded = page.locator(".details-verified")
        selectors["history-loaded"] = {
            "count": history_loaded.count(),
            "visible": history_loaded.count() > 0 and history_loaded.first.is_visible(),
            "text": history_loaded.first.inner_text().strip() if history_loaded.count() > 0 and history_loaded.first.is_visible() else "",
        }
        body_text = page.locator("body").inner_text(timeout=5_000).strip()
        dimensions = page.evaluate(
            """() => ({
              innerWidth: window.innerWidth,
              documentScrollWidth: document.documentElement.scrollWidth,
              bodyScrollWidth: document.body ? document.body.scrollWidth : 0,
            })"""
        )
        max_scroll_width = max(int(dimensions["documentScrollWidth"]), int(dimensions["bodyScrollWidth"]))
        overflow_passed = max_scroll_width <= int(dimensions["innerWidth"])
        notice_layout = page.evaluate(
            """() => {
              const node = document.querySelector('[data-testid="fixed-notices"]');
              if (!node) return { present: false, position: '', overlaysContent: false };
              const style = getComputedStyle(node);
              return {
                present: true,
                position: style.position,
                overlaysContent: style.position === 'fixed' || style.position === 'sticky',
              };
            }"""
        )

        focus = {"checked": False, "passed": None}
        if scenario.expect_focus:
            focus = focus_with_keyboard(page, "reload-local-data")

        links = audit_links(page, context, timeout_ms)
        fixed_text = str(selectors["fixed-notices"].get("text", ""))
        fixed_ok, missing_fixed = fixed_notice_check(fixed_text)
        mobile = {"checked": False, "passed": None}
        if viewport.name == "mobile_390" and not scenario.expect_error:
            mobile = mobile_contract(selectors)

        page.screenshot(path=str(screenshot), full_page=True)
        failures = scenario_expectations(
            scenario,
            viewport,
            body_text,
            selectors,
            str(payload.get("schema_version", "")),
        )
        if not overflow_passed:
            failures.append(
                f"page horizontal overflow: scrollWidth={max_scroll_width} innerWidth={dimensions['innerWidth']}"
            )
        if notice_layout.get("overlaysContent"):
            failures.append("fixed notices overlay document content")
        if console_errors:
            failures.append(f"console errors={len(console_errors)}")
        if page_errors:
            failures.append(f"page errors={len(page_errors)}")
        if failed_requests:
            failures.append(f"failed requests={len(failed_requests)}")
        if bad_responses:
            failures.append(f"HTTP >=400 responses={len(bad_responses)}")
        if not links["same_origin_all_ok"]:
            failures.append("one or more same-origin links failed")
        if scenario.expect_focus and focus.get("passed") is not True:
            failures.append("keyboard focus is not visibly painted")
        if mobile.get("checked") and mobile.get("passed") is not True:
            failures.append("mobile trust/valuation contract is incomplete")

        result.update(
            {
                "screenshot_sha256": sha256_file(screenshot),
                "dimensions": dimensions,
                "overflow": {
                    "max_scroll_width": max_scroll_width,
                    "inner_width": dimensions["innerWidth"],
                    "passed": overflow_passed,
                },
                "fixed_notice_layout": notice_layout,
                "console_errors": console_errors,
                "console_warnings": console_warnings,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "bad_responses": bad_responses,
                "selectors": selectors,
                "key_copy": {
                    "body_excerpt": body_text[:3_000],
                    "trust_summary": str(selectors["trust-summary"].get("text", "")),
                    "source_freshness": str(selectors["source-freshness"].get("text", "")),
                    "valuation_mobile": str(selectors["valuation-mobile"].get("text", "")),
                    "fixed_notices": fixed_text,
                    "long_text_visible": LONG_TEXT_SENTINEL in body_text,
                    "current_buy_copy": current_buy_copy(str(selectors["trust-summary"].get("text", ""))),
                    "fixed_notices_complete": fixed_ok,
                    "missing_fixed_notices": missing_fixed,
                },
                "mobile_contract": mobile,
                "focus": focus,
                "links": links,
                "failure_reasons": failures,
                "passed": not failures,
            }
        )
    except Exception as exc:  # noqa: BLE001 - a case failure must not abort the remaining matrix
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            screenshot_hash = sha256_file(screenshot)
        except Exception:  # noqa: BLE001
            screenshot_hash = ""
        result.update(
            {
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "screenshot_sha256": screenshot_hash,
                "console_errors": console_errors,
                "console_warnings": console_warnings,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "bad_responses": bad_responses,
                "failure_reasons": [f"case execution failed: {exc}"],
                "passed": False,
            }
        )
    finally:
        context.close()
    return result


def render_markdown(results: Mapping[str, Any]) -> str:
    lines = [
        "# Dashboard 可信度 UI 自动化验收",
        "",
        f"- 状态：`{results.get('status', 'unknown')}`",
        f"- 生成时间：`{results.get('generated_at', '')}`",
        f"- Preview：`{results.get('preview_url', '')}`",
        f"- 数据端点：`{results.get('data_url', '')}`",
        f"- Chrome：`{results.get('chrome_path', '')}`",
        f"- 通过：`{results.get('passed_case_count', 0)}/{results.get('case_count', 0)}`",
        "",
    ]
    blockers = list(results.get("environment_blockers", []))
    if blockers:
        lines.extend(["## 环境阻断", ""])
        lines.extend(f"- {item}" for item in blockers)
        lines.append("")
    cases = list(results.get("cases", []))
    if cases:
        lines.extend(
            [
                "## 场景矩阵",
                "",
                "| 场景 | 视口 | 截图 | Overflow | Console/PageError | Mobile | Focus | 结果 |",
                "|---|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for item in cases:
            console_count = len(item.get("console_errors", [])) + len(item.get("page_errors", []))
            mobile = item.get("mobile_contract", {})
            focus = item.get("focus", {})
            lines.append(
                "| {scenario} | {viewport} | `{screenshot}` | {overflow} | {console} | {mobile} | {focus} | {status} |".format(
                    scenario=item.get("scenario", ""),
                    viewport=item.get("viewport", {}).get("name", ""),
                    screenshot=item.get("screenshot", ""),
                    overflow="pass" if item.get("overflow", {}).get("passed") else "fail",
                    console=console_count,
                    mobile=("pass" if mobile.get("passed") else "fail") if mobile.get("checked") else "n/a",
                    focus=("pass" if focus.get("passed") else "fail") if focus.get("checked") else "n/a",
                    status="PASS" if item.get("passed") else "FAIL",
                )
            )
        lines.append("")
        failures = [item for item in cases if not item.get("passed")]
        if failures:
            lines.extend(["## 失败项", ""])
            for item in failures:
                reasons = "; ".join(str(reason) for reason in item.get("failure_reasons", [])) or "unknown"
                lines.append(f"- `{item.get('case_id', '')}`：{reasons}")
            lines.append("")
    lines.extend(
        [
            "## 边界",
            "",
            "本工具只拦截浏览器内的 Dashboard JSON，不改写研究数据、状态文件或门禁。",
            "外部链接只记录，不主动访问。",
            "",
        ]
    )
    return "\n".join(lines)


def write_results(output_dir: Path, results: MutableMapping[str, Any]) -> None:
    cases = list(results.get("cases", []))
    results["case_count"] = len(cases)
    results["passed_case_count"] = sum(bool(item.get("passed")) for item in cases)
    results["failed_case_count"] = len(cases) - int(results["passed_case_count"])
    if results.get("environment_blockers"):
        results["status"] = "blocked_environment"
    elif results["failed_case_count"]:
        results["status"] = "fail"
    else:
        results["status"] = "pass"
    atomic_write_json(output_dir / "ui_qa_results.json", results)
    atomic_write_text(output_dir / "ui_qa_report.md", render_markdown(results))


def dependency_error(exc: Exception) -> str:
    return (
        f"Python Playwright is unavailable: {exc}. Install the Python package `playwright`; "
        "a browser download is not required when --chrome points to the system Chrome executable."
    )


def run_audit(args: argparse.Namespace) -> int:
    preview_url = args.url if args.url.endswith("/") else args.url + "/"
    data_url = args.data_url or urljoin(preview_url, "data/dashboard_data.json")
    output_dir = Path(args.output_dir).resolve()
    chrome_path = Path(args.chrome).resolve()
    timeout_seconds = max(args.timeout_ms / 1000.0, 1.0)
    results: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "generated_at": iso_now(),
        "preview_url": preview_url,
        "data_url": data_url,
        "chrome_path": str(chrome_path),
        "python_version": platform.python_version(),
        "expected_dashboard_schema": EXPECTED_SCHEMA,
        "environment_blockers": [],
        "cases": [],
        "research_boundary": "browser-only QA; no research data, status, threshold, or gate mutation",
    }

    if not chrome_path.is_file():
        results["environment_blockers"].append(f"system Chrome not found: {chrome_path}")

    try:
        preview_status, _, _ = fetch_bytes(preview_url, timeout_seconds)
        results["preview_status"] = preview_status
        if not 200 <= preview_status < 400:
            results["environment_blockers"].append(f"preview returned HTTP {preview_status}")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        results["environment_blockers"].append(f"preview is unavailable: {exc}")

    base_payload: dict[str, Any] = {}
    try:
        data_status, base_payload = fetch_json(data_url, timeout_seconds)
        results["data_status"] = data_status
        results["baseline_schema"] = base_payload.get("schema_version")
        if not 200 <= data_status < 400:
            results["environment_blockers"].append(f"dashboard data returned HTTP {data_status}")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        results["environment_blockers"].append(f"dashboard data is unavailable or invalid: {exc}")

    sync_playwright: Callable[[], Any] | None = None
    try:
        from playwright.sync_api import sync_playwright as imported_sync_playwright

        sync_playwright = imported_sync_playwright
        results["playwright_available"] = True
    except (ImportError, ModuleNotFoundError) as exc:
        results["playwright_available"] = False
        results["environment_blockers"].append(dependency_error(exc))

    if results["environment_blockers"]:
        write_results(output_dir, results)
        print(f"status={results['status']}")
        print(f"results={relative_path(output_dir / 'ui_qa_results.json')}")
        for blocker in results["environment_blockers"]:
            print(f"blocker={blocker}")
        return 2

    assert sync_playwright is not None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(chrome_path),
                headless=not args.headed,
                args=["--disable-background-networking", "--disable-component-update"],
            )
            try:
                for scenario in SCENARIOS:
                    for viewport in VIEWPORTS:
                        print(f"capture={scenario.name}/{viewport.name}")
                        results["cases"].append(
                            capture_case(
                                browser,
                                preview_url,
                                data_url,
                                output_dir,
                                base_payload,
                                scenario,
                                viewport,
                                args.timeout_ms,
                            )
                        )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - launch/runtime failures are environment evidence
        results["environment_blockers"].append(f"Playwright could not launch system Chrome: {exc}")
        results["runtime_traceback"] = traceback.format_exc()
        write_results(output_dir, results)
        print(f"status={results['status']}")
        print(f"results={relative_path(output_dir / 'ui_qa_results.json')}")
        print(f"blocker={results['environment_blockers'][-1]}")
        return 2

    write_results(output_dir, results)
    print(f"status={results['status']}")
    print(f"cases={results['passed_case_count']}/{results['case_count']}")
    print(f"results={relative_path(output_dir / 'ui_qa_results.json')}")
    print(f"report={relative_path(output_dir / 'ui_qa_report.md')}")
    return 0 if results["status"] == "pass" else 1


def self_check() -> None:
    base = {
        "schema_version": "dashboard-data-v1",
        "generated_at": "2026-07-18T15:30:00+08:00",
        "decision_as_of_date": "2026-07-18",
        "data_quality_warnings": [],
        "summaries": {"current": {"as_of_date": "2026-07-18"}},
        "top_candidates": {"valuation": [{"PE_TTM": 10.0, "PB": 1.0}]},
        "valuation_snapshot": {"candidate_count": 1, "candidates": [{"industry_code": "801010"}]},
        "current_recommendation": {
            "action": "BUY_CANDIDATE",
            "candidates": [{"action": "BUY_CANDIDATE"}],
            "human_confirmation_required": True,
            "auto_execution_allowed": True,
        },
    }
    for scenario in [item.name for item in SCENARIOS if item.intercept]:
        variant = build_variant(base, scenario)
        if scenario == "contract_error":
            assert variant["schema_version"] != EXPECTED_SCHEMA
            expected = copy.deepcopy(base)
            expected["schema_version"] = "dashboard-data-unsupported-ui-qa"
            assert variant == expected
            continue
        assert variant["schema_version"] == EXPECTED_SCHEMA
        assert variant["current_recommendation"]["action"] == "NO_ACTION"
        assert variant["current_recommendation"]["candidates"] == []
        assert variant["current_recommendation"]["auto_execution_allowed"] is False
        assert variant["trust_summary"]["auto_execution_allowed"] is False
        assert all(isinstance(item, dict) for item in variant["fixed_notices"])
        assert all(isinstance(item, dict) for item in variant["data_quality_warnings"])
    stale = build_variant(base, "stale")
    assert any(item.get("status") == "stale_optional" for item in stale["source_freshness"])
    long_text = build_variant(base, "long_text_focus")
    assert any(item.get("message") == LONG_TEXT_SENTINEL for item in long_text["data_quality_warnings"])
    empty = build_variant(base, "empty_candidates")
    assert empty["valuation_snapshot"]["candidate_count"] == 0
    assert empty["valuation_snapshot"]["candidates"] == []
    notices_text = "\n".join(item["text"] for item in build_variant(base, "warning")["fixed_notices"])
    assert fixed_notice_check(notices_text) == (True, [])
    assert classify_link(DEFAULT_URL, "/data/dashboard_data.json")[0] == "same_origin"
    assert classify_link(DEFAULT_URL, "https://www.tradingview.com/")[0] == "external"
    assert current_buy_copy("NO_ACTION，暂不买入") == []
    assert current_buy_copy("BUY_CANDIDATE") == ["BUY_CANDIDATE"]
    print("self_check=pass")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay Dashboard trust states with system Chrome and Playwright.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Already-running Vite preview URL.")
    parser.add_argument("--data-url", default="", help="Dashboard JSON URL; defaults to <url>/data/dashboard_data.json.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT), help="Directory for screenshots and QA artifacts.")
    parser.add_argument("--chrome", default=str(DEFAULT_CHROME), help="System Chrome executable path.")
    parser.add_argument("--timeout-ms", type=int, default=20_000, help="Navigation and assertion timeout in milliseconds.")
    parser.add_argument("--headed", action="store_true", help="Show Chrome while capturing the matrix.")
    parser.add_argument("--self-check", action="store_true", help="Run deterministic fixture checks without a browser.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        self_check()
        return 0
    return run_audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
