"""
Group emails by sender for bulk review and deletion.

The primary workflow for cleaning a large mailbox: scan all folders once,
aggregate emails by sender address, then make one decision per sender that
applies to all their messages.

The scan result is a SenderIndex. It is checkpointed to disk while scanning
(so an interrupted scan resumes from the last UID seen, not from zero), and
it carries the AI verdicts and the user's flags, so the review can span
several sessions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime, parseaddr
from typing import Dict, List, Optional, Callable, Any, Iterable

from .config import BULK_RATIO_THRESHOLD, SCAN_CHUNK_SIZE
from .imap_client import IMAPClient, IMAPUnavailable, decode_header

MAX_SAMPLE_SUBJECTS = 5


@dataclass
class SenderGroup:
    """All emails from a single sender across all scanned folders."""
    email: str
    name: str
    domain: str
    count: int = 0
    # folder name -> UIDs in that folder
    uids_by_folder: Dict[str, List[int]] = field(default_factory=dict)
    # most recent distinct subjects, oldest first
    sample_subjects: List[str] = field(default_factory=list)
    first_date: Optional[datetime] = None
    last_date: Optional[datetime] = None
    # messages carrying List-Unsubscribe or Precedence: bulk/list
    bulk_hits: int = 0
    # Filled in by Claude analysis
    category: Optional[str] = None
    ai_useful: Optional[bool] = None
    ai_reason: str = ""
    # UI / plan state
    flagged_for_delete: bool = False

    @property
    def bulk_ratio(self) -> float:
        return self.bulk_hits / self.count if self.count else 0.0

    @property
    def looks_bulk(self) -> bool:
        """Heuristic: most of this sender's mail carries a bulk-mail marker."""
        return self.count >= 3 and self.bulk_ratio >= BULK_RATIO_THRESHOLD

    @property
    def verdict(self) -> str:
        if self.ai_useful is False:
            return "delete"
        if self.ai_useful is True:
            return "keep"
        return "bulk?" if self.looks_bulk else "?"

    def add(self, folder: str, uid: int, subject: str, date: Optional[datetime], is_bulk: bool) -> None:
        self.uids_by_folder.setdefault(folder, []).append(int(uid))
        self.count += 1
        if is_bulk:
            self.bulk_hits += 1
        if subject:
            # scanned in ascending UID order, so later = newer: keep the newest few
            if subject in self.sample_subjects:
                self.sample_subjects.remove(subject)
            self.sample_subjects.append(subject)
            if len(self.sample_subjects) > MAX_SAMPLE_SUBJECTS:
                del self.sample_subjects[0]
        if date is not None:
            if self.first_date is None or date < self.first_date:
                self.first_date = date
            if self.last_date is None or date > self.last_date:
                self.last_date = date

    def remove_uids(self, folder: str, uids: Iterable[int]) -> int:
        """Forget messages that were moved/deleted. Returns how many were removed."""
        gone = set(int(u) for u in uids)
        have = self.uids_by_folder.get(folder)
        if not have:
            return 0
        kept = [u for u in have if u not in gone]
        removed = len(have) - len(kept)
        if kept:
            self.uids_by_folder[folder] = kept
        else:
            del self.uids_by_folder[folder]
        self.count -= removed
        return removed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "domain": self.domain,
            "count": self.count,
            "uids_by_folder": self.uids_by_folder,
            "sample_subjects": self.sample_subjects,
            "first_date": self.first_date.isoformat() if self.first_date else None,
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "bulk_hits": self.bulk_hits,
            "category": self.category,
            "ai_useful": self.ai_useful,
            "ai_reason": self.ai_reason,
            "flagged_for_delete": self.flagged_for_delete,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SenderGroup":
        def dt(v):
            try:
                return datetime.fromisoformat(v) if v else None
            except (TypeError, ValueError):
                return None
        return cls(
            email=d.get("email", "unknown"),
            name=d.get("name", ""),
            domain=d.get("domain", "unknown"),
            count=int(d.get("count", 0)),
            uids_by_folder={f: [int(u) for u in us] for f, us in (d.get("uids_by_folder") or {}).items()},
            sample_subjects=list(d.get("sample_subjects") or []),
            first_date=dt(d.get("first_date")),
            last_date=dt(d.get("last_date")),
            bulk_hits=int(d.get("bulk_hits", 0)),
            category=d.get("category"),
            ai_useful=d.get("ai_useful"),
            ai_reason=d.get("ai_reason") or "",
            flagged_for_delete=bool(d.get("flagged_for_delete", False)),
        )


def parse_sender(raw_from: str) -> tuple:
    """Return (display_name, email_lowercase, domain) from a raw From header."""
    if not raw_from:
        return ("unknown", "unknown", "unknown")
    try:
        decoded = decode_header(raw_from)
        name, addr = parseaddr(decoded)
        addr = (addr or "").strip().lower()
        if not addr or "@" not in addr:
            return (decoded.strip() or "unknown", decoded.strip().lower() or "unknown", "unknown")
        domain = addr.split("@", 1)[1]
        return (name.strip() or addr, addr, domain)
    except Exception:
        clean = raw_from.strip().lower()
        return (clean, clean, "unknown")


def _parse_date_safe(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
        if d is not None and d.tzinfo is not None:
            d = d.replace(tzinfo=None)
        return d
    except Exception:
        return None


def _is_bulk(hdr: Dict[str, str]) -> bool:
    if hdr.get("list_unsubscribe", "").strip():
        return True
    return hdr.get("precedence", "").strip().lower() in ("bulk", "list", "junk")


@dataclass
class SenderIndex:
    """The scan result: sender groups + per-folder scan progress + pending AI batch."""
    groups: Dict[str, SenderGroup] = field(default_factory=dict)
    # folder -> {"last_uid": int, "scanned": int, "expected": int, "done": bool}
    scan_progress: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # A submitted Message Batches job awaiting collection:
    # {"id", "model", "submitted_at", "groups": {custom_id: [sender keys]}}
    ai_batch: Optional[Dict[str, Any]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # ── building ──
    def add_message(self, folder: str, uid: int, hdr: Dict[str, str]) -> None:
        name, addr, domain = parse_sender(hdr.get("from", ""))
        subject = decode_header(hdr.get("subject", ""))
        date = _parse_date_safe(hdr.get("date", ""))
        key = addr if addr and addr != "unknown" else f"unknown::{domain}"
        group = self.groups.get(key)
        if group is None:
            group = self.groups[key] = SenderGroup(email=addr, name=name, domain=domain)
        group.add(folder, uid, subject, date, _is_bulk(hdr))

    def prune(self, sender_email: str, folder: str, uids: Iterable[int]) -> None:
        """Forget messages that were moved/deleted; drop senders left with nothing."""
        for key, g in list(self.groups.items()):
            if g.email == sender_email:
                g.remove_uids(folder, uids)
                if g.count <= 0:
                    del self.groups[key]

    # ── queries ──
    def sorted_groups(self) -> List[SenderGroup]:
        """Groups by count descending (biggest offenders first)."""
        return sorted(self.groups.values(), key=lambda g: (-g.count, g.email))

    def total_emails(self) -> int:
        return sum(g.count for g in self.groups.values())

    def pending_folders(self, folders: List[str]) -> List[str]:
        return [f for f in folders if not self.scan_progress.get(f, {}).get("done")]

    def is_complete(self, folders: List[str]) -> bool:
        return not self.pending_folders(folders)

    def scanned_total(self) -> int:
        return sum(p.get("scanned", 0) for p in self.scan_progress.values())

    # ── persistence ──
    def to_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "scan_progress": self.scan_progress,
            "ai_batch": self.ai_batch,
            "groups": {k: g.to_dict() for k, g in self.groups.items()},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SenderIndex":
        idx = cls(
            scan_progress=dict(d.get("scan_progress") or {}),
            ai_batch=d.get("ai_batch"),
            created_at=d.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            updated_at=d.get("updated_at") or "",
        )
        idx.groups = {k: SenderGroup.from_dict(g) for k, g in (d.get("groups") or {}).items()}
        return idx


def scan_folders(
    client: IMAPClient,
    folders: List[str],
    index: SenderIndex,
    chunk_size: int = SCAN_CHUNK_SIZE,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    checkpoint_callback: Optional[Callable[[SenderIndex], None]] = None,
    checkpoint_every: int = 10,
) -> None:
    """Scan folders into `index`, resuming any folder that was left half-done.

    Headers are fetched in ranges over the sorted UID list; progress is
    recorded per folder as the last UID seen, so a resume re-reads nothing.
    `checkpoint_callback` is called every `checkpoint_every` chunks and after
    each folder; it is also called before re-raising IMAPUnavailable, so a
    dropped connection never costs more than a few chunks of work.
    """
    for folder in folders:
        prog = index.scan_progress.setdefault(
            folder, {"last_uid": 0, "scanned": 0, "expected": 0, "done": False})
        if prog.get("done"):
            continue

        ok, _ = client.select_folder(folder, readonly=True)
        if not ok:
            if progress_callback:
                progress_callback(folder, 0, 0)
            prog["done"] = True
            prog["error"] = "could not select folder"
            continue

        uids = client.uid_search("ALL")
        uids = [u for u in uids if u > int(prog.get("last_uid", 0))]
        total = len(uids)
        prog["expected"] = prog.get("scanned", 0) + total
        if progress_callback:
            progress_callback(folder, 0, total)

        try:
            for n, i in enumerate(range(0, total, chunk_size), 1):
                chunk = uids[i:i + chunk_size]
                headers = client.fetch_headers_batch(chunk, as_range=True)
                for uid, hdr in headers.items():
                    index.add_message(folder, uid, hdr)
                prog["scanned"] = prog.get("scanned", 0) + len(headers)
                prog["last_uid"] = chunk[-1]
                if progress_callback:
                    progress_callback(folder, i + len(chunk), total)
                if checkpoint_callback and n % checkpoint_every == 0:
                    checkpoint_callback(index)
        except IMAPUnavailable:
            if checkpoint_callback:
                checkpoint_callback(index)
            raise

        prog["done"] = True
        if checkpoint_callback:
            checkpoint_callback(index)


def sorted_groups(index: SenderIndex) -> List[SenderGroup]:
    """Return groups sorted by count descending (biggest offenders first)."""
    return index.sorted_groups()
