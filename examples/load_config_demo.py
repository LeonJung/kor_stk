"""Load a YAML portfolio config and print what was wired.

Run:
    uv run examples/load_config_demo.py [path/to/portfolio.yaml]

Defaults to configs/sample_portfolio.yaml.
"""

import sys
from pathlib import Path

from ks_ws.strategies.config import load_portfolio


def main() -> None:
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "configs" / "sample_portfolio.yaml"
    )

    print(f"Loading {path}")
    strategies, allocator = load_portfolio(path)

    print(f"\n=== {len(strategies)} strategies ===")
    for s in strategies:
        params = ", ".join(f"{k}={v}" for k, v in vars(s).items() if not k.startswith("_"))
        print(f"  - {s.name:<22} ({type(s).__name__})")
        if params:
            print(f"      {params}")

    print("\n=== Allocator ===")
    print(f"  max_position_per_symbol: {allocator.max_position_per_symbol}")
    print("  weights:")
    for s in strategies:
        weight = allocator.weight_for(s.name)
        print(f"    {s.name:<22} {weight:.2f}")


if __name__ == "__main__":
    main()
