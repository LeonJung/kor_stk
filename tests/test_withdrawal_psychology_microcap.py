"""Tests for DailyWithdrawalRule + PsychologyGuard + MicroCapStrategy."""

from datetime import UTC, date, datetime, timedelta

import pytest

from ks_ws.domain import OrderIntent, Side, Tick
from ks_ws.events import OrderbookImbalance, VolumeSpike
from ks_ws.psychology import PsychologyGuard
from ks_ws.strategies.microcap import MicroCapStrategy
from ks_ws.withdrawal import DailyWithdrawalRule


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# DailyWithdrawalRule -------------------------------------------------------


def test_withdrawal_validation():
    with pytest.raises(ValueError):
        DailyWithdrawalRule(seed_cap_krw=0)


def test_settle_pnl_only():
    rule = DailyWithdrawalRule(seed_cap_krw=100_000_000)
    entry = rule.settle(
        settlement_date=date(2026, 5, 11),
        daily_pnl_krw=500_000,
        current_seed_krw=100_000_000,  # at cap
    )
    assert entry.pnl_krw == 500_000
    assert entry.excess_seed_krw == 0
    assert entry.total_withdrawn_krw == 500_000
    assert entry.seed_after_krw == 99_500_000


def test_settle_loss_no_withdrawal():
    rule = DailyWithdrawalRule(seed_cap_krw=100_000_000)
    entry = rule.settle(
        settlement_date=date(2026, 5, 11),
        daily_pnl_krw=-200_000,
        current_seed_krw=99_800_000,
    )
    assert entry.pnl_krw == 0
    assert entry.excess_seed_krw == 0
    assert entry.seed_after_krw == 99_800_000


def test_settle_excess_seed_above_cap():
    rule = DailyWithdrawalRule(seed_cap_krw=100_000_000)
    entry = rule.settle(
        settlement_date=date(2026, 5, 11),
        daily_pnl_krw=200_000,
        current_seed_krw=110_200_000,  # 10.2M above cap
    )
    # PnL withdraw 200k → seed 110M → excess 10M → final 100M
    assert entry.pnl_krw == 200_000
    assert entry.excess_seed_krw == 10_000_000
    assert entry.total_withdrawn_krw == 10_200_000
    assert entry.seed_after_krw == 100_000_000


def test_history_accumulates():
    rule = DailyWithdrawalRule()
    rule.settle(settlement_date=date(2026, 5, 11), daily_pnl_krw=100_000, current_seed_krw=100_000_000)
    rule.settle(settlement_date=date(2026, 5, 12), daily_pnl_krw=200_000, current_seed_krw=100_000_000)
    assert rule.total_pnl_withdrawn_krw == 300_000
    assert rule.latest().settlement_date == date(2026, 5, 12)


# PsychologyGuard ---------------------------------------------------------


def _intent(symbol: str = "X", side: Side = Side.BUY, qty: int = 10, ts=None) -> OrderIntent:
    return OrderIntent(symbol=symbol, side=side, quantity=qty, timestamp=ts or _ts(0))


def test_psychology_validation():
    with pytest.raises(ValueError):
        PsychologyGuard(revenge_window=timedelta(0))
    with pytest.raises(ValueError):
        PsychologyGuard(surge_threshold=1)


def test_no_history_allows_buy():
    g = PsychologyGuard()
    decision = g.check(_intent("X", Side.BUY))
    assert decision.allow


def test_revenge_block_after_loss():
    g = PsychologyGuard(revenge_block_duration=timedelta(minutes=30))
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    # Try to buy 5 minutes later
    decision = g.check(_intent("X", Side.BUY, ts=_ts(300)))
    assert not decision.allow
    assert "revenge" in decision.reason


def test_revenge_block_expires():
    g = PsychologyGuard(revenge_block_duration=timedelta(minutes=10))
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    decision = g.check(_intent("X", Side.BUY, ts=_ts(601)))  # 10 min + 1 sec
    assert decision.allow


def test_winning_sell_doesnt_block():
    g = PsychologyGuard()
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=+100_000, when=_ts(0))
    assert g.check(_intent("X", Side.BUY, ts=_ts(60))).allow


def test_revenge_block_per_symbol():
    g = PsychologyGuard()
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    # Other symbol — not blocked
    assert g.check(_intent("Y", Side.BUY, ts=_ts(60))).allow


def test_surge_blocks_when_too_many_fills():
    g = PsychologyGuard(surge_threshold=3, surge_window=timedelta(minutes=5))
    for i in range(3):
        g.record_fill(symbol=f"S{i}", side=Side.BUY, pnl_krw=0, when=_ts(i * 30))
    decision = g.check(_intent("Z", Side.BUY, ts=_ts(120)))
    assert not decision.allow
    assert "surge" in decision.reason


def test_apply_returns_none_when_blocked():
    g = PsychologyGuard()
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    out = g.apply(_intent("X", Side.BUY, ts=_ts(60)))
    assert out is None


def test_sell_intents_pass_through():
    g = PsychologyGuard()
    g.record_fill(symbol="X", side=Side.SELL, pnl_krw=-100_000, when=_ts(0))
    out = g.apply(_intent("X", Side.SELL, ts=_ts(60)))
    assert out is not None  # selling allowed even after loss


# MicroCapStrategy --------------------------------------------------------


def test_microcap_validation():
    with pytest.raises(ValueError):
        MicroCapStrategy(watchlist=set())
    with pytest.raises(ValueError):
        MicroCapStrategy(watchlist={"X"}, take_profit_pct=0)
    with pytest.raises(ValueError, match="strict stop"):
        MicroCapStrategy(watchlist={"X"}, stop_loss_pct=4.0)


def test_microcap_entry_on_volume_spike():
    s = MicroCapStrategy(watchlist={"X"})
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=10000, volume=10))
    sigs = s.on_event(VolumeSpike(symbol="X", timestamp=_ts(1), multiplier=4.0, window_seconds=60))
    assert len(sigs) == 1
    assert sigs[0].side == Side.BUY


def test_microcap_entry_on_orderbook_imbalance():
    s = MicroCapStrategy(watchlist={"X"})
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=10000, volume=10))
    sigs = s.on_event(OrderbookImbalance(symbol="X", timestamp=_ts(1), bid_to_ask_ratio=3.0, levels_used=5))
    assert len(sigs) == 1


def test_microcap_no_entry_outside_watchlist():
    s = MicroCapStrategy(watchlist={"X"})
    s.on_tick(Tick(symbol="Y", timestamp=_ts(0), price=10000, volume=10))
    sigs = s.on_event(VolumeSpike(symbol="Y", timestamp=_ts(1), multiplier=4.0, window_seconds=60))
    assert sigs == []


def test_microcap_take_profit():
    s = MicroCapStrategy(watchlist={"X"}, take_profit_pct=5.0)
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=10000, volume=10))
    s.on_event(VolumeSpike(symbol="X", timestamp=_ts(1), multiplier=4.0, window_seconds=60))
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(60), price=10500, volume=10))
    assert sigs and sigs[0].side == Side.SELL
    assert "take-profit" in sigs[0].note


def test_microcap_strict_stop_loss():
    s = MicroCapStrategy(watchlist={"X"}, stop_loss_pct=1.5)
    s.on_tick(Tick(symbol="X", timestamp=_ts(0), price=10000, volume=10))
    s.on_event(VolumeSpike(symbol="X", timestamp=_ts(1), multiplier=4.0, window_seconds=60))
    sigs = s.on_tick(Tick(symbol="X", timestamp=_ts(60), price=9849, volume=10))  # -1.51%
    assert sigs and sigs[0].side == Side.SELL
    assert sigs[0].urgency == "high"
