# Email Reviewer

Clean a very large iCloud mailbox (hundreds of thousands of emails) one **sender**
at a time: scan every header once, group by sender, let Claude judge each sender
against a keep policy, review the plan, then move the flagged senders' mail to
Trash in bulk. A manual per-email review mode is also included.

## How it works

1. **Scan** — fetches `From / Subject / Date / List-Unsubscribe / Precedence` for
   every message in every folder (except Drafts/Sent/Trash/Notes) and groups them by
   sender into `data/sender_index.json`. Resumable: progress is checkpointed
   continuously, so a dropped connection or Ctrl-C costs at most a few chunks.
   ~25 minutes per 400k emails.
2. **Classify** — Claude judges each sender (address, volume, share of mail with a
   bulk-mail marker, date range, recent subjects) against the keep policy in
   `src/categorizer.py`: keep mail with information you may need later (people,
   receipts, finance, travel, documents); delete promotions, newsletters, social
   notifications, one-time codes, spam. Mixed or unsure → keep. Verdicts are
   structured JSON keyed by sender, so nothing can shift onto the wrong sender.
   Runs as a Message Batch by default (async, 50% cost) or synchronously.
3. **Plan** — flag every sender Claude said to delete, print the summary (totals,
   by category, by folder, top deleted senders, top *kept* senders as a sanity
   check) and write `data/plan_*.json`.
4. **Execute** — move the plan's messages to Trash (reversible for ~30 days under
   iCloud's default), folder by folder, in large sequence sets. Requires typing the
   exact number of emails. Hard delete (permanent) is only allowed for folders listed
   in `HARD_DELETE_ALLOWED_FOLDERS` (Junk, promotions, Trash).

## Setup

1. **Get an App-Specific Password** from Apple:
   https://appleid.apple.com → Sign-In and Security → App-Specific Passwords
2. **Install dependencies** (Python 3.10+):
   ```bash
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Configure credentials**: `cp .secrets.example .secrets` and fill in
   `EMAIL_ADDRESS`, `APP_PASSWORD`, `CLAUDE_API_KEY` (and optionally `CLAUDE_MODEL`).
4. **A Gmail account as well, or instead?** See
   [Several mailboxes: iCloud and Gmail](#several-mailboxes-icloud-and-gmail) below.

## Usage

Headless pipeline (each step is resumable; run one at a time):

```bash
python main.py scan                  # build/resume the sender index
python main.py classify              # submit unanalyzed senders as a Message Batch
python main.py collect --wait        # pull the verdicts into the index when the batch ends
python main.py status                # what has been scanned / classified / flagged
python main.py plan                  # flag Claude's "delete" senders, print summary, write plan
python main.py execute data/plan_X.json --dry-run
python main.py execute data/plan_X.json --confirm <total_emails>
```

Useful variants: `classify --sync` (call the API now instead of a batch),
`classify --min-count 5` (skip senders with fewer emails), `plan --bulk` (also flag
unanalyzed senders whose mail is ≥80% bulk-marked), `execute … --hard` (permanent,
eligible folders only, plan must be mode=hard).

The scan and execute steps run over IMAP from this machine, so it must stay awake
and online while they run (`caffeinate -i python main.py scan`). Classification runs
on Anthropic's side; the machine can be off while a batch processes.

Interactive menu: `python main.py` — option 3 opens the sender table (flag with
`f 1 2 5-10`, `auto` flags Claude's delete verdicts, `bulk` flags unanalyzed bulk
senders, `ai` classifies, `x` executes to Trash). `python main.py --stats` shows
folder counts.

## Project Structure

```
email_review/
├── main.py                # menu + headless subcommands; --profile NAME picks the mailbox
├── gmail_categories.py    # read-only: Gmail Promotions/Social/Updates/Forums counts by age
├── dedupe.py              # iCloud helpers: duplicates, unread-review apply, Inbox trim,
├── apply_review.py        #   sender purge to Trash, restore from Trash (dry run by default,
├── trim_inbox.py          #   --confirm N must match the live count)
├── purge_senders.py
├── restore_from_trash.py
├── .secrets.example       # iCloud credentials template  -> .secrets
├── .secrets.gmail.example # Gmail credentials template   -> .secrets.gmail
├── src/
│   ├── config.py          # settings, provider defaults (icloud | gmail), profile resolution
│   ├── imap_client.py     # UID-based IMAP client: reconnect/retry, range fetch, special-use
│   │                      #   folder lookup, MOVE where offered else COPY/STORE/UID EXPUNGE
│   ├── sender_groups.py   # SenderGroup / SenderIndex + resumable scan
│   ├── categorizer.py     # keep policy, structured-output requests, sync + Batches
│   ├── deleter.py         # plan build/execute, per-folder merged sequence sets
│   ├── storage.py         # atomic JSON persistence
│   └── ui.py              # terminal helpers
└── data/                  # gitignored: sender_index.json, plan_*.json, execution_*.json;
    └── gmail/             #   each non-default profile keeps its own files here
```

## Notes on iCloud IMAP

- No `MOVE` capability; `UIDPLUS` is present. Moves are `COPY` + `\Deleted` +
  `UID EXPUNGE`, so only the targeted messages are ever expunged.
- `UID SEARCH ALL` comes back unsorted and, on INBOX, ~10% larger than the
  message count (phantom UIDs that `FETCH` silently skips). The client sorts and
  the scan counts only headers actually returned.
- Trash still counts toward iCloud storage until it is emptied.

## Categories

| Category | Description |
|----------|-------------|
| 📰 newsletters | News digests, daily updates, blogs |
| 🏷️ promotions | Marketing, sales, discounts |
| 👥 social | LinkedIn, social networks |
| 💰 finance | Banks, investments, payments |
| ✈️ travel | Airlines, hotels, bookings |
| 📦 shopping | Orders, shipping, receipts |
| 💪 fitness | Fitness, nutrition, health |
| 🔔 notifications | Alerts, system notifications |
| 📧 other | Uncategorized |

## Several mailboxes: iCloud and Gmail

Every command takes `--profile NAME` (or `EMAIL_PROFILE=NAME`). Without it the tool works on the
iCloud mailbox in `.secrets` with its data in `data/`, exactly as before. A profile named `gmail`
reads `.secrets.gmail` (template: `.secrets.gmail.example`) and keeps its index, plans and
execution records in `data/gmail/`, so the two mailboxes never mix. The Claude API key is read
from `.secrets` when the profile file does not set one, so it is written only once.

### Adding a Gmail account, step by step

Nothing here changes the mailbox. The first write happens only at `execute`, with `--confirm`.

1. **Turn on 2-Step Verification** on the Google account, if it is not on already:
   https://myaccount.google.com/signinoptions/two-step-verification
   App Passwords only exist for accounts with 2-Step Verification.

2. **Create an App Password** for this tool: https://myaccount.google.com/apppasswords
   Give it a name such as `email_review`, and copy the 16 letters Google shows. Google displays
   them as four groups of four; the spaces do not matter, the tool strips them. The password is
   shown once. If you lose it, delete it on that page and create a new one.

   A Google Workspace (work or school) account may have App Passwords or IMAP switched off by
   its administrator, and the Advanced Protection Program blocks them. Both show up as a login
   error in step 4.

3. **Create the profile file** from the template and fill in the address and the App Password:

   ```bash
   cp .secrets.gmail.example .secrets.gmail
   open -t .secrets.gmail          # or any editor
   ```

   `.secrets.gmail` is listed in `.gitignore`, like `.secrets`. Never commit it and never paste
   the password into a chat.

4. **Check the connection**, read-only. The first command lists folders (Gmail labels) and their
   counts; the second counts Gmail's own Promotions / Social / Updates / Forums categories by
   age, which is the best first look at what a plan should target:

   ```bash
   ./venv/bin/python main.py --stats --profile gmail
   ./venv/bin/python gmail_categories.py --profile gmail
   ```

   A failed login prints the App Password page for this provider. The usual causes are a
   normal account password used instead of an App Password, 2-Step Verification not enabled,
   or IMAP disabled. IMAP is always on in current Gmail; if the account still shows a
   `Forwarding and POP/IMAP` tab with an IMAP switch under *Settings → See all settings*, turn
   it on there. On the same tab, leave *Folder size limits* on *Do not limit the number of
   messages in an IMAP folder*, otherwise `[Gmail]/All Mail` is scanned incompletely.

5. **Scan, classify, plan and review**, the same pipeline as on iCloud. Scanning reads headers
   only, so the daily IMAP download allowance (about 2.5 GB) is not a concern:

   ```bash
   ./venv/bin/python main.py scan --profile gmail
   ./venv/bin/python main.py classify --profile gmail
   ./venv/bin/python main.py plan --profile gmail
   ./venv/bin/python main.py status --profile gmail
   ```

   Everything lands in `data/gmail/`. Review the plan (the saved JSON, or a review page built
   from it) before the next step.

6. **Execute**, when the plan is right. Messages are moved to `[Gmail]/Trash` with Gmail's
   native `MOVE`, in one command per batch; nothing is deleted in place:

   ```bash
   ./venv/bin/python main.py execute data/gmail/plan_*.json --confirm N --profile gmail
   ```

   Gmail empties its Trash by itself after 30 days. To get the storage back at once, open Trash
   on the Gmail website and choose *Empty Trash now*, or run the tool's hard delete, which this
   profile allows only in `[Gmail]/Trash` and `[Gmail]/Spam`. Gmail storage is the shared Google
   quota (15 GB on a free account) with Drive and Photos, so the gain shows up there.

Another Gmail account, say a work one, gets its own profile: create `.secrets.work` with
`PROVIDER=gmail` and its own credentials, then run with `--profile work`. Its data goes to
`data/work/`.

### What differs for Gmail

Handled by the `gmail` provider defaults in `src/config.py`:

- Labels are folders over IMAP. Only `[Gmail]/All Mail` is scanned, because it holds every
  message once; moving a message from there to `[Gmail]/Trash` removes it from every label, and
  Gmail empties the Trash after 30 days. Localised label names (an Italian account shows
  `[Gmail]/Tutti i messaggi`, for example) are found through the `\All` and `\Trash`
  special-use flags, so nothing needs configuring.
- Gmail supports `MOVE` and `UIDPLUS`, so moves are a single fast command per batch and none of
  the iCloud throttling workarounds come into play.
- `gmail_categories.py` reads Gmail's own Promotions / Social / Updates / Forums classification
  with the `X-GM-RAW` search extension; for most promotional mail that replaces the Claude pass.
- The helper scripts written for the iCloud cleanup (`dedupe.py`, `apply_review.py`,
  `trim_inbox.py`, `purge_senders.py`) assume an iCloud-style `Keep` folder and are not needed
  on Gmail; their data files follow the profile's data directory anyway.

## License

MIT
