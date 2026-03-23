"""
Data persistence for decisions and categories.
"""

import os
import json
from typing import Dict, Any
from datetime import datetime

from .config import DATA_DIR, DECISIONS_FILE, CATEGORIES_FILE


def ensure_data_dir():
    """Create data directory if it doesn't exist."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def load_json(filepath: str) -> Dict[str, Any]:
    """Load JSON data from file."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return {}


def save_json(filepath: str, data: Dict[str, Any]):
    """Save data to JSON file."""
    ensure_data_dir()
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def load_decisions() -> Dict[str, str]:
    """Load email decisions (useful/not_useful)."""
    return load_json(DECISIONS_FILE)


def save_decisions(decisions: Dict[str, str]):
    """Save email decisions."""
    save_json(DECISIONS_FILE, decisions)


def load_categories() -> Dict[str, str]:
    """Load email categories."""
    return load_json(CATEGORIES_FILE)


def save_categories(categories: Dict[str, str]):
    """Save email categories."""
    save_json(CATEGORIES_FILE, categories)


def export_summary(decisions: Dict[str, str]) -> str:
    """Export a summary report of all decisions."""
    ensure_data_dir()
    
    useful = [k for k, v in decisions.items() if v == "useful"]
    useless = [k for k, v in decisions.items() if v == "not_useful"]
    
    filename = f"data/summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    with open(filename, "w") as f:
        f.write(f"EMAIL REVIEW SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"{'=' * 60}\n\n")
        f.write(f"Total reviewed: {len(decisions)}\n")
        f.write(f"Useful: {len(useful)}\n")
        f.write(f"Not useful: {len(useless)}\n\n")
        
        f.write(f"USEFUL ({len(useful)})\n{'─' * 50}\n")
        for k in useful:
            f.write(f"  {k}\n")
        
        f.write(f"\nNOT USEFUL ({len(useless)})\n{'─' * 50}\n")
        for k in useless:
            f.write(f"  {k}\n")
    
    return filename
