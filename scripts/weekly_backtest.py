"""weekly_backtest — 매주 토요일 backtest 자동 재실행 + baseline 권고 출력.

흐름:
1. 일봉 696일 backtest 실행 (run cost 반영)
2. 분봉 180일 backtest 실행
3. 결과 종합 → 각 strategy 의 win_rate / total_pnl
4. _BACKTEST_BASELINE 갱신 권고 출력 (사용자가 paper_trade_breakout 의 dict 수동 update)

cron::

    30 8 * * 6 cd /home/bpearson/ks_ws && \\
      PYTHONPATH=src .venv/bin/python -m scripts.weekly_backtest \\
      > data/reports/backtest_$(date +\\%Y\\%m\\%d).txt

토요일 08:30 (weekly_report 는 09:00 라 backtest 먼저 실행 후 report).
주의: 자동 hot-reload 안 함 — 사용자가 결과 보고 _BACKTEST_BASELINE 수정 결정.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.weekly_backtest
    PYTHONPATH=src .venv/bin/python -m scripts.weekly_backtest --minute-days 90
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO = Path(__file__).parent.parent

_STRATEGY_KR = {
    "breakout": "신고가매매", "double_bottom": "쌍바닥", "box_breakout": "박스권돌파",
    "inverse_head_shoulders": "역헤드앤숄더", "flag_pennant": "깃발페넌트",
    "cup_handle": "컵앤핸들", "triangle": "삼각수렴", "wedge": "웨지",
    "volatility_breakout": "변동성돌파", "nr7_breakout": "NR7돌파",
    "dual_thrust": "듀얼트러스트", "color_streak": "양봉연속",
    "pivot_half_pullback": "피벗절반눌림", "bnf_disparity": "BNF이격도",
    "vwap_reversion": "VWAP평균회귀", "opening_momentum": "시초모멘텀",
}


def _run_subprocess(args: list[str]) -> str:
    env = {"PYTHONPATH": str(_REPO / "src"),
           **{k: v for k, v in __import__("os").environ.items()
              if k != "PYTHONPATH"}}
    result = subprocess.run(
        args, cwd=_REPO, env=env, capture_output=True, text=True,
        timeout=600, check=False,
    )
    return result.stdout + result.stderr


def _parse_results(output: str) -> dict[str, dict]:
    """Parse '=== Strategy 결과' 표 → {strategy_name_kr: {n, win_rate, total}}."""
    results: dict[str, dict] = {}
    in_table = False
    for line in output.splitlines():
        if "=== Strategy 결과" in line:
            in_table = True
            continue
        if in_table:
            stripped = line.strip()
            if not stripped or stripped.startswith("=") or stripped.startswith("Per-"):
                if "Per-strategy" in line or "===" in line:
                    in_table = False
                continue
            if "전략" in line or "---" in line:
                continue
            parts = stripped.split()
            if len(parts) < 6:
                continue
            try:
                name = parts[0]
                n = int(parts[1])
                win_str = parts[4]
                if not win_str.endswith("%"):
                    continue
                win = float(win_str.rstrip("%"))
                total = int(parts[-1].replace(",", "").replace("+", ""))
                results[name] = {"n": n, "win_rate": win, "total": total}
            except (ValueError, IndexError):
                continue
    return results


def _baseline_recommendation(daily: dict, minute: dict) -> str:
    """Combine both timeframes → weight recommendation per strategy."""
    out = ["[Backtest baseline 자동 권고]"]
    out.append(f"  {'전략':<14} {'일봉win':>8} {'분봉win':>8} {'권고 weight':>12}")
    out.append("  " + "-" * 60)
    kr_to_eng = {v: k for k, v in _STRATEGY_KR.items()}
    all_names = sorted(set(daily) | set(minute))
    for kr in all_names:
        d = daily.get(kr, {})
        m = minute.get(kr, {})
        d_win = d.get("win_rate", -1)
        m_win = m.get("win_rate", -1)
        # 권고 로직:
        # - 양 TF win >= 55% → boost 1.2
        # - 양 TF win < 25% → 0.0 disable
        # - 한 쪽 < 25% & 다른 쪽 < 40% → 0.5 weak
        # - 그 외 → 1.0 default
        wins = [w for w in (d_win, m_win) if w >= 0]
        if not wins:
            rec = 1.0
        elif all(w >= 55 for w in wins):
            rec = 1.2
        elif all(w < 25 for w in wins):
            rec = 0.0
        elif min(wins) < 25:
            rec = 0.5
        elif min(wins) < 40:
            rec = 0.8
        else:
            rec = 1.0
        out.append(
            f"  {kr:<14} {d_win:>7.1f}% {m_win:>7.1f}% "
            f"{rec:>11.1f}  // {kr_to_eng.get(kr, '?')}",
        )
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--daily-days", type=int, default=700)
    p.add_argument("--minute-days", type=int, default=90)
    args = p.parse_args()

    now_kst = datetime.now(UTC).astimezone()
    print(f"\n=== Weekly backtest @ {now_kst.strftime('%Y-%m-%d %H:%M KST')} ===\n")

    print(f"[1/2] 일봉 {args.daily_days}일 backtest 실행...")
    daily_out = _run_subprocess([
        sys.executable, "-m", "scripts.backtest_all_strategies",
        "--days", str(args.daily_days),
    ])
    daily = _parse_results(daily_out)
    print(f"  {len(daily)} strategies fired")

    print(f"\n[2/2] 분봉 {args.minute_days}일 backtest 실행 (no-vwap)...")
    minute_out = _run_subprocess([
        sys.executable, "-m", "scripts.backtest_all_strategies_minute",
        "--days", str(args.minute_days), "--no-vwap",
    ])
    minute = _parse_results(minute_out)
    print(f"  {len(minute)} strategies fired")

    print()
    print(_baseline_recommendation(daily, minute))

    # Also dump full output to log
    print("\n\n=== 일봉 backtest output ===")
    print(daily_out[-2000:])  # tail 2KB
    print("\n=== 분봉 backtest output ===")
    print(minute_out[-2000:])

    return 0


if __name__ == "__main__":
    sys.exit(main())
