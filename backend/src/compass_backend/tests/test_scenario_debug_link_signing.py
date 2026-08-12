from __future__ import annotations

import hmac
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from pydantic import SecretStr

from nctqai.routes.compass.scenarios import _launch_url


def test_dashboard_launch_url_signs_non_staging_scenario_debug_links() -> None:
    settings = SimpleNamespace(
        compass_frontend_url="https://compass.example.org",
        compass_scenario_link_secret=SecretStr("shared-secret"),
        compass_scenario_link_ttl_seconds=300,
    )

    url = _launch_url(42, settings=settings, now=1_700_000_000)

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    exp = "1700000300"
    expected_sig = hmac.new(
        b"shared-secret",
        f"42.{exp}".encode(),
        sha256,
    ).hexdigest()
    assert parsed.scheme == "https"
    assert params["debug"] == ["true"]
    assert params["case_id"] == ["42"]
    assert params["case_exp"] == [exp]
    assert params["case_sig"] == [expected_sig]


def test_dashboard_launch_url_can_remain_unsigned_for_local_dev() -> None:
    settings = SimpleNamespace(
        compass_frontend_url="http://localhost:3000",
        compass_scenario_link_secret=SecretStr(""),
        compass_scenario_link_ttl_seconds=300,
    )

    params = parse_qs(urlparse(_launch_url(42, settings=settings)).query)

    assert params == {"case_id": ["42"], "debug": ["true"]}


def test_dashboard_launch_url_uses_durable_unsigned_staging_links() -> None:
    settings = SimpleNamespace(
        compass_frontend_url="https://staging-compass.nctq.ai",
        compass_scenario_link_secret=SecretStr("shared-secret"),
        compass_scenario_link_ttl_seconds=300,
    )

    params = parse_qs(urlparse(_launch_url(42, settings=settings)).query)

    assert params == {"case_id": ["42"], "debug": ["true"]}
