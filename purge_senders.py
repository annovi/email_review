# purge_senders.py — move EVERY message (read or unread) from the senders marked Delete in the
# unread review (data/unread_review_<date>.json) out of INBOX and Keep into Trash ("Deleted Messages").
# Reversible until the Trash is emptied: review the Trash first, move back what matters, then empty it.
#
# Dry run (read-only):
#     ./venv/bin/python purge_senders.py
# Really do it (the number must equal the dry run's total):
#     ./venv/bin/python purge_senders.py --confirm 3275
#
# Options:
#     --trash-receipts            also trash the receipt/bill/booking senders in SPARE_RECEIPTS (default: spare them)
#     --exclude a@x.com,b@y.com   spare these senders too
#     --extra path.txt            also trash every sender address found in this file (e.g. a list copied from a review page)
# Senders the review marked Keep or Look first, or that it never saw, are never touched.
import imaplib
import json
import os
import re
import socket
import sys
import time
from email.utils import parseaddr

from dedupe import connect, read_headers, uid_set, log, CHUNK, PAUSE_MIN, PAUSE_MAX
from apply_review import busy, load_review, decide
from src.config import DATA_DIR

FOLDERS = ["INBOX", "Keep"]
TRASH = "Deleted Messages"

# Receipts, invoices, bills, statements and booking confirmations from stores and services other than
# Amazon. Spared by default because a purchase record may matter later. Parking, transit and fuel
# micro-receipts, notices, tickets and marketplace chatter are NOT in this list and still go to Trash.
SPARE_RECEIPTS = {
    # store and service receipts
    "no_reply@email.apple.com", "help@oddbunch.ca", "donotreply@oddbunch.ca", "messenger@messaging.squareup.com",
    "receipts@messaging.squareup.com", "orders@poshmark.com", "info@goodprotein.ca", "noreply@e.bestbuy.ca",
    "email@email.salomon.com", "account@email.canadiantire.ca", "kitsilano@baileynelson.com", "info@hitechonline.it",
    "receipts@moneris.com", "app@clover.com", "receipts@lush.com", "news@em.mec.ca", "info@redcanoebrands.com",
    "donotreply@audible.ca", "hello@ooni.com", "do-not-reply@shell.com", "noreply@c.levi.com", "bitdefender@2checkout.com",
    "transaction@transaction-metro.ca", "no-reply@toasttab.com", "ordermessages@orders.discogs.com", "donotreply@fresh.ca",
    "no-reply@excalidraw.com", "noreply@mobibikes.ca",
    # bills and statements
    "fatture@vodafone.it", "donotreply@mail.schwab.com", "notifications@o.wealthsimple.com", "noreply@scotiabank.com",
    "onlineservices@rogersbank.com", "ebill@bell.ca", "koodobilling@mailing.koodomobile.com",
    "edelivery@etradefrommorganstanley.com", "notifiche@serverplan.com",
    # bookings and confirmations
    "confirmations@camis.com", "whistler_lodge@portal.dormproject.ch", "express@airbnb.com", "no-reply@opentable.ca",
    "vancouverboardofparksandrecreation@active.com", "business537581@mindbodyonline.com", "no-reply@recurpay.com",
    "emails@secure-booker.com",
}
SPARE_PREFIXES = ("receipts+acct_", "do-not-reply_ne-pas-repondre_arrivecan@")


def spared(addr, spare_receipts):
    return spare_receipts and (addr in SPARE_RECEIPTS or addr.startswith(SPARE_PREFIXES))


def move_uids(m, folder, uids, dest=TRASH):
    """COPY to dest, flag \\Deleted, UID EXPUNGE, in chunks; returns (connection, number moved by EXISTS delta)."""
    pending = list(uids)
    moved, pause, t0 = 0, PAUSE_MIN, time.time()
    typ, data = m.select(f'"{folder}"')
    exists = int(data[0])
    while pending:
        try:
            s, used = uid_set(pending[:CHUNK])
            typ, data = m.uid("COPY", s, f'"{dest}"')
            if typ != "OK":
                raise imaplib.IMAP4.error(f"COPY refused: {(data[0] or b'')[:160]}")
            typ, data = m.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"STORE refused: {(data[0] or b'')[:160]}")
            typ, data = m.uid("EXPUNGE", s)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"EXPUNGE refused: {(data[0] or b'')[:160]}")
            typ, data = m.select(f'"{folder}"')
            left = int(data[0])
            step, exists = exists - left, left
            moved += max(step, 0)
            pending = pending[used:]
            pause = PAUSE_MIN
            rate = moved / max(time.time() - t0, 1) * 3600
            log(f"  {folder}: moved {moved:,} ({step:,} this step), {len(pending):,} to go, {left:,} left in folder, {rate:,.0f}/h")
            if step <= 0:
                log("  server reported no change for this chunk; waiting a minute")
                time.sleep(60)
        except KeyboardInterrupt:
            log(f"  stopped by you; {moved:,} moved, {len(pending):,} not done. Re-run to continue.")
            raise SystemExit(1)
        except imaplib.IMAP4.error as e:
            log(f"  server refused ({str(e)[:160]}); pausing {pause}s")
            time.sleep(pause)
            pause = min(pause * 2, PAUSE_MAX)
        except (imaplib.IMAP4.abort, socket.error, OSError, EOFError) as e:
            log(f"  connection lost ({str(e)[:120]}); reconnecting in 30s")
            time.sleep(30)
            while True:
                try:
                    m, exists = connect(folder, readonly=False)
                    log(f"  reconnected, {exists:,} in {folder}")
                    break
                except Exception as e2:
                    log(f"  reconnect failed ({str(e2)[:120]}); retrying in 60s")
                    time.sleep(60)
    return m, moved


def main(argv):
    confirm = int(argv[argv.index("--confirm") + 1]) if "--confirm" in argv else None
    spare_receipts = "--trash-receipts" not in argv
    exclude = set(a.strip().lower() for a in argv[argv.index("--exclude") + 1].split(",")) if "--exclude" in argv else set()
    extra = set()
    if "--extra" in argv:
        extra = set(a.lower() for a in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", open(argv[argv.index("--extra") + 1]).read()))
    dry = confirm is None
    b = busy(("dedupe.py", "apply_review.py", "trim_inbox.py", "empty_trash2.py", "main.py execute"))
    if b:
        raise SystemExit(f"❌ {', '.join(b)} is still running against the mailbox. Wait for it, then run this again.")
    path, rv = load_review()
    log(("DRY RUN: nothing will be changed" if dry else f"LIVE RUN: will move up to {confirm:,} messages to {TRASH}")
        + f"  (review file: {path}; receipts {'spared' if spare_receipts else 'trashed too'}; {len(extra)} extra senders; {len(exclude)} excluded)")

    plan, total, per_sender, spared_count = {}, 0, {}, {}
    for folder in FOLDERS:
        m, exists = connect(folder, readonly=True)
        log(f"{folder}: {exists:,} messages, reading headers")
        hdr = read_headers(m, folder, exists)
        m.logout()
        uids, unread = [], 0
        for uid, v in hdr.items():
            addr = (parseaddr(v["frm"])[1] or v["frm"]).lower()
            if addr in exclude:
                continue
            if addr in extra or decide(addr, rv) == "delete":
                if spared(addr, spare_receipts) and addr not in extra:
                    spared_count[addr] = spared_count.get(addr, 0) + 1
                    continue
                uids.append(uid)
                unread += 0 if v["seen"] else 1
                per_sender[addr] = per_sender.get(addr, 0) + 1
        log(f"{folder}: {len(uids):,} messages to move to Trash ({unread:,} unread), {exists - len(uids):,} would remain")
        plan[folder] = sorted(uids)
        total += len(uids)
    log(f"TOTAL to move: {total:,} from {len(per_sender):,} senders; spared as receipts/bills: "
        f"{sum(spared_count.values()):,} messages from {len(spared_count):,} senders. Biggest to move:")
    for a, n in sorted(per_sender.items(), key=lambda x: -x[1])[:15]:
        log(f"   {n:5,}  {a}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(DATA_DIR, f"purge_senders_{stamp}.json"), "w") as f:
        json.dump({"at": stamp, "dry_run": dry, "review_file": path, "spare_receipts": spare_receipts, "exclude": sorted(exclude),
                   "extra": sorted(extra), "senders": per_sender, "spared": spared_count, "folders": plan}, f)
    if dry:
        opts = ("" if spare_receipts else " --trash-receipts") + (f" --exclude {','.join(sorted(exclude))}" if exclude else "") \
            + (f" --extra {argv[argv.index('--extra') + 1]}" if extra else "")
        log(f"To apply, run:   ./venv/bin/python purge_senders.py --confirm {total}{opts}")
        return
    if confirm != total:
        log(f"❌ --confirm {confirm:,} does not match the {total:,} found now. Nothing changed.")
        return
    for folder in FOLDERS:
        if not plan[folder]:
            continue
        m, exists = connect(folder, readonly=False)
        log(f"{folder}: moving {len(plan[folder]):,} of {exists:,} to {TRASH}")
        m, moved = move_uids(m, folder, plan[folder])
        typ, data = m.status(f'"{folder}"', "(MESSAGES UNSEEN)")
        log(f"{folder}: {moved:,} moved, now {data[0].decode()}")
        typ, data = m.status(f'"{TRASH}"', "(MESSAGES)")
        log(f"{TRASH}: now {data[0].decode()}")
        m.logout()
    log("all done — review the Trash, move back anything important, then empty it")


if __name__ == "__main__":
    main(sys.argv[1:])
