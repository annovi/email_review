#!/usr/bin/env python3
"""
Email Reviewer - Main Application
==================================
A comprehensive email review tool with AI-powered categorization.

Usage:
    python main.py              # Interactive review mode
    python main.py --stats      # Show email statistics
    python main.py --help       # Show help

Requirements:
    pip install anthropic       # Optional, for AI features
"""

import sys
import getpass
from collections import defaultdict, Counter

from src.config import (
    EMAIL_ADDRESS, APP_PASSWORD, BATCH_SIZE, 
    SKIP_FOLDERS, CATEGORIES
)
from src.imap_client import (
    IMAPClient, decode_header, get_body_text, 
    format_date, extract_domain
)
from src.storage import (
    load_decisions, save_decisions, load_categories, 
    save_categories, export_summary
)
from src.categorizer import auto_categorize, ai_categorize, ai_summary, ai_batch_analyze, BATCH_SIZE as AI_BATCH_SIZE
from src.ui import (
    clear_screen, divider, print_banner, print_wrapped, 
    print_menu, get_choice, confirm
)


class EmailReviewer:
    """Main application class."""
    
    def __init__(self):
        self.client: IMAPClient = None
        self.decisions = load_decisions()
        self.categories = load_categories()
        self.folders = []
    
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
        """Review emails in a folder."""
        success, total = self.client.select_folder(folder)
        if not success:
            print(f"  ❌ Could not open: {folder}")
            input("  Press Enter…")
            return
        
        print(f"  Searching {folder}...")
        msg_ids = self.client.search(search_query)
        
        if not msg_ids:
            print("  No matching emails found.")
            input("  Press Enter…")
            return
        
        msg_ids = msg_ids[::-1]  # Most recent first
        total = len(msg_ids)
        pending = [mid for mid in msg_ids if f"{folder}::{mid.decode()}" not in self.decisions]
        
        print(f"  {total:,} matching  |  {len(pending):,} not yet reviewed")
        
        if not pending:
            print("  ✅ All emails in this folder already reviewed!")
            input("  Press Enter…")
            return
        
        batch = pending[:BATCH_SIZE]
        print(f"  Reviewing next {len(batch)} emails…")
        input("  Press Enter to start…")
        
        session_count = 0
        
        for i, mid in enumerate(batch, 1):
            key = f"{folder}::{mid.decode()}"
            existing = self.decisions.get(key)
            
            # Fetch headers first (fast)
            hdr = self.client.fetch_headers(mid)
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
                        full = self.client.fetch_full(mid)
                        preview = get_body_text(full) if full else "(could not load)"
                        preview_loaded = True
                    self._display_email(folder, i, len(batch), sender, subject, date, preview, category, existing)
                    print("\n  [u] Useful  [n] Not useful  [a] AI  [c] Category  [s] Skip  [q] Quit\n")
                
                elif choice == "a":
                    if not preview_loaded:
                        print("  Loading body for AI…", end="\r")
                        full = self.client.fetch_full(mid)
                        preview = get_body_text(full) if full else ""
                        preview_loaded = True
                    
                    print("\n  🤖 Analyzing with AI...")
                    cat, useful, reason = ai_categorize(subject, sender, preview)
                    
                    if cat:
                        print(f"  Category: {CATEGORIES[cat]['emoji']} {cat}")
                        self.categories[key] = cat
                        save_categories(self.categories)
                    
                    if useful is not None:
                        print(f"  Useful: {'✅ Yes' if useful else '🗑️ No'}")
                    
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
        
        choice = input("\n  Select (1-9): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(cat_list):
                new_cat = cat_list[idx]
                self.categories[key] = new_cat
                save_categories(self.categories)
                print(f"  Changed to: {CATEGORIES[new_cat]['emoji']} {new_cat}")
        except:
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
            sender = input("  Sender domain or address: ").strip()
            return f'FROM "{sender}"', f"from {sender}"
        elif choice == "3":
            kw = input("  Subject keyword: ").strip()
            return f'SUBJECT "{kw}"', f"subject contains '{kw}'"
        elif choice == "4":
            return "UNSEEN", "unread emails"
        else:
            return "ALL", "all emails"
    
    # ── Statistics ───────────────────────────────────────────────────────────
    
    def show_stats(self):
        """Show detailed email statistics."""
        print_banner("Email Reviewer", "Statistics")
        
        # Folder counts
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
        
        # Category breakdown
        if self.categories:
            print("  Categories (reviewed emails):")
            divider()
            cat_counts = Counter(self.categories.values())
            for cat, count in cat_counts.most_common():
                emoji = CATEGORIES.get(cat, {}).get("emoji", "📧")
                print(f"    {emoji} {cat:15} {count:>6}")
            print()
        
        # Decision breakdown
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
    
    # ── Auto Review with AI ──────────────────────────────────────────────────
    
    def auto_review(self, folder: str = "INBOX", limit: int = 100):
        """Automatically review emails using Claude AI."""
        import time
        
        print_banner("Email Reviewer", f"🤖 Auto-Review: {folder}")
        
        success, total = self.client.select_folder(folder)
        if not success:
            print(f"  ❌ Could not open folder: {folder}")
            input("  Press Enter…")
            return
        
        print(f"  📁 Folder: {folder} ({total:,} emails)")
        print(f"  🔍 Searching...")
        
        msg_ids = self.client.search("ALL")
        if not msg_ids:
            print("  No emails found.")
            input("  Press Enter…")
            return
        
        # Most recent first, filter already reviewed
        msg_ids = msg_ids[::-1]
        pending = [mid for mid in msg_ids if f"{folder}::{mid.decode()}" not in self.decisions]
        
        print(f"  📊 Total: {len(msg_ids):,} | Reviewed: {len(msg_ids) - len(pending):,} | Pending: {len(pending):,}")
        
        if not pending:
            print("  ✅ All emails already reviewed!")
            input("  Press Enter…")
            return
        
        to_review = pending[:limit]
        print(f"  🎯 Will review: {len(to_review)} emails")
        print()
        
        if not confirm("Start auto-review?"):
            return
        
        print()
        divider()
        
        # Stats
        stats = {"useful": 0, "not_useful": 0, "categories": {}}
        
        # Process in batches
        batch = []
        batch_keys = []
        reviewed = 0
        start_time = time.time()
        
        for i, mid in enumerate(to_review):
            key = f"{folder}::{mid.decode()}"
            
            # Fetch email
            hdr = self.client.fetch_headers(mid)
            if hdr is None:
                continue
            
            sender = decode_header(hdr.get("From", "unknown"))
            subject = decode_header(hdr.get("Subject", "(no subject)"))
            date = format_date(hdr)
            
            # Get preview
            full = self.client.fetch_full(mid)
            preview = get_body_text(full, max_length=300) if full else ""
            
            batch.append({
                "sender": sender,
                "subject": subject,
                "date": date,
                "preview": preview
            })
            batch_keys.append(key)
            
            # Process batch
            if len(batch) >= AI_BATCH_SIZE or i == len(to_review) - 1:
                print(f"\n  📨 Analyzing emails {reviewed + 1}-{reviewed + len(batch)}...")
                
                results = ai_batch_analyze(batch)
                
                for j, (category, is_useful, reason) in enumerate(results):
                    email_key = batch_keys[j]
                    email_data = batch[j]
                    
                    # Update stats
                    decision = "useful" if is_useful else "not_useful"
                    stats["useful" if is_useful else "not_useful"] += 1
                    stats["categories"][category] = stats["categories"].get(category, 0) + 1
                    
                    # Display
                    subj_short = email_data["subject"][:50] + "..." if len(email_data["subject"]) > 50 else email_data["subject"]
                    emoji = "✅" if is_useful else "🗑️"
                    cat_emoji = CATEGORIES.get(category, {}).get("emoji", "📧")
                    
                    print(f"    {emoji} {cat_emoji} {subj_short}")
                    print(f"       → {reason}")
                    
                    # Save
                    self.decisions[email_key] = decision
                    self.categories[email_key] = category
                
                save_decisions(self.decisions)
                save_categories(self.categories)
                
                reviewed += len(batch)
                batch = []
                batch_keys = []
                
                time.sleep(1)  # Rate limiting
        
        # Summary
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
            print("  2. 🤖 Auto-review with AI")
            print("  3. Quick: INBOX from apple.com")
            print("  4. Quick: Junk folder")
            print("  5. Quick: promotions folder")
            print("  6. Show statistics")
            print("  7. Export summary")
            print("  8. Quit")
            divider()
            print()
            
            choice = input("  Choice [1-8]: ").strip()
            
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
                # Auto-review with AI
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
                except:
                    limit = 100
                
                self.auto_review(folder, limit=limit)
            
            elif choice == "3":
                self.review_folder("INBOX", search_query='FROM "apple.com"')
            
            elif choice == "4":
                self.review_folder("Junk", search_query="ALL")
            
            elif choice == "5":
                self.review_folder("promotions", search_query="ALL")
            
            elif choice == "6":
                self.show_stats()
            
            elif choice == "7":
                filename = export_summary(self.decisions)
                print(f"\n  📄 Exported to: {filename}")
                input("  Press Enter…")
            
            elif choice == "8":
                print(f"\n  👋 Goodbye! Decisions saved to data/decisions.json\n")
                break


def main():
    """Main entry point."""
    # Handle command line args
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--help", "-h"):
            print(__doc__)
            return
    
    print_banner("Email Reviewer")
    
    # Get credentials
    email_addr = EMAIL_ADDRESS
    password = APP_PASSWORD
    
    if not email_addr:
        email_addr = input("  Your @mac.com address: ").strip()
    if not password:
        print("  (Generate at appleid.apple.com → App-Specific Passwords)")
        password = getpass.getpass("  App-specific password: ")
    
    print(f"\n  Connecting...")
    
    app = EmailReviewer()
    
    if not app.connect(email_addr, password):
        return
    
    print("  ✅ Connected!\n")
    
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--stats":
            app.show_stats()
        else:
            app.main_menu()
    finally:
        app.disconnect()


if __name__ == "__main__":
    main()
