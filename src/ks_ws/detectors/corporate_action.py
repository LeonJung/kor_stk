"""CorporateActionDetector — 무상증자 / 신규 상장 / 액면분할 등 detect.

book Sec 28: 무상증자 / 신규주는 일반 매매와 다른 처리. 일반 strategy 가 잘못된
시그널을 emit 하지 않도록 corporate action 발생 시점에 해당 종목을 일시 mute
하거나 별도 strategy 가 처리.

V1: stub fetcher 패턴. ``fetcher(symbol) -> CorporateAction | None`` 외부에서
주입. KIS API 의 corporate action endpoint 또는 KRX 공시 데이터가 source.
본 모듈은 이미 알고 있는 action 들의 list 를 받아 적절한 시점에 emit.

운영: Scheduler.daily_at(8, 50, ...) 에서 fetch + bus publish.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from ks_ws.events import CorporateAction


@dataclass(frozen=True)
class ActionRecord:
    symbol: str
    action_type: str
    effective_date: datetime
    detail: str = ""


class CorporateActionDetector:
    def __init__(
        self,
        *,
        emit: Callable[[CorporateAction], None],
    ) -> None:
        self._emit = emit
        self._published: set[tuple[str, str, str]] = set()  # (symbol, action_type, isoformat)

    def feed(self, records: Iterable[ActionRecord]) -> None:
        """Emit a CorporateAction event for each record we haven't seen
        before. Caller is expected to call this with the latest known
        action list (idempotent)."""
        for r in records:
            key = (r.symbol, r.action_type, r.effective_date.isoformat())
            if key in self._published:
                continue
            self._published.add(key)
            self._emit(
                CorporateAction(
                    symbol=r.symbol,
                    timestamp=r.effective_date,
                    action_type=r.action_type,
                    detail=r.detail,
                )
            )

    @property
    def published_count(self) -> int:
        return len(self._published)
