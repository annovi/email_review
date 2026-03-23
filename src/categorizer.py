"""
Email categorization using rules and AI.
"""

import time
from typing import Optional, Tuple, List, Dict

from .config import CATEGORIES, CLAUDE_API_KEY
from .imap_client import extract_domain


# AI Model configuration
# Claude Sonnet 4.5 - best balance of speed, accuracy, and cost
# Pricing: $3/M input tokens, $15/M output tokens
AI_MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 10


def get_anthropic_client():
    """Get Anthropic client using API key from config."""
    import anthropic
    return anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def auto_categorize(sender: str, subject: str, body_preview: str = "") -> str:
    """
    Automatically categorize an email based on sender domain and keywords.
    Returns the category name.
    """
    domain = extract_domain(sender)
    text = f"{subject} {body_preview}".lower()
    
    # Check domain matches first (most reliable)
    for cat_name, cat_info in CATEGORIES.items():
        if cat_name == "other":
            continue
        for cat_domain in cat_info["domains"]:
            if cat_domain in domain:
                return cat_name
    
    # Check keyword matches
    for cat_name, cat_info in CATEGORIES.items():
        if cat_name == "other":
            continue
        for keyword in cat_info["keywords"]:
            if keyword.lower() in text:
                return cat_name
    
    return "other"


def ai_categorize(subject: str, sender: str, preview: str) -> Tuple[Optional[str], Optional[bool], str]:
    """
    Use Claude to categorize and assess usefulness.
    Returns (category, is_useful, explanation).
    """
    try:
        client = get_anthropic_client()
        
        categories_list = ", ".join(CATEGORIES.keys())
        
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Categorize this email and assess if it's useful to keep.\n\n"
                    f"Categories: {categories_list}\n\n"
                    f"From: {sender}\nSubject: {subject}\nPreview: {preview}\n\n"
                    f"Respond in this exact format:\n"
                    f"CATEGORY: [category_name]\n"
                    f"USEFUL: [yes/no]\n"
                    f"REASON: [brief 1-line reason]"
                )
            }]
        )
        
        text = response.content[0].text.strip()
        
        # Parse response
        category = None
        is_useful = None
        reason = text
        
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("CATEGORY:"):
                cat = line.split(":", 1)[1].strip().lower()
                if cat in CATEGORIES:
                    category = cat
            elif line.startswith("USEFUL:"):
                val = line.split(":", 1)[1].strip().lower()
                is_useful = val in ("yes", "true", "1")
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        
        return category, is_useful, reason
        
    except ImportError:
        return None, None, "⚠️  Install 'anthropic' package: pip install anthropic"
    except Exception as e:
        return None, None, f"⚠️  AI error: {e}"


def ai_summary(subject: str, sender: str, preview: str) -> str:
    """Get a quick AI summary of whether an email is worth keeping."""
    try:
        client = get_anthropic_client()
        
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    "1-2 sentences: is this email worth keeping or safe to delete? "
                    "Start with '✅ Worth keeping:' or '🗑️ Safe to delete:'.\n"
                    f"From: {sender}\nSubject: {subject}\nPreview: {preview}"
                )
            }]
        )
        return response.content[0].text.strip()
        
    except ImportError:
        return "⚠️  Run: pip install anthropic  to enable AI summaries."
    except Exception as e:
        return f"⚠️  AI error: {e}"


def ai_batch_analyze(emails: List[Dict]) -> List[Tuple[str, bool, str]]:
    """
    Analyze a batch of emails using Claude.
    
    Args:
        emails: List of dicts with 'sender', 'subject', 'date', 'preview' keys
    
    Returns:
        List of (category, is_useful, reason) tuples
    """
    try:
        client = get_anthropic_client()
    except ImportError:
        return [("other", False, "anthropic not installed")] * len(emails)
    except Exception as e:
        return [("other", False, f"API error: {e}")] * len(emails)
    
    categories_list = ", ".join(CATEGORIES.keys())
    
    # Build batch prompt
    prompt = f"""Analyze these emails and for each one determine:
1. Category (one of: {categories_list})
2. Whether it's useful to keep (yes/no)
3. Brief reason (10 words max)

Respond in this exact format for each email, one per line:
EMAIL_1: category|yes/no|reason
EMAIL_2: category|yes/no|reason
...

Emails to analyze:
"""
    
    for i, email_data in enumerate(emails, 1):
        prompt += f"\n--- EMAIL_{i} ---\n"
        prompt += f"From: {email_data['sender']}\n"
        prompt += f"Subject: {email_data['subject']}\n"
        prompt += f"Date: {email_data['date']}\n"
        if email_data.get('preview'):
            prompt += f"Preview: {email_data['preview'][:200]}\n"
    
    try:
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=300 * len(emails),
            messages=[{"role": "user", "content": prompt}]
        )
        
        text = response.content[0].text.strip()
        results = []
        
        for line in text.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            
            try:
                # Parse: EMAIL_1: category|yes/no|reason
                parts = line.split(":", 1)[1].strip().split("|")
                if len(parts) >= 3:
                    category = parts[0].strip().lower()
                    is_useful = parts[1].strip().lower() in ("yes", "true", "1")
                    reason = parts[2].strip()
                    
                    # Validate category
                    if category not in CATEGORIES:
                        category = "other"
                    
                    results.append((category, is_useful, reason))
            except:
                results.append(("other", False, "parse error"))
        
        # Pad results if parsing failed for some
        while len(results) < len(emails):
            results.append(("other", False, "analysis failed"))
        
        return results
        
    except Exception as e:
        return [("other", False, f"API error: {e}")] * len(emails)
