# CYBOS Plus realtime 호가 (Orderbook) fetcher 설계

> 2026-05-13 설계 단계. KIS mock H0STASP0 (호가) 미지원이 3 paper_trade run
> (12h+ 각각 frame=0) 으로 라이브 확정 → 대신 Windows PC 의 CYBOS Plus 로
> 실시간 호가 받고 Linux 의 ks_ws 로 push.

---

## 1. 배경

| API | tick (체결) | 호가 (orderbook) | historical 분봉 |
|---|---|---|---|
| KIS mock | 정상 (H0STCNT0) | **미지원** (H0STASP0 → frame=0) | 미지원 |
| KIS live | 정상 | 정상 | 미지원 |
| 대신 CYBOS Plus | 정상 | **정상** | 정상 (1m / 5m / 1h / 1d / ...) |
| 키움 OpenAPI+ | 정상 | 정상 | 정상 | (사용자 제외 — 키움 X)

이미 **historical** 분봉/시봉/일봉은 `daishin_windows_setup.md` 흐름으로 Windows
PC 에서 Parquet 출력 + rsync 받아 `BarStore` 에 적재 중. 이는 **batch** 흐름.

본 doc 은 **realtime 호가** push 흐름. 즉 Windows fetcher 가 시장 시간 동안
계속 살아있으면서 Linux ks_ws 로 호가 frame 을 stream 한다.

---

## 2. 요구사항

- KOSPI/KOSDAQ 시총 상위 20-200 종목 5 단계 호가 실시간 수신.
- Linux ks_ws 의 `OrderBook` 도메인 모델로 변환 + EventBus 에 publish.
- KIS mock 의 tick (H0STCNT0) 과 동시 사용. 두 stream 이 같은 EventBus 로
  들어와도 일관된 (symbol, timestamp) 표현 + 중복 X.
- 사용자 rule `feedback_no_force_close` 호환 — 호가 frame drop / disconnection 시
  도 strategy 가 force-close 안 함. 단순 stale 처리.
- 보안: Windows 비번 (`123123`) 은 chat / git X. SSH key (`~/.ssh/id_daishin`)
  사용.

---

## 3. 아키텍처 선택지

### 옵션 A: WebSocket push (Windows server → Linux client) — **추천**

```
[Windows PC]
  fetcher_realtime.py
   ├ CYBOS Plus COM event → orderbook callback
   ├ FastAPI + uvicorn (websocket server) :8001
   └ broadcasts JSON to connected clients

      ↑↓  websocket (LAN, 172.30.1.38:8001)

[Linux ks_ws]
  ks_ws.sources.cybos_realtime.CybosRealtimeSource
   ├ websockets client → JSON parse → OrderBook
   ├ EventBus.publish(OrderBook)
   └ reconnect on disconnect (exponential backoff)
```

**장점**: 단방향 push, latency 짧음 (LAN 1-3ms), 표준 FastAPI/websockets,
backpressure 자연 처리, 디버깅 쉬움.

**단점**: Windows server 가 죽으면 Linux 가 알아채야 함 (ping / heartbeat).

### 옵션 B: SSH tunnel + UDP

CYBOS event → JSON UDP packet → Linux UDP socket → publish.
빠르지만 패킷 loss 가능 (UDP). 호가는 1초 수~수십개 → loss 위험.

### 옵션 C: Redis stream

Windows 가 Linux 의 Redis 에 XADD. Linux 가 XREAD.
순서 보장 / persist 가능. 단 Redis 추가 의존성 + Windows-Linux 사이 latency 늘 수
있음 (Redis 가 어디 있느냐에 따라).

→ **옵션 A WebSocket 으로 진행** (단순 + LAN 동일 sub 환경에서 가장 안정).

---

## 4. 메시지 포맷

JSON frame, 1 호가 = 1 frame:

```json
{
  "type": "orderbook",
  "symbol": "005930",
  "ts": "2026-05-14T09:01:23.456+09:00",
  "ask_px": [82500, 82600, 82700, 82800, 82900],
  "ask_qty": [1200, 800, 500, 300, 200],
  "bid_px": [82400, 82300, 82200, 82100, 82000],
  "bid_qty": [900, 700, 400, 250, 150]
}
```

선택적으로:
- `"type": "heartbeat"` (5 초마다, server alive)
- `"type": "error"`, `"code": "...", "msg": "..."` (CYBOS reject)

---

## 5. Windows fetcher_realtime.py (예시 스켈레톤, 사용자 작성)

```python
# C:\ks_ws_export\fetcher_realtime.py
# Python 32-bit (CYBOS Plus 가 32-bit COM)
import asyncio
import json
import win32com.client
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, WebSocket
import uvicorn

KST = timezone(timedelta(hours=9))
app = FastAPI()
clients: set[WebSocket] = set()

class _Handler:
    def OnReceived(self):
        # CYBOS Plus realtime callback - StockJpBid 실시간 호가 plugin
        sym = self.obj.GetHeaderValue(0)
        out = {
            "type": "orderbook",
            "symbol": sym,
            "ts": datetime.now(KST).isoformat(timespec="milliseconds"),
            "ask_px": [self.obj.GetHeaderValue(i) for i in (3, 5, 7, 9, 11)],
            "ask_qty": [self.obj.GetHeaderValue(i) for i in (4, 6, 8, 10, 12)],
            "bid_px": [self.obj.GetHeaderValue(i) for i in (13, 15, 17, 19, 21)],
            "bid_qty": [self.obj.GetHeaderValue(i) for i in (14, 16, 18, 20, 22)],
        }
        # fan-out to all websocket clients
        asyncio.run_coroutine_threadsafe(_broadcast(out), loop)

async def _broadcast(msg: dict) -> None:
    data = json.dumps(msg)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

@app.websocket("/ws/orderbook")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # client doesn't send; keep alive
    except Exception:
        clients.discard(ws)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    # Subscribe StockJpBid for 20-200 codes (universe loaded from a config file)
    for code in load_universe():  # noqa: F821
        obj = win32com.client.Dispatch("DsCbo1.StockJpBid")
        obj.SetInputValue(0, code)
        handler = win32com.client.WithEvents(obj, _Handler)
        handler.obj = obj
        obj.Subscribe()
    uvicorn.run(app, host="0.0.0.0", port=8001)
```

CYBOS Plus 호가 plugin = `DsCbo1.StockJpBid` (확인 필요, doc:
https://cybosplus.github.io/cpsysdib_-stockjpbid).

### Rate limit
- CYBOS Plus 시세 = 15초/60 호출. 실시간 subscribe 는 별도 한도 (보통 200
  종목 동시 subscribe 가능). 사용자 매뉴얼 확인 필요.

---

## 6. Linux 측 — `src/ks_ws/sources/cybos_realtime.py` (V1)

```python
"""CybosRealtime — Windows CYBOS Plus 호가 websocket → OrderBook event."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import websockets

from ks_ws.bus import EventBus
from ks_ws.domain import OrderBook

log = logging.getLogger("ks_ws.sources.cybos_realtime")


class CybosRealtimeSource:
    def __init__(
        self,
        bus: EventBus,
        *,
        url: str = "ws://172.30.1.38:8001/ws/orderbook",
        reconnect_initial_sec: float = 1.0,
        reconnect_max_sec: float = 60.0,
    ) -> None:
        self._bus = bus
        self.url = url
        self.reconnect_initial = reconnect_initial_sec
        self.reconnect_max = reconnect_max_sec
        self._task: asyncio.Task | None = None
        self.received_count = 0
        self.reconnect_count = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        backoff = self.reconnect_initial
        while True:
            try:
                async with websockets.connect(self.url) as ws:
                    log.info("connected to CYBOS realtime %s", self.url)
                    backoff = self.reconnect_initial
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") != "orderbook":
                            continue
                        ob = OrderBook(
                            symbol=msg["symbol"],
                            timestamp=datetime.fromisoformat(msg["ts"]),
                            ask_prices=tuple(msg["ask_px"]),
                            ask_quantities=tuple(msg["ask_qty"]),
                            bid_prices=tuple(msg["bid_px"]),
                            bid_quantities=tuple(msg["bid_qty"]),
                        )
                        self._bus.publish(ob)
                        self.received_count += 1
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as e:
                log.warning("CYBOS websocket disconnected: %s, reconnect in %.1fs", e, backoff)
                self.reconnect_count += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max)
```

---

## 7. paper_trade 통합

paper_trade_breakout.py 의 hub.start() 이후:

```python
cybos_source = CybosRealtimeSource(bus, url="ws://172.30.1.38:8001/ws/orderbook")
await cybos_source.start()
log.info("CybosRealtimeSource started (Windows PC orderbook stream)")
```

기존 KIS hub 의 호가 빈 stream 과 동시에 들어옴. OrderBook subscriber 가 양쪽
중복 frame 받을 가능성 X (KIS mock 은 frame=0). live KIS 전환 시 호가는
deduplicate 필요 — 그때 가서 (symbol, ts millisecond) 기준 dedupe layer 추가.

---

## 8. 데이터 캡처

기존 paper_trade 의 `_ob_logger` 가 EventBus subscribe(OrderBook) 으로 잡고
SQLite (`data/ticks.sqlite::orderbook` table) 에 insert. CYBOS source 도 동일
경로 → 코드 변경 X.

단 cutoff guard (`_ORDERBOOK_CAPTURE_END_KST`) 는 5/13 만료 → 갱신 필요.
CYBOS realtime 본격 도입 시 cutoff 를 한 달 이상으로 연장 (사용자 결정 필요).

---

## 9. 검증 절차

1. **Windows fetcher_realtime.py 단독 검증** — Windows 측에서 `curl
   ws://localhost:8001/ws/orderbook` (혹은 websocat) 으로 1 종목 호가 frame
   수신 확인.
2. **Linux ↔ Windows 연결 검증** — Linux 의 임시 client:
   ```bash
   .venv/bin/python -c "
   import asyncio, websockets, json
   async def main():
       async with websockets.connect('ws://172.30.1.38:8001/ws/orderbook') as ws:
           for _ in range(10):
               print(json.loads(await ws.recv())['symbol'])
   asyncio.run(main())
   "
   ```
3. **CybosRealtimeSource integration test** — mock websocket server (in-process)
   + 5 frames → assert EventBus received 5 OrderBook events.
4. **paper_trade 통합 1일 검증** — 5/14 / 5/15 양일 paper_trade 가 CYBOS source
   로 호가 정상 수신, `data/ticks.sqlite::orderbook` row count > 0 확인.

---

## 10. 진행 상태

- [x] 설계 doc (이 파일)
- [ ] Windows PC 측 fetcher_realtime.py 작성 + 단독 검증 (사용자 작업)
- [ ] Windows OpenSSH / 방화벽 8001 포트 열기 (사용자)
- [ ] Linux `src/ks_ws/sources/cybos_realtime.py` 구현 (in-process mock 테스트 포함)
- [ ] paper_trade_breakout.py 통합 (cybos_source.start())
- [ ] cutoff 연장 (`_ORDERBOOK_CAPTURE_END_KST` 한 달 이상)
- [ ] 1일 검증 + 메트릭 (orderbook row count vs CYBOS frame count, drop rate)

---

## 11. 위험 / 미정

- **CYBOS Plus subscribe 한도** — 200 종목 가능 여부 확인 필요. 한도 초과
  시 universe shrink.
- **Windows PC 안정성** — 24h 운영 검증 필요. CYBOS 자동 로그아웃, COM event
  loop hang 등. nssm 으로 서비스화 권장.
- **시간 동기화** — Windows ↔ Linux NTP sync 필수 (호가 ts 정확도). LAN 동일
  서브넷이라 보통 ±50ms.
- **메모리** — 200 종목 × ~분당 수십 frame × LAN 하루 = 약 50-200MB
  websocket 트래픽. WS frame 자체는 작아 OK. SQLite 누적은 기존 cutoff guard 로
  제어.
