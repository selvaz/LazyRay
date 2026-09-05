# -*- coding: utf-8 -*-
"""Send the LazyRay Brief (and optionally the old full country-risk report)
to Telegram.

Uses LazyTools' Telegram connector (same pattern as market-data-hub's
send_telegram_run_report.py). Configuration comes from environment:

    TELEGRAM_BOT_TOKEN   Bot token from BotFather
    TELEGRAM_CHAT_ID     Target chat id or @channel username

docs/DALIO_PROD_ASSESSMENT_2026-09.md/WP5: the digests were raw text nobody
read, and the old dalio_v2/report.py HTML (398KB, 64-row tables) looked
identical day to day even as the numbers under it changed. The LazyRay Brief
(lazyray.brief) replaces BOTH as what actually gets sent: its short ASCII
text is ALWAYS the message, and its own (small, self-contained) HTML is
ALWAYS attached by default -- the old full report is only attached with
--attach-full (or when --monthly/--monthly-auto decide this is the first
send of the month), exactly as report.py's monthly attachment worked before,
just under a name that says what it actually is now.

Sending needs the ``telegram`` extra; everything else here does not. Finding
the report, printing it with ``--dry-run``, and importing this module all work
in a bare install, and the import of the connector happens inside the one
function that sends -- so a missing extra is reported when someone tries to
send, naming what to install, instead of making the module unimportable.

Usage:
    pip install -e ".[telegram]"       # only needed to actually send
    python send_telegram_report.py
    python send_telegram_report.py --report-dir reports/dalio_v2
    python send_telegram_report.py --attach-full
    python send_telegram_report.py --monthly
    python send_telegram_report.py --monthly-auto   # first send of the month attaches the full report
    python send_telegram_report.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from lazyray.brief import build_brief
from lazyray.config_loader import get_settings
from lazyray.db.connection import get_conn
from lazyray.lock import db_write_lock

ROOT = Path(__file__).resolve().parent

#: What to do about it, said once and reused by the error below.
_TELEGRAM_HINT = (
    'sending needs the Telegram connector: pip install -e ".[telegram]" '
    "(LazyTools requires Python >= 3.11, so the extra cannot be installed on "
    "3.9 or 3.10 — the rest of this script works there)"
)

#: Telegram's hard cap on a text message body.
_TELEGRAM_TEXT_LIMIT = 4096


def _reports_root() -> Path:
    cfg = get_settings().get("reports", {})
    base = Path(cfg.get("dir") or "reports")
    if not base.is_absolute():
        base = ROOT / base
    return base


def _report_dir() -> Path:
    """The OLD full dalio_v2 report's directory (398KB HTML, unchanged --
    docs/DALIO_PROD_ASSESSMENT_2026-09.md keeps it as the monthly detail
    artifact)."""
    return _reports_root() / "dalio_v2"


def _brief_dir() -> Path:
    """The LazyRay Brief's own directory -- see lazyray/brief and
    run_dalio_v2.py, which writes lazyray_brief_<ref_date>.html here on
    every run."""
    return _reports_root() / "brief"


def _latest_report(report_dir: Path) -> Path:
    candidates = sorted(report_dir.glob("dalio_v2_*.html"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(
            f"No dalio_v2_*.html report found in {report_dir}; run run_dalio_v2.py first."
        )
    return candidates[-1]


def _ref_date_from_report(report: Path) -> date:
    """Parse ref_date back out of a "dalio_v2_<ref_date>.html" filename --
    the Brief is built from LazyRay's own DB, keyed by that same ref_date,
    not from anything inside the HTML file itself."""
    stem = report.stem  # "dalio_v2_2026-08-12"
    prefix = "dalio_v2_"
    if not stem.startswith(prefix):
        raise ValueError(f"Unrecognized report filename {report.name!r}")
    return date.fromisoformat(stem[len(prefix):])


def _brief_path(brief_dir: Path, ref_date: date) -> Path:
    return brief_dir / f"lazyray_brief_{ref_date}.html"


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


_DIGEST_LOG_DDL = (
    "CREATE TABLE IF NOT EXISTS digest_log ("
    "ref_date DATE PRIMARY KEY, attached_html BOOLEAN, sent_at TIMESTAMP)"
)


def monthly_attach_due(con: Any, ref_date: date) -> bool:
    """True until one send in ref_date's calendar month has attached the OLD
    full report. Stateful on purpose: with --if-changed a run on the 1st may
    be skipped, so "day <= 3" would silently drop the monthly attachment
    some months."""
    con.execute(_DIGEST_LOG_DDL)
    row = con.execute(
        "SELECT count(*) FROM digest_log WHERE attached_html AND "
        "date_trunc('month', ref_date) = date_trunc('month', ?::DATE)", [ref_date]).fetchone()
    return not row or row[0] == 0


def record_send(db_path: Any, ref_date: date, attached_full_report: bool) -> None:
    with db_write_lock(db_path):
        con = get_conn(db_path)
        try:
            con.execute(_DIGEST_LOG_DDL)
            con.execute("INSERT OR REPLACE INTO digest_log VALUES (?, ?, current_timestamp)",
                        [ref_date, attached_full_report])
        finally:
            con.close()


def send_digest_text(text: str, *, token: str, chat_id: str) -> None:
    TelegramClient = _telegram_client_class()
    with TelegramClient.from_token(token) as client:
        client.send_message(chat_id=chat_id, text=text[:_TELEGRAM_TEXT_LIMIT])


def _ensure_brief_html(con: Any, ref_date: date, brief_dir: Path) -> "tuple[str, Path]":
    """(brief_text, brief_path). run_dalio_v2.py already writes the Brief
    HTML on every run; this only (re)builds+writes it when that file is
    somehow missing (e.g. a manual send against an older run's DB state),
    so sending never depends on the two scripts being invoked in lockstep."""
    text, html = build_brief(con, ref_date)
    brief_path = _brief_path(brief_dir, ref_date)
    if not brief_path.exists():
        brief_dir.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(html, encoding="utf-8")
    return text, brief_path


def _attachment_line(brief_path: Path, attach_full: bool, full_report: Path) -> str:
    if attach_full:
        return f"HTML allegato: {brief_path.name} + mensile ({full_report.name})"
    return f"HTML allegato: {brief_path.name}"


def main() -> int:
    # Windows consoles/log redirects default to cp1252; never let a report
    # character kill the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description="Send the LazyRay Brief (and optionally the old full HTML "
                    "report) via Telegram")
    p.add_argument("--report-dir", help="Override the dalio_v2 (old full report) directory")
    p.add_argument("--brief-dir", help="Override the LazyRay Brief directory")
    p.add_argument("--db", help="LazyRay's own DuckDB path (read-only, for the Brief); "
                                "defaults to lazyray settings")
    p.add_argument("--attach-full", "--attach-html", dest="attach_full", action="store_true",
                   help="Also send the OLD full HTML report as a document attachment "
                        "(--attach-html is a deprecated alias)")
    p.add_argument("--monthly", action="store_true",
                   help="Same as --attach-full: the caller has decided this is the "
                        "first run of the month")
    p.add_argument("--monthly-auto", action="store_true",
                   help="Attach the OLD full report on the first send of each calendar "
                        "month (tracked in LazyRay's own DB, table digest_log)")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and print what would be sent, but do not send")
    args = p.parse_args()

    report_dir = Path(args.report_dir) if args.report_dir else _report_dir()
    brief_dir = Path(args.brief_dir) if args.brief_dir else _brief_dir()
    report = _latest_report(report_dir)
    ref_date = _ref_date_from_report(report)
    attach_full = args.attach_full or args.monthly

    con = get_conn(args.db)
    try:
        text, brief_path = _ensure_brief_html(con, ref_date, brief_dir)
        if args.monthly_auto and monthly_attach_due(con, ref_date):
            attach_full = True
    finally:
        con.close()

    message = text + "\n" + _attachment_line(brief_path, attach_full, report)

    if args.dry_run:
        print(f"Would send Brief (ref_date={ref_date}):")
        print(message)
        print(f"Would attach Brief HTML: {brief_path}")
        if attach_full:
            print(f"Would also attach full report: {report}")
        else:
            print(f"Full report NOT attached (pass --attach-full/--monthly): {report}")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        print(f"Brief was generated but not sent: {brief_path}", file=sys.stderr)
        return 2

    send_digest_text(message, token=token, chat_id=chat_id)
    print(f"Sent Telegram Brief (ref_date={ref_date})")
    send_report_document(brief_path, token=token, chat_id=chat_id,
                        caption=f"LazyRay Brief | {ref_date}")
    print(f"Sent Telegram Brief attachment: {brief_path.name}")
    if attach_full:
        caption = f"LazyRay Dalio v2 country risk report | {report.stem}"
        send_report_document(report, token=token, chat_id=chat_id, caption=caption)
        print(f"Sent Telegram full report attachment: {report.name}")
    record_send(args.db, ref_date, attach_full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
