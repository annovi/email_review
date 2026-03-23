"""
IMAP client for connecting to email servers.
"""

import imaplib
import email
import email.header
from email.utils import parsedate_to_datetime
from typing import Optional, List, Tuple, Dict

from .config import IMAP_HOST, IMAP_PORT

# Increase IMAP line limit for large mailboxes
imaplib._MAXLINE = 10_000_000


class IMAPClient:
    """Handles IMAP connection and email operations."""
    
    def __init__(self, email_address: str, password: str):
        self.email_address = email_address
        self.password = password
        self.mail: Optional[imaplib.IMAP4_SSL] = None
    
    def connect(self) -> bool:
        """Connect to the IMAP server."""
        try:
            self.mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            self.mail.login(self.email_address, self.password)
            return True
        except imaplib.IMAP4.error as e:
            raise ConnectionError(f"Login failed: {e}")
    
    def disconnect(self):
        """Disconnect from the server."""
        if self.mail:
            try:
                self.mail.logout()
            except:
                pass
    
    def list_folders(self) -> List[str]:
        """List all mailbox folders."""
        if not self.mail:
            return []
        
        status, folders = self.mail.list()
        result = []
        
        if status != "OK":
            return result
        
        for f in folders:
            decoded = f.decode() if isinstance(f, bytes) else f
            parts = decoded.split('"')
            name = parts[-2] if len(parts) >= 2 and parts[-1].strip() == "" else parts[-1].strip()
            name = name.strip().strip('"')
            result.append(name)
        
        return result
    
    def select_folder(self, folder: str) -> Tuple[bool, int]:
        """Select a folder and return (success, message_count)."""
        if not self.mail:
            return False, 0
        
        try:
            # Try with quotes first, then without
            status, data = self.mail.select(f'"{folder}"', readonly=True)
            if status != "OK":
                status, data = self.mail.select(folder, readonly=True)
            
            if status == "OK" and data[0]:
                try:
                    count = int(data[0].decode())
                    return True, count
                except:
                    pass
        except imaplib.IMAP4.error:
            # Handle large folder responses - use STATUS instead
            try:
                status, data = self.mail.status(f'"{folder}"', "(MESSAGES)")
                if status == "OK" and data[0]:
                    # Parse: b'"INBOX" (MESSAGES 176820)'
                    response = data[0].decode()
                    count = int(response.split("MESSAGES")[1].strip().rstrip(")"))
                    return True, count
            except:
                pass
        
        return False, 0
    
    def count_folder(self, folder: str) -> int:
        """Get the number of emails in a folder."""
        success, count = self.select_folder(folder)
        return count if success else 0
    
    def search(self, criteria: str = "ALL") -> List[bytes]:
        """Search for emails matching criteria."""
        if not self.mail:
            return []
        
        try:
            status, data = self.mail.search(None, criteria)
            if status == "OK" and data[0]:
                return data[0].split()
        except imaplib.IMAP4.error:
            # Handle large inbox search failures
            pass
        
        return []
    
    def fetch_headers(self, msg_id: bytes) -> Optional[email.message.Message]:
        """Fetch only email headers (fast)."""
        if not self.mail:
            return None
        
        status, data = self.mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if status != "OK" or not data:
            return None
        
        raw = self._extract_bytes(data)
        if raw:
            return email.message_from_bytes(raw)
        return None
    
    def fetch_full(self, msg_id: bytes) -> Optional[email.message.Message]:
        """Fetch full email including body."""
        if not self.mail:
            return None
        
        status, data = self.mail.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data:
            return None
        
        raw = self._extract_bytes(data)
        if raw:
            return email.message_from_bytes(raw)
        return None
    
    def _extract_bytes(self, data) -> Optional[bytes]:
        """Extract bytes from IMAP fetch response."""
        for response_part in data:
            if isinstance(response_part, tuple) and len(response_part) >= 2:
                if isinstance(response_part[1], bytes):
                    return response_part[1]
        return None


# Email parsing utilities

def decode_header(value: str) -> str:
    """Decode a MIME-encoded email header."""
    if value is None:
        return ""
    
    parts = email.header.decode_header(value)
    decoded = []
    
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except:
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
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace")
        except:
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
    except:
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
