# Project: ks_ws (Korean stock auto-trading)

KIS OpenAPI 를 사용하는 한국 주식 자동매매 + 백테스팅 + 시세 수집 프로젝트. Ubuntu/Python 3.12. 모의투자(mock) 부터 시작, 검증 후 live 로.

## 매 사용자 명령마다 — 응답 생성 전 반드시 실행

다음 두 파일을 Read 도구로 먼저 읽고 컨텍스트로 흡수한 뒤 응답한다:

1. `response_rule.md` — 사용자가 부과한 누적 응답 규칙
2. `think.md` — 과거 추론·결정의 시간순 로그

이 룰은 `response_rule.md` 자체에도 명시되어 있고, 두 파일을 안 읽으면 누적된 룰·과거 결정을 잃어 일관성이 깨진다.

## 같이 유지할 파일

- `todo.md` — 사용자에게 "대안 / 다음 후보 / 선택지" 로 제시한 모든 항목 누적
