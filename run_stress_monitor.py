"""Run LazyRay's daily market-based systemic stress monitor."""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import date

from lazyray.db.connection import get_conn
from lazyray.stress.config import REGIONS
from lazyray.stress.digest import build_digest, should_notify
from lazyray.stress.persist import apply_schema
from lazyray.stress.pipeline import run_monitor


def _send(text: str) -> None:
    try:
        from lazytools.connectors.telegram import TelegramClient
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError('sending needs the Telegram connector: pip install -e ".[telegram]"') from exc
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    with TelegramClient.from_token(token) as client:
        client.send_message(chat_id=chat_id, text=text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="all", help="us,ea,em or all")
    p.add_argument("--as-of", default=str(date.today()))
    p.add_argument("--db")
    p.add_argument("--hub-db")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--force-notify", action="store_true")
    args = p.parse_args()
    regions = tuple(REGIONS) if args.region == "all" else tuple(x.strip() for x in args.region.split(","))
    unknown = set(regions) - set(REGIONS)
    if unknown:
        p.error(f"unknown region(s): {', '.join(sorted(unknown))}")
    result = run_monitor(regions, hub_db=args.hub_db, db_path=args.db, as_of=args.as_of, write=not args.dry_run)
    if args.dry_run:
        # Use the normal persistence/digest path in a disposable database so
        # operators preview precisely the message a real run would produce.
        with tempfile.TemporaryDirectory() as directory:
            preview_db = os.path.join(directory, "stress-preview.duckdb")
            from lazyray.stress.persist import write_results
            write_results(result["index_rows"], result["components"], preview_db)
            con = get_conn(preview_db)
            try:
                print(build_digest(con, regions, args.as_of))
            finally:
                con.close()
        print("missing: " + repr(result["missing"]))
        return 0
    con = get_conn(args.db)
    try:
        apply_schema(con)
        digest = build_digest(con, regions, args.as_of)
        print(digest)
        warranted = should_notify(con, args.as_of)
    finally:
        con.close()
    if args.notify or args.force_notify:
        if args.force_notify or warranted:
            _send(digest)
        else:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
