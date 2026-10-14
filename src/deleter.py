"""
Build and execute deletion plans against the IMAP server.

Two modes:
- "trash"  (default, reversible): moves messages to the Trash folder. iCloud
  retains them for ~30 days by default, so recovery is possible.
- "hard"   (permanent): marks \\Deleted and UID-EXPUNGEs. Only allowed for
  folders in HARD_DELETE_ALLOWED_FOLDERS (e.g. Junk, promotions) and only
  when the caller explicitly opts in.

A plan is executed folder by folder: all flagged UIDs in a folder are merged
and sent in a few large sequence sets (contiguous runs collapse to `a:b`),
then the outcome is attributed back to each sender item.
"""

from collections import defaultdict
from typing import Dict, List, Any, Callable, Optional
from datetime import datetime

from .imap_client import IMAPClient
from .config import HARD_DELETE_ALLOWED_FOLDERS
from .sender_groups import SenderGroup


def build_plan(flagged_senders: List[SenderGroup], mode: str = "trash") -> Dict[str, Any]:
    """
    Create a deletion plan from a list of flagged sender groups.

    mode: "trash" (safe, reversible) or "hard" (permanent expunge).
    The plan is a self-contained artifact that can be saved, inspected,
    and executed later.
    """
    items: List[Dict[str, Any]] = []
    total_count = 0
    folders_touched = set()

    for sg in flagged_senders:
        for folder, uids in sg.uids_by_folder.items():
            uid_list = [int(u) for u in uids]
            items.append({
                "sender_email": sg.email,
                "sender_name": sg.name,
                "domain": sg.domain,
                "folder": folder,
                "uids": uid_list,
                "count": len(uid_list),
                "category": sg.category,
                "ai_useful": sg.ai_useful,
                "ai_reason": sg.ai_reason,
                "bulk_ratio": round(sg.bulk_ratio, 3),
                "sample_subjects": sg.sample_subjects[-3:],
                "first_date": sg.first_date.isoformat() if sg.first_date else None,
                "last_date": sg.last_date.isoformat() if sg.last_date else None,
            })
            total_count += len(uid_list)
            folders_touched.add(folder)

    return {
        "created_at": datetime.now().isoformat(),
        "mode": mode,
        "total_emails": total_count,
        "total_senders": len(flagged_senders),
        "folders": sorted(folders_touched),
        "items": items,
    }


def hard_delete_eligible(folder: str) -> bool:
    """True if hard-delete is allowed for this folder."""
    return folder in HARD_DELETE_ALLOWED_FOLDERS


def execute_plan(
    client: IMAPClient,
    plan: Dict[str, Any],
    hard_delete: bool = False,
    trash_folder: str = "Deleted Messages",
    dry_run: bool = False,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    on_item_done: Optional[Callable[[Dict[str, Any], List[int]], None]] = None,
    hard_all_folders: bool = False,
) -> Dict[str, Any]:
    """
    Execute a deletion plan.

    - dry_run: report what would happen without touching the server.
    - hard_delete: permanently expunge. Ignored for folders not in
      HARD_DELETE_ALLOWED_FOLDERS, which always fall back to move-to-trash.
      Also ignored unless the plan itself was built with mode="hard".
    - hard_all_folders: lift the folder restriction, so hard_delete applies
      to every folder in the plan (INBOX included). There is no undo; the
      caller must have obtained an explicit confirmation for this.
    - on_item_done(item, done_uids): called per plan item with the UIDs that
      were actually moved/deleted, so the caller can update its index.

    If the connection is lost and cannot be recovered, the remaining folders
    are reported as "skipped" and execution stops; re-running the same plan
    later is safe (UIDs already gone are simply not found).
    """
    plan_allows_hard = plan.get("mode") == "hard"
    items = plan.get("items", [])

    # folder -> uid -> item index, so results can be attributed per sender
    by_folder: Dict[str, Dict[int, int]] = defaultdict(dict)
    for idx, item in enumerate(items):
        for u in item["uids"]:
            by_folder[item["folder"]][int(u)] = idx

    entries: List[Dict[str, Any]] = [{
        "sender": item["sender_email"],
        "folder": item["folder"],
        "count": item["count"],
        "action": None,
        "done": 0,
        "status": "pending",
    } for item in items]

    trashed_total = deleted_total = failed_total = 0
    folder_errors: Dict[str, List[str]] = {}
    connection_lost = False

    for folder in sorted(by_folder):
        uid_map = by_folder[folder]
        uids = sorted(uid_map)
        will_hard = hard_delete and plan_allows_hard and hard_delete_eligible(folder)
        action = "hard_delete" if will_hard else "move_to_trash"
        item_indices = set(uid_map.values())
        for idx in item_indices:
            entries[idx]["action"] = action

        if dry_run:
            for idx in item_indices:
                entries[idx]["status"] = "dry_run"
            if progress_callback:
                progress_callback(f"{folder}: {len(uids):,} emails ({action})", len(uids), len(uids))
            continue

        if connection_lost:
            for idx in item_indices:
                entries[idx]["status"] = "skipped"
                entries[idx]["error"] = "connection lost earlier in this run"
            failed_total += len(uids)
            continue

        if getattr(client, "throttled", False):
            for idx in item_indices:
                entries[idx]["status"] = "skipped"
                entries[idx]["error"] = "server refused repeated batches earlier in this run (throttling); re-run the plan later"
            failed_total += len(uids)
            continue

        ok, _ = client.select_folder_rw(folder)
        if not ok:
            for idx in item_indices:
                entries[idx]["status"] = "failed"
                entries[idx]["error"] = "could not open folder in read-write mode"
            failed_total += len(uids)
            folder_errors[folder] = ["could not open folder in read-write mode"]
            continue

        def on_chunk(done, total, _folder=folder, _action=action):
            if progress_callback:
                progress_callback(f"{_folder}: {_action}", done, total)

        if will_hard:
            done_uids, errors = client.delete_permanently(uids, on_chunk=on_chunk)
            deleted_total += len(done_uids)
        else:
            done_uids, errors = client.move_to_folder(uids, trash_folder, on_chunk=on_chunk)
            trashed_total += len(done_uids)
        if errors:
            folder_errors[folder] = errors
        if not client.connected:
            connection_lost = True

        # attribute per item
        done_by_item: Dict[int, List[int]] = defaultdict(list)
        for u in done_uids:
            done_by_item[uid_map[u]].append(u)
        for idx in item_indices:
            got = done_by_item.get(idx, [])
            e = entries[idx]
            e["done"] = len(got)
            if len(got) == e["count"]:
                e["status"] = "success"
            elif got:
                e["status"] = "partial"
                e["error"] = "; ".join(errors)[:300]
            else:
                e["status"] = "failed"
                e["error"] = "; ".join(errors)[:300] or "IMAP command rejected"
            failed_total += e["count"] - len(got)
            if on_item_done and got:
                on_item_done(items[idx], got)

    return {
        "executed_at": datetime.now().isoformat(),
        "plan_mode": plan.get("mode"),
        "hard_delete_requested": hard_delete,
        "dry_run": dry_run,
        "trash_folder": trash_folder,
        "trashed_total": trashed_total,
        "hard_deleted_total": deleted_total,
        "failed_total": failed_total,
        "connection_lost": connection_lost,
        "throttled": bool(getattr(client, "throttled", False)),
        "folder_errors": folder_errors,
        "items": entries,
    }
