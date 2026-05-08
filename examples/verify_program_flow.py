"""Live verification of the KIS program-trade endpoint mapping.

Calls ``kis_program_flow_fetcher`` for one symbol and prints what came
back — verifies tr_id and field-name assumptions in
``ks_ws.sources.program_flow``.

If this returns 0 with a logged warning about the missing field, the
field-name fallback list in kis_program_flow_fetcher needs an update
to match KIS's current spec.

Run:
    uv run examples/verify_program_flow.py [SYMBOL]
"""

import logging
import sys

from ks_ws.sources.program_flow import kis_program_flow_fetcher


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    print(f"Calling kis_program_flow_fetcher({symbol!r})...")
    net = kis_program_flow_fetcher(symbol)
    print(f"\n  net program-buy KRW: {net:,}")
    if net == 0:
        print("\n  Note: 0 may indicate either real zero net flow OR a field-name miss.")
        print("  Check the WARNING log line above for 'missing net-flow field'.")
    else:
        print("\n  endpoint + field mapping verified.")


if __name__ == "__main__":
    main()
