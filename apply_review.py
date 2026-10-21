# apply_review.py — apply the unread-mail review (data/unread_review_<date>.json) to INBOX and Keep.
#
# Dry run (read-only, shows what would happen):
#     ./venv/bin/python apply_review.py
# Really do it (the number must equal the dry run's "to delete" total):
#     ./venv/bin/python apply_review.py --confirm 3203
#
# For every UNREAD message in each folder, the sender's decision in the review file says:
#   delete  -> the message is flagged \Deleted and expunged in place (no Trash detour)
#   keep    -> the message is marked read
#   check   -> left exactly as it is (unread), for you to look at
#   unknown -> left as it is (mail from senders that were not in the review, e.g. arrived later)
# Read messages are never touched. Nothing is copied anywhere.
import imaplib
import json
import os
import socket
import sys
import time
import glob
import subprocess
from email.utils import parseaddr

from dedupe import connect, read_headers, uid_set, delete_uids, log
from src.config import DATA_DIR

FOLDERS = ["INBOX", "Keep"]
CHUNK = 1000


def busy(names=("dedupe.py", "trim_inbox.py", "empty_trash2.py", "main.py execute")):
    out = subprocess.run(["pgrep", "-fl", "python"], capture_output=True, text=True).stdout
    return [n for n in names if n in out]


def load_review():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "unread_review_*.json")))
    if not files:
        raise SystemExit(f"no unread_review_*.json found in {DATA_DIR}/")
    rv = json.load(open(files[-1]))
    return files[-1], rv


def decide(addr, rv):
    s = rv["senders"].get(addr)
    if s:
        return s["action"]
    for p, v in rv["prefixes"].items():
        if addr.startswith(p):
            return v["action"]
    d = addr.split("@")[-1]
    if d in rv["domains"]:
        return rv["domains"][d]["action"]
    return "unknown"


def mark_read(m, folder, uids):
    pending = sorted(uids)
    done, pause = 0, 60
    while pending:
        s, used = uid_set(pending[:CHUNK])
        try:
            typ, data = m.uid("STORE", s, "+FLAGS.SILENT", r"(\Seen)")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"STORE refused: {(data[0] or b'')[:160]}")
            pending = pending[used:]
            done += used
            pause = 60
            log(f"  {folder}: marked read {done:,}, {len(pending):,} to go")
        except imaplib.IMAP4.error as e:
            log(f"  server refused ({str(e)[:120]}); pausing {pause}s")
            time.sleep(pause)
            pause = min(pause * 2, 900)
        except (imaplib.IMAP4.abort, socket.error, OSError, EOFError) as e:
            log(f"  connection lost ({str(e)[:100]}); reconnecting in 30s")
            time.sleep(30)
            m, _ = connect(folder, readonly=False)
    return m, done


def main(argv):
    confirm = int(argv[argv.index("--confirm") + 1]) if "--confirm" in argv else None
    dry = confirm is None
    b = busy()
    if b:
        raise SystemExit(f"❌ {', '.join(b)} is still running against the mailbox. Wait for it to finish, then run this again.")
    path, rv = load_review()
    log(("DRY RUN: nothing will be changed" if dry else f"LIVE RUN: will delete up to {confirm:,} unread messages") + f"  (review file: {path})")

    plan, total_del = {}, 0
    for folder in FOLDERS:
        m, exists = connect(folder, readonly=True)
        log(f"{folder}: {exists:,} messages, reading headers")
        hdr = read_headers(m, folder, exists)
        m.logout()
        acts = {"delete": [], "keep": [], "check": [], "unknown": []}
        for uid, v in hdr.items():
            if v["seen"]:
                continue
            addr = (parseaddr(v["frm"])[1] or v["frm"]).lower()
            acts[decide(addr, rv)].append(uid)
        unread = sum(len(x) for x in acts.values())
        log(f"{folder}: {unread:,} unread -> delete {len(acts['delete']):,}, mark read {len(acts['keep']):,}, "
            f"leave for you to look at {len(acts['check']):,}, leave (sender not reviewed) {len(acts['unknown']):,}")
        plan[folder] = acts
        total_del += len(acts["delete"])

    log(f"TOTAL to delete: {total_del:,}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(DATA_DIR, f"apply_review_{stamp}.json"), "w") as f:
        json.dump({"at": stamp, "dry_run": dry, "review_file": path,
                   "folders": {k: {a: sorted(u) for a, u in v.items()} for k, v in plan.items()}}, f)
    if dry:
        log(f"To apply, run:   ./venv/bin/python apply_review.py --confirm {total_del}")
        return
    if confirm != total_del:
        log(f"❌ --confirm {confirm:,} does not match the {total_del:,} found now. Nothing changed.")
        return
    for folder in FOLDERS:
        acts = plan[folder]
        m, exists = connect(folder, readonly=False)
        if acts["delete"]:
            log(f"{folder}: deleting {len(acts['delete']):,} unread messages from {exists:,}")
            m, removed = delete_uids(m, folder, sorted(acts["delete"]))
            log(f"{folder}: {removed:,} removed")
        if acts["keep"]:
            log(f"{folder}: marking {len(acts['keep']):,} messages read")
            m, done = mark_read(m, folder, acts["keep"])
        typ, data = m.status(f'"{folder}"', "(MESSAGES UNSEEN)")
        log(f"{folder}: now {data[0].decode()}")
        m.logout()
    log("all done")


if __name__ == "__main__":
    main(sys.argv[1:])
