# gmail_categories.py — read-only overview of a Gmail mailbox by Gmail's own categories.
#
#     ./venv/bin/python gmail_categories.py --profile gmail
#
# For the All Mail label it counts, per category (Primary, Promotions, Social, Updates, Forums),
# how many messages there are in total and how many are older than 1, 2 and 5 years, using
# Gmail's search syntax over IMAP (X-GM-RAW). Nothing is fetched or changed. Use it to decide
# what the plan should target before running the full sender scan.
import imaplib
import sys
import time

from src.config import IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, APP_PASSWORD, PROFILE, PROVIDER

imaplib._MAXLINE = 50_000_000
CATEGORIES = ["primary", "promotions", "social", "updates", "forums"]
AGES = [("all", ""), ("older than 1y", "older_than:1y"), ("older than 2y", "older_than:2y"), ("older than 5y", "older_than:5y")]
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)


def count(m, raw):
    typ, data = m.uid("SEARCH", None, "X-GM-RAW", f'"{raw}"')
    if typ != "OK":
        raise SystemExit(f"search refused: {data}")
    return len(data[0].split()) if data and data[0] else 0


def main(argv):
    if PROVIDER != "gmail":
        raise SystemExit(f"profile '{PROFILE}' is {PROVIDER}, not Gmail. Run with --profile <gmail profile>.")
    if not EMAIL_ADDRESS or not APP_PASSWORD:
        raise SystemExit(f"no EMAIL_ADDRESS / APP_PASSWORD for profile '{PROFILE}'. Create .secrets.{PROFILE} first.")
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=300)
    m.login(EMAIL_ADDRESS, APP_PASSWORD)
    typ, boxes = m.list()
    allmail = next((b.decode().rsplit('"', 2)[-2] for b in boxes if b"\\All" in b), None)
    trash = next((b.decode().rsplit('"', 2)[-2] for b in boxes if b"\\Trash" in b), None)
    log(f"{EMAIL_ADDRESS}: {len(boxes)} labels; All Mail = {allmail!r}, Trash = {trash!r}")
    typ, data = m.select(f'"{allmail}"', readonly=True)
    total = int(data[0])
    log(f"All Mail: {total:,} messages")
    print(f"\n  {'category':12}" + "".join(f"{a:>16}" for a, _ in AGES))
    grand = {a: 0 for a, _ in AGES}
    for cat in CATEGORIES:
        row = []
        for label, age in AGES:
            n = count(m, f"category:{cat} {age}".strip())
            grand[label] += n
            row.append(n)
        print(f"  {cat:12}" + "".join(f"{n:>16,}" for n in row))
    print(f"  {'total':12}" + "".join(f"{grand[a]:>16,}" for a, _ in AGES))
    unread = count(m, "is:unread")
    attach = count(m, "has:attachment larger:5M")
    print(f"\n  unread: {unread:,}   with attachments over 5 MB: {attach:,}")
    m.logout()


if __name__ == "__main__":
    main(sys.argv[1:])
