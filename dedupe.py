# dedupe.py — remove duplicate copies of the same message inside INBOX and Keep.
#
# Dry run (read-only, reports what it would remove):
#     ./venv/bin/python dedupe.py
# Really remove the extra copies (the number must equal the dry run's total):
#     ./venv/bin/python dedupe.py --confirm 21646
# One folder only:
#     ./venv/bin/python dedupe.py --folder INBOX [--confirm N]
#
# A duplicate is a message whose Message-ID, Date, From AND Subject all equal another
# message's in the same folder. Messages without a Message-ID are never touched. One copy
# per group is kept (a read copy if there is one, otherwise the oldest); the others are
# flagged \Deleted and expunged in place, 1,000 at a time, with the same back-off and
# reconnect logic as empty_trash2.py. Nothing is copied to Trash.
import imaplib
import json
import os
import re
import socket
import sys
import time
from collections import defaultdict, Counter

from src.config import IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, APP_PASSWORD, DATA_DIR

imaplib._MAXLINE = 50_000_000
FOLDERS = ["INBOX", "Keep"]
FETCH_STEP = 1500
CHUNK = 1000
MAX_SET_CHARS = 1500
PAUSE_MIN, PAUSE_MAX = 60, 900
HDR = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)]"

log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)


def connect(folder, readonly):
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=600)
    m.login(EMAIL_ADDRESS, APP_PASSWORD)
    typ, data = m.select(f'"{folder}"', readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"cannot open {folder}: {data}")
    return m, int(data[0])


def read_headers(m, folder, exists):
    """uid -> dict(mid, date, frm, subj, seen). Read-only (BODY.PEEK)."""
    out = {}
    for start in range(1, exists + 1, FETCH_STEP):
        end = min(start + FETCH_STEP - 1, exists)
        for attempt in range(3):
            try:
                typ, items = m.fetch(f"{start}:{end}", f"(UID FLAGS {HDR})")
                if typ != "OK":
                    raise imaplib.IMAP4.abort(f"FETCH {start}:{end} -> {typ}")
                break
            except (imaplib.IMAP4.abort, socket.error, OSError) as e:
                if attempt == 2:
                    raise
                log(f"  fetch {start}:{end} failed ({str(e)[:80]}); retrying in 15s")
                time.sleep(15)
        for it in items:
            if not isinstance(it, tuple):
                continue
            meta, raw = it[0], it[1]
            um = re.search(rb"UID (\d+)", meta)
            if not um:
                continue
            h = re.sub(r"\r?\n[ \t]+", " ", raw.decode("utf-8", "replace"))
            g = lambda name: (re.search(rf"^{name}:\s*(.*)$", h, re.I | re.M) or [None, ""])[1].strip()
            flags = (re.search(rb"FLAGS \(([^)]*)\)", meta) or [None, b""])[1].decode()
            out[int(um.group(1))] = {"mid": g("Message-ID"), "date": g("Date"), "frm": g("From"),
                                     "subj": g("Subject"), "seen": "\\Seen" in flags}
        if (start // FETCH_STEP) % 5 == 4:
            log(f"  {folder}: read {min(end, exists):,} / {exists:,}")
    return out


def find_duplicates(headers):
    groups = defaultdict(list)
    for uid, v in headers.items():
        if not v["mid"]:
            continue  # no Message-ID: never treated as a duplicate
        groups[(v["mid"].lower(), v["date"], v["frm"], v["subj"])].append(uid)
    to_delete, kept = [], {}
    for key, uids in groups.items():
        if len(uids) < 2:
            continue
        uids.sort()
        seen = [u for u in uids if headers[u]["seen"]]
        keep = seen[0] if seen else uids[0]
        kept[keep] = len(uids) - 1
        to_delete.extend(u for u in uids if u != keep)
    # safety: every kept copy must survive
    assert not set(kept) & set(to_delete)
    return sorted(to_delete), kept


def uid_set(uids):
    parts, used, i = [], 0, 0
    while i < len(uids):
        j = i
        while j + 1 < len(uids) and uids[j + 1] == uids[j] + 1:
            j += 1
        piece = str(uids[i]) if i == j else f"{uids[i]}:{uids[j]}"
        if sum(len(p) + 1 for p in parts) + len(piece) > MAX_SET_CHARS:
            break
        parts.append(piece)
        used = j + 1
        i = j + 1
    return ",".join(parts), used


def delete_uids(m, folder, uids):
    """Flag + UID EXPUNGE `uids` in chunks; returns number removed (by EXISTS delta)."""
    pending = list(uids)
    removed, pause, t0 = 0, PAUSE_MIN, time.time()
    typ, data = m.select(f'"{folder}"')
    exists = int(data[0])
    while pending:
        try:
            s, used = uid_set(pending[:CHUNK])
            typ, data = m.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"STORE refused: {(data[0] or b'')[:160]}")
            typ, data = m.uid("EXPUNGE", s)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"EXPUNGE refused: {(data[0] or b'')[:160]}")
            typ, data = m.select(f'"{folder}"')
            left = int(data[0])
            step, exists = exists - left, left
            removed += max(step, 0)
            pending = pending[used:]
            pause = PAUSE_MIN
            rate = removed / max(time.time() - t0, 1) * 3600
            log(f"  {folder}: removed {removed:,} ({step:,} this step), {len(pending):,} to go, "
                f"{left:,} left in folder, {rate:,.0f}/h")
            if step <= 0:
                log("  server reported no change for this chunk; waiting a minute")
                time.sleep(60)
        except KeyboardInterrupt:
            log(f"  stopped by you; {removed:,} removed, {len(pending):,} not done. Re-run to continue.")
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
    return m, removed


def main(argv):
    confirm = int(argv[argv.index("--confirm") + 1]) if "--confirm" in argv else None
    folders = [argv[argv.index("--folder") + 1]] if "--folder" in argv else FOLDERS
    dry = confirm is None
    log("DRY RUN: nothing will be changed" if dry else f"LIVE RUN: will remove up to {confirm:,} duplicate copies")

    plan, total = {}, 0
    for folder in folders:
        m, exists = connect(folder, readonly=True)
        log(f"{folder}: {exists:,} messages, reading headers")
        headers = read_headers(m, folder, exists)
        m.logout()
        to_delete, kept = find_duplicates(headers)
        no_mid = sum(1 for v in headers.values() if not v["mid"])
        senders = Counter(headers[u]["frm"][:60] for u in kept)
        log(f"{folder}: {len(headers):,} read, {no_mid:,} without Message-ID (ignored); "
            f"{len(kept):,} messages have extra copies; {len(to_delete):,} extra copies to remove; "
            f"{len(headers) - len(to_delete):,} would remain")
        for s, c in senders.most_common(5):
            log(f"    {c:5,} duplicated messages from {s}")
        plan[folder] = {"exists": exists, "to_delete": to_delete, "remain": len(headers) - len(to_delete)}
        total += len(to_delete)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    record = os.path.join(DATA_DIR, f"dedupe_{stamp}.json")
    with open(record, "w") as f:
        json.dump({"at": stamp, "dry_run": dry, "folders": plan}, f)
    log(f"TOTAL extra copies: {total:,}  (details saved to {record})")

    if dry:
        log(f"To remove them, run:   ./venv/bin/python dedupe.py --confirm {total}"
            + (f" --folder {folders[0]}" if len(folders) == 1 else ""))
        return
    if confirm != total:
        log(f"❌ --confirm {confirm:,} does not match the {total:,} duplicates found now. Nothing changed.")
        return
    for folder in folders:
        uids = plan[folder]["to_delete"]
        if not uids:
            continue
        m, exists = connect(folder, readonly=False)
        log(f"{folder}: removing {len(uids):,} extra copies from {exists:,} messages")
        m, removed = delete_uids(m, folder, uids)
        typ, data = m.select(f'"{folder}"', readonly=True)
        log(f"{folder}: done, {removed:,} removed, {int(data[0]):,} messages now (expected {plan[folder]['remain']:,})")
        m.logout()
    log("all done")


if __name__ == "__main__":
    main(sys.argv[1:])
