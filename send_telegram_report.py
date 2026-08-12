# -*- coding: utf-8 -*-
"""Send the latest Dalio v2 country-risk report to Telegram.

Uses LazyTools' Telegram connector (same pattern as market-data-hub's
send_telegram_run_report.py). Configuration comes from environment:

    TELEGRAM_BOT_TOKEN   Bot token from BotFather
    TELEGRAM_CHAT_ID     Target chat id or @channel username

Sending needs the ``telegram`` extra; everything else here does not. Finding
the report, printing it with ``--dry-run``, and importing this module all work
in a bare install, and the import of the connector happens inside the one
function that sends -- so a missing extra is reported when someone tries to
send, naming what to install, instead of making the module unimportable.

Usage:
    pip install -e ".[telegram]"       # only needed to actually send
    python send_telegram_report.py
    python send_telegram_report.py --report-dir reports/dalio_v2
    python send_telegram_report.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from lazyray.config_loader import get_settings

ROOT = Path(__file__).resolve().parent

#: What to do about it, said once and reused by the error below.
_TELEGRAM_HINT = (
    'sending needs the Telegram connector: pip install -e ".[telegram]" '
    "(LazyTools requires Python >= 3.11, so the extra cannot be installed on "
    "3.9 or 3.10 — the rest of this script works there)"
)


def _report_dir() -> Path:
    cfg = get_settings().get("reports", {})
    base = Path(cfg.get("dir") or "reports")
    if not base.is_absolute():
        base = ROOT / base
    return base / "dalio_v2"


def _latest_report(report_dir: Path) -> Path:
    candidates = sorted(report_dir.glob("dalio_v2_*.html"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(
            f"No dalio_v2_*.html report found in {report_dir}; run run_dalio_v2.py first."
        )
    return candidates[-1]


def _telegram_client_class() -> Any:
    """Import the connector, and say what to install when it is not there.

    Imported here rather than at module scope so that importing this file,
    resolving a report and ``--dry-run`` all work without the extra.

    Only the connector's own absence is turned into advice. A LazyTools that
    is installed but raises while importing is a different problem, and
    swallowing it under an "install the extra" message would send someone to
    fix something that is not broken.
    """
    try:
        from lazytools.connectors.telegram import TelegramClient
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] != "lazytools":
            raise
        raise ModuleNotFoundError(f"{exc}; {_TELEGRAM_HINT}") from exc
    return TelegramClient


def send_report_document(file_path: Path, *, token: str, chat_id: str, caption: str) -> None:
    TelegramClient = _telegram_client_class()
    blob = file_path.read_bytes()
    with TelegramClient.from_token(token) as client:
        client.send_document(
            chat_id=chat_id,
            document=blob,
            filename=file_path.name,
            caption=caption[:1024],
        )


def main() -> int:
    p = argparse.ArgumentParser(description="Send the latest LazyRay Dalio v2 report via Telegram")
    p.add_argument("--report-dir", help="Override the dalio_v2 report directory")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and print the report path, but do not send")
    args = p.parse_args()

    report_dir = Path(args.report_dir) if args.report_dir else _report_dir()
    report = _latest_report(report_dir)
    caption = f"LazyRay Dalio v2 country risk report | {report.stem}"

    if args.dry_run:
        print(f"Would send: {report}")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        print(f"Report was generated but not sent: {report}", file=sys.stderr)
        return 2

    send_report_document(report, token=token, chat_id=chat_id, caption=caption)
    print(f"Sent Telegram report attachment: {report.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
