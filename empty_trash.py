# empty_trash.py  —  permanently erase everything in the iCloud Trash folder.
# Run from the project folder:  ./venv/bin/python empty_trash.py
#
# Three IMAP commands, the same thing a mail client's "Empty Trash" does:
#   1. open the Trash folder read-write
#   2. mark every message \Deleted
#   3. EXPUNGE (removes every message marked \Deleted in the selected folder)
import imaplib
import time

from src.config import IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, APP_PASSWORD, TRASH_FOLDER

M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=900)   # expunging 54k messages can take minutes
M.login(EMAIL_ADDRESS, APP_PASSWORD)

typ, data = M.select(f'"{TRASH_FOLDER}"')                   # read-write
print(time.strftime("%H:%M:%S"), "messages in Trash:", data[0].decode())

typ, data = M.uid("STORE", "1:*", "+FLAGS.SILENT", r"(\Deleted)")
print(time.strftime("%H:%M:%S"), "mark all as deleted:", typ, (data[0] or b"")[:200] if typ != "OK" else "")

typ, data = M.expunge()                                     # removes every message marked deleted in Trash
if typ == "OK":
    print(time.strftime("%H:%M:%S"), "expunge:", typ, "removed:", len([d for d in data if d]))
else:
    print(time.strftime("%H:%M:%S"), "expunge:", typ, "server said:", (data[0] or b"")[:200])

typ, data = M.select(f'"{TRASH_FOLDER}"')
print(time.strftime("%H:%M:%S"), "left in Trash:", data[0].decode())
M.logout()
