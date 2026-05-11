"""Orchestrator: pull Daishin Securities historical bars from Windows PC.

Usage::

    PYTHONPATH=src .venv/bin/python -m scripts.sync_daishin_data \
        --channel rsync_ssh --timeframes 1m 1h 1d

Idempotent — re-runs only fetch newer files via rsync --update.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_daishin_data")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--channel", choices=["rsync_ssh", "shared_folder", "http_api"], default="rsync_ssh")
    p.add_argument("--pc-ip", default="172.30.1.38")
    p.add_argument("--pc-user", default="owner")
    p.add_argument("--remote-base", default="C:/ks_ws_export")
    p.add_argument("--ssh-key", default=None)
    p.add_argument("--mount-path", default="/mnt/daishin_share")
    p.add_argument("--timeframes", nargs="+", default=["1m", "1h", "1d"])
    p.add_argument("--ping-only", action="store_true",
                   help="just check Windows PC reachability and exit")
    args = p.parse_args()

    from ks_ws.sources.daishin_remote import (
        DaishinRemoteConfig, DaishinRemoteSync, reachable,
    )

    log.info("Probing Windows PC %s ...", args.pc_ip)
    if not reachable(args.pc_ip, timeout=2):
        log.error("Windows PC %s not reachable. Check network / firewall.", args.pc_ip)
        return 2
    log.info("Windows PC reachable.")
    if args.ping_only:
        return 0

    cfg = DaishinRemoteConfig(
        pc_ip=args.pc_ip, pc_user=args.pc_user,
        remote_base=args.remote_base, channel=args.channel,
        ssh_key=args.ssh_key, mount_path=args.mount_path,
    )
    syncer = DaishinRemoteSync(cfg, args.data_dir)
    try:
        n = syncer.sync(timeframes=args.timeframes)
        log.info("Sync complete — %d entries synced", n)
    except Exception as e:
        log.error("Sync failed: %s", e)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
