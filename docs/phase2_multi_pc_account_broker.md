# ks_ws Phase 2 — 다PC / 다계정 / 다증권사 확장 설계

> 2026-05-14 작성. memory `project_roadmap` Phase 2.
>
> Phase 1 (현재): 1 PC + 1 KIS 계정 + 200 sym 목표 (실제 KIS WS 한도 20).
> Phase 2: 다 PC 분산 + 다 계정 + 다 증권사 (KIS + Daishin + 키움?) 통합.
> Phase 3 (later): deep learning 트레이딩.

---

## 1. 현재 한계 (Phase 1 막힘 지점)

| 한계 | 영향 |
|---|---|
| KIS WS subscription max ~20 종목 | universe 시총 top 20 만 실시간 |
| KIS mock 호가 (H0STASP0) 미지원 | 호가 strategy 라이브 X |
| KIS mock fill 안 줌 | MockFillSimulator 우회 (cycle 23) |
| 1 process, 1 PC | 장애 시 전체 중단 |
| 1 계정 | daily_loss_limit 1 계정 한도 안에서만 |
| 키움 X (사용자 결정) | 키움 API 사용 안 함 |

---

## 2. 목표 (Phase 2 완성 시)

- 동시 모니터링 universe **100-200 종목** (KIS 다중 connection + Daishin realtime)
- 다 계정 fail-over (1 계정 장애 → 다른 계정 자동 take-over)
- 다 PC redundancy (Linux 1 + Linux 2 + Windows = 3 PC)
- 증권사 abstraction → KIS / Daishin / (나중) 다른 증권사 swap 가능
- universe 시총 top 200 + 매매대금 폭증 top 50 동시 추적

---

## 3. 아키텍처

### 3.1 단일 process → multi-worker

```
[Linux PC1 — Coordinator]
  - coordinator process (universe 분배, weight management, report 집계)
  - sqlite DB (trade_review / ledger / universe_candidates) — 공유
  - KIS WS connection 1 (20 sym)
  - Daishin realtime CYBOS Plus (호가, doc cycle 10)

[Linux PC2 — Worker]
  - paper_trade worker 1 (KIS WS connection 2, 20 sym)
  - paper_trade worker 2 (Daishin realtime, 20 sym)
  - 같은 sqlite DB 에 write (LAN NFS or 또는 별도 ingest pipeline)

[Windows PC — Daishin host]
  - CYBOS Plus historical fetch + sync (이미 cycle 10 design)
  - realtime 호가 push (cycle 10 doc)
```

### 3.2 Coordinator 역할
- 시총 universe + active_candidates 결정
- 종목 → worker 매핑 (work assignment)
- weight_manager 중앙 관리
- daily/weekly report 통합

### 3.3 Worker 역할
- 자기 할당 universe 만 실시간 모니터
- LiveExecutor 자기 KIS account 사용
- 같은 sqlite (trade_review / ledger / universe_candidates) 에 write
- Coordinator 가 매 30s polling 으로 worker 상태 확인

### 3.4 통신
- 단순화: 공유 SQLite (sqlite3 WAL mode) 로 worker 간 데이터 공유.
- 또는: NATS / Redis Streams 로 event publish/subscribe.
- 초기 V1 = SQLite 공유 (가장 단순).

---

## 4. 다 계정

### 4.1 계정 추가 흐름
1. 사용자 KIS 추가 계정 (또는 다른 증권사) 신청
2. .env 에 `KIS_ACCOUNT_2_CANO`, `KIS_ACCOUNT_2_APP_KEY` 등 추가
3. `KisOrderRouter(settings, account="acc2")` 로 분기
4. paper_trade worker 2 가 acc2 사용 → 별도 universe 할당

### 4.2 Risk 분리
- 계정별 daily_loss_limit_krw 별도 (예: acc1 -5M / acc2 -3M)
- 전체 합산 limit 도 가능 (-7M 통합 stop)

### 4.3 자본 분배
- 자본 100M = acc1 60M + acc2 40M
- 각 계정의 max_position_per_symbol 비례 조정

---

## 5. 다 증권사 (Broker abstraction)

### 5.1 현재 ks_ws.orders.KisOrderRouter
- KIS REST API 직접 호출 (submit / cancel / status)

### 5.2 Phase 2 abstraction
```python
class BrokerAdapter(Protocol):
    def submit(self, intent: OrderIntent) -> SubmittedOrder: ...
    def cancel(self, order_id: str) -> bool: ...
    def get_status(self, order_id: str) -> OrderStatus: ...

class KisAdapter(BrokerAdapter): ...    # 기존
class DaishinAdapter(BrokerAdapter): ...  # 신규 (CYBOS Plus)
```

### 5.3 증권사 별 차이
- 거래수수료 (0.015% KIS / 0.0?% Daishin)
- order endpoint
- realtime fill notification 방식
- API rate limit

### 5.4 자동 라우팅
- 같은 종목을 어느 증권사로 보낼지: 단순 = static config (sym → broker)
- 동적 = 빠른 fill / 낮은 commission 우선 — 복잡, V2

---

## 6. KIS 다중 WS connection

### 6.1 한 계정 multiple connection
- KIS docs 확인 필요 — 같은 계정 동시 multiple WS 허용?
- 기본 1 connection / approval_key 1개. 다중은 OK 일 수도 (테스트 필요).
- 만약 1 계정 X면 다 계정 (cycle 5.1) 으로 해결.

### 6.2 connection pool
- 종목 20개 = 1 connection / 200 종목 = 10 connections
- 각 connection = 별도 hub + bus subscription
- 단일 EventBus 로 통합 → strategy 는 변화 X

---

## 7. 단계별 구현 plan

### Phase 2.1 (small) — paper_trade worker 분리 + 공유 SQLite
- 현재 paper_trade_breakout.py 를 worker mode 로 변경 (--worker-id N)
- coordinator script 신규 (`coordinator.py`) — universe 분배
- 1 PC 위에서 multiple worker (process pool)

### Phase 2.2 (medium) — 다 PC redundancy
- Linux PC2 setup (rsync 로 코드 sync)
- 공유 SQLite (NFS or 별도 ingestion)
- failover 자동화

### Phase 2.3 (large) — 다 증권사 abstraction
- BrokerAdapter Protocol
- DaishinAdapter 구현 (Windows PC 의 CYBOS Plus 활용)
- 두 broker 동시 운영

### Phase 2.4 (large) — 다 계정
- 다 계정 정책 결정 (사용자 결정)
- Risk 분리 + 자본 분배 정책

---

## 8. 보안 / 안전

- 사용자 룰 `feedback_kis_key_silent` 준수 — KIS keys 회전 알림 X (2026-08-09 까지)
- 사용자 룰 미수/신용 hard block — 다 계정으로 우회 시도 안 함
- daily_loss_limit 전체 합산 stop — 통합 안전망

---

## 9. 진행 상태

- [x] 설계 doc (이 파일)
- [ ] Phase 2.1: paper_trade --worker-id 분기 + coordinator script
- [ ] Phase 2.2: PC2 setup + rsync
- [ ] Phase 2.3: BrokerAdapter Protocol
- [ ] Phase 2.4: 다 계정

각 phase 단독 진행 가능. Phase 2.1 부터 점진 도입.

---

## 10. 미정 / 위험

- **KIS WS 다중 connection 허용 여부** — 미확인 (테스트 필요)
- **다 PC 시간 동기화** — NTP 필수, ±50ms 이내
- **SQLite 동시 write 성능** — WAL mode 로 어느 정도 가능. 워크로드 폭증 시 PostgreSQL 마이그레이션
- **다 계정 관리 복잡도** — 1 계정 검증 후 확장 권고
