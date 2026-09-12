#!/usr/bin/env python3
"""
3DPrintTally — catalog reset.

Clears every product, printed part, saved job cost and label print job, leaving an
empty catalog ready for a fresh start under the bill-of-materials workflow.

KEPT:    settings (labor rate, electricity, machine wear, margins, scrap rate,
         sales tax, printer calibration offsets, QR/company config)
CLEARED: products, product_parts, job_costs, print_jobs, product photos,
         and the autoincrement counters, so new job IDs start at 1

Takes its own timestamped backup before touching anything, and refuses to run
unless you confirm at the prompt.

Run on the host, with the container stopped:
    cd /path/to/3dprinttally
    docker compose down
    python3 reset-catalog.py --yes

Without --yes it prompts for confirmation, which only works when you run it on its
own. Pasted inside a multi-line block, the shell feeds the NEXT line of your paste
to the prompt and the script aborts — so --yes is the right flag for a paste.
"""
import sqlite3
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "labels.db"
PHOTO_DIR = APP_DIR / "data" / "product-photos"

TABLES = ["product_parts", "products", "job_costs", "print_jobs"]


def main():
    if not DB_PATH.exists():
        sys.exit(f"No database at {DB_PATH} — run this from the stack directory.")

    con = sqlite3.connect(DB_PATH)
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    print(f"\nDatabase: {DB_PATH}\n")
    print("  Will be CLEARED:")
    for t in TABLES:
        if t in existing:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"    {t:<16} {n:>4} row(s)")
        else:
            print(f"    {t:<16}    – (table not present)")
    photos = [p for p in PHOTO_DIR.glob("*") if p.is_file() and p.name != ".gitkeep"] \
        if PHOTO_DIR.exists() else []
    print(f"    {'product photos':<16} {len(photos):>4} file(s)")

    if "settings" in existing:
        n = con.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
        print(f"\n  Will be KEPT:\n    {'settings':<16} {n:>4} row(s)")

    assume_yes = "--yes" in sys.argv or "-y" in sys.argv
    if assume_yes:
        print("\n--yes supplied — proceeding without a prompt.")
    elif not sys.stdin.isatty():
        # Being fed from a pipe or a pasted multi-line block. Refuse loudly rather than
        # reading the next pasted command as the answer and silently aborting.
        con.close()
        sys.exit("\nERROR: no interactive terminal. Re-run as:  python3 reset-catalog.py --yes\n"
                 "Nothing was changed.")
    else:
        answer = input('\nType RESET to confirm, anything else to abort: ').strip()
        if answer != "RESET":
            con.close()
            sys.exit(f"\nAborted (you entered {answer!r}) — nothing was changed.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.parent / f"labels-prereset-{stamp}.db"
    con.close()
    shutil.copy2(DB_PATH, backup)
    print(f"\nBackup written: {backup}")

    con = sqlite3.connect(DB_PATH)
    for t in TABLES:
        if t in existing:
            con.execute(f'DELETE FROM "{t}"')
    if "sqlite_sequence" in existing:
        con.executemany("DELETE FROM sqlite_sequence WHERE name=?",
                        [(t,) for t in TABLES])
    con.commit()

    for p in photos:
        p.unlink()

    con.execute("VACUUM")
    con.close()

    print("\nCleared. Remaining row counts:")
    con = sqlite3.connect(DB_PATH)
    for t in TABLES + (["settings"] if "settings" in existing else []):
        if t in existing:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"    {t:<16} {n:>4}")
    con.close()
    print("\nDone. Start the stack again with:  docker compose up -d\n")


if __name__ == "__main__":
    main()
