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
├── main.py                # menu + headless subcommands
├── src/
│   ├── config.py          # settings; secrets loaded from .secrets
│   ├── imap_client.py     # UID-based IMAP client: reconnect/retry, range fetch,
│   │                      #   chunked COPY/STORE/UID EXPUNGE (iCloud has no MOVE)
│   ├── sender_groups.py   # SenderGroup / SenderIndex + resumable scan
│   ├── categorizer.py     # keep policy, structured-output requests, sync + Batches
│   ├── deleter.py         # plan build/execute, per-folder merged sequence sets
│   ├── storage.py         # atomic JSON persistence
│   └── ui.py              # terminal helpers
└── data/                  # gitignored: sender_index.json, plan_*.json, execution_*.json
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

## License

MIT
