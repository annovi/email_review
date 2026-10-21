# restore_from_trash.py — move messages from Trash ("Deleted Messages") back into Keep, by sender.
#
# Dry run (read-only):
#     ./venv/bin/python restore_from_trash.py --senders a@x.com,b@y.com
# Really do it (the number must equal the dry run's total):
#     ./venv/bin/python restore_from_trash.py --senders a@x.com,b@y.com --confirm 57
# A sender list can also come from a text file:   --senders-file picks.txt   (addresses are picked out of the text)
import re
import sys
import time
from email.utils import parseaddr

from dedupe import connect, read_headers, log
from apply_review import busy
from purge_senders import move_uids, TRASH

DEST = "Keep"


def main(argv):
    confirm = int(argv[argv.index("--confirm") + 1]) if "--confirm" in argv else None
    senders = set()
    if "--senders" in argv:
        senders |= set(a.strip().lower() for a in argv[argv.index("--senders") + 1].split(",") if a.strip())
    if "--senders-file" in argv:
        senders |= set(a.lower() for a in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", open(argv[argv.index("--senders-file") + 1]).read()))
    if not senders:
        raise SystemExit("give --senders a@x.com,b@y.com or --senders-file picks.txt")
    b = busy(("dedupe.py", "apply_review.py", "trim_inbox.py", "empty_trash2.py", "purge_senders.py", "main.py execute"))
    if b:
        raise SystemExit(f"❌ {', '.join(b)} is still running against the mailbox. Wait for it, then run this again.")
    dry = confirm is None
    log(("DRY RUN: nothing will be changed" if dry else f"LIVE RUN: will move up to {confirm:,} messages from {TRASH} to {DEST}") + f"; {len(senders)} senders")
    m, exists = connect(TRASH, readonly=True)
    log(f"{TRASH}: {exists:,} messages, reading headers")
    hdr = read_headers(m, TRASH, exists)
    m.logout()
    uids, per = [], {}
    for uid, v in hdr.items():
        addr = (parseaddr(v["frm"])[1] or v["frm"]).lower()
        if addr in senders:
            uids.append(uid)
            per[addr] = per.get(addr, 0) + 1
    for a in sorted(senders):
        log(f"   {per.get(a, 0):5,}  {a}" + ("" if per.get(a) else "   (nothing from this sender in Trash)"))
    log(f"TOTAL to move back: {len(uids):,}")
    if dry:
        log(f"To apply, run:   ./venv/bin/python restore_from_trash.py --senders {','.join(sorted(senders))} --confirm {len(uids)}")
        return
    if confirm != len(uids):
        log(f"❌ --confirm {confirm:,} does not match the {len(uids):,} found now. Nothing changed.")
        return
    m, exists = connect(TRASH, readonly=False)
    m, moved = move_uids(m, TRASH, sorted(uids), dest=DEST)
    typ, data = m.status(f'"{DEST}"', "(MESSAGES UNSEEN)")
    log(f"{moved:,} moved back; {DEST} now {data[0].decode()}")
    m.logout()
    log("done")


if __name__ == "__main__":
    main(sys.argv[1:])
