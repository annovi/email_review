"""
IMAP client for connecting to email servers.

Everything is UID-based: sequence numbers shift whenever anything is expunged,
UIDs do not. Commands go through `_retry`, which reconnects, reselects the open
folder and tries again when the socket drops — a 400k-message scan or a bulk
move runs long enough that iCloud will drop the connection at some point.

Server quirks this code is written around (measured against imap.mail.me.com):
- no MOVE capability, but UIDPLUS: a move is COPY + \\Deleted + UID EXPUNGE,
  so only OUR messages get expunged, never everything flagged in the folder
- UID SEARCH ALL returns UIDs unsorted and, on INBOX, ~10% more UIDs than the
  folder has messages; FETCH silently skips the phantoms. UID SEARCH DELETED
  returns exactly those phantoms (already-expunged UIDs), so it is useless as
  a "pending removal" count — trust EXISTS/STATUS and FETCH FLAGS instead
- bulk writes are throttled per command (~1–2 min each regardless of size) and,
  after a burst, answered NO for a while; see IMAP_MAX_CONSECUTIVE_REFUSALS
"""

import re
import time
import email
import email.header
import email.message
import imaplib
import logging
from email.utils import parsedate_to_datetime
from typing import Optional, List, Tuple, Dict, Sequence, Callable, Any, Iterator

from .config import (
    IMAP_HOST, IMAP_PORT, IMAP_TIMEOUT, IMAP_MAX_RETRIES,
    UID_COMMAND_CHUNK, UID_COMMAND_MAX_CHARS, IMAP_MAX_CONSECUTIVE_REFUSALS,
)

# UID SEARCH ALL on a 400k-message folder comes back as one ~3 MB line.
imaplib._MAXLINE = 50_000_000

log = logging.getLogger(__name__)

SCAN_HEADER_FIELDS = "FROM SUBJECT DATE LIST-UNSUBSCRIBE PRECEDENCE"
SCAN_FETCH_SPEC = f"(BODY.PEEK[HEADER.FIELDS ({SCAN_HEADER_FIELDS})])"
REVIEW_FETCH_SPEC = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"

_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$')


class IMAPRefused(Exception):
    """The server answered NO/BAD. Not a connection problem; retrying won't help."""


class IMAPUnavailable(Exception):
    """The connection could not be re-established after IMAP_MAX_RETRIES attempts."""


def _text(data) -> str:
    """Render an imaplib response payload for an error message."""
    if not data:
        return ""
    parts = [x for x in data if isinstance(x, bytes)]
    return b" ".join(parts).decode(errors="replace")[:200]


class IMAPClient:
    """Handles IMAP connection and email operations."""

    def __init__(self, email_address: str, password: str):
        self.email_address = email_address
        self.password = password
        self.mail: Optional[imaplib.IMAP4_SSL] = None
        self.capabilities: set = set()
        # (folder, readonly) — reselected after a reconnect
        self._selected: Optional[Tuple[str, bool]] = None
        # set once the server has refused IMAP_MAX_CONSECUTIVE_REFUSALS bulk batches in a row
        self.throttled = False

    # ── Connection ───────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self.mail is not None

    def connect(self) -> bool:
        """Connect to the IMAP server."""
        try:
            self._login()
        except imaplib.IMAP4.error as e:
            raise ConnectionError(f"Login failed: {e}")
        except OSError as e:  # DNS failure, no route, TLS handshake, timeout
            raise ConnectionError(f"Could not reach {IMAP_HOST}:{IMAP_PORT}: {e}")
        return True

    def _login(self):
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.login(self.email_address, self.password)
        self.mail = mail
        status, data = mail.capability()
        if status == "OK" and data and data[0]:
            self.capabilities = set(data[0].decode(errors="replace").upper().split())

    def supports(self, capability: str) -> bool:
        return capability.upper() in self.capabilities

    def disconnect(self):
        """Disconnect from the server."""
        if self.mail:
            try:
                self.mail.logout()
            except Exception:
                pass
            self.mail = None

    def _drop(self):
        """Discard a (probably dead) socket without the LOGOUT round-trip."""
        if self.mail:
            try:
                self.mail.shutdown()
            except Exception:
                pass
            self.mail = None

    def _reconnect(self):
        self._drop()
        self._login()
        if self._selected:
            folder, readonly = self._selected
            status, data = self.mail.select(self._quote(folder), readonly=readonly)
            if status != "OK":
                raise imaplib.IMAP4.abort(f"could not reselect {folder}: {status} {_text(data)}")

    def _retry(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run fn; when the connection drops, reconnect (reselecting the folder) and retry.

        A NO/BAD answer from the server is raised as IMAPRefused immediately —
        that is the server declining the command, not the network failing.
        """
        delay, last = 2.0, None
        for attempt in range(1, IMAP_MAX_RETRIES + 1):
            if self.mail is None:
                try:
                    self._reconnect()
                except (imaplib.IMAP4.error, OSError, EOFError) as e:
                    last = e
                    log.warning("%s: reconnect failed (attempt %d/%d): %s",
                                label, attempt, IMAP_MAX_RETRIES, e)
                    self._drop()
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
            try:
                return fn()
            except imaplib.IMAP4.abort as e:        # subclass of error: check first
                last = e
            except imaplib.IMAP4.error as e:
                raise IMAPRefused(f"{label}: {e}") from e
            except (OSError, EOFError) as e:        # ssl.SSLError, socket.timeout, reset
                last = e
            log.warning("%s: connection lost (attempt %d/%d): %s — reconnecting",
                        label, attempt, IMAP_MAX_RETRIES, last)
            self._drop()
            time.sleep(delay)
            delay = min(delay * 2, 60)
        self._drop()
        raise IMAPUnavailable(f"{label}: gave up after {IMAP_MAX_RETRIES} attempts ({last})")

    @staticmethod
    def _quote(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    # ── Folders ──────────────────────────────────────────────────────────────

    def list_folders(self) -> List[str]:
        """List all mailbox folders."""
        def do():
            status, data = self.mail.list()
            if status != "OK":
                raise IMAPRefused(f"LIST: {status} {_text(data)}")
            return data

        names: List[str] = []
        for item in self._retry("LIST", do):
            if isinstance(item, tuple):  # literal-form folder name
                raw = item[1]
                name = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            else:
                line = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
                m = _LIST_RE.match(line)
                name = m.group("name").strip() if m else line.rsplit(" ", 1)[-1]
                if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                    name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if name:
                names.append(name)
        return names

    def select_folder(self, folder: str, readonly: bool = True) -> Tuple[bool, int]:
        """Select a folder and return (success, message_count)."""
        def do():
            status, data = self.mail.select(self._quote(folder), readonly=readonly)
            if status != "OK":
                raise IMAPRefused(f"SELECT {folder}: {status} {_text(data)}")
            try:
                return int(data[0]) if data and data[0] else 0
            except (TypeError, ValueError):
                return 0

        try:
            count = self._retry(f"SELECT {folder}", do)
        except IMAPRefused as e:
            log.info("%s", e)
            return False, 0
        self._selected = (folder, readonly)
        return True, count

    def select_folder_rw(self, folder: str) -> Tuple[bool, int]:
        """Select a folder in read-write mode (required for delete/move)."""
        return self.select_folder(folder, readonly=False)

    def count_folder(self, folder: str) -> int:
        """Number of messages in a folder, via STATUS (does not change the selected folder)."""
        def do():
            status, data = self.mail.status(self._quote(folder), "(MESSAGES)")
            if status != "OK":
                raise IMAPRefused(f"STATUS {folder}: {status} {_text(data)}")
            m = re.search(rb"MESSAGES\s+(\d+)", data[0] or b"")
            return int(m.group(1)) if m else 0

        try:
            return self._retry(f"STATUS {folder}", do)
        except IMAPRefused:
            return 0

    def detect_trash_folder(self, preferred: Optional[str] = None) -> str:
        """Find the trash/deleted folder name for this server."""
        folders = self.list_folders()
        candidates = [preferred] if preferred else []
        candidates += ["Deleted Messages", "Trash", "[Gmail]/Trash",
                       "INBOX.Trash", "Deleted Items"]
        for c in candidates:
            if c and c in folders:
                return c
        return preferred or "Deleted Messages"

    def create_folder(self, folder: str) -> bool:
        """Create a folder unless it already exists. True if it exists afterwards."""
        if folder in self.list_folders():
            return True

        def do():
            status, data = self.mail.create(self._quote(folder))
            if status != "OK":
                raise IMAPRefused(f"CREATE {folder}: {status} {_text(data)}")
            return True

        try:
            return bool(self._retry(f"CREATE {folder}", do))
        except IMAPRefused as e:
            log.warning("%s", e)
            return False

    # ── Search / fetch (read-only) ───────────────────────────────────────────

    def uid_search(self, criteria: str = "ALL") -> List[int]:
        """UIDs matching criteria in the selected folder, as sorted unique ints.

        Sorting is what makes range fetches and resume-by-last-UID valid —
        iCloud returns the list in no particular order.
        """
        def do():
            status, data = self.mail.uid("SEARCH", None, criteria)
            if status != "OK":
                raise IMAPRefused(f"UID SEARCH: {status} {_text(data)}")
            if not data or not data[0]:
                return []
            return sorted({int(x) for x in data[0].split()})

        return self._retry("UID SEARCH", do)

    def fetch_headers_batch(self, uids: Sequence[int], as_range: bool = False) -> Dict[int, Dict[str, str]]:
        """Fetch scan headers for many messages in one command.

        Returns {uid: {"from", "subject", "date", "list_unsubscribe", "precedence"}}.
        UIDs the server does not know are simply absent from the result.

        as_range=True sends `first:last` instead of every UID — valid only when
        `uids` is a contiguous slice of the folder's sorted UID list (then every
        existing message in that range is in the slice). ~15 bytes instead of ~4 KB.
        """
        if not uids:
            return {}
        if as_range:
            uid_set = f"{uids[0]}:{uids[-1]}"
        else:
            uid_set = ",".join(str(u) for u in uids)

        def do():
            status, data = self.mail.uid("FETCH", uid_set, SCAN_FETCH_SPEC)
            if status != "OK":
                raise IMAPRefused(f"UID FETCH: {status} {_text(data)}")
            return data or []

        result: Dict[int, Dict[str, str]] = {}
        for item in self._retry("UID FETCH headers", do):
            if not (isinstance(item, tuple) and len(item) >= 2):
                continue
            prefix = item[0]
            if isinstance(prefix, bytes):
                prefix = prefix.decode("utf-8", errors="replace")
            m = re.search(r"UID\s+(\d+)", prefix)
            raw = item[1]
            if not m or not isinstance(raw, bytes):
                continue
            try:
                msg = email.message_from_bytes(raw)
            except Exception:
                continue
            result[int(m.group(1))] = {
                "from": msg.get("From", "") or "",
                "subject": msg.get("Subject", "") or "",
                "date": msg.get("Date", "") or "",
                "list_unsubscribe": msg.get("List-Unsubscribe", "") or "",
                "precedence": msg.get("Precedence", "") or "",
            }
        return result

    def fetch_headers(self, uid: int) -> Optional[email.message.Message]:
        """Fetch only From/Subject/Date headers of one message (fast)."""
        return self._fetch_one(uid, REVIEW_FETCH_SPEC)

    def fetch_full(self, uid: int) -> Optional[email.message.Message]:
        """Fetch one full message including body, without setting \\Seen."""
        return self._fetch_one(uid, "(BODY.PEEK[])")

    def _fetch_one(self, uid: int, spec: str) -> Optional[email.message.Message]:
        def do():
            status, data = self.mail.uid("FETCH", str(uid), spec)
            if status != "OK":
                raise IMAPRefused(f"UID FETCH {uid}: {status} {_text(data)}")
            return data or []

        try:
            data = self._retry(f"UID FETCH {uid}", do)
        except IMAPRefused:
            return None
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                return email.message_from_bytes(part[1])
        return None

    # ── Write operations (destructive) ───────────────────────────────────────

    @staticmethod
    def _sequence_sets(uids: Sequence[int],
                       max_uids: int = UID_COMMAND_CHUNK,
                       max_chars: int = UID_COMMAND_MAX_CHARS) -> Iterator[Tuple[str, List[int]]]:
        """Yield (sequence-set string, uids it covers), collapsing runs into a:b.

        When most of a folder is being removed the UIDs form long runs and one
        short set covers thousands of messages; scattered UIDs fall back to
        ~max_chars/8 per command, which keeps every command line small.
        """
        us = sorted(set(int(u) for u in uids))
        chunk: List[int] = []
        tokens: List[str] = []
        length = 0
        start = prev = None

        def flush_run():
            nonlocal length
            if start is None:
                return
            tok = f"{start}:{prev}" if prev > start else str(start)
            tokens.append(tok)
            length += len(tok) + 1

        for u in us:
            if start is not None and u == prev + 1 and len(chunk) < max_uids:
                prev = u
                chunk.append(u)
                continue
            flush_run()
            if chunk and (length >= max_chars or len(chunk) >= max_uids):
                yield ",".join(tokens), chunk
                chunk, tokens, length = [], [], 0
            start = prev = u
            chunk.append(u)
        flush_run()
        if chunk:
            yield ",".join(tokens), chunk

    def _uid_command(self, label: str, *args) -> Any:
        def do():
            status, data = self.mail.uid(*args)
            if status != "OK":
                raise IMAPRefused(f"{label}: {status} {_text(data)}")
            return data

        return self._retry(label, do)

    def _expunge_uids(self, uid_set: str):
        if self.supports("UIDPLUS"):
            self._uid_command("UID EXPUNGE", "EXPUNGE", uid_set)
        else:
            # Without UIDPLUS a plain EXPUNGE removes every \Deleted message in
            # the folder, not just ours — including anything a mail client
            # flagged but has not purged yet.
            log.warning("server lacks UIDPLUS: plain EXPUNGE removes ALL \\Deleted messages in the folder")
            self._retry("EXPUNGE", lambda: self.mail.expunge())

    def copy_to_folder(self, uids: Sequence[int], dest_folder: str,
                       on_chunk: Optional[Callable[[int, int], None]] = None,
                       max_uids: int = UID_COMMAND_CHUNK,
                       max_chars: int = UID_COMMAND_MAX_CHARS) -> Tuple[List[int], List[str]]:
        """COPY messages by UID into dest_folder, in chunks. The originals are untouched.

        Returns (uids copied, error messages). Phantom/missing UIDs are skipped by
        the server. Unlike move/delete this never flags or expunges anything, and
        iCloud has accepted COPY even while refusing expunges.
        """
        if not uids:
            return [], []
        done: List[int] = []
        errors: List[str] = []
        total = len(uids)
        dest = self._quote(dest_folder)
        refused_in_a_row = 0
        for uid_set, chunk in self._sequence_sets(uids, max_uids=max_uids, max_chars=max_chars):
            try:
                self._uid_command("UID COPY", "COPY", uid_set, dest)
                done.extend(chunk)
                refused_in_a_row = 0
            except IMAPRefused as e:
                errors.append(str(e))
                refused_in_a_row += 1
                if self._too_many_refusals(refused_in_a_row, errors):
                    if on_chunk:
                        on_chunk(len(done), total)
                    break
            except IMAPUnavailable as e:
                errors.append(str(e))
                break
            if on_chunk:
                on_chunk(len(done), total)
        return done, errors

    def move_to_folder(self, uids: Sequence[int], dest_folder: str,
                       on_chunk: Optional[Callable[[int, int], None]] = None) -> Tuple[List[int], List[str]]:
        """Move messages by UID to dest_folder, in chunks.

        Returns (uids actually moved, error messages). Stops early when the
        connection cannot be recovered; the caller sees that via `connected`.
        Uses MOVE when the server has it, else COPY + \\Deleted + UID EXPUNGE.
        """
        if not uids:
            return [], []
        done: List[int] = []
        errors: List[str] = []
        total = len(uids)
        dest = self._quote(dest_folder)
        use_move = self.supports("MOVE") and "MOVE" in imaplib.Commands
        refused_in_a_row = 0
        for uid_set, chunk in self._sequence_sets(uids):
            try:
                if use_move:
                    self._uid_command("UID MOVE", "MOVE", uid_set, dest)
                else:
                    self._uid_command("UID COPY", "COPY", uid_set, dest)
                    self._uid_command("UID STORE", "STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)")
                    self._expunge_uids(uid_set)
                done.extend(chunk)
                refused_in_a_row = 0
            except IMAPRefused as e:
                errors.append(str(e))
                refused_in_a_row += 1
                if self._too_many_refusals(refused_in_a_row, errors):
                    if on_chunk:
                        on_chunk(len(done), total)
                    break
            except IMAPUnavailable as e:
                errors.append(str(e))
                break
            if on_chunk:
                on_chunk(len(done), total)
        return done, errors

    def _too_many_refusals(self, refused_in_a_row: int, errors: List[str]) -> bool:
        """iCloud throttles by answering NO to every bulk command for a while; after a few
        refusals in a row, further batches only pile duplicate copies into Trash.
        Sets self.throttled so the caller can skip the remaining folders too."""
        if refused_in_a_row < IMAP_MAX_CONSECUTIVE_REFUSALS:
            return False
        msg = (f"server refused {refused_in_a_row} batches in a row — stopping; "
               f"re-run the same plan later (already-moved messages are skipped)")
        log.warning(msg)
        errors.append(msg)
        self.throttled = True
        return True

    def delete_permanently(self, uids: Sequence[int],
                           on_chunk: Optional[Callable[[int, int], None]] = None) -> Tuple[List[int], List[str]]:
        """Flag \\Deleted and expunge by UID, in chunks. Returns (uids deleted, errors)."""
        if not uids:
            return [], []
        done: List[int] = []
        errors: List[str] = []
        total = len(uids)
        refused_in_a_row = 0
        for uid_set, chunk in self._sequence_sets(uids):
            try:
                self._uid_command("UID STORE", "STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)")
                self._expunge_uids(uid_set)
                done.extend(chunk)
                refused_in_a_row = 0
            except IMAPRefused as e:
                errors.append(str(e))
                refused_in_a_row += 1
                if self._too_many_refusals(refused_in_a_row, errors):
                    if on_chunk:
                        on_chunk(len(done), total)
                    break
            except IMAPUnavailable as e:
                errors.append(str(e))
                break
            if on_chunk:
                on_chunk(len(done), total)
        return done, errors


# Email parsing utilities

def decode_header(value: str) -> str:
    """Decode a MIME-encoded email header."""
    if value is None:
        return ""

    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value).strip()
    decoded = []

    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))

    return " ".join(decoded).strip()


def get_body_text(msg: email.message.Message, max_length: int = 300) -> str:
    """Extract plain-text body from an email."""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition", ""))):
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            pass

    # Clean and truncate
    body = " ".join(body.split())
    if len(body) > max_length:
        return body[:max_length] + "…"
    return body


def format_date(msg: email.message.Message) -> str:
    """Parse and format the email date."""
    try:
        return parsedate_to_datetime(msg.get("Date", "")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return msg.get("Date", "unknown date")


def extract_domain(sender: str) -> str:
    """Extract domain from sender email address."""
    if "<" in sender and ">" in sender:
        email_addr = sender.split("<")[1].split(">")[0]
    else:
        email_addr = sender

    if "@" in email_addr:
        return email_addr.split("@")[1].lower()
    return "unknown"
