"""Tests for JournalSystem + CorporateActionDetector."""

from datetime import UTC, datetime, timedelta

from ks_ws.detectors.corporate_action import (
    ActionRecord,
    CorporateActionDetector,
)
from ks_ws.events import CorporateAction
from ks_ws.storage.journal import JournalSystem


def _ts(seconds: int = 0):
    return datetime(2026, 5, 11, 9, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# JournalSystem -----------------------------------------------------------


def test_journal_record_and_list(tmp_path):
    j = JournalSystem(tmp_path / "j.sqlite")
    tid = j.record(
        symbol="X", strategy="alpha",
        opened_at=_ts(0), closed_at=_ts(60),
        quantity=10, entry_price=10000, exit_price=10500,
        pnl_krw=5000,
    )
    assert tid >= 1
    entries = j.all()
    assert len(entries) == 1
    e = entries[0]
    assert e.symbol == "X"
    assert e.pnl_krw == 5000
    assert e.entry_reason == ""


def test_journal_filter_by_strategy(tmp_path):
    j = JournalSystem(tmp_path / "j.sqlite")
    j.record(symbol="X", strategy="alpha", opened_at=_ts(0), closed_at=_ts(60),
             quantity=10, entry_price=100, exit_price=110, pnl_krw=100)
    j.record(symbol="X", strategy="beta", opened_at=_ts(60), closed_at=_ts(120),
             quantity=10, entry_price=100, exit_price=90, pnl_krw=-100)
    assert len(j.all(strategy="alpha")) == 1
    assert len(j.all(strategy="beta")) == 1
    assert len(j.all()) == 2


def test_journal_annotate(tmp_path):
    j = JournalSystem(tmp_path / "j.sqlite")
    tid = j.record(symbol="X", strategy="alpha", opened_at=_ts(0), closed_at=_ts(60),
                    quantity=10, entry_price=100, exit_price=110, pnl_krw=100)
    j.annotate(tid, entry_reason="news A", lesson="lesson A")
    e = j.all()[0]
    assert e.entry_reason == "news A"
    assert e.lesson == "lesson A"


def test_journal_needs_reflection(tmp_path):
    j = JournalSystem(tmp_path / "j.sqlite")
    j.record(symbol="X", strategy="alpha", opened_at=_ts(0), closed_at=_ts(60),
             quantity=10, entry_price=100, exit_price=110, pnl_krw=100)
    full = j.record(symbol="X", strategy="alpha", opened_at=_ts(60), closed_at=_ts(120),
                    quantity=10, entry_price=100, exit_price=110, pnl_krw=100)
    j.annotate(full, entry_reason="x", lesson="y")
    pending = j.needs_reflection()
    assert len(pending) == 1
    assert pending[0].entry_reason == ""


# CorporateActionDetector -------------------------------------------------


def test_corporate_action_emits_unique_records():
    events = []
    det = CorporateActionDetector(emit=events.append)
    det.feed([
        ActionRecord(symbol="X", action_type="bonus_issue", effective_date=_ts(0)),
        ActionRecord(symbol="Y", action_type="ipo", effective_date=_ts(0)),
    ])
    assert len(events) == 2
    assert {e.symbol for e in events} == {"X", "Y"}


def test_corporate_action_dedupes():
    events = []
    det = CorporateActionDetector(emit=events.append)
    rec = ActionRecord(symbol="X", action_type="bonus_issue", effective_date=_ts(0))
    det.feed([rec])
    det.feed([rec])  # same record again
    assert len(events) == 1
    assert det.published_count == 1


def test_corporate_action_emit_payload():
    events: list[CorporateAction] = []
    det = CorporateActionDetector(emit=events.append)
    det.feed([
        ActionRecord(
            symbol="X", action_type="ipo", effective_date=_ts(0),
            detail="new listing 2026-05-11",
        )
    ])
    e = events[0]
    assert isinstance(e, CorporateAction)
    assert e.action_type == "ipo"
    assert "new listing" in e.detail
