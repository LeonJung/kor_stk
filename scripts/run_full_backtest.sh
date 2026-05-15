#!/usr/bin/env bash
# Full-scope backtest orchestrator (사용자 명시 2026-05-15):
#   - 일봉: 전체 universe × 전체 기간 (~700일) — single process
#   - 분봉: 전체 universe × 13개월 — chunked 50씩, 병렬 2 worker
#   - CPU 70% 상한 = 4 core × 0.7 = 2.8 → 2 worker (50%) + nice -19 안전 마진
#
# Output: data/reports/full_backtest_2026_05_15/
#   daily_trades.csv  daily_summary.csv  daily_log.txt
#   minute_chunk00000_trades.csv  ...  (chunked)
#   minute_concat_trades.csv  minute_concat_summary.csv
#   manifest.txt (실행 메타 + 진행 status)

set -uo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="data/reports/full_backtest_2026_05_15"
mkdir -p "$OUT_DIR"
MANIFEST="$OUT_DIR/manifest.txt"

# Detect universe size
UNIVERSE_SIZE=$(PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src .venv/bin/python -c "
from ks_ws.storage.universe import UniverseRegistry
reg = UniverseRegistry('data/universe.sqlite')
print(len(reg.top_by_market_cap(100_000)))
" 2>/dev/null)

CHUNK_SIZE=30        # 메모리 ~10GB/chunk (400일 × 30종목)
WORKERS=2            # 분봉 병렬 worker 수. 일봉 + 2 = 3 = 시스템 75% CPU
DAILY_DAYS=700       # 일봉 보유 데이터 전체
MINUTE_DAYS=400      # 분봉 13개월 ≈ 395일 — 보유 데이터 전체

{
  echo "=== Full backtest manifest ==="
  echo "started: $(date -Iseconds)"
  echo "universe: $UNIVERSE_SIZE"
  echo "daily: --days $DAILY_DAYS (전체 기간)"
  echo "minute: --days $MINUTE_DAYS, chunk_size=$CHUNK_SIZE, workers=$WORKERS"
  echo "cpu policy: nice -n 19 + max $WORKERS parallel python"
  echo
} > "$MANIFEST"

# ---- 1. 일봉 (single process, background) ----
echo "[1/2] 일봉 backtest 시작 (전체 universe × $DAILY_DAYS일)..."
DAILY_LOG="$OUT_DIR/daily_log.txt"
nice -n 19 env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  .venv/bin/python -m scripts.backtest_all_strategies \
  --top 0 --days $DAILY_DAYS \
  --csv-out-prefix "$OUT_DIR/daily" \
  > "$DAILY_LOG" 2>&1 &
DAILY_PID=$!
echo "daily PID=$DAILY_PID log=$DAILY_LOG" | tee -a "$MANIFEST"

# ---- 2. 분봉 chunked (parallel WORKERS) ----
echo "[2/2] 분봉 backtest 시작 (chunked $CHUNK_SIZE × ceil($UNIVERSE_SIZE/$CHUNK_SIZE) batch)..."
MINUTE_LOG="$OUT_DIR/minute_log.txt"
echo "minute log: $MINUTE_LOG" | tee -a "$MANIFEST"
echo > "$MINUTE_LOG"

# Generate chunk offsets
CHUNK_OFFSETS=()
for ((off=0; off<UNIVERSE_SIZE; off+=CHUNK_SIZE)); do
  CHUNK_OFFSETS+=("$off")
done
TOTAL_CHUNKS=${#CHUNK_OFFSETS[@]}
echo "total minute chunks: $TOTAL_CHUNKS" | tee -a "$MANIFEST"

# Worker pool: feed chunks to up to WORKERS python processes
# Use named pipe + flock approach for simple bash worker pool
RUNNING=0
CHUNK_IDX=0
for off in "${CHUNK_OFFSETS[@]}"; do
  # Wait if too many running
  while [ $(jobs -rp | wc -l) -ge $WORKERS ]; do
    sleep 2
  done
  CHUNK_IDX=$((CHUNK_IDX + 1))
  echo "[chunk $CHUNK_IDX/$TOTAL_CHUNKS] offset=$off (running $(jobs -rp | wc -l)/$WORKERS workers)" | tee -a "$MANIFEST"
  nice -n 19 env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
    .venv/bin/python -m scripts.backtest_all_strategies_minute \
    --top 0 --days $MINUTE_DAYS \
    --chunk-offset "$off" --chunk-size $CHUNK_SIZE \
    --no-vwap \
    --csv-out-prefix "$OUT_DIR/minute" \
    >> "$MINUTE_LOG" 2>&1 &
done

# Wait for all background jobs (daily + minute chunks)
echo "Waiting for all chunks + daily to finish..." | tee -a "$MANIFEST"
wait

echo "[+] All backtests finished" | tee -a "$MANIFEST"
echo "finished: $(date -Iseconds)" >> "$MANIFEST"

# ---- 3. Concat minute chunk CSVs ----
echo "[3/3] concat 분봉 chunk CSVs..."
for kind in trades summary period; do
  OUT="$OUT_DIR/minute_concat_${kind}.csv"
  FIRST=$(ls "$OUT_DIR"/minute_chunk*_${kind}.csv 2>/dev/null | head -n 1)
  if [ -n "$FIRST" ]; then
    {
      head -n 1 "$FIRST"
      for f in "$OUT_DIR"/minute_chunk*_${kind}.csv; do
        tail -n +2 "$f"
      done
    } > "$OUT"
    echo "  → $OUT ($(wc -l < "$OUT") rows)"
  fi
done
echo "[+] concat done" | tee -a "$MANIFEST"

echo
echo "=== 결과 파일 ==="
ls -la "$OUT_DIR"/{daily,minute_concat}*.csv 2>/dev/null
echo
echo "manifest: $MANIFEST"
