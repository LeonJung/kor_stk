"""Integration smoke tests — verify all demo scripts can be imported and run.

Each test runs the demo's ``main`` (or driver) end-to-end without external
dependencies (KIS API, network). Network-bound demos (daily_watchlist_refresh
when KIS keys present) fall back to local data.

These tests catch regressions caused by Strategy/Allocator/event refactors
that break the demos people actually run.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

# Allow running examples by manipulating sys.path (mirror PYTHONPATH=src usage)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _import_example(name: str):
    return importlib.import_module(f"examples.{name}")


# Per the project layout, examples live at /examples not under src/.
@pytest.fixture(autouse=True)
def _examples_on_path():
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    yield


def test_pair_follow_scenario_runs(capsys):
    mod = _import_example("pair_follow_scenario")
    mod.main()
    out = capsys.readouterr().out
    assert "PnL" in out or "pair_follow" in out


def test_full_portfolio_backtest_runs(capsys, monkeypatch):
    # Run from project root so configs/sample_portfolio.yaml resolves
    monkeypatch.chdir(_PROJECT_ROOT)
    mod = _import_example("full_portfolio_backtest")
    rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Per-strategy PnL" in out
    assert "TOTAL" in out


def test_daily_watchlist_refresh_runs(capsys, monkeypatch, tmp_path):
    """Demo should run even when bar_store is essentially empty (falls back to
    THEME_OF universe, then yields no pairs — graceful)."""
    monkeypatch.chdir(_PROJECT_ROOT)
    monkeypatch.setenv("KS_WS_DATA_DIR", str(tmp_path))
    mod = _import_example("daily_watchlist_refresh")
    rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Watchlist" in out


def test_strategy_pnl_report_missing_file(capsys):
    """Reports gracefully when ledger doesn't exist."""
    mod = _import_example("strategy_pnl_report")
    rc = mod.main(["/nonexistent/ledger.sqlite"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ledger not found" in err


def test_strategy_pnl_report_with_real_ledger(tmp_path, capsys):
    """Run the report against a freshly-built ledger (via portfolio backtest)
    pickled into a temp ledger.sqlite path."""
    from datetime import UTC, datetime

    from ks_ws.domain import OrderIntent, Side
    from ks_ws.orders import SubmittedOrder
    from ks_ws.storage.ledger import Ledger

    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    now = datetime.now(UTC)
    for i, (strategy, sell_price) in enumerate([("alpha", 11000), ("alpha", 10500), ("beta", 9500)]):
        buy = OrderIntent(
            symbol="X", side=Side.BUY, quantity=10, timestamp=now, sources=(strategy,)
        )
        sell = OrderIntent(
            symbol="X", side=Side.SELL, quantity=10, timestamp=now, sources=(strategy,)
        )
        bo = SubmittedOrder(order_id=f"b{i}", intent=buy, submitted_at=now)
        so = SubmittedOrder(order_id=f"s{i}", intent=sell, submitted_at=now)
        ledger.record_order(bo)
        ledger.apply_fill(order_id=bo.order_id, symbol="X", side=Side.BUY, quantity=10, price=10000)
        ledger.record_order(so)
        ledger.apply_fill(order_id=so.order_id, symbol="X", side=Side.SELL, quantity=10, price=sell_price)
    ledger.close()

    mod = _import_example("strategy_pnl_report")
    rc = mod.main([str(ledger_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "TOTAL" in out
