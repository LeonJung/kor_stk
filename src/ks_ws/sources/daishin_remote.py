"""DaishinRemote — Ubuntu 에서 Windows PC (172.30.1.38) 의 대신증권 fetcher
가 만든 Parquet/SQLite 결과를 sync 받아 BarStore 에 적재.

사용자 결정 (2026-05-10): 키움 X, 대신증권 API O. KIS API 가 historical
분봉/시봉 미지원 → Windows PC 의 대신증권 OpenAPI 가 deep historical 받음.

V1 stub: Windows 측 fetcher 가 어떤 형식으로 데이터를 노출할지에 따라 sync
방식 분기. 현재 3 방식 지원:

1. **shared_folder** (CIFS mount): Windows samba 공유 폴더에 Parquet 출력
   → Ubuntu가 mount 후 read.
2. **rsync_ssh**: Windows OpenSSH 활성화 후 ``rsync owner@172.30.1.38:/path .``
3. **http_api**: Windows FastAPI 서버 노출 후 GET /bars/{symbol}/{timeframe}

본 V1 은 인터페이스 + rsync_ssh 구현만 (가장 단순). 다른 방식은 후속.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("ks_ws.sources.daishin_remote")

WindowsPC_IP_DEFAULT = "172.30.1.38"
WindowsPC_USER_DEFAULT = "owner"
WindowsPC_REMOTE_BASE_DEFAULT = "C:/ks_ws_export"  # 사용자 setup 시 정해질 path


@dataclass
class DaishinRemoteConfig:
    """Windows PC 에서 대신증권 fetcher 가 출력하는 위치 + sync 채널."""
    pc_ip: str = WindowsPC_IP_DEFAULT
    pc_user: str = WindowsPC_USER_DEFAULT
    remote_base: str = WindowsPC_REMOTE_BASE_DEFAULT
    channel: Literal["rsync_ssh", "shared_folder", "http_api"] = "rsync_ssh"
    # rsync_ssh
    ssh_key: str | None = None  # ~/.ssh/id_ed25519 등
    # shared_folder
    mount_path: str = "/mnt/daishin_share"
    # http_api
    api_url: str = "http://172.30.1.38:8000"


class DaishinRemoteSync:
    """Sync orchestrator. Pulls Parquet / SQLite from Windows PC into the
    local BarStore root."""

    def __init__(self, config: DaishinRemoteConfig, local_data_dir: Path | str) -> None:
        self.config = config
        self.local_data_dir = Path(local_data_dir)
        self.local_data_dir.mkdir(parents=True, exist_ok=True)

    def sync(self, *, timeframes: Iterable[str] = ("1m", "1h", "1d")) -> int:
        """Sync the requested timeframes. Returns total file count synced."""
        if self.config.channel == "rsync_ssh":
            return self._sync_rsync_ssh(timeframes)
        if self.config.channel == "shared_folder":
            return self._sync_shared_folder(timeframes)
        if self.config.channel == "http_api":
            raise NotImplementedError("http_api channel not yet implemented")
        raise ValueError(f"unknown channel {self.config.channel!r}")

    # ----- rsync over SSH ---------------------------------------------

    def _sync_rsync_ssh(self, timeframes: Iterable[str]) -> int:
        if shutil.which("rsync") is None:
            raise RuntimeError("rsync not installed on Ubuntu side")
        synced = 0
        for tf in timeframes:
            remote_path = f"{self.config.pc_user}@{self.config.pc_ip}:{self.config.remote_base}/bars/{tf}/"
            local_path = self.local_data_dir / "bars" / tf
            local_path.mkdir(parents=True, exist_ok=True)
            cmd = [
                "rsync", "-avz", "--update",
                "--rsync-path=rsync.exe",  # Windows rsync 가 cwRsync 등이면
            ]
            if self.config.ssh_key:
                cmd += ["-e", f"ssh -i {self.config.ssh_key}"]
            cmd += [remote_path, str(local_path) + "/"]
            log.info("rsync %s → %s", remote_path, local_path)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.warning("rsync %s failed: %s", tf, result.stderr[:300])
                continue
            # 간단 count: 새로 추가된 file lines
            added = sum(
                1 for line in result.stdout.splitlines()
                if line and not line.startswith(("sending", "sent", "total", "receiving"))
                and "/" in line
            )
            synced += added
            log.info("rsync %s synced ~%d entries", tf, added)
        return synced

    # ----- CIFS shared folder -----------------------------------------

    def _sync_shared_folder(self, timeframes: Iterable[str]) -> int:
        mount = Path(self.config.mount_path)
        if not mount.exists():
            raise RuntimeError(
                f"shared folder not mounted at {mount}; run "
                f"`sudo mount -t cifs //{self.config.pc_ip}/share {mount} ...`"
            )
        synced = 0
        for tf in timeframes:
            remote_dir = mount / "bars" / tf
            local_dir = self.local_data_dir / "bars" / tf
            local_dir.mkdir(parents=True, exist_ok=True)
            if not remote_dir.exists():
                log.warning("remote dir %s missing", remote_dir)
                continue
            for src in remote_dir.rglob("*.parquet"):
                rel = src.relative_to(remote_dir)
                dst = local_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(src, dst)
                    synced += 1
        return synced


def reachable(pc_ip: str = WindowsPC_IP_DEFAULT, timeout: float = 1.0) -> bool:
    """Quick ping check — does Windows PC respond at all? (returns True/False)"""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), pc_ip],
            capture_output=True, text=True, timeout=timeout + 1,
        )
        return result.returncode == 0
    except Exception:
        return False
