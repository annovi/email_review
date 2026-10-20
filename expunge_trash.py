# expunge_trash.py — ask iCloud once an hour to purge the Trash messages already marked deleted.
#
# Run it yourself from the project folder (Mail quit on the Mac):
#     ./venv/bin/python expunge_trash.py
# Stop any time with Ctrl-C.
#
# Background: a "mark everything deleted" request sent on 19 Oct is still being applied by
# iCloud at roughly 170 messages a minute, and while it works the server answers every
# EXPUNGE with "[UNAVAILABLE] Server Busy". This script does nothing but report how far the
# marking has got and send ONE expunge attempt per hour; it exits when the Trash is empty.
import imaplib
import socket
import sys
import time

from src.config import IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, APP_PASSWORD, TRASH_FOLDER

imaplib._MAXLINE = 50_000_000      # SEARCH over 300k messages returns a very long line
EVERY = 3600                       # seconds between attempts

log = lambda *a: print(time.strftime("%F %T"), *a, flush=True)


def attempt():
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=1800)
    m.login(EMAIL_ADDRESS, APP_PASSWORD)
    typ, data = m.select(f'"{TRASH_FOLDER}"')          # read-write
    total = int(data[0])
    typ, d = m.search(None, "DELETED")
    marked = len(d[0].split()) if typ == "OK" and d and d[0] else -1
    log(f"in Trash: {total:,}; marked deleted: {marked:,}; not yet marked: {total - marked:,}")
    if total == 0:
        m.logout()
        return 0
    typ, data = m.expunge()
    if typ == "OK":
        typ2, data2 = m.select(f'"{TRASH_FOLDER}"')
        left = int(data2[0])
        log(f"EXPUNGE accepted: {total - left:,} removed, {left:,} left")
    else:
        log(f"EXPUNGE refused: {(data[0] or b'')[:160]}")
        left = total
    m.logout()
    return left


def main():
    while True:
        try:
            left = attempt()
            if left == 0:
                log("Trash is empty. Done.")
                return
        except KeyboardInterrupt:
            log("stopped by you")
            return
        except Exception as e:
            log(f"error: {str(e)[:160]}")
        try:
            log(f"next attempt in {EVERY // 60} minutes")
            time.sleep(EVERY)
        except KeyboardInterrupt:
            log("stopped by you")
            return


if __name__ == "__main__":
    main()
