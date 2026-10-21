"""
Data persistence for decisions, categories, the sender index and plans.

All writes are atomic (write to a temp file, then rename) — the sender index
is checkpointed in the middle of hour-long scans and must never be left
half-written.
"""

import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from .config import (
    DATA_DIR, DECISIONS_FILE, CATEGORIES_FILE, SENDER_INDEX_FILE,
    PLAN_FILE_PREFIX, EXECUTION_FILE_PREFIX,
)


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


def save_json(filepath: str, data: Any, indent: Optional[int] = 2):
    """Atomically save data to a JSON file."""
    ensure_data_dir()
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, default=str)
    os.replace(tmp, filepath)


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

    filename = os.path.join(DATA_DIR, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

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


# ── Sender index persistence ─────────────────────────────────────────────────

def sender_index_exists() -> bool:
    return os.path.exists(SENDER_INDEX_FILE)


def save_sender_index(index_dict: Dict[str, Any]) -> str:
    """Atomically write the sender index (compact JSON: it holds every UID)."""
    save_json(SENDER_INDEX_FILE, index_dict, indent=None)
    return SENDER_INDEX_FILE


def load_sender_index() -> Optional[Dict[str, Any]]:
    if not os.path.exists(SENDER_INDEX_FILE):
        return None
    with open(SENDER_INDEX_FILE, "r") as f:
        return json.load(f)


# ── Deletion plan persistence ────────────────────────────────────────────────

def save_plan_json(plan: Dict[str, Any], filename: Optional[str] = None) -> str:
    """Save a deletion plan to JSON and return the filename."""
    ensure_data_dir()
    if filename is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(DATA_DIR, f"{PLAN_FILE_PREFIX}{ts}.json")
    save_json(filename, plan)
    return filename


def load_plan_json(filename: str) -> Dict[str, Any]:
    """Load a previously saved deletion plan."""
    if not os.path.isabs(filename) and not os.path.exists(filename):
        candidate = os.path.join(DATA_DIR, filename)
        if os.path.exists(candidate):
            filename = candidate
    with open(filename, "r") as f:
        return json.load(f)


def list_plans() -> List[str]:
    """List all saved plan files, most recent first."""
    ensure_data_dir()
    files = [
        f for f in os.listdir(DATA_DIR)
        if f.startswith(PLAN_FILE_PREFIX) and f.endswith(".json")
    ]
    return sorted(files, reverse=True)


def save_execution_result(result: Dict[str, Any]) -> str:
    """Save the result of executing a plan (what actually got deleted)."""
    ensure_data_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(DATA_DIR, f"{EXECUTION_FILE_PREFIX}{ts}.json")
    save_json(filename, result)
    return filename
