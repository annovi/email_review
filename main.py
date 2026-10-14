#!/usr/bin/env python3
"""
Email Reviewer - Main Application
==================================
Clean a very large mailbox one SENDER at a time: scan every header once,
group by sender, let Claude judge each sender against a keep policy, review
the plan, then move the flagged senders' mail to Trash in bulk.

Interactive:
    python main.py                      # menu (option 3 is the sender workflow)
    python main.py --stats              # folder statistics

Headless pipeline (each step is resumable; run one at a time):
    python main.py scan                 # scan all folders into data/sender_index.json
    python main.py classify [--sync] [--min-count N] [--limit N]
                                        # classify unanalyzed senders with Claude;
                                        # default submits a Message Batch (async, 50% cost),
                                        # --sync calls the API right now instead
    python main.py collect [--wait]     # pull the submitted batch's verdicts into the index
    python main.py status               # what has been scanned / classified / flagged
    python main.py plan [--bulk]        # flag every sender Claude said to delete (and, with
                                        # --bulk, unanalyzed senders that look like bulk mail),
                                        # write data/plan_*.json and print the summary
    python main.py execute PLAN --confirm N [--dry-run] [--hard]
    python main.py preserve [--folder Keep] [--dry-run] [--new-only]
                                        # COPY the kept INBOX messages (+ new mail) into a folder, so
                                        # the Inbox can be cleared wholesale; --new-only = just the mail
                                        # that arrived since the last preserve run
                                        # execute a plan; N must equal its total_emails
"""

import sys
import time
import math
import getpass
import logging
from collections import defaultdict, Counter
from typing import Optional, List, Tuple

from src.config import (
    EMAIL_ADDRESS, APP_PASSWORD, BATCH_SIZE, AI_MIN_SENDER_COUNT,
    SKIP_FOLDERS, CATEGORIES, TRASH_FOLDER, HARD_DELETE_ALLOWED_FOLDERS,
)
from src.imap_client import (
    IMAPClient, IMAPUnavailable, decode_header, get_body_text, format_date,
)
from src.storage import (
    load_decisions, save_decisions, load_categories,
    save_categories, export_summary,
    save_plan_json, load_plan_json, list_plans,
    save_execution_result, save_sender_index, load_sender_index,
)
from src.categorizer import (
    auto_categorize, ai_categorize, ai_batch_analyze,
    ai_batch_analyze_senders, submit_sender_batch, batch_status, collect_sender_batch,
    AIError, AI_MODEL, BATCH_SIZE as AI_BATCH_SIZE, SENDER_BATCH_SIZE,
)
from src.sender_groups import SenderGroup, SenderIndex, scan_folders
from src.deleter import build_plan, execute_plan, hard_delete_eligible
from src.ui import (
    clear_screen, divider, print_banner, print_wrapped, confirm,
)


VERDICT_LABEL = {"delete": "🗑️ del", "keep": "✅ keep", "bulk?": "📣 bulk", "?": "?"}


class EmailReviewer:
    """Main application class."""

    def __init__(self):
        self.client: Optional[IMAPClient] = None
        self.decisions = load_decisions()
        self.categories = load_categories()
        self.folders: List[str] = []

    def connect(self, email_addr: str, password: str) -> bool:
        """Connect to the email server."""
        self.client = IMAPClient(email_addr, password)
        try:
            self.client.connect()
            self.folders = self.client.list_folders()
            return True
        except ConnectionError as e:
            print(f"\n  ❌ {e}")
            print("  → You must use an App-Specific Password, not your regular Apple ID password.")
            print("  → https://appleid.apple.com → Sign-In & Security → App-Specific Passwords")
            return False

    def disconnect(self):
        """Disconnect from server."""
        if self.client:
            self.client.disconnect()

    # ── Folder Overview ──────────────────────────────────────────────────────

    def show_folder_overview(self):
        """Display overview of all folders."""
        print_banner("Email Reviewer", "Mailbox Overview")

        folder_counts = {}
        for folder in self.folders:
            if folder in SKIP_FOLDERS:
                continue
            folder_counts[folder] = self.client.count_folder(folder)

        # Count reviewed per folder
        reviewed = defaultdict(lambda: {"useful": 0, "not_useful": 0})
        for key, val in self.decisions.items():
            folder = key.split("::")[0] if "::" in key else "INBOX"
            reviewed[folder][val] += 1

        # Display table
        rows = sorted(folder_counts.items(), key=lambda x: -x[1])
        max_name = max((len(r[0]) for r in rows), default=10)
        fmt = f"  {{:<{max_name}}}  {{:>9}}  {{:>9}}  {{:>7}}  {{:>7}}"

        print(fmt.format("Folder", "Emails", "Reviewed", "✅", "🗑️"))
        divider()

        for folder, count in rows:
            u = reviewed[folder]["useful"]
            n = reviewed[folder]["not_useful"]
            done = u + n
            print(fmt.format(folder, f"{count:,}", f"{done:,}", str(u), str(n)))

        print()
        divider()

        # Overall stats
        total_r = len(self.decisions)
        useful_n = sum(1 for v in self.decisions.values() if v == "useful")
        useless_n = sum(1 for v in self.decisions.values() if v == "not_useful")
        print(f"\n  Overall: {total_r:,} reviewed  |  ✅ {useful_n:,}  |  🗑️ {useless_n:,}")
        print()

        return folder_counts, sorted(folder_counts.keys())

    # ── Review Emails ────────────────────────────────────────────────────────

    def review_folder(self, folder: str, search_query: str = "ALL"):
        """Review emails in a folder, one at a time."""
        success, total = self.client.select_folder(folder)
        if not success:
            print(f"  ❌ Could not open: {folder}")
            input("  Press Enter…")
            return

        print(f"  Searching {folder}...")
        uids = self.client.uid_search(search_query)

        if not uids:
            print("  No matching emails found.")
            input("  Press Enter…")
            return

        uids = uids[::-1]  # Most recent first
        total = len(uids)
        pending = [u for u in uids if f"{folder}::{u}" not in self.decisions]

        print(f"  {total:,} matching  |  {len(pending):,} not yet reviewed")

        if not pending:
            print("  ✅ All emails in this folder already reviewed!")
            input("  Press Enter…")
            return

        batch = pending[:BATCH_SIZE]
        print(f"  Reviewing next {len(batch)} emails…")
        input("  Press Enter to start…")

        session_count = 0

        for i, uid in enumerate(batch, 1):
            key = f"{folder}::{uid}"
            existing = self.decisions.get(key)

            # Fetch headers first (fast)
            hdr = self.client.fetch_headers(uid)
            if hdr is None:
                continue

            sender = decode_header(hdr.get("From", "unknown"))
            subject = decode_header(hdr.get("Subject", "(no subject)"))
            date = format_date(hdr)
            preview = ""
            preview_loaded = False

            # Auto-categorize
            if key not in self.categories:
                self.categories[key] = auto_categorize(sender, subject)
                save_categories(self.categories)

            category = self.categories.get(key, "other")

            # Display email
            self._display_email(folder, i, len(batch), sender, subject, date, preview, category, existing)

            # Action menu
            print()
            print("  [u] ✅ Useful        [n] 🗑️  Not useful")
            print("  [b] 📄 Load body     [a] 🤖 AI analysis")
            print("  [c] 🏷️  Change cat    [s] ⏭️  Skip")
            print("  [q] 💾 Save & quit")
            print()

            while True:
                choice = input("  Choice: ").strip().lower()

                if choice == "u":
                    self.decisions[key] = "useful"
                    session_count += 1
                    save_decisions(self.decisions)
                    break

                elif choice == "n":
                    self.decisions[key] = "not_useful"
                    session_count += 1
                    save_decisions(self.decisions)
                    break

                elif choice == "b":
                    if not preview_loaded:
                        print("  Loading…", end="\r")
                        full = self.client.fetch_full(uid)
                        preview = get_body_text(full) if full else "(could not load)"
                        preview_loaded = True
                    self._display_email(folder, i, len(batch), sender, subject, date, preview, category, existing)
                    print("\n  [u] Useful  [n] Not useful  [a] AI  [c] Category  [s] Skip  [q] Quit\n")

                elif choice == "a":
                    if not preview_loaded:
                        print("  Loading body for AI…", end="\r")
                        full = self.client.fetch_full(uid)
                        preview = get_body_text(full) if full else ""
                        preview_loaded = True

                    print("\n  🤖 Analyzing with AI...")
                    try:
                        cat, keep, reason = ai_categorize(subject, sender, preview)
                    except AIError as e:
                        print(f"  ⚠️  {e}\n")
                        continue
                    print(f"  Category: {CATEGORIES[cat]['emoji']} {cat}")
                    self.categories[key] = cat
                    save_categories(self.categories)
                    print(f"  Useful: {'✅ Yes' if keep else '🗑️ No'}")
                    print(f"  Reason: {reason}\n")

                elif choice == "c":
                    self._change_category(key)

                elif choice == "s":
                    break

                elif choice == "q":
                    print(f"\n  💾 {session_count} decisions saved.")
                    input("  Press Enter…")
                    return

                else:
                    print("  Enter: u / n / b / a / c / s / q")

        print(f"\n  ✅ Batch done! {session_count} new decisions this session.")
        remaining = len(pending) - len(batch)
        if remaining > 0:
            print(f"  📌 {remaining:,} more pending — run again to continue.")
        input("  Press Enter…")

    def _display_email(self, folder, idx, total, sender, subject, date, preview, category, decision):
        """Display a single email card."""
        clear_screen()
        divider("═")

        cat_emoji = CATEGORIES.get(category, {}).get("emoji", "📧")
        tag = {
            "useful": "  [✅ USEFUL]",
            "not_useful": "  [🗑️ NOT USEFUL]",
        }.get(decision, "")

        print(f"  📁 {folder}   {idx}/{total}   {cat_emoji} {category}{tag}")
        divider("═")
        print(f"  From    : {sender}")
        print(f"  Subject : {subject}")
        print(f"  Date    : {date}")
        print()
        divider()

        if preview:
            print_wrapped(preview)
        else:
            print("  (press 'b' to load body)")

        print()
        divider()

    def _change_category(self, key: str):
        """Change the category of an email."""
        print("\n  Available categories:")
        cat_list = list(CATEGORIES.keys())
        for i, cat in enumerate(cat_list, 1):
            emoji = CATEGORIES[cat]["emoji"]
            print(f"    [{i}] {emoji} {cat}")

        choice = input(f"\n  Select (1-{len(cat_list)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(cat_list):
                new_cat = cat_list[idx]
                self.categories[key] = new_cat
                save_categories(self.categories)
                print(f"  Changed to: {CATEGORIES[new_cat]['emoji']} {new_cat}")
        except ValueError:
            print("  Invalid selection")

    # ── Search Query Builder ─────────────────────────────────────────────────

    def build_search_query(self) -> tuple:
        """Build an IMAP search query interactively."""
        print()
        divider()
        print("  Search Filter")
        divider()
        print("  1. All emails")
        print("  2. From specific sender (e.g. apple.com)")
        print("  3. Subject contains keyword")
        print("  4. Unread only")
        print()

        choice = input("  Filter [1-4, default 1]: ").strip() or "1"

        if choice == "2":
            sender = input("  Sender domain or address: ").strip().replace('"', "")
            return f'FROM "{sender}"', f"from {sender}"
        elif choice == "3":
            kw = input("  Subject keyword: ").strip().replace('"', "")
            return f'SUBJECT "{kw}"', f"subject contains '{kw}'"
        elif choice == "4":
            return "UNSEEN", "unread emails"
        else:
            return "ALL", "all emails"

    # ── Statistics ───────────────────────────────────────────────────────────

    def show_stats(self):
        """Show detailed email statistics."""
        print_banner("Email Reviewer", "Statistics")

        print("  Folder Counts:")
        divider()

        total_emails = 0
        for folder in self.folders:
            if folder in SKIP_FOLDERS:
                continue
            count = self.client.count_folder(folder)
            total_emails += count
            print(f"    {folder:20} {count:>10,}")

        print()
        print(f"  Total: {total_emails:,} emails")
        print()

        if self.categories:
            print("  Categories (reviewed emails):")
            divider()
            cat_counts = Counter(self.categories.values())
            for cat, count in cat_counts.most_common():
                emoji = CATEGORIES.get(cat, {}).get("emoji", "📧")
                print(f"    {emoji} {cat:15} {count:>6}")
            print()

        if self.decisions:
            useful = sum(1 for v in self.decisions.values() if v == "useful")
            not_useful = sum(1 for v in self.decisions.values() if v == "not_useful")
            print("  Decisions:")
            divider()
            print(f"    ✅ Useful:      {useful:>6}")
            print(f"    🗑️  Not useful:  {not_useful:>6}")
            print(f"    Total:         {len(self.decisions):>6}")

        print()
        input("  Press Enter…")

    # ── Sender index: scan / persist ─────────────────────────────────────────

    def _reviewable_folders(self) -> list:
        """Folders eligible for scanning (excluding Drafts/Trash/etc)."""
        return [f for f in self.folders if f not in SKIP_FOLDERS]

    @staticmethod
    def _load_index() -> Optional[SenderIndex]:
        raw = load_sender_index()
        return SenderIndex.from_dict(raw) if raw else None

    @staticmethod
    def _save_index(index: SenderIndex) -> None:
        save_sender_index(index.to_dict())

    @staticmethod
    def _progress_printer():
        """Progress callback: \\r updates on a TTY, one line every ~20 chunks otherwise."""
        tty = sys.stdout.isatty()
        state = {"folder": None, "calls": 0}

        def progress(folder, done, total):
            pct = (done / total * 100) if total else 100.0
            line = f"  📁 {folder:22s} {done:>9,} / {total:<9,} ({pct:5.1f}%)"
            if tty:
                print(line, end="\r", flush=True)
                if done >= total:
                    print()
            else:
                state["calls"] += 1
                if folder != state["folder"] or done >= total or state["calls"] % 20 == 0:
                    print(f"{time.strftime('%H:%M:%S')}{line}", flush=True)
                    state["folder"] = folder
        return progress

    def scan_index(self, index: Optional[SenderIndex] = None, folders: Optional[List[str]] = None) -> SenderIndex:
        """Scan (or resume scanning) the reviewable folders; checkpoints to disk as it goes."""
        folders = folders or self._reviewable_folders()
        index = index or SenderIndex()
        pending = index.pending_folders(folders)
        if not pending:
            print("  ✅ Index already complete.")
            return index

        resuming = index.scanned_total() > 0
        print(f"  Scanning {len(pending)} folder(s){' (resuming)' if resuming else ''}: {', '.join(pending)}")
        t0 = time.time()
        try:
            scan_folders(self.client, pending, index,
                         progress_callback=self._progress_printer(),
                         checkpoint_callback=self._save_index)
        except IMAPUnavailable as e:
            print(f"\n  ⚠️  Scan interrupted: {e}")
            print(f"     Progress is saved ({index.scanned_total():,} emails). Run `scan` again to resume.")
            return index
        except KeyboardInterrupt:
            self._save_index(index)
            print(f"\n  ⚠️  Interrupted. Progress is saved ({index.scanned_total():,} emails); run `scan` again to resume.")
            return index

        self._save_index(index)
        print(f"\n  ✅ Scan complete in {(time.time() - t0) / 60:.1f} min: "
              f"{index.total_emails():,} emails from {len(index.groups):,} senders.")
        return index

    # ── Sender index: AI classification ──────────────────────────────────────

    @staticmethod
    def _sender_payload(g: SenderGroup) -> dict:
        return {
            "email": g.email,
            "count": g.count,
            "bulk_ratio": g.bulk_ratio,
            "first_date": g.first_date.date().isoformat() if g.first_date else None,
            "last_date": g.last_date.date().isoformat() if g.last_date else None,
            "sample_subjects": g.sample_subjects,
        }

    @staticmethod
    def _unanalyzed(index: SenderIndex, min_count: int) -> List[Tuple[str, SenderGroup]]:
        todo = [(k, g) for k, g in index.groups.items() if g.ai_useful is None and g.count >= min_count]
        todo.sort(key=lambda kg: (-kg[1].count, kg[1].email))
        return todo

    def classify_sync(self, index: SenderIndex, min_count: int = AI_MIN_SENDER_COUNT,
                      limit: Optional[int] = None) -> int:
        """Classify unanalyzed senders now, request by request, saving as it goes."""
        todo = self._unanalyzed(index, min_count)
        if limit:
            todo = todo[:limit]
        if not todo:
            print("  ✅ Nothing to classify.")
            return 0
        n_req = math.ceil(len(todo) / SENDER_BATCH_SIZE)
        print(f"  🤖 Classifying {len(todo):,} senders with {AI_MODEL} "
              f"({SENDER_BATCH_SIZE} per request → {n_req:,} requests)…")
        done = 0
        t0 = time.time()
        try:
            for i in range(0, len(todo), SENDER_BATCH_SIZE):
                batch = todo[i:i + SENDER_BATCH_SIZE]
                verdicts = ai_batch_analyze_senders([self._sender_payload(g) for _, g in batch])
                for j, (_, g) in enumerate(batch, 1):
                    if j in verdicts:
                        g.category, g.ai_useful, g.ai_reason = verdicts[j]
                done += len(batch)
                if (i // SENDER_BATCH_SIZE) % 10 == 9:
                    self._save_index(index)
                print(f"    {done:>6,} / {len(todo):<6,} senders  ({(time.time() - t0) / 60:4.1f} min)",
                      end="\r" if sys.stdout.isatty() else "\n", flush=True)
        except AIError as e:
            print(f"\n  ❌ AI error after {done:,} senders: {e}")
            print("     Verdicts so far are saved; run `classify` again to continue.")
        except KeyboardInterrupt:
            print(f"\n  ⚠️  Interrupted after {done:,} senders; verdicts so far are saved.")
        self._save_index(index)
        print()
        return done

    def classify_batch_submit(self, index: SenderIndex, min_count: int = AI_MIN_SENDER_COUNT,
                              limit: Optional[int] = None) -> Optional[str]:
        """Submit unanalyzed senders as one Message Batches job (async, half price)."""
        if index.ai_batch:
            print(f"  ⏳ A batch is already pending ({index.ai_batch['id']}); run `collect` first.")
            return None
        todo = self._unanalyzed(index, min_count)
        if limit:
            todo = todo[:limit]
        if not todo:
            print("  ✅ Nothing to classify.")
            return None
        groups, keys = {}, {}
        for n, i in enumerate(range(0, len(todo), SENDER_BATCH_SIZE)):
            chunk = todo[i:i + SENDER_BATCH_SIZE]
            cid = f"s{n}"
            groups[cid] = [self._sender_payload(g) for _, g in chunk]
            keys[cid] = [k for k, _ in chunk]
        print(f"  📨 Submitting {len(todo):,} senders in {len(groups):,} requests to {AI_MODEL} (Message Batches)…")
        try:
            batch_id = submit_sender_batch(groups)
        except AIError as e:
            print(f"  ❌ {e}")
            return None
        index.ai_batch = {
            "id": batch_id, "model": AI_MODEL,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "senders": len(todo), "groups": keys,
        }
        self._save_index(index)
        print(f"  ✅ Batch {batch_id} submitted. Most batches finish within an hour; run `collect` (or `collect --wait`).")
        return batch_id

    def classify_batch_collect(self, index: SenderIndex, wait: bool = False) -> bool:
        """Pull a finished batch's verdicts into the index. Returns True when applied."""
        pending = index.ai_batch
        if not pending:
            print("  No batch pending.")
            return False
        # In wait mode a network blip must not abort the wait: the batch keeps
        # running on Anthropic's side regardless. Give up after ~1h of failures.
        failures = 0
        sizes = {cid: len(ks) for cid, ks in pending["groups"].items()}
        while True:
            try:
                status, counts = batch_status(pending["id"])
                if status == "ended":
                    verdicts, errors = collect_sender_batch(pending["id"], sizes)
                    break
                failures = 0
                print(f"  ⏳ {time.strftime('%H:%M:%S')} batch {pending['id']}: {status} — "
                      f"processing {counts['processing']:,}, succeeded {counts['succeeded']:,}, "
                      f"errored {counts['errored']:,}", flush=True)
                if not wait:
                    return False
            except AIError as e:
                failures += 1
                if not wait or failures >= 60:
                    print(f"  ❌ {e}")
                    return False
                print(f"  ⚠️  {time.strftime('%H:%M:%S')} {e} — retrying in 60s ({failures}/60)", flush=True)
            time.sleep(60)

        applied = missing = 0
        for cid, ks in pending["groups"].items():
            vs = verdicts.get(cid, {})
            for j, key in enumerate(ks, 1):
                g = index.groups.get(key)
                if g is None:
                    continue
                if j in vs:
                    g.category, g.ai_useful, g.ai_reason = vs[j]
                    applied += 1
                else:
                    missing += 1
        index.ai_batch = None
        self._save_index(index)
        print(f"  ✅ Applied {applied:,} verdicts; {missing:,} senders got none"
              f"{f' ({len(errors)} requests errored)' if errors else ''}.")
        if errors:
            for cid, err in list(errors.items())[:5]:
                print(f"     {cid}: {err[:120]}")
        left = len(self._unanalyzed(index, 1))
        if left:
            print(f"  📌 {left:,} senders still unanalyzed — run `classify` again for them.")
        return True

    # ── Sender index: flags, plan, summaries ─────────────────────────────────

    @staticmethod
    def flag_from_verdicts(index: SenderIndex, include_bulk: bool = False) -> Tuple[int, int]:
        """Flag senders Claude marked delete (+ unanalyzed bulk-looking ones). Returns (n_ai, n_bulk)."""
        n_ai = n_bulk = 0
        for g in index.groups.values():
            if g.ai_useful is False:
                if not g.flagged_for_delete:
                    n_ai += 1
                g.flagged_for_delete = True
            elif include_bulk and g.ai_useful is None and g.looks_bulk:
                if not g.flagged_for_delete:
                    n_bulk += 1
                g.flagged_for_delete = True
        return n_ai, n_bulk

    def print_status(self, index: Optional[SenderIndex]) -> None:
        if index is None:
            print("  No sender index yet — run `scan`.")
            return
        groups = list(index.groups.values())
        analyzed = [g for g in groups if g.ai_useful is not None]
        keep = [g for g in analyzed if g.ai_useful]
        delete = [g for g in analyzed if g.ai_useful is False]
        flagged = [g for g in groups if g.flagged_for_delete]
        unanalyzed = [g for g in groups if g.ai_useful is None]
        print(f"  Index updated {index.updated_at}")
        print(f"  Scan: {index.scanned_total():,} emails, {len(groups):,} senders")
        for folder, p in sorted(index.scan_progress.items(), key=lambda fp: -fp[1].get('scanned', 0)):
            state = "done" if p.get("done") else f"IN PROGRESS (last uid {p.get('last_uid')})"
            print(f"    {folder:22s} {p.get('scanned', 0):>9,}   {state}")
        print(f"  Classified: {len(analyzed):,} senders  "
              f"(✅ keep {len(keep):,} = {sum(g.count for g in keep):,} emails,  "
              f"🗑️ delete {len(delete):,} = {sum(g.count for g in delete):,} emails)")
        print(f"  Unanalyzed: {len(unanalyzed):,} senders = {sum(g.count for g in unanalyzed):,} emails"
              f"  ({sum(1 for g in unanalyzed if g.looks_bulk):,} look like bulk mail)")
        print(f"  Flagged for deletion: {len(flagged):,} senders = {sum(g.count for g in flagged):,} emails")
        if index.ai_batch:
            print(f"  ⏳ Pending batch {index.ai_batch['id']} ({index.ai_batch.get('senders', '?')} senders, "
                  f"submitted {index.ai_batch.get('submitted_at')}) — run `collect`")

    @staticmethod
    def print_plan_summary(index: SenderIndex, top: int = 25) -> None:
        groups = list(index.groups.values())
        flagged = [g for g in groups if g.flagged_for_delete]
        kept = [g for g in groups if not g.flagged_for_delete]
        total = sum(g.count for g in groups)
        n_flag = sum(g.count for g in flagged)
        divider("═")
        print(f"  📋 Plan: delete {n_flag:,} of {total:,} emails ({(n_flag / total * 100) if total else 0:.1f}%) "
              f"from {len(flagged):,} senders; keep {total - n_flag:,} emails from {len(kept):,} senders")
        divider("═")
        by_cat = Counter()
        for g in flagged:
            by_cat[g.category or "unclassified"] += g.count
        print("  Deleting, by category:")
        for cat, n in by_cat.most_common():
            emoji = CATEGORIES.get(cat, {}).get("emoji", "📧")
            print(f"    {emoji} {cat:15} {n:>9,}")
        by_folder = Counter()
        for g in flagged:
            for folder, uids in g.uids_by_folder.items():
                by_folder[folder] += len(uids)
        print("  Deleting, by folder:")
        for folder, n in by_folder.most_common():
            mode = "🔥 hard-delete OK" if hard_delete_eligible(folder) else "🛟 trash"
            print(f"    {folder:22s} {n:>9,}   [{mode}]")
        print(f"  Top {top} senders being deleted:")
        for g in sorted(flagged, key=lambda x: -x.count)[:top]:
            print(f"    {g.count:>7,}  {g.email[:44]:44s} {(g.category or '-')[:12]:12s} {g.ai_reason[:50]}")
        print(f"  Top {top} senders being KEPT (sanity check):")
        for g in sorted(kept, key=lambda x: -x.count)[:top]:
            print(f"    {g.count:>7,}  {g.email[:44]:44s} {(g.category or '-')[:12]:12s} "
                  f"{'(unanalyzed)' if g.ai_useful is None else g.ai_reason[:50]}")
        divider()

    def write_plan(self, index: SenderIndex, mode: str = "trash") -> Optional[str]:
        flagged = [g for g in index.groups.values() if g.flagged_for_delete]
        if not flagged:
            print("  Nothing flagged — no plan written.")
            return None
        plan = build_plan(flagged, mode=mode)
        filename = save_plan_json(plan)
        print(f"  💾 Plan saved: {filename}  ({plan['total_senders']:,} senders, {plan['total_emails']:,} emails, mode={mode})")
        return filename

    # ── Execution ────────────────────────────────────────────────────────────

    def run_plan(self, plan: dict, hard: bool, dry_run: bool, index: Optional[SenderIndex]) -> dict:
        """Execute a plan, pruning the index as messages go, and save the report."""
        trash = self.client.detect_trash_folder(TRASH_FOLDER)
        tty = sys.stdout.isatty()

        def progress(label, done, total):
            line = f"    {time.strftime('%H:%M:%S')}  {label[:50]:50s} {done:>9,} / {total:<9,}"
            print(line, end="\r" if tty else "\n", flush=True)
            if tty and done >= total:
                print()

        def on_item_done(item, done_uids):
            if index is not None:
                index.prune(item["sender_email"], item["folder"], done_uids)

        if not dry_run:
            print(f"  🗑️  Trash folder: {trash}")
        result = execute_plan(self.client, plan, hard_delete=hard, trash_folder=trash,
                              dry_run=dry_run, progress_callback=progress, on_item_done=on_item_done)
        if index is not None and not dry_run:
            self._save_index(index)
        result_file = save_execution_result(result)
        print()
        divider("═")
        if dry_run:
            print(f"  🧪 Dry run complete. Report: {result_file}")
        else:
            print(f"  ✅ Executed. Report: {result_file}")
            print(f"     Trashed:      {result['trashed_total']:,}")
            print(f"     Hard-deleted: {result['hard_deleted_total']:,}")
            print(f"     Failed:       {result['failed_total']:,}")
            if result.get("connection_lost"):
                print("     ⚠️  Connection was lost; re-run the same plan to finish the rest.")
            if result.get("throttled"):
                print("     ⚠️  The server refused repeated batches (throttling); the rest was skipped. "
                      "Re-run the same plan later.")
            for folder, errs in result.get("folder_errors", {}).items():
                print(f"     {folder}: {errs[0][:150]}")
        divider("═")
        return result

    PRESERVE_STATE_FILE = "data/preserve_state.json"

    def preserve_keepers(self, index: Optional[SenderIndex], dest: str, dry_run: bool,
                         new_only: bool = False) -> None:
        """COPY every INBOX message the plan keeps into `dest`, so the Inbox can then be
        cleared wholesale (e.g. iCloud.com → Select All → Delete) without losing them.

        Keepers = INBOX UIDs of every sender NOT flagged for deletion, plus anything that
        arrived after the index was scanned. Originals stay where they are; nothing is
        flagged, moved or deleted. Re-running copies again (duplicates in `dest`).

        new_only: copy only mail newer than the last preserve run (or the index), i.e. what
        arrived since — meant to be run right before clearing the Inbox.
        """
        import json as _json
        import os as _os
        if index is None:
            print("  No sender index yet — run `scan` first.")
            return
        state = {}
        if _os.path.exists(self.PRESERVE_STATE_FILE):
            try:
                with open(self.PRESERVE_STATE_FILE) as f:
                    state = _json.load(f)
            except (OSError, ValueError):
                state = {}
        max_copied = int(state.get("max_uid_copied") or 0)

        keep_uids: set = set()
        flagged_uids: set = set()
        keep_senders = 0
        if not new_only:
            for g in index.groups.values():
                uids = g.uids_by_folder.get("INBOX", [])
                if not uids:
                    continue
                if g.flagged_for_delete:
                    flagged_uids.update(int(u) for u in uids)
                else:
                    keep_uids.update(int(u) for u in uids)
                    keep_senders += 1
            keep_uids -= flagged_uids

        last_uid = int((index.scan_progress.get("INBOX") or {}).get("last_uid") or 0)
        threshold = max(last_uid, max_copied) if new_only else last_uid
        ok, exists = self.client.select_folder_rw("INBOX")
        if not ok:
            print("  ❌ Could not open INBOX.")
            return
        new_uids = [u for u in self.client.uid_search(f"UID {threshold + 1}:*") if u > threshold] if threshold else []
        all_uids = sorted(keep_uids | set(new_uids))

        print(f"  Inbox on server: {exists:,} messages")
        if new_only:
            print(f"  --new-only: only mail newer than UID {threshold:,} "
                  f"({'last preserve run' if max_copied >= last_uid else 'the index'})")
        else:
            print(f"  Keepers from the index: {len(keep_uids):,} UIDs from {keep_senders:,} senders "
                  f"(phantom UIDs are skipped by the server)")
        print(f"  Newer than that: {len(new_uids):,}")
        print(f"  → {len(all_uids):,} UIDs to COPY into \"{dest}\"; the Inbox is not modified")
        if not all_uids:
            print("  Nothing to copy.")
            return
        if dry_run:
            print("  🧪 Dry run — nothing copied.")
            return

        if not self.client.create_folder(dest):
            print(f"  ❌ Could not create folder {dest!r}.")
            return
        before = self.client.count_folder(dest)
        self.client.select_folder_rw("INBOX")  # STATUS does not change the selection, but be explicit
        tty = sys.stdout.isatty()

        def progress(done, total):
            line = f"    {time.strftime('%H:%M:%S')}  INBOX → {dest}: copied {done:>9,} / {total:<9,}"
            print(line, end="\r" if tty else "\n", flush=True)

        done, errors = self.client.copy_to_folder(all_uids, dest, on_chunk=progress,
                                                  max_uids=1000, max_chars=8000)
        after = self.client.count_folder(dest)
        if done:
            state.update({"max_uid_copied": max(max(done), max_copied), "dest": dest,
                          "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            try:
                with open(self.PRESERVE_STATE_FILE, "w") as f:
                    _json.dump(state, f, indent=2)
            except OSError as e:
                print(f"  ⚠️  could not save {self.PRESERVE_STATE_FILE}: {e}")
        print()
        divider("═")
        print(f"  ✅ Copied {len(done):,} of {len(all_uids):,} UIDs into \"{dest}\"  "
              f"({before:,} → {after:,} messages in that folder)")
        if errors:
            print(f"     ⚠️  {errors[-1][:160]}")
        if len(done) < len(all_uids):
            print("     Re-run `preserve` later for the rest (messages copied already will be duplicated in the folder).")
        divider("═")

    # ── Interactive: group by sender ─────────────────────────────────────────

    def group_by_sender(self):
        """Scan (or load) the sender index, then review and flag senders interactively."""
        print_banner("Email Reviewer", "📊 Group by Sender")

        folders = self._reviewable_folders()
        if not folders:
            print("  No reviewable folders.")
            input("  Press Enter…")
            return

        index = self._load_index()
        if index is not None:
            complete = index.is_complete(folders)
            print(f"  Saved index: {index.total_emails():,} emails, {len(index.groups):,} senders, "
                  f"updated {index.updated_at}{'' if complete else '  — scan INCOMPLETE'}")
            if complete:
                print("  [u] use it    [r] rescan from scratch    [q] back")
            else:
                print("  [u] use it as is    [r] resume the scan    [q] back")
            choice = input("  > ").strip().lower()
            if choice == "r":
                if complete:
                    index = None
            elif choice != "u":
                return

        if index is None or (choice == "r" and not index.is_complete(folders)):
            print(f"\n  Will scan {len(index.pending_folders(folders) if index else folders)} folders. "
                  "This fetches headers for every email; a 400k mailbox takes 30–60 minutes.")
            print("  Progress is saved continuously — you can Ctrl-C and resume later.")
            if not confirm("Start scan?"):
                return
            print()
            index = self.scan_index(index, folders)
            if not index.is_complete(folders):
                input("  Press Enter…")
                return
            print()

        self._interactive_sender_selection(index)

    def _interactive_sender_selection(self, index: SenderIndex):
        """
        Interactive table for flagging senders to delete.
        Commands:
            l           list/reshow current page
            n / p       next/previous page
            f <nums>    flag senders (e.g. 'f 1 2 5-10')
            u <nums>    unflag
            v <num>     view details of a sender
            ai [N]      classify unanalyzed senders with ≥N emails now (synchronous)
            ai batch [N]  …as a Message Batch (async, half price); 'ai poll' collects it
            auto        flag all senders Claude said to delete
            bulk        flag unanalyzed senders that look like bulk mail
            cat <name>  flag all senders in a category (e.g. 'cat promotions')
            summary     show what's currently flagged
            save        save flags + write a plan file, and return
            x           execute now (move to Trash, reversible)
            X           execute now (HARD delete where allowed, permanent)
            q           quit (flags are kept in the index)
        """
        page = 0
        page_size = 25
        groups = index.sorted_groups()

        while True:
            self._render_sender_page(groups, page, page_size)
            raw = input("  > ").strip()
            if not raw:
                continue

            parts = raw.split()
            cmd = parts[0].lower() if parts[0] != "X" else "X"
            args = parts[1:]
            total_pages = max(1, (len(groups) + page_size - 1) // page_size)

            if cmd in ("q", "quit"):
                self._save_index(index)
                return

            elif cmd in ("l", "list"):
                continue

            elif cmd in ("n", "next"):
                page = min(page + 1, total_pages - 1)

            elif cmd in ("p", "prev"):
                page = max(page - 1, 0)

            elif cmd == "f" and args:
                self._apply_flag(groups, args, flag=True)
                self._save_index(index)

            elif cmd == "u" and args:
                self._apply_flag(groups, args, flag=False)
                self._save_index(index)

            elif cmd == "v" and args:
                self._view_sender(groups, args[0])

            elif cmd == "ai":
                sub = args[0].lower() if args else ""
                if sub == "poll":
                    self.classify_batch_collect(index)
                elif sub == "batch":
                    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else AI_MIN_SENDER_COUNT
                    todo = len(self._unanalyzed(index, n))
                    if todo and confirm(f"Submit {todo:,} senders (≥{n} emails) to {AI_MODEL} as a batch?"):
                        self.classify_batch_submit(index, n)
                else:
                    n = int(sub) if sub.isdigit() else AI_MIN_SENDER_COUNT
                    todo = len(self._unanalyzed(index, n))
                    if todo and confirm(f"Classify {todo:,} senders (≥{n} emails) with {AI_MODEL} now?"):
                        self.classify_sync(index, n)
                groups = index.sorted_groups()
                input("  Press Enter…")

            elif cmd == "auto":
                n_ai, _ = self.flag_from_verdicts(index)
                self._save_index(index)
                print(f"  ✅ Flagged {n_ai:,} more senders marked delete by Claude "
                      f"({sum(1 for g in groups if g.flagged_for_delete):,} flagged in total).")
                input("  Press Enter…")

            elif cmd == "bulk":
                n = 0
                for g in groups:
                    if g.ai_useful is None and g.looks_bulk and not g.flagged_for_delete:
                        g.flagged_for_delete = True
                        n += 1
                self._save_index(index)
                print(f"  ✅ Flagged {n:,} unanalyzed senders that look like bulk mail.")
                input("  Press Enter…")

            elif cmd == "cat" and args:
                target = args[0].lower()
                if target not in CATEGORIES:
                    print(f"  Unknown category. Valid: {', '.join(CATEGORIES.keys())}")
                    input("  Press Enter…")
                    continue
                n = 0
                for g in groups:
                    if g.category == target:
                        g.flagged_for_delete = True
                        n += 1
                self._save_index(index)
                print(f"  ✅ Flagged {n:,} senders in '{target}'.")
                input("  Press Enter…")

            elif cmd == "summary":
                self.print_plan_summary(index)
                input("  Press Enter…")

            elif cmd == "save":
                self._save_index(index)
                self.write_plan(index, mode="trash")
                input("  Press Enter…")
                return

            elif cmd == "x":
                if self._execute_flagged(index, hard_delete=False):
                    groups = index.sorted_groups()

            elif cmd == "X":
                if self._execute_flagged(index, hard_delete=True):
                    groups = index.sorted_groups()

            else:
                print("  Commands: l, n, p, f <nums>, u <nums>, v <num>, ai [N] | ai batch [N] | ai poll,")
                print("            auto, bulk, cat <name>, summary, save, x (trash), X (hard delete), q")
                input("  Press Enter…")

    def _render_sender_page(self, groups: list, page: int, page_size: int):
        """Display one page of the sender table."""
        clear_screen()
        divider("═")
        analyzed = sum(1 for g in groups if g.ai_useful is not None)
        print(f"  📊 Senders by volume  —  {sum(g.count for g in groups):,} emails, "
              f"{len(groups):,} senders, {analyzed:,} classified, "
              f"{sum(1 for g in groups if g.flagged_for_delete):,} flagged")
        divider("═")

        start = page * page_size
        end = min(start + page_size, len(groups))
        total_pages = max(1, (len(groups) + page_size - 1) // page_size)

        fmt = "  {:>5} {:1} {:>7} {:>5}  {:<12} {:<36} {:<8} {}"
        print(fmt.format("#", "", "Count", "Bulk", "Category", "Sender", "Verdict", "Reason"))
        divider()

        for idx in range(start, end):
            g = groups[idx]
            flag_mark = "⚑" if g.flagged_for_delete else " "
            cat = (g.category or "-")[:12]
            sender = (g.email or g.domain or "unknown")[:36]
            bulk = f"{g.bulk_ratio:.0%}" if g.count else "-"
            print(fmt.format(idx + 1, flag_mark, g.count, bulk, cat, sender,
                             VERDICT_LABEL.get(g.verdict, "?"), (g.ai_reason or "")[:48]))

        divider()
        print(f"  Page {page + 1}/{total_pages}   [n]ext [p]rev [f/u <nums>] [v <num>] [ai] [auto] [bulk] "
              f"[cat <name>] [summary] [save] [x=trash] [X=hard] [q]uit")
        divider()

    def _parse_number_list(self, tokens: list) -> list:
        """Parse '1 2 5-10' into [0, 1, 4, 5, 6, 7, 8, 9] (0-based indices)."""
        out = []
        for tok in tokens:
            tok = tok.strip().rstrip(",")
            if "-" in tok:
                try:
                    a, b = tok.split("-", 1)
                    for i in range(int(a), int(b) + 1):
                        out.append(i - 1)
                except ValueError:
                    continue
            else:
                try:
                    out.append(int(tok) - 1)
                except ValueError:
                    continue
        return out

    def _apply_flag(self, groups: list, args: list, flag: bool):
        indices = self._parse_number_list(args)
        n = 0
        for i in indices:
            if 0 <= i < len(groups):
                groups[i].flagged_for_delete = flag
                n += 1
        action = "Flagged" if flag else "Unflagged"
        print(f"  {action} {n} sender(s).")
        input("  Press Enter…")

    def _view_sender(self, groups: list, arg: str):
        try:
            idx = int(arg) - 1
        except ValueError:
            return
        if not (0 <= idx < len(groups)):
            return
        g = groups[idx]
        clear_screen()
        divider("═")
        print(f"  Sender: {g.email}")
        print(f"  Name:   {g.name}")
        print(f"  Domain: {g.domain}")
        print(f"  Count:  {g.count:,}   bulk-marker share: {g.bulk_ratio:.0%}")
        print(f"  Range:  {g.first_date} → {g.last_date}")
        print(f"  Cat:    {g.category}")
        print(f"  AI:     {VERDICT_LABEL.get(g.verdict, '?')}  — {g.ai_reason}")
        print(f"  Flagged: {'YES ⚑' if g.flagged_for_delete else 'no'}")
        divider()
        print("  By folder:")
        for folder, uids in g.uids_by_folder.items():
            print(f"    • {folder}: {len(uids):,} emails")
        divider()
        print("  Recent subjects:")
        for s in reversed(g.sample_subjects):
            print(f"    - {s[:100]}")
        divider()
        input("  Press Enter…")

    def _execute_flagged(self, index: SenderIndex, hard_delete: bool) -> bool:
        flagged = [g for g in index.groups.values() if g.flagged_for_delete]
        if not flagged:
            print("  Nothing flagged.")
            input("  Press Enter…")
            return False

        mode = "hard" if hard_delete else "trash"
        plan = build_plan(flagged, mode=mode)

        if hard_delete:
            blocked = [i for i in plan["items"] if not hard_delete_eligible(i["folder"])]
            if blocked:
                print()
                print(f"  ⚠️  {len(blocked)} plan items are in folders NOT eligible for hard delete.")
                print("     Those will automatically fall back to move-to-Trash.")
                print(f"     Hard-delete allowed folders: {', '.join(sorted(HARD_DELETE_ALLOWED_FOLDERS))}")
                print()

        self.print_plan_summary(index, top=15)
        print(f"  ⚠️  About to execute: mode={mode}")
        if hard_delete:
            print("     🔥 HARD DELETE (permanent) where allowed, Trash elsewhere")
        else:
            print(f"     🛟 Move to Trash: {TRASH_FOLDER}  (reversible)")
        print()

        typed = input(f"  Type the number of emails to confirm ({plan['total_emails']}): ").strip()
        if typed != str(plan["total_emails"]):
            print("  Confirmation mismatch — cancelled.")
            input("  Press Enter…")
            return False

        saved = save_plan_json(plan)
        print(f"  💾 Plan backed up to {saved}")
        self.run_plan(plan, hard=hard_delete, dry_run=False, index=index)
        input("  Press Enter…")
        return True

    # ── Interactive: execute a previously saved plan ─────────────────────────

    def execute_saved_plan(self):
        """Load a plan JSON from disk and execute it."""
        print_banner("Email Reviewer", "📋 Execute Saved Plan")

        plans = list_plans()
        if not plans:
            print("  No saved plans found in data/.")
            input("  Press Enter…")
            return

        print("  Available plans:")
        for i, p in enumerate(plans, 1):
            print(f"    {i:2}. {p}")
        print()

        sel = input("  Select plan number: ").strip()
        try:
            filename = plans[int(sel) - 1]
        except (ValueError, IndexError):
            print("  Invalid selection.")
            input("  Press Enter…")
            return

        try:
            plan = load_plan_json(filename)
        except Exception as e:
            print(f"  ❌ Could not load plan: {e}")
            input("  Press Enter…")
            return

        print()
        print(f"  Plan: {filename}")
        print(f"    Created:  {plan.get('created_at')}")
        print(f"    Mode:     {plan.get('mode')}")
        print(f"    Senders:  {plan.get('total_senders'):,}")
        print(f"    Emails:   {plan.get('total_emails'):,}")
        print(f"    Folders:  {', '.join(plan.get('folders', []))}")
        print()

        print("  1. Dry run (show actions, touch nothing)")
        print("  2. Move to Trash (reversible)")
        if plan.get("mode") == "hard":
            print("  3. Hard delete (PERMANENT, where allowed)")
        print("  q. Cancel")
        print()

        choice = input("  Choice: ").strip().lower()

        if choice == "q":
            return

        dry = choice == "1"
        hard = choice == "3" and plan.get("mode") == "hard"

        if not dry:
            typed = input(f"\n  Type the number of emails to confirm ({plan['total_emails']}): ").strip()
            if typed != str(plan["total_emails"]):
                print("  Confirmation mismatch — cancelled.")
                input("  Press Enter…")
                return

        self.run_plan(plan, hard=hard, dry_run=dry, index=self._load_index())
        input("  Press Enter…")

    # ── Auto Review with AI (per email) ──────────────────────────────────────

    def auto_review(self, folder: str = "INBOX", limit: int = 100):
        """Automatically review emails using Claude AI."""
        print_banner("Email Reviewer", f"🤖 Auto-Review: {folder}")

        success, total = self.client.select_folder(folder)
        if not success:
            print(f"  ❌ Could not open folder: {folder}")
            input("  Press Enter…")
            return

        print(f"  📁 Folder: {folder} ({total:,} emails)")
        print(f"  🔍 Searching...")

        uids = self.client.uid_search("ALL")
        if not uids:
            print("  No emails found.")
            input("  Press Enter…")
            return

        # Most recent first, filter already reviewed
        uids = uids[::-1]
        pending = [u for u in uids if f"{folder}::{u}" not in self.decisions]

        print(f"  📊 Total: {len(uids):,} | Reviewed: {len(uids) - len(pending):,} | Pending: {len(pending):,}")

        if not pending:
            print("  ✅ All emails already reviewed!")
            input("  Press Enter…")
            return

        to_review = pending[:limit]
        print(f"  🎯 Will review: {len(to_review)} emails with {AI_MODEL}")
        print()

        if not confirm("Start auto-review?"):
            return

        print()
        divider()

        stats = {"useful": 0, "not_useful": 0, "categories": {}}
        batch, batch_keys = [], []
        reviewed = 0
        start_time = time.time()

        for i, uid in enumerate(to_review):
            key = f"{folder}::{uid}"

            hdr = self.client.fetch_headers(uid)
            if hdr is None:
                continue

            sender = decode_header(hdr.get("From", "unknown"))
            subject = decode_header(hdr.get("Subject", "(no subject)"))
            date = format_date(hdr)

            full = self.client.fetch_full(uid)
            preview = get_body_text(full, max_length=300) if full else ""

            batch.append({"sender": sender, "subject": subject, "date": date, "preview": preview})
            batch_keys.append(key)

            if len(batch) >= AI_BATCH_SIZE or i == len(to_review) - 1:
                print(f"\n  📨 Analyzing emails {reviewed + 1}-{reviewed + len(batch)}...")

                try:
                    verdicts = ai_batch_analyze(batch)
                except AIError as e:
                    print(f"  ❌ {e}")
                    print("     Stopping; nothing from this batch was recorded.")
                    break

                for j, email_key in enumerate(batch_keys, 1):
                    email_data = batch[j - 1]
                    subj_short = email_data["subject"][:50] + "..." if len(email_data["subject"]) > 50 else email_data["subject"]
                    if j not in verdicts:
                        print(f"    ❔ {subj_short}  → no verdict, left unreviewed")
                        continue
                    category, keep, reason = verdicts[j]
                    decision = "useful" if keep else "not_useful"
                    stats[decision] += 1
                    stats["categories"][category] = stats["categories"].get(category, 0) + 1

                    emoji = "✅" if keep else "🗑️"
                    cat_emoji = CATEGORIES.get(category, {}).get("emoji", "📧")
                    print(f"    {emoji} {cat_emoji} {subj_short}")
                    print(f"       → {reason}")

                    self.decisions[email_key] = decision
                    self.categories[email_key] = category

                save_decisions(self.decisions)
                save_categories(self.categories)

                reviewed += len(batch)
                batch, batch_keys = [], []

        elapsed = time.time() - start_time

        print()
        divider("═")
        print("  📊 Auto-Review Summary")
        divider("═")
        print()
        print(f"  Reviewed: {reviewed} emails in {elapsed:.1f}s")
        print(f"  ✅ Useful: {stats['useful']}")
        print(f"  🗑️  Not useful: {stats['not_useful']}")
        print()
        print("  By category:")
        for cat, count in sorted(stats["categories"].items(), key=lambda x: -x[1]):
            emoji = CATEGORIES.get(cat, {}).get("emoji", "📧")
            print(f"    {emoji} {cat}: {count}")
        print()
        input("  Press Enter…")

    # ── Main Menu ────────────────────────────────────────────────────────────

    def main_menu(self):
        """Main application menu."""
        while True:
            folder_counts, reviewable = self.show_folder_overview()

            divider()
            print("  1. Review a folder (manual)")
            print("  2. 🤖 Auto-review with AI (per email)")
            print("  3. 📊 Group by sender + bulk delete (recommended)")
            print("  4. 📋 Execute saved deletion plan")
            print("  5. Quick: INBOX from apple.com")
            print("  6. Quick: Junk folder")
            print("  7. Quick: promotions folder")
            print("  8. Show statistics")
            print("  9. Export summary (.txt)")
            print("  0. Quit")
            divider()
            print()

            choice = input("  Choice [0-9]: ").strip()

            if choice == "1":
                print()
                for i, f in enumerate(reviewable, 1):
                    n = folder_counts.get(f, 0)
                    print(f"  {i:2}. {f}  ({n:,})")
                print()

                sel = input("  Folder number or name: ").strip()
                try:
                    folder = reviewable[int(sel) - 1]
                except (ValueError, IndexError):
                    folder = sel

                query, desc = self.build_search_query()
                self.review_folder(folder, search_query=query)

            elif choice == "2":
                print()
                for i, f in enumerate(reviewable, 1):
                    n = folder_counts.get(f, 0)
                    print(f"  {i:2}. {f}  ({n:,})")
                print()

                sel = input("  Folder to auto-review: ").strip()
                try:
                    folder = reviewable[int(sel) - 1]
                except (ValueError, IndexError):
                    folder = sel

                limit_str = input("  How many emails? [100]: ").strip() or "100"
                try:
                    limit = int(limit_str)
                except ValueError:
                    limit = 100

                self.auto_review(folder, limit=limit)

            elif choice == "3":
                self.group_by_sender()

            elif choice == "4":
                self.execute_saved_plan()

            elif choice == "5":
                self.review_folder("INBOX", search_query='FROM "apple.com"')

            elif choice == "6":
                self.review_folder("Junk", search_query="ALL")

            elif choice == "7":
                self.review_folder("promotions", search_query="ALL")

            elif choice == "8":
                self.show_stats()

            elif choice == "9":
                filename = export_summary(self.decisions)
                print(f"\n  📄 Exported to: {filename}")
                input("  Press Enter…")

            elif choice == "0":
                print(f"\n  👋 Goodbye! Decisions saved to data/decisions.json\n")
                break


# ── CLI ──────────────────────────────────────────────────────────────────────

def _opt(args: List[str], name: str) -> Optional[str]:
    """Value following `name` in args, or None."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _opt_int(args: List[str], name: str, default: Optional[int]) -> Optional[int]:
    v = _opt(args, name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        print(f"  ❌ {name} expects a number, got {v!r}")
        sys.exit(2)


HEADLESS = {"scan", "classify", "collect", "status", "plan", "execute", "preserve"}
NEEDS_IMAP = {"scan", "execute", "preserve"}


def main():
    """Main entry point."""
    args = sys.argv[1:]
    if args and args[0] in ("--help", "-h"):
        print(__doc__)
        return

    logging.basicConfig(level=logging.WARNING, format="  ⚠️  %(message)s")

    cmd = args[0] if args and args[0] in HEADLESS else None
    app = EmailReviewer()

    # Headless steps that only touch the index and the Claude API
    if cmd and cmd not in NEEDS_IMAP:
        index = app._load_index()
        if cmd == "status":
            app.print_status(index)
        elif cmd == "classify":
            if index is None:
                print("  No sender index yet — run `scan` first.")
                sys.exit(1)
            min_count = _opt_int(args, "--min-count", AI_MIN_SENDER_COUNT)
            limit = _opt_int(args, "--limit", None)
            if "--sync" in args:
                app.classify_sync(index, min_count, limit)
            else:
                app.classify_batch_submit(index, min_count, limit)
        elif cmd == "collect":
            if index is None:
                print("  No sender index yet — run `scan` first.")
                sys.exit(1)
            if not app.classify_batch_collect(index, wait="--wait" in args):
                sys.exit(1)  # not applied yet (still processing, or gave up)
        elif cmd == "plan":
            if index is None:
                print("  No sender index yet — run `scan` first.")
                sys.exit(1)
            n_ai, n_bulk = app.flag_from_verdicts(index, include_bulk="--bulk" in args)
            app._save_index(index)
            print(f"  Flagged {n_ai:,} senders from Claude verdicts"
                  f"{f' + {n_bulk:,} unanalyzed bulk-looking senders' if '--bulk' in args else ''}.")
            app.print_plan_summary(index)
            app.write_plan(index, mode="trash")
        return

    if not cmd:
        print_banner("Email Reviewer")

    # Get credentials
    email_addr = EMAIL_ADDRESS
    password = APP_PASSWORD

    if not email_addr:
        email_addr = input("  Your @mac.com address: ").strip()
    if not password:
        print("  (Generate at appleid.apple.com → App-Specific Passwords)")
        password = getpass.getpass("  App-specific password: ")

    print(f"  Connecting...")
    if not app.connect(email_addr, password):
        sys.exit(3)
    print("  ✅ Connected!\n")

    try:
        if cmd == "scan":
            index = app.scan_index(app._load_index())
            if not index.is_complete(app._reviewable_folders()):
                sys.exit(1)  # lets a shell loop retry until the scan is done
        elif cmd == "execute":
            if len(args) < 2 or args[1].startswith("--"):
                print("  Usage: python main.py execute PLAN --confirm N [--dry-run] [--hard]")
                sys.exit(2)
            plan = load_plan_json(args[1])
            dry = "--dry-run" in args
            hard = "--hard" in args
            if hard and plan.get("mode") != "hard":
                print("  ❌ --hard requires a plan built with mode=hard.")
                sys.exit(2)
            confirm_n = _opt_int(args, "--confirm", None)
            if not dry and confirm_n != plan.get("total_emails"):
                print(f"  ❌ Pass --confirm {plan.get('total_emails')} (the plan's total_emails) to execute.")
                sys.exit(2)
            print(f"  Plan {args[1]}: {plan.get('total_senders'):,} senders, {plan.get('total_emails'):,} emails, "
                  f"folders: {', '.join(plan.get('folders', []))}")
            app.run_plan(plan, hard=hard, dry_run=dry, index=app._load_index())
        elif cmd == "preserve":
            # python main.py preserve [--folder Keep] [--dry-run] [--new-only]
            dest = _opt(args, "--folder") or "Keep"
            app.preserve_keepers(app._load_index(), dest, dry_run="--dry-run" in args,
                                 new_only="--new-only" in args)
        elif "--stats" in args:
            app.show_stats()
        else:
            app.main_menu()
    finally:
        app.disconnect()


if __name__ == "__main__":
    main()
