from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_industry_index_research_validation as runner


def response_text(*, code: str = "801156", close: object = 1234.5) -> str:
    return (
        '{"code":"200","message":"ok","data":['
        f'{{"swindexcode":"{code}","bargaindate":"2026-07-21",'
        f'"openindex":1200.0,"maxindex":1250.0,"minindex":1190.0,"closeindex":{json.dumps(close)},'
        '"hike":1.0,"markup":NaN,"bargainamount":2.0,"bargainsum":3.0}]}'
    )


def test_raw_parser_accepts_only_observed_nan_and_preserves_official_history() -> None:
    frame = runner.parse_sws_history_response(response_text(), "801156")

    assert frame.columns.tolist() == ["代码", "日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]
    assert frame.loc[0, "代码"] == "801156"
    assert frame.loc[0, "日期"].isoformat() == "2026-07-21"
    assert frame.loc[0, "收盘"] == pytest.approx(1234.5)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"code":"500","data":[]}', "status is not successful"),
        ('{"code":"200","data":[]}', "data is empty or invalid"),
        (response_text(code="801157"), "code mismatch"),
        (response_text().replace(',"bargainsum":3.0', ""), "missing fields"),
        (response_text().replace("NaN", "Infinity"), "unsupported non-finite JSON token"),
    ],
)
def test_raw_parser_fails_closed_on_status_schema_identity_and_unknown_constants(
    text: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        runner.parse_sws_history_response(text, "801156")


@pytest.mark.parametrize(
    "field",
    ["swindexcode", "bargaindate", "openindex", "maxindex", "minindex", "closeindex", "bargainamount", "bargainsum"],
)
def test_raw_parser_rejects_nan_outside_unused_markup(field: str) -> None:
    text = response_text()
    if field in {"swindexcode", "bargaindate"}:
        current = '"801156"' if field == "swindexcode" else '"2026-07-21"'
    else:
        current = {
            "openindex": "1200.0",
            "maxindex": "1250.0",
            "minindex": "1190.0",
            "closeindex": "1234.5",
            "bargainamount": "2.0",
            "bargainsum": "3.0",
        }[field]
    text = text.replace(f'"{field}":{current}', f'"{field}":NaN')

    with pytest.raises(ValueError, match="allowed only in markup"):
        runner.parse_sws_history_response(text, "801156")


def test_raw_parser_rejects_null_required_value_and_bad_date() -> None:
    with pytest.raises(ValueError, match="null required fields: closeindex"):
        runner.parse_sws_history_response(
            response_text().replace('"closeindex":1234.5', '"closeindex":null'),
            "801156",
        )
    with pytest.raises(ValueError, match="invalid ISO date: bargaindate"):
        runner.parse_sws_history_response(
            response_text().replace("2026-07-21", "not-a-date"),
            "801156",
        )


@pytest.mark.parametrize(
    "replacement",
    [
        '"bargaindate":20260721',
        '"bargaindate":"2026-07-21T00:00:00"',
        '"bargaindate":"2026-7-21"',
    ],
)
def test_raw_parser_requires_exact_iso_date_string(replacement: str) -> None:
    text = response_text().replace('"bargaindate":"2026-07-21"', replacement)

    with pytest.raises(ValueError, match="invalid ISO date: bargaindate"):
        runner.parse_sws_history_response(text, "801156")


@pytest.mark.parametrize(
    ("field", "current"),
    [
        ("openindex", "1200.0"),
        ("maxindex", "1250.0"),
        ("minindex", "1190.0"),
        ("closeindex", "1234.5"),
        ("bargainamount", "2.0"),
        ("bargainsum", "3.0"),
    ],
)
def test_raw_parser_rejects_boolean_numeric_fields(field: str, current: str) -> None:
    text = response_text().replace(f'"{field}":{current}', f'"{field}":true')

    with pytest.raises(ValueError, match=f"invalid numeric field: {field}"):
        runner.parse_sws_history_response(text, "801156")


@pytest.mark.parametrize(
    ("field", "current", "replacement"),
    [
        ("openindex", "1200.0", "0"),
        ("maxindex", "1250.0", "-1"),
        ("minindex", "1190.0", "0"),
        ("closeindex", "1234.5", "-1"),
    ],
)
def test_raw_parser_requires_positive_prices(
    field: str,
    current: str,
    replacement: str,
) -> None:
    text = response_text().replace(f'"{field}":{current}', f'"{field}":{replacement}')

    with pytest.raises(ValueError, match=f"non-positive price field: {field}"):
        runner.parse_sws_history_response(text, "801156")


@pytest.mark.parametrize(
    ("field", "current"),
    [("bargainamount", "2.0"), ("bargainsum", "3.0")],
)
def test_raw_parser_requires_nonnegative_volume_and_amount(
    field: str,
    current: str,
) -> None:
    text = response_text().replace(f'"{field}":{current}', f'"{field}":-1')

    with pytest.raises(ValueError, match=f"negative volume/amount field: {field}"):
        runner.parse_sws_history_response(text, "801156")


def test_raw_parser_rejects_finite_overflow_and_numeric_nan_string() -> None:
    with pytest.raises(ValueError, match="invalid numeric field: closeindex"):
        runner.parse_sws_history_response(
            response_text().replace('"closeindex":1234.5', '"closeindex":1e999'),
            "801156",
        )
    with pytest.raises(ValueError, match="invalid numeric field: closeindex"):
        runner.parse_sws_history_response(
            response_text().replace('"closeindex":1234.5', '"closeindex":"NaN"'),
            "801156",
        )


def test_raw_fallback_keeps_default_tls_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class FakeResponse:
        text = response_text()

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        observed["url"] = url
        observed.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    frame = runner.fetch_industry_history_from_sws_raw("801156")

    assert len(frame) == 1
    assert observed["params"] == {"swindexcode": "801156", "period": "DAY"}
    assert observed["timeout"] == 30
    assert "verify" not in observed


def test_fetcher_uses_raw_official_fallback_only_for_json_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = pd.DataFrame({"代码": ["801156"], "日期": ["2026-07-21"], "收盘": [1234.5]})

    def broken_fetch(*, symbol: str, period: str) -> pd.DataFrame:
        assert (symbol, period) == ("801156", "day")
        raise json.JSONDecodeError("invalid constant", "NaN", 0)

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_hist_sw=broken_fetch))
    monkeypatch.setattr(runner, "fetch_industry_history_from_sws_raw", lambda code: expected if code == "801156" else pd.DataFrame())

    assert runner.fetch_industry_history("801156").equals(expected)


def test_fetcher_accepts_requests_simplejson_decode_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = pd.DataFrame({"代码": ["801156"], "日期": ["2026-07-21"], "收盘": [1234.5]})

    def broken_fetch(*, symbol: str, period: str) -> pd.DataFrame:
        raise RequestsJSONDecodeError("invalid constant", "NaN", 0)

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_hist_sw=broken_fetch))
    monkeypatch.setattr(runner, "fetch_industry_history_from_sws_raw", lambda _code: expected)

    assert runner.fetch_industry_history("801156").equals(expected)


def test_same_named_non_json_exception_does_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JSONDecodeError(Exception):
        pass

    def failed_fetch(*, symbol: str, period: str) -> pd.DataFrame:
        raise JSONDecodeError(f"{symbol}:{period}")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_hist_sw=failed_fetch))
    monkeypatch.setattr(
        runner,
        "fetch_industry_history_from_sws_raw",
        lambda _code: pytest.fail("same-named exceptions must not trigger the raw fallback"),
    )

    with pytest.raises(JSONDecodeError, match="801156:day"):
        runner.fetch_industry_history("801156")


def test_fetcher_does_not_mask_non_json_source_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_fetch(*, symbol: str, period: str) -> pd.DataFrame:
        raise TimeoutError(f"{symbol}:{period}")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_hist_sw=failed_fetch))
    monkeypatch.setattr(
        runner,
        "fetch_industry_history_from_sws_raw",
        lambda _code: pytest.fail("raw fallback must not run for a transport failure"),
    )

    with pytest.raises(TimeoutError, match="801156:day"):
        runner.fetch_industry_history("801156")
