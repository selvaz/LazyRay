"""Run LazyRay's daily market-based systemic stress monitor."""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import date

from lazyray.db.connection import get_conn
from lazyray.lock import DBLockTimeout, db_write_lock
from lazyray.stress.config import REGIONS
from lazyray.stress.digest import build_digest, should_notify
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
    try:
        result = run_monitor(regions, hub_db=args.hub_db, db_path=args.db, as_of=args.as_of, write=not args.dry_run)
        if args.dry_run:
            # Use the normal persistence/digest path in a disposable database
            # so operators preview precisely the message a real run would
            # produce. Disposable + never contended, so no lock needed here.
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
        # run_monitor() takes and releases the write lock internally (via
        # persist.write_results), so by the time we get here it is free.
        # This block reopens the same DuckDB file to build the digest, and
        # it is a pure read: build_digest/should_notify only SELECT, and
        # write_results already ran apply_schema unconditionally under the
        # lock above (inside run_monitor), so the schema is guaranteed to
        # exist here -- reapplying it in this block would be redundant.
        # It still takes db_write_lock even though it only reads, because
        # DuckDB's rule -- one writer, or several readers -- is stricter
        # than "writers exclude writers": a reader and a writer refuse each
        # other in either direction, with an IOException at open that no
        # DBLockTimeout handler sees. The lock is therefore the coordination
        # point for every open of this file, in either mode, and every
        # opener in the scheduled chain now goes through it (see
        # lazyray/lock.py). Any contention here surfaces as DBLockTimeout,
        # handled below with exit 3.
        with db_write_lock(args.db):
            con = get_conn(args.db, read_only=True)
            try:
                digest = build_digest(con, regions, args.as_of)
                print(digest)
                warranted = should_notify(con, args.as_of)
            finally:
                con.close()
    except DBLockTimeout as exc:
        # The sibling ray_dalio_v2 job writes the same DuckDB file and can
        # still hold the lock when the two are triggered close together
        # (e.g. StartWhenAvailable catching up after a weekend). This
        # monitor recomputes every region's full history on each run, so a
        # skipped run loses nothing -- the next run rebuilds it all,
        # including today. Exit 3 ("nothing to do"), not a red task. This
        # holds whether the contention hits while run_monitor is writing or
        # while this command is only reopening the file to read the digest:
        # either way the rows (if any) are already committed and safe.
        print(f"{exc} The next run recomputes the full history, so nothing is lost.")
        return 3
    if args.notify or args.force_notify:
        if args.force_notify or warranted:
            _send(digest)
        else:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
