"""trade_review_app.py — Streamlit GUI 로 backtest trade 검토 + 사용자 평가 누적.

사용법::

    PYTHONPATH=src .venv/bin/streamlit run scripts/trade_review_app.py

기능:
- V1/V2/V3/V4 mode 선택 (4-way backtest 결과 source)
- 종목 + 거래 날짜 selectbox
- 선택된 trade 의 entry/exit/PnL + 종목 섹터 표시
- 매매 이유 자동 설명 (strategy 룰 + signal 컨텍스트)
- 분봉 차트 (plotly candlestick) + 매수/매도 마커
- 사용자 평가 버튼: 잘한 판단 / 잘못된 판단 / 의견 (의문)
- 이미 평가한 trade 는 자동 skip → 다음 미평가 trade 로
- Claude 의문 케이스 ~10% (PnL 극단치 / 짧은 hold 손실 등) 만 "이유 설명 요청" 박스 노출
- 평가 결과 sqlite (`data/trade_evaluations.sqlite`) 누적 → 향후 전략 코드 개선 input

종속성: streamlit, plotly, pandas (이미 설치됨).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ks_ws.sources.sector import DEFAULT_KOSPI_TOP30_GICS, SectorClassifier
from ks_ws.storage.bars import BarStore
from ks_ws.storage.universe import UniverseRegistry

_KST = ZoneInfo("Asia/Seoul")
_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
_REPORTS_ROOT = _DATA_ROOT / "reports"
_DB_PATH = _DATA_ROOT / "trade_evaluations.sqlite"

_MODE_INFO: dict[str, dict] = {
    "V1 (개선 전 baseline)": {
        "csv": "v1_trades.csv",
        "rule": (
            "**vb baseline** — Larry Williams 변동성돌파.\n"
            "- 매일 시가 + k×(전일 High-Low) 의 trigger 가격 산출 (k=0.5).\n"
            "- 분봉이 trigger 위로 cross 하면 BUY.\n"
            "- TP +2.5% / SL -1.5% / max_hold 240분 / same-day 1회 진입.\n"
            "- 종목 필터/symbol_weight/trailing 없음."
        ),
    },
    "V2 (Tier 1+3+5)": {
        "csv": "v2_trades.csv",
        "rule": (
            "**vb + Tier 1+3+5**.\n"
            "- Tier 1: sector blacklist (변동 큰 sector 60+ 종목 substring 매칭) "
            "universe 필터.\n"
            "- Tier 3: anchor-based trailing — entry × 1.015 도달 후 max_seen × 0.99 "
            "이탈 시 청산.\n"
            "- Tier 5: SymbolWeightMatrix (walk-forward 60%→×3 / 50%→×2 / 40%→×1 / 차단)."
        ),
    },
    "V3 (vb_scalein 분할매매)": {
        "csv": "v3_trades.csv",
        "rule": (
            "**vb_scalein** — vb 기반 분할매수/매도.\n"
            "- 진입: 50% (trigger) + 30% (+0.5%) + 20% (+1.0%, trailing 무장).\n"
            "- 청산: 33% (TP1 +2.0%, SL→BE) + 33% (TP2 +3.0%) + 잔여 (trailing).\n"
            "- universe = V2 와 동일, symbol_weight 적용.\n"
            "⚠️ round_close tolerance 버그 있음 (-39.8M backtest 신뢰도 낮음)."
        ),
    },
    "V4 (blacklist 만, honest)": {
        "csv": "v4_trades.csv",
        "rule": (
            "**vb + Tier 1 blacklist 만** — fixed rule, no symbol_weight, no trailing.\n"
            "- Tier 1 blacklist 만 적용한 honest baseline.\n"
            "- 5/17 phase A/B/D 의 overfit 경고 검증용 — dynamic factor 없는 안전 룰.\n"
            "- 결과: V1 -5.27M → V4 -3.87M (손실 27% 감소, 승률 거의 동일)."
        ),
    },
}


# ---------- DB ----------


def _ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation (
            mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT DEFAULT '',
            evaluated_at TEXT NOT NULL,
            PRIMARY KEY (mode, symbol, entry_ts, exit_ts)
        )
        """
    )
    conn.commit()
    return conn


def _trade_key(mode: str, row: dict) -> tuple[str, str, str, str]:
    return (mode, row["symbol"], row["entry_ts"], row["exit_ts"])


def _already_evaluated(conn: sqlite3.Connection, mode: str, row: dict) -> str | None:
    cur = conn.execute(
        "SELECT verdict FROM evaluation WHERE mode=? AND symbol=? AND entry_ts=? AND exit_ts=?",
        _trade_key(mode, row),
    )
    r = cur.fetchone()
    return r[0] if r else None


def _save_evaluation(
    conn: sqlite3.Connection, mode: str, row: dict, verdict: str, note: str = "",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO evaluation "
        "(mode, symbol, entry_ts, exit_ts, verdict, note, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*_trade_key(mode, row), verdict, note, datetime.now(_KST).isoformat()),
    )
    conn.commit()


def _evaluation_stats(conn: sqlite3.Connection, mode: str) -> dict[str, int]:
    cur = conn.execute(
        "SELECT verdict, COUNT(*) FROM evaluation WHERE mode=? GROUP BY verdict",
        (mode,),
    )
    return {v: c for v, c in cur.fetchall()}


# ---------- Data loaders ----------


@st.cache_data(show_spinner=False)
def _list_backtest_dirs() -> list[str]:
    return sorted(
        p.name for p in _REPORTS_ROOT.iterdir()
        if p.is_dir() and (p / "v1_trades.csv").exists()
    )


@st.cache_data(show_spinner=False)
def _load_trades(backtest_dir: str, csv_name: str) -> pd.DataFrame:
    path = _REPORTS_ROOT / backtest_dir / csv_name
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["entry_dt"] = pd.to_datetime(df["entry_ts"])
    df["exit_dt"] = pd.to_datetime(df["exit_ts"])
    df["entry_date"] = df["entry_dt"].dt.tz_convert(_KST).dt.strftime("%Y-%m-%d")
    return df


@st.cache_resource(show_spinner=False)
def _bar_store() -> BarStore:
    return BarStore(str(_DATA_ROOT))


@st.cache_resource(show_spinner=False)
def _sector_classifier() -> SectorClassifier:
    return SectorClassifier(mapping=DEFAULT_KOSPI_TOP30_GICS)


@st.cache_data(show_spinner=False)
def _name_map() -> dict[str, str]:
    try:
        reg = UniverseRegistry(str(_DATA_ROOT / "universe.sqlite"))
        entries = reg.top_by_market_cap(100_000)
        reg.close()
        return {e.code: getattr(e, "name", "?") for e in entries}
    except Exception:
        return {}


@st.cache_data(show_spinner=False)
def _load_minute_bars(symbol: str, date_str: str) -> pd.DataFrame:
    """주어진 종목 + KST 날짜의 분봉 데이터 (KST 09:00-15:30 범위)."""
    bs = _bar_store()
    kst_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_KST)
    start = kst_date.replace(hour=8, minute=0)
    end = kst_date.replace(hour=16, minute=0)
    bars = list(bs.read(symbol, "1m", start=start.astimezone(),
                        end=end.astimezone()))
    if not bars:
        return pd.DataFrame()
    rows = [
        {
            "timestamp": b.timestamp.astimezone(_KST),
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    return pd.DataFrame(rows)


# ---------- Claude 의문 케이스 식별 ----------


def _is_claude_uncertain(row: dict) -> bool:
    """Claude 가 의문 갖는 trade — 사용자 설명 요청 candidate.

    기준 (대략 10% 비율):
    - 매우 빠른 손절 (hold < 30분 + 손실) — 강한 SL 이거나 fake breakout
    - 큰 손실 (pnl_pct < -2%) — TP/SL 너머 또는 timeout 큰 손실
    - 큰 이익 (pnl_pct > +5%) — TP 너머 도약, 정확한 이유 알기 어려움
    """
    pct = float(row["pnl_pct"])
    hold = int(row["hold_minutes"])
    if hold < 30 and float(row["pnl_krw"]) < 0:
        return True
    if pct < -2.0 or pct > 5.0:
        return True
    return False


def _trade_reason(row: dict, mode: str, rule_text: str) -> str:
    """매매 이유 자동 서술."""
    pct = float(row["pnl_pct"])
    hold = int(row["hold_minutes"])
    entry_price = int(row["entry_price"])
    exit_price = int(row["exit_price"])
    pnl = int(row["pnl_krw"])

    if pct > 1.5:
        exit_kind = "TP (목표가 도달)"
    elif pct < -1.0:
        exit_kind = "SL (손절선 이탈)"
    elif hold >= 200:
        exit_kind = "timeout (max_hold 240분 도달)"
    else:
        exit_kind = "trailing / 중간 청산"

    lines = [
        f"### 📋 매매 로직 ({mode})",
        rule_text,
        "",
        "### 🎯 이번 trade",
        f"- 매수 가격: **{entry_price:,}원** ({row['entry_ts']})",
        f"- 매도 가격: **{exit_price:,}원** ({row['exit_ts']})",
        f"- 보유 시간: **{hold}분**",
        f"- 청산 사유 (추정): **{exit_kind}**",
        f"- PnL: **{pnl:+,}원 ({pct:+.2f}%)**",
    ]
    return "\n".join(lines)


# ---------- Chart ----------


def _make_chart(
    bars: pd.DataFrame, entry_ts: str, entry_price: int,
    exit_ts: str, exit_price: int,
) -> go.Figure:
    if bars.empty:
        fig = go.Figure()
        fig.add_annotation(text="분봉 데이터 없음", x=0.5, y=0.5,
                            xref="paper", yref="paper", showarrow=False)
        return fig
    fig = go.Figure(data=[go.Candlestick(
        x=bars["timestamp"], open=bars["open"], high=bars["high"],
        low=bars["low"], close=bars["close"], name="1m",
    )])
    entry_dt = pd.to_datetime(entry_ts).tz_convert(_KST)
    exit_dt = pd.to_datetime(exit_ts).tz_convert(_KST)
    fig.add_trace(go.Scatter(
        x=[entry_dt], y=[entry_price], mode="markers+text",
        marker=dict(color="green", size=15, symbol="triangle-up"),
        text=[f"BUY {entry_price:,}"], textposition="bottom center",
        name="매수",
    ))
    fig.add_trace(go.Scatter(
        x=[exit_dt], y=[exit_price], mode="markers+text",
        marker=dict(color="red", size=15, symbol="triangle-down"),
        text=[f"SELL {exit_price:,}"], textposition="top center",
        name="매도",
    ))
    fig.update_layout(
        height=500, xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.05),
    )
    return fig


# ---------- App ----------


def main() -> None:
    st.set_page_config(page_title="ks_ws Trade Review", layout="wide")
    st.title("📈 ks_ws Trade Review — 매매 검토 + 사용자 평가")

    conn = _ensure_db()

    # ----- Sidebar -----
    with st.sidebar:
        st.header("⚙️ 데이터 선택")
        backtest_dirs = _list_backtest_dirs()
        if not backtest_dirs:
            st.error("backtest 결과 디렉토리가 없습니다.")
            return
        default_idx = (
            backtest_dirs.index("vb_compare_20260517_4way")
            if "vb_compare_20260517_4way" in backtest_dirs
            else len(backtest_dirs) - 1
        )
        backtest_dir = st.selectbox(
            "Backtest 결과", backtest_dirs, index=default_idx,
        )
        mode_label = st.selectbox(
            "Mode (전략)", list(_MODE_INFO.keys()), index=3,
        )
        mode_info = _MODE_INFO[mode_label]
        df = _load_trades(backtest_dir, mode_info["csv"])
        if df.empty:
            st.warning(f"{mode_info['csv']} 데이터 없음.")
            return

        st.markdown(f"**전체 거래수**: {len(df):,}")

        # 종목 selectbox
        nm = _name_map()
        sym_counts = df["symbol"].value_counts().to_dict()
        symbols = sorted(sym_counts.keys(), key=lambda s: -sym_counts[s])
        sym_labels = {
            s: f"{s} {nm.get(s, '?')} ({sym_counts[s]}회)"
            for s in symbols
        }
        sym_filter = st.selectbox(
            "종목", ["(전체)"] + symbols,
            format_func=lambda s: "(전체)" if s == "(전체)" else sym_labels[s],
        )

        # 날짜 selectbox
        dates = sorted(df["entry_date"].unique(), reverse=True)
        date_filter = st.selectbox("거래 날짜", ["(전체)"] + list(dates))

        st.divider()
        st.header("📊 평가 통계")
        stats = _evaluation_stats(conn, mode_label)
        col1, col2, col3 = st.columns(3)
        col1.metric("잘함", stats.get("good", 0))
        col2.metric("잘못", stats.get("bad", 0))
        col3.metric("의문", stats.get("unsure", 0))
        total_eval = sum(stats.values())
        st.markdown(f"**총 평가**: {total_eval:,} / {len(df):,}")

        st.divider()
        skip_unsure_only = st.checkbox(
            "Claude 의문 케이스만 보기 (~10%)", value=False,
            help="PnL 극단치 / 짧은 손절 등 Claude 가 이유 모르는 케이스만 필터링",
        )

    # ----- Filter trades -----
    filtered = df
    if sym_filter != "(전체)":
        filtered = filtered[filtered["symbol"] == sym_filter]
    if date_filter != "(전체)":
        filtered = filtered[filtered["entry_date"] == date_filter]
    if filtered.empty:
        st.warning("선택한 조건에 해당하는 trade 없음.")
        return

    # ----- 다음 미평가 trade 찾기 -----
    candidates = filtered.to_dict("records")
    if skip_unsure_only:
        candidates = [r for r in candidates if _is_claude_uncertain(r)]
    next_trade = None
    for row in candidates:
        if _already_evaluated(conn, mode_label, row) is None:
            next_trade = row
            break

    if next_trade is None:
        st.success("🎉 이 필터의 모든 trade 평가 완료! "
                   "다른 필터/모드/날짜로 이동하세요.")
        st.markdown(f"**필터에서 매칭된 trade**: {len(candidates):,}")
        return

    # ----- 메인: 현재 trade 표시 -----
    row = next_trade
    is_uncertain = _is_claude_uncertain(row)

    col_main, col_eval = st.columns([3, 1])

    with col_main:
        sym = row["symbol"]
        name = _name_map().get(sym, "?")
        sector = _sector_classifier().classify(sym)
        pnl = int(row["pnl_krw"])
        pct = float(row["pnl_pct"])
        st.subheader(f"📊 {sym} {name}")
        st.caption(
            f"섹터: **{sector}** | "
            f"PnL: **{pnl:+,}원 ({pct:+.2f}%)** | "
            f"보유: **{row['hold_minutes']}분**"
        )

        # 분봉 차트
        bars = _load_minute_bars(sym, row["entry_date"])
        fig = _make_chart(
            bars, row["entry_ts"], int(row["entry_price"]),
            row["exit_ts"], int(row["exit_price"]),
        )
        st.plotly_chart(fig, use_container_width=True)

        # 매매 이유
        with st.expander("📝 매매 이유 (자동 생성)", expanded=True):
            st.markdown(_trade_reason(row, mode_label, mode_info["rule"]))

        if is_uncertain:
            st.info(
                "🤔 **Claude 의문 케이스** — 이 trade 는 짧은 손절이거나 PnL 극단치라 "
                "Claude 가 이유를 명확히 파악하지 못함. 사용자가 이유를 적어주면 "
                "차후 전략 개선에 사용."
            )

    with col_eval:
        st.markdown("### 🎯 사용자 평가")
        st.markdown(f"**잔여**: {len(candidates) - sum(1 for r in candidates if _already_evaluated(conn, mode_label, r)):,}개")

        user_note = ""
        if is_uncertain:
            user_note = st.text_area(
                "이 매매의 이유 (선택)", value="", height=120,
                placeholder="예: 시초 갭 후 trigger cross — 시장 강세장에서 짧은 SL 정상...",
            )

        if st.button("✅ 잘한 판단", use_container_width=True, type="primary"):
            _save_evaluation(conn, mode_label, row, "good", user_note)
            st.rerun()
        if st.button("❌ 잘못된 판단", use_container_width=True):
            _save_evaluation(conn, mode_label, row, "bad", user_note)
            st.rerun()
        if st.button("🤷 잘 모름", use_container_width=True):
            _save_evaluation(conn, mode_label, row, "unsure", user_note)
            st.rerun()

        st.divider()
        st.caption(
            "한 번 평가한 scene 은 자동 skip — 다음 미평가 trade 로 이동합니다."
        )


if __name__ == "__main__":
    main()
