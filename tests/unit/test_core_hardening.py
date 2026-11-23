"""Security and correctness tests for refactored ChronoX core modules."""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

from config.config_manager import ConfigManager, expand_env_vars, redact_secrets
from data.ingestion.polygon.client import PolygonAPIError, PolygonClient, _parse_retry_after


class TestConfigManagerHardening:
    def test_expand_env_vars_supports_defaults(self, monkeypatch):
        monkeypatch.setenv("TEST_HOST_VAR", "db.internal")
        monkeypatch.delenv("TEST_MISSING_VAR", raising=False)
        data = {
            "host": "${TEST_HOST_VAR:-localhost}",
            "fallback": "${TEST_MISSING_VAR:-fallback_val}",
            "empty": "${TEST_MISSING_VAR}",
        }
        expanded = expand_env_vars(data)
        assert expanded["host"] == "db.internal"
        assert expanded["fallback"] == "fallback_val"
        assert expanded["empty"] == ""

    def test_load_default_yaml_resolves_env_and_redacts_on_save(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-opencode-super-secret-123456")
        monkeypatch.setenv("POLYGON_API_KEY", "poly-secret-999999")
        monkeypatch.setenv("TIMESCALE_PASSWORD", "super_secure_pw")

        cfg = ConfigManager("config/default.yaml")
        assert cfg.opencode_go.api_key == "sk-opencode-super-secret-123456"
        assert cfg.opencode_go.base_url == "https://opencode.ai/zen/go/v1"
        assert cfg.polygon.api_key == "poly-secret-999999"
        assert cfg.timescale.port == 5433
        assert cfg.timescale.password == "super_secure_pw"

        # Save must redact secrets
        out_path = tmp_path / "exported.yaml"
        cfg.save(str(out_path))
        saved_text = out_path.read_text(encoding="utf-8")
        assert "sk-opencode-super-secret-123456" not in saved_text
        assert "poly-secret-999999" not in saved_text
        assert "super_secure_pw" not in saved_text

    def test_redact_secrets_masks_nested_keys(self):
        masked = redact_secrets({"nested": {"api_key": "abcdefghijklmnop", "safe": "ok"}})
        assert masked["nested"]["safe"] == "ok"
        assert masked["nested"]["api_key"].startswith("***")
        assert "abcdef" not in masked["nested"]["api_key"]


class TestPolygonClientHardening:
    @patch("data.ingestion.polygon.client.requests.Session.get")
    def test_api_key_not_leaked_in_query_params(self, mock_get):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"status": "OK", "results": [{"ticker": "AAPL"}]}
        mock_get.return_value = resp

        client = PolygonClient("secret_poly_key", rate_limit_calls=100, rate_limit_period=1)
        data = client._get("/v3/reference/tickers", {"limit": 1, "apiKey": "should_strip"})
        assert data["status"] == "OK"
        _, kwargs = mock_get.call_args
        assert "apiKey" not in kwargs["params"]
        assert client.session.headers["Authorization"] == "Bearer secret_poly_key"

    @patch("data.ingestion.polygon.client.time.sleep", return_value=None)
    @patch("data.ingestion.polygon.client.requests.Session.get")
    def test_bounded_429_retry_raises_cleanly(self, mock_get, _mock_sleep):
        resp = Mock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "1"}
        mock_get.return_value = resp

        client = PolygonClient("secret_key", max_rate_limit_retries=2, rate_limit_calls=100)
        with pytest.raises(PolygonAPIError, match="rate limit exceeded"):
            client._get("/v3/reference/tickers")
        assert mock_get.call_count == 3

    def test_parse_retry_after_handles_garbage_safely(self):
        assert _parse_retry_after("10") == 10
        assert _parse_retry_after("99999", max_wait=60) == 60
        assert _parse_retry_after("not-a-date", default=7) == 7

    @patch("data.ingestion.polygon.client.requests.Session.get")
    def test_pagination_parses_encoded_cursor(self, mock_get):
        r1 = Mock(status_code=200)
        r1.json.return_value = {
            "status": "OK",
            "results": [{"id": 1}],
            "next_url": "https://api.polygon.io/v3/test?cursor=abc%3D123&limit=1",
        }
        r2 = Mock(status_code=200)
        r2.json.return_value = {"status": "OK", "results": [{"id": 2}]}
        mock_get.side_effect = [r1, r2]

        client = PolygonClient("secret_key", rate_limit_calls=100)
        items = client.get_with_pagination("/v3/test")
        assert [i["id"] for i in items] == [1, 2]
        assert mock_get.call_args_list[1].kwargs["params"]["cursor"] == "abc=123"


class TestTimescaleWriterSQLInjectionDefense:
    def test_validate_timeframe_blocks_sql_injection(self):
        # Import only classmethod without needing live PostgreSQL
        pytest.importorskip("psycopg2")
        from data.storage.timescale_writer import TimescaleDBError, TimescaleWriter

        assert TimescaleWriter._validate_timeframe("15min") == "15min"
        assert TimescaleWriter._validate_timeframe("DAILY") == "daily"

        for bad in ("1min; DROP TABLE market_data;--", "1min' OR '1'='1", "../etc", ""):
            with pytest.raises(ValueError):
                TimescaleWriter._validate_timeframe(bad)

        # Must require password (no hardcoded fallback!)
        with patch.dict(os.environ, {"TIMESCALE_PASSWORD": ""}, clear=False):
            with pytest.raises(TimescaleDBError, match="password is required"):
                TimescaleWriter(password="")
