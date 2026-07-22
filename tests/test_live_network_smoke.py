from __future__ import annotations

import requests
import pytest


@pytest.mark.live
def test_sse_etf_catalog_endpoint_is_reachable() -> None:
    """显式联网冒烟；默认离线测试通过 pyproject 的 marker 过滤不执行。"""
    response = requests.get(
        "https://query.sse.com.cn/commonQuery.do",
        params={"sqlId": "COMMON_JJZWZ_JJLB_L", "CATEGORY": "F110", "type": "inParams"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://etf.sse.com.cn/fundlist/"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload.get("result"), list)
