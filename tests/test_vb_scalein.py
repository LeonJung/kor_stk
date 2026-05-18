"""VBScaleInStrategy — vb 기반 분할매수/매도 응용."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ks_ws.domain import Side, Tick
from ks_ws.strategies.vb_scalein import (
    DEFAULT_ENTRY_PLAN,
    DEFAULT_EXIT_PLAN,
    VBScaleInStrategy,
)

_KST_HOUR = 0  # UTC 00:00 = KST 09:00


def _tick(price: int, *, ts_offset_min: int = 0,
          sym: str = "005930") -> Tick:
    base = datetime(2026, 5, 13, _KST_HOUR, 0, tzinfo=UTC)
    return Tick(
        symbol=sym, price=price, volume=100,
        timestamp=base + timedelta(minutes=ts_offset_min),
    )


# day open 100 + range 200 (high=300 low=100) → trigger = 100 + 0.5*200 = 200
# 이 setup 으로 E2 = 201, E3 = 202, TP1 = 204, TP2 = 206, SL = 197 - 분해능 OK.

def test_e1_entry_emits_half_size() -> None:
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    sigs = s.on_tick(_tick(200, ts_offset_min=1))
    assert len(sigs) == 1
    assert sigs[0].side is Side.BUY
    assert sigs[0].confidence == pytest.approx(0.50)
    assert "E1" in sigs[0].note


def test_e2_e3_pyramid_emits_decreasing_fracs() -> None:
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1 at 200
    sigs_e2 = s.on_tick(_tick(201, ts_offset_min=2))  # E2 trigger=201
    assert sigs_e2 and sigs_e2[0].side is Side.BUY
    assert sigs_e2[0].confidence == pytest.approx(0.30)
    sigs_e3 = s.on_tick(_tick(202, ts_offset_min=3))  # E3 trigger=202
    assert sigs_e3 and sigs_e3[0].confidence == pytest.approx(0.20)
    pos = s.open_positions()["005930"]
    assert pos.buy_levels_hit == 3
    assert pos.trail_armed is True
    assert pos.cum_bought == pytest.approx(1.0)


def test_tp1_partial_exit_and_sl_to_be() -> None:
    """한 tick = 한 액션 만. TP1 cross 시 SELL 만 emit, E2/E3 는 다음 tick."""
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1 at 200
    # Jump to 204 — TP1 우선, BUY 는 skip
    sigs = s.on_tick(_tick(204, ts_offset_min=2))
    assert len(sigs) == 1
    assert sigs[0].side is Side.SELL
    assert sigs[0].confidence == pytest.approx(1.0 / 3)
    pos = s.open_positions()["005930"]
    assert pos.sl_at_be is True
    assert pos.sell_levels_hit == 1


def test_one_action_per_tick_priority() -> None:
    """SL > timeout > TP > trail > E_n. 우선순위 검증."""
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1
    # tick 201 = E2 trigger only (no TP)
    sigs1 = s.on_tick(_tick(201, ts_offset_min=2))
    assert len(sigs1) == 1 and sigs1[0].side is Side.BUY
    # tick 204 = TP1 + E3 모두 cross 가능. 새 룰: TP1 만 emit (SELL 우선)
    sigs2 = s.on_tick(_tick(204, ts_offset_min=3))
    assert len(sigs2) == 1 and sigs2[0].side is Side.SELL


def test_sl_exits_all_qty() -> None:
    s = VBScaleInStrategy(
        prev_high_low={"005930": (300, 100)}, k=0.5, stop_loss_pct=1.5,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1 at 200
    # SL = 200 * 0.985 = 197 → tick 197 cross
    sigs = s.on_tick(_tick(197, ts_offset_min=2))
    sells = [x for x in sigs if x.side is Side.SELL]
    assert sells
    assert sells[0].urgency == "high"
    assert "005930" not in s.open_positions()


def test_trailing_exit_after_all_tps_hit() -> None:
    """단계별 tick 으로 full pyramid 후 TP1/TP2 hit → 잔여 trail 청산."""
    s = VBScaleInStrategy(
        prev_high_low={"005930": (300, 100)}, k=0.5, trailing_pct=1.0,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1
    s.on_tick(_tick(201, ts_offset_min=2))  # E2
    s.on_tick(_tick(202, ts_offset_min=3))  # E3 → trail armed
    pos = s.open_positions()["005930"]
    assert pos.trail_armed is True
    s.on_tick(_tick(204, ts_offset_min=4))  # TP1 SELL (qty 1/3)
    s.on_tick(_tick(206, ts_offset_min=5))  # TP2 SELL (qty 1/3, 잔여 1/3)
    pos = s.open_positions().get("005930")
    assert pos is not None and pos.qty_frac_in_pos > 1e-6
    # max_seen push 210
    s.on_tick(_tick(210, ts_offset_min=6))
    # 207 cross trail_stop = 210*0.99 = 207
    sigs = s.on_tick(_tick(207, ts_offset_min=7))
    assert sigs and sigs[0].side is Side.SELL and "trail" in sigs[0].note
    assert "005930" not in s.open_positions()


def test_no_double_entry_same_day() -> None:
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))
    s.on_tick(_tick(190, ts_offset_min=2))  # SL exit
    # Re-cross same day → no entry
    assert s.on_tick(_tick(205, ts_offset_min=3)) == []


def test_edge_detect_no_entry_if_already_above() -> None:
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    # First tick already above trigger (200) — no entry (no edge from below)
    sigs = s.on_tick(_tick(220))
    assert sigs == []


def test_timeout_closes_all() -> None:
    s = VBScaleInStrategy(
        prev_high_low={"005930": (300, 100)}, k=0.5, max_hold_minutes=10,
    )
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1
    sigs = s.on_tick(_tick(200, ts_offset_min=12))
    assert any(x.side is Side.SELL and "timeout" in x.note for x in sigs)


def test_invalid_entry_plan_sum() -> None:
    with pytest.raises(ValueError):
        VBScaleInStrategy(
            prev_high_low={}, entry_plan=[(0.0, 0.4), (0.005, 0.3)],
        )


def test_invalid_exit_plan_sum() -> None:
    with pytest.raises(ValueError):
        VBScaleInStrategy(
            prev_high_low={},
            exit_plan=[(0.02, 0.6), (0.03, 0.5)],
        )


def test_invalid_k() -> None:
    with pytest.raises(ValueError):
        VBScaleInStrategy(prev_high_low={}, k=0)
    with pytest.raises(ValueError):
        VBScaleInStrategy(prev_high_low={}, k=2.5)


def test_no_trigger_when_prev_hl_missing() -> None:
    s = VBScaleInStrategy(prev_high_low={})
    assert s.on_tick(_tick(150)) == []


def test_avg_entry_weighted_average() -> None:
    s = VBScaleInStrategy(prev_high_low={"005930": (300, 100)}, k=0.5)
    s.on_tick(_tick(100))
    s.on_tick(_tick(200, ts_offset_min=1))  # E1: 200 × 0.5
    s.on_tick(_tick(201, ts_offset_min=2))  # E2: 201 × 0.3
    s.on_tick(_tick(202, ts_offset_min=3))  # E3: 202 × 0.2
    pos = s.open_positions()["005930"]
    # avg = (200*0.5 + 201*0.3 + 202*0.2) / 1.0 = 100 + 60.3 + 40.4 = 200.7 → int 200
    assert pos.avg_entry == 200
    assert pos.initial_entry == 200


def test_defaults_match_design() -> None:
    """진입 50/30/20, 청산 33/33/잔여 — 설계 문서대로."""
    assert DEFAULT_ENTRY_PLAN[0][1] == pytest.approx(0.50)
    assert DEFAULT_ENTRY_PLAN[1][1] == pytest.approx(0.30)
    assert DEFAULT_ENTRY_PLAN[2][1] == pytest.approx(0.20)
    assert DEFAULT_EXIT_PLAN[0][1] == pytest.approx(1.0 / 3)
    assert DEFAULT_EXIT_PLAN[1][1] == pytest.approx(1.0 / 3)
    assert sum(f for _, f in DEFAULT_ENTRY_PLAN) == pytest.approx(1.0)
    assert sum(f for _, f in DEFAULT_EXIT_PLAN) < 1.0  # 잔여 = trailing
