#!/usr/bin/env bash
# install_cron.sh — ks_ws paper_trade + report cron 자동 등록.
#
# Usage:
#   bash scripts/install_cron.sh         # 등록 (default)
#   bash scripts/install_cron.sh remove  # 제거
#
# 기존 crontab 보존 + KS_WS_CRON 마커 라인 사이에만 ks_ws entries 추가/제거.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER_START="# === KS_WS_CRON_START ==="
MARKER_END="# === KS_WS_CRON_END ==="
PYTHON="$REPO_DIR/.venv/bin/python"
PYTHONPATH_PREFIX="PYTHONPATH=$REPO_DIR/src"
REPORTS_DIR="$REPO_DIR/data/reports"

mkdir -p "$REPORTS_DIR"

# cron entries
cron_entries() {
    cat <<EOF
$MARKER_START
# Paper trade — 평일 07:50 KST 시작 (08:00 trade), 20:00 자동 종료 (script 내장).
50 7 * * 1-5 cd $REPO_DIR && $PYTHONPATH_PREFIX $PYTHON -m scripts.paper_trade_breakout >> $REPORTS_DIR/paper_trade_\$(date +\\%Y\\%m\\%d).log 2>&1
# Daily report — 평일 20:30 KST (paper_trade 종료 직후).
30 20 * * 1-5 cd $REPO_DIR && $PYTHONPATH_PREFIX $PYTHON -m scripts.daily_report > $REPORTS_DIR/daily_\$(date +\\%Y\\%m\\%d).txt 2>&1
# Weekly backtest — 토요일 08:30 KST.
30 8 * * 6 cd $REPO_DIR && $PYTHONPATH_PREFIX $PYTHON -m scripts.weekly_backtest > $REPORTS_DIR/backtest_\$(date +\\%Y\\%m\\%d).txt 2>&1
# Weekly report — 토요일 09:00 KST (backtest 끝난 후).
0 9 * * 6 cd $REPO_DIR && $PYTHONPATH_PREFIX $PYTHON -m scripts.weekly_report > $REPORTS_DIR/weekly_\$(date +\\%Y\\%m\\%d).txt 2>&1
# Daily backup — 매일 03:00 KST. trade_review + ledger + universe_candidates.
0 3 * * * cd $REPO_DIR && mkdir -p data/backups && for f in data/trade_review.sqlite data/paper_breakout_ledger.sqlite data/universe_candidates.sqlite; do [ -f \"\$f\" ] && cp -a \"\$f\" data/backups/\$(basename \"\$f\" .sqlite)_\$(date +\\%Y\\%m\\%d).sqlite; done
$MARKER_END
EOF
}

remove_existing() {
    crontab -l 2>/dev/null | awk "
        /$MARKER_START/ { skip=1; next }
        /$MARKER_END/   { skip=0; next }
        !skip
    "
}

install_cron() {
    echo "Installing ks_ws cron entries to crontab..."
    {
        remove_existing
        echo ""
        cron_entries
    } | crontab -
    echo "Done. Current crontab:"
    crontab -l | grep -A 100 "$MARKER_START" || true
}

uninstall_cron() {
    echo "Removing ks_ws cron entries..."
    remove_existing | crontab -
    echo "Done."
}

case "${1:-install}" in
    install)
        install_cron
        ;;
    remove|uninstall)
        uninstall_cron
        ;;
    show)
        crontab -l 2>/dev/null | grep -A 100 "$MARKER_START" || \
            echo "(no ks_ws cron entries)"
        ;;
    *)
        echo "Usage: bash $0 [install|remove|show]"
        exit 1
        ;;
esac
