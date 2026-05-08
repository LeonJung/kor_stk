"""Place a tiny mock-investment order through the same Risk + KisOrderRouter
path that live trading would use. Defaults to a 1-share LIMIT BUY at a
price 5% below the current market — extremely unlikely to fill, intended
as a path-validation test rather than a real position.

Without --confirm it prints what it WOULD do and stops. Pass --confirm
to actually submit.

Run (dry):
    uv run examples/place_test_order_demo.py
Run (live submit, mock account):
    uv run examples/place_test_order_demo.py --confirm
"""

import argparse
import time
from datetime import UTC, datetime

from ks_ws.config import get_settings
from ks_ws.domain import OrderIntent, Side
from ks_ws.market.kis_rest import fetch_current_price
from ks_ws.orders import KisOrderRejected, KisOrderRouter
from ks_ws.risk import Risk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument(
        "--offset-pct",
        type=float,
        default=-5.0,
        help="LIMIT price offset from current price, percent. Negative = below market.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually submit the order. Without this flag, dry-run only.",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"env: {settings.env}, account: {settings.account_cano}-{settings.account_prdt}")

    snapshot = fetch_current_price(args.symbol)
    raw_target = snapshot.price * (1 + args.offset_pct / 100)
    # Round to KRX tick (500 KRW above 100k seems to be the tick for blue chips,
    # but we just round to the nearest 100 — KIS will reject if invalid).
    target_price = int(round(raw_target / 100) * 100)
    side = Side.BUY if args.side == "buy" else Side.SELL

    intent = OrderIntent(
        symbol=args.symbol,
        side=side,
        quantity=args.qty,
        order_type="limit",
        limit_price=target_price,
        timestamp=datetime.now(UTC),
    )

    print(f"\nMarket price: {snapshot.price:,} KRW")
    print(
        f"Order intent: {side} {args.qty} @ {target_price:,} ({args.offset_pct:+.1f}% from market)"
    )

    # Risk gate. With current_position=0 (we're not tracking — paper test).
    risk = Risk(max_position_per_symbol=10)  # tiny cap for safety
    approved = risk.check(intent, current_position=0, realized_pnl_today_krw=0)
    if approved is None:
        print("Risk REJECTED — no submission.")
        return
    print(f"Risk APPROVED (qty after risk: {approved.quantity})")

    if not args.confirm:
        print("\n(dry run — pass --confirm to actually submit)")
        return

    # Brief sleep to avoid hitting mock rate limit after current price call.
    time.sleep(0.5)

    print("\nSubmitting to KIS...")
    try:
        result = KisOrderRouter(settings).submit(approved)
    except KisOrderRejected as e:
        print(f"  REJECTED by KIS: rt_cd={e.rt_cd}, msg={e.msg}")
        return

    print(f"  ACCEPTED: order_id={result.order_id}")
    print(f"  submitted_at={result.submitted_at.isoformat()}")
    print("\nNote: limit order placed below market — almost certainly will not fill.")
    print("Cancel via KIS app if you want to clean up the open order.")


if __name__ == "__main__":
    main()
