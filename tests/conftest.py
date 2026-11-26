"""Global pytest configuration for hermetic ChronoX Finance test execution."""

from __future__ import annotations

from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_local_ai_clis_and_bg_tasks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    1. Keep tests hermetic from ambient host-installed AI CLIs (`claude`, `opencode`,
       `codex`, `agy`) by defaulting `CHRONOX_LOCAL_CLI_AUTO_DETECT=0` during pytest runs.
       Tests that explicitly verify local-CLI auto-detection override this via
       `monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "1")`.
    2. Guarantee any background Plaid poller/retry timers spawned by tests that omit
       `svc.close()` are halted cleanly at test teardown so zero timers leak across tests.
    """
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "0")
    yield
    from chronox_finance.service import FinanceService

    for svc in list(FinanceService._live_instances):
        svc._stop_plaid_background_tasks()
