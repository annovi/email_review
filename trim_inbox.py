# trim_inbox.py — shrink the INBOX to its newest N messages, keeping everything else only in Keep.
#
# Dry run (read-only):
#     ./venv/bin/python trim_inbox.py [--keep 500]
# Really do it (the number must equal the dry run's "to remove" total):
#     ./venv/bin/python trim_inbox.py --keep 500 --confirm 13382
#
# Safety gate: before anything is removed, every INBOX message outside the newest N must have a
# copy in Keep with the same Message-ID, Date, From and Subject. If even one is missing, the
# script stops and lists them; nothing is changed. Removal is flag + expunge in place (no Trash).
# Afterwards the newest N are marked read, and so is everything in Keep, so no badge lingers.
import imaplib
import json
import os
import sys
import time

from dedupe import connect, read_headers, delete_uids, log
from apply_review import busy, mark_read
from src.config import DATA_DIR

KEEP_FOLDER = "Keep"


def key(v):
    return (v["mid"].lower(), v["date"], v["frm"], v["subj"])


def main(argv):
    keep_n = int(argv[argv.index("--keep") + 1]) if "--keep" in argv else 500
    confirm = int(argv[argv.index("--confirm") + 1]) if "--confirm" in argv else None
    dry = confirm is None
    b = busy(("dedupe.py", "apply_review.py", "empty_trash2.py", "main.py execute"))
    if b:
        raise SystemExit(f"❌ {', '.join(b)} is still running against the mailbox. Wait for it, then run this again.")
    log(("DRY RUN: nothing will be changed" if dry else f"LIVE RUN: will remove up to {confirm:,} messages from INBOX") + f"; keeping the newest {keep_n:,}")

    m, exists = connect("INBOX", readonly=True)
    log(f"INBOX: {exists:,} messages, reading headers")
    inbox = read_headers(m, "INBOX", exists)
    m.logout()
    m, kexists = connect(KEEP_FOLDER, readonly=True)
    log(f"{KEEP_FOLDER}: {kexists:,} messages, reading headers")
    keep = read_headers(m, KEEP_FOLDER, kexists)
    m.logout()

    uids = sorted(inbox)
    newest = set(uids[-keep_n:])
    old = [u for u in uids if u not in newest]
    keep_keys = {key(v) for v in keep.values()}
    missing = [u for u in old if not inbox[u]["mid"] or key(inbox[u]) not in keep_keys]
    unread_new = [u for u in newest if not inbox[u]["seen"]]
    unread_keep = [u for u, v in keep.items() if not v["seen"]]
    log(f"INBOX: newest {len(newest):,} stay (oldest of them dated {inbox[min(newest)]['date'][:16] if newest else '-'}); "
        f"{len(old):,} older messages to remove; {len(missing):,} of those have NO copy in {KEEP_FOLDER}")
    log(f"to mark read afterwards: {len(unread_new):,} in INBOX, {len(unread_keep):,} in {KEEP_FOLDER}")
    if missing:
        log("❌ Stopping: these INBOX messages have no copy in Keep (first 20):")
        for u in missing[:20]:
            v = inbox[u]
            log(f"   UID {u}  {v['date'][:25]:25}  {v['frm'][:40]:40}  {v['subj'][:60]}")
        log("   Run `./venv/bin/python main.py preserve --new-only` (or tell Claude) and try again.")
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(DATA_DIR, f"trim_inbox_{stamp}.json"), "w") as f:
        json.dump({"at": stamp, "dry_run": dry, "keep_n": keep_n, "remove_uids": old, "keep_uids": sorted(newest)}, f)
    if dry:
        log(f"To apply, run:   ./venv/bin/python trim_inbox.py --keep {keep_n} --confirm {len(old)}")
        return
    if confirm != len(old):
        log(f"❌ --confirm {confirm:,} does not match the {len(old):,} messages found now. Nothing changed.")
        return

    m, exists = connect("INBOX", readonly=False)
    log(f"INBOX: removing {len(old):,} of {exists:,}")
    m, removed = delete_uids(m, "INBOX", old)
    log(f"INBOX: {removed:,} removed")
    if unread_new:
        m, _ = mark_read(m, "INBOX", unread_new)
    typ, data = m.status('"INBOX"', "(MESSAGES UNSEEN)")
    log(f"INBOX now {data[0].decode()}")
    m.logout()
    if unread_keep:
        m, _ = connect(KEEP_FOLDER, readonly=False)
        m, _ = mark_read(m, KEEP_FOLDER, unread_keep)
        typ, data = m.status(f'"{KEEP_FOLDER}"', "(MESSAGES UNSEEN)")
        log(f"{KEEP_FOLDER} now {data[0].decode()}")
        m.logout()
    log("all done")


if __name__ == "__main__":
    main(sys.argv[1:])
