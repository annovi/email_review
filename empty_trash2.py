# empty_trash2.py — empty the iCloud Trash in small, visible, resumable steps.
#
# Run it yourself from the project folder (Mail quit on the Mac):
#     ./venv/bin/python empty_trash2.py
# Stop any time with Ctrl-C; running it again just continues where it was.
#
# Why not one big command: iCloud accepts "mark everything deleted + expunge" but
# processes it silently in the background, refuses it with NO [UNAVAILABLE] when it is
# busy, and drops long connections. This script instead takes the oldest CHUNK messages,
# marks them \Deleted and expunges exactly those (UID EXPUNGE), prints a progress line
# after every chunk, backs off when the server refuses, and reconnects when the
# connection drops. Output is unbuffered so it shows up live, also in the background.
import imaplib
import re
import socket
import sys
import time

from src.config import IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, APP_PASSWORD, TRASH_FOLDER

CHUNK = 1000             # messages per step
MAX_SET_CHARS = 1500     # keep the UID set short enough for iCloud
PAUSE_MIN, PAUSE_MAX = 60, 900

log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)


def connect():
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=300)
    m.login(EMAIL_ADDRESS, APP_PASSWORD)
    typ, data = m.select(f'"{TRASH_FOLDER}"')  # read-write
    if typ != "OK":
        raise RuntimeError(f"cannot open {TRASH_FOLDER}: {data}")
    return m, int(data[0])


def uid_set(uids):
    """Compact 'a:b,c,d:e' form, trimmed to MAX_SET_CHARS. Returns (set_string, uids_used)."""
    parts, used = [], 0
    i = 0
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


def first_uids(m, n):
    typ, data = m.fetch(f"1:{n}", "(UID)")
    if typ != "OK":
        raise imaplib.IMAP4.abort(f"FETCH failed: {data}")
    uids = sorted(int(x) for x in re.findall(rb"UID (\d+)", b"".join(d for d in data if isinstance(d, bytes))))
    return uids


def main():
    m, exists = connect()
    start_total = exists
    log(f"messages in Trash: {exists:,}")
    removed, pause, t0 = 0, PAUSE_MIN, time.time()
    while True:
        try:
            uids = first_uids(m, CHUNK)
            if not uids:
                log("Trash is empty.")
                break
            s, used = uid_set(uids)
            typ, data = m.uid("STORE", s, "+FLAGS.SILENT", r"(\Deleted)")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"STORE refused: {(data[0] or b'')[:160]}")
            typ, data = m.uid("EXPUNGE", s)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"EXPUNGE refused: {(data[0] or b'')[:160]}")
            typ, data = m.select(f'"{TRASH_FOLDER}"')
            left = int(data[0])
            step = exists - left
            exists = left
            removed += max(step, 0)
            pause = PAUSE_MIN
            rate = removed / max(time.time() - t0, 1) * 3600
            eta_h = exists / rate if rate else float("inf")
            log(f"removed {removed:,} so far ({step:,} this step), {exists:,} left, {rate:,.0f}/h, about {eta_h:.1f} h to go")
            if step <= 0:
                log("server reported no change for this chunk; waiting a minute before retrying")
                time.sleep(60)
        except KeyboardInterrupt:
            log(f"stopped by you; {removed:,} removed, {exists:,} left. Run again to continue.")
            break
        except imaplib.IMAP4.error as e:
            log(f"server refused ({str(e)[:160]}); pausing {pause}s")
            time.sleep(pause)
            pause = min(pause * 2, PAUSE_MAX)
        except (imaplib.IMAP4.abort, socket.error, OSError, EOFError) as e:
            log(f"connection lost ({str(e)[:120]}); reconnecting in 30s")
            time.sleep(30)
            try:
                m.logout()
            except Exception:
                pass
            while True:
                try:
                    m, exists = connect()
                    log(f"reconnected, {exists:,} left")
                    break
                except Exception as e2:
                    log(f"reconnect failed ({str(e2)[:120]}); retrying in 60s")
                    time.sleep(60)
    try:
        m.logout()
    except Exception:
        pass
    log(f"done: {start_total:,} at start, {exists:,} now.")


if __name__ == "__main__":
    main()
