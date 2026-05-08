import asyncio
from datetime import UTC, datetime

import pytest

from ks_ws.bus import EventBus
from ks_ws.detectors.program_flow import ProgramFlowDetector
from ks_ws.events import ProgramFlowEnter, ProgramFlowExit
from ks_ws.sources.program_flow import ProgramFlowSource


def _fetcher_returning(value_or_seq):
    """Build a fetcher that returns a fixed value, or pulls from a sequence."""
    if callable(value_or_seq):
        return value_or_seq
    if isinstance(value_or_seq, list):
        seq = iter(value_or_seq)

        def _f(_symbol):
            return next(seq)

        return _f

    def _f(_symbol):
        return value_or_seq

    return _f


def test_step_polls_each_symbol_and_feeds_detector():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    detector = ProgramFlowDetector(
        bus, entry_threshold_krw=1_000_000_000, exit_threshold_krw=100_000_000
    )
    src = ProgramFlowSource(
        detector,
        ["005930", "000660"],
        fetcher=_fetcher_returning(2_000_000_000),
        interval_sec=30,
    )
    polled = src.step()
    assert polled == 2
    assert sub.qsize() == 2  # both symbols crossed the entry threshold


def test_step_propagates_to_detector_with_state_transitions():
    bus = EventBus()
    enter_sub = bus.subscribe(ProgramFlowEnter)
    exit_sub = bus.subscribe(ProgramFlowExit)
    detector = ProgramFlowDetector(
        bus, entry_threshold_krw=1_000_000_000, exit_threshold_krw=100_000_000
    )
    # Sequence: huge, huge (already in), small (exit), small (no event)
    src = ProgramFlowSource(
        detector,
        ["005930"],
        fetcher=_fetcher_returning([2_000_000_000, 2_500_000_000, 50_000_000, 30_000_000]),
        interval_sec=30,
    )
    src.step()
    src.step()
    src.step()
    src.step()
    assert enter_sub.qsize() == 1  # only first crossing
    assert exit_sub.qsize() == 1


def test_fetcher_exception_is_skipped_not_raised():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    detector = ProgramFlowDetector(bus)

    def flaky(symbol):
        if symbol == "BAD":
            raise RuntimeError("transient")
        return 2_000_000_000

    src = ProgramFlowSource(detector, ["BAD", "005930"], fetcher=flaky)
    polled = src.step()
    # BAD is skipped, 005930 still polled
    assert polled == 1
    assert sub.qsize() == 1


def test_invalid_interval_rejected():
    bus = EventBus()
    detector = ProgramFlowDetector(bus)
    with pytest.raises(ValueError):
        ProgramFlowSource(detector, ["005930"], interval_sec=0)


def test_continuous_mode_runs_step_periodically():
    """Async start spins up a loop; one short cycle should produce at least
    one poll event in the detector."""
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    detector = ProgramFlowDetector(bus)
    src = ProgramFlowSource(
        detector,
        ["005930"],
        fetcher=_fetcher_returning(2_000_000_000),
        interval_sec=0.05,
    )

    async def run():
        await src.start()
        await asyncio.sleep(0.12)  # allow ~2 iterations
        await src.stop()
        return src.poll_count, sub.qsize()

    polls, fired = asyncio.run(run())
    assert polls >= 1
    assert fired >= 1


def test_kis_fetcher_parses_response(monkeypatch, tmp_path):
    """Mocked KIS transport returning a program-trade response with the
    expected net-flow field is parsed correctly."""
    import httpx

    from ks_ws.auth import token as token_mod
    from ks_ws.kis import http as http_mod
    from ks_ws.sources import program_flow as pf_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                    "access_token_token_expired": "2099-01-01 00:00:00",
                },
            )
        if request.url.path.endswith("/program-trade-by-stock"):
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": [{"whol_smtn_ntby_tr_pbmn": "1500000000"}],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_make_client(settings, **_kw):
        return httpx.Client(
            transport=transport,
            base_url="https://mock",
            headers={"appkey": settings.app_key, "appsecret": settings.app_secret},
        )

    monkeypatch.setattr(http_mod, "make_client", fake_make_client)
    monkeypatch.setattr(pf_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "make_client", fake_make_client)
    monkeypatch.setattr(token_mod, "_CACHE_DIR", tmp_path)

    from ks_ws.sources.program_flow import kis_program_flow_fetcher

    assert kis_program_flow_fetcher("005930") == 1_500_000_000


def test_step_records_poll_count_increments():
    bus = EventBus()
    detector = ProgramFlowDetector(bus)
    src = ProgramFlowSource(detector, ["005930"], fetcher=_fetcher_returning(0), interval_sec=30)
    assert src.poll_count == 0
    src.step()
    src.step()
    assert src.poll_count == 2


def test_now_passed_to_detector_is_recent():
    bus = EventBus()
    sub = bus.subscribe(ProgramFlowEnter)
    detector = ProgramFlowDetector(bus)
    src = ProgramFlowSource(detector, ["005930"], fetcher=_fetcher_returning(2_000_000_000))
    before = datetime.now(UTC)
    src.step()
    after = datetime.now(UTC)
    e = sub.get_nowait()
    assert before <= e.timestamp <= after
