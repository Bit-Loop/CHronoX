"""
ChronoX Finance — Research-Backed ADHD-Friendly Self-Updating Auto-Budgeting
& Grounded OpenCode Go AI Financial Coach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

__version__ = "1.0.0"


def get_safe_trading_capital(db_path: Optional[str] = None) -> float:
    """
    ChronoX Trading Guardrail ("Don't Trade Your Rent").

    Returns the exact dollar amount currently available in the protected
    `Invest / ChronoX Vault` AFTER upcoming bills, daily living reserves,
    and the emergency buffer floor are 100% funded.
    """
    from chronox_finance.service import FinanceService

    resolved = db_path or ".chronox/finance.sqlite3"
    if not Path(resolved).exists():
        return 0.0
    service = FinanceService(db_path=resolved)
    try:
        snapshot = service.get_dashboard()
        return round(snapshot.safe_to_invest_cents / 100.0, 2)
    finally:
        service.close()
