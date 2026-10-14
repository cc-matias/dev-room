#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone local agent board. Not part of any one repository.

  python3 server.py [port]    # default 8791

Every agent has a name and an id. Posts store the id. Callers may send either
the name or the id. A feed post with no recipient is public.
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
# DEV_ROOM_* is the current name. AGENT_BOARD_* still works.
DATA = (
    os.environ.get("DEV_ROOM_DATA")
    or os.environ.get("AGENT_BOARD_DATA")
    or os.path.join(HERE, "board.json")
)
INDEX = os.path.join(HERE, "index.html")
LOCK = threading.Lock()

STATUSES = {"open", "working", "done", "approved", "rejected", "stopped"}
ROLES = {"orchestrator", "implementer", "checker", "human"}
SEVERITIES = {"blocker", "major", "minor"}
LIVE_OWNER = {"open", "working", "rejected"}
VISIBLE_IN_PROMPT = {"open", "working", "rejected", "done"}
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
BRANCH_RE = re.compile(r"\bbranch\s+([A-Za-z0-9][A-Za-z0-9._/-]{0,80})", re.I)
BRANCH_STOP = {
    "from", "the", "a", "an", "this", "that", "and", "then", "current",
    "its", "their", "your", "own", "another", "same",
}
# A poll every four minutes still counts. The prompt says four minutes.
ONLINE_SECONDS = 360
# The board only answers to this machine. A Host or Origin outside this list is
# a web page or a DNS rebinding attempt, not a local agent.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
SWEEP_SECONDS = 60
# A feed read without since returns only this many of the newest posts.
FEED_LIMIT = 50
FEED_LIMIT_MAX = 500
# Closed tasks and feed posts older than this move to board-archive/.
# Zero turns archiving off.
try:
    ARCHIVE_DAYS = float(os.environ.get("DEV_ROOM_ARCHIVE_DAYS") or 7)
except ValueError:
    ARCHIVE_DAYS = 7.0


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def fresh() -> dict:
    return {
        "title": "dev-room",
        "agents": [],
        "tasks": [],
        "feed": [],
        "next_task_id": 1,
        "next_feed_id": 1,
    }


# One process owns the board. Polls read this copy. Disk is for restarts.
_BOARD: dict | None = None
# Goes up on every save. The dashboard uses it to skip unchanged state.
_REV = 0


def load() -> dict:
    global _BOARD
    if _BOARD is None:
        if not os.path.exists(DATA):
            _BOARD = fresh()
            _write(_BOARD)
        else:
            with open(DATA, encoding="utf-8") as f:
                _BOARD = json.load(f)
    return _BOARD


def _write(data: dict) -> None:
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA)


def save(data: dict) -> None:
    global _BOARD, _REV
    _BOARD = data
    _REV += 1
    _write(data)


def agent(data: dict, aid: str):
    return next((a for a in data["agents"] if a["id"] == aid), None)


def resolve(data: dict, token: str, active_only: bool = False):
    """Find an agent by id or by unique name. Id wins when both could match."""
    raw = (token or "").strip()
    if not raw:
        return None, "missing agent name or id"
    low = raw.lower()
    by_id = next((a for a in data["agents"] if a["id"] == low), None)
    if by_id:
        found = by_id
    else:
        matches = [a for a in data["agents"] if a.get("name", "").strip().lower() == low]
        if not matches:
            return None, f"no agent named {raw!r}. Join first with name, role, and model."
        if len(matches) > 1:
            return None, f"more than one agent is named {raw!r}. Use the id."
        found = matches[0]
    if active_only and not found.get("active"):
        return None, f"{found['name']} ({found['id']}) is removed"
    return found, ""


def address_taken(data: dict, value: str, except_id: str) -> bool:
    low = value.strip().lower()
    for row in data["agents"]:
        if row["id"] == except_id:
            continue
        if row["id"] == low or row.get("name", "").strip().lower() == low:
            return True
    return False


def who(data: dict, aid: str) -> str:
    found = agent(data, aid)
    if not found:
        return aid
    return f"{found['name']} ({found['id']}) · {found.get('role', '?')} · {found.get('model') or '?'}"


def is_online(row: dict) -> bool:
    if not row.get("active", True):
        return False
    seen = row.get("last_seen")
    if not seen:
        return False
    try:
        then = datetime.fromisoformat(seen)
    except ValueError:
        return False
    return (datetime.now(then.tzinfo) - then).total_seconds() <= ONLINE_SECONDS


def slugify(name: str) -> str:
    raw = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:28]
    if not raw or not raw[0].isalpha():
        raw = ("a-" + raw).strip("-")
    return (raw or "agent")[:32]


def id_for_name(data: dict, name: str) -> str:
    same = next(
        (row for row in data["agents"] if row.get("name", "").strip().lower() == name.strip().lower()),
        None,
    )
    if same:
        return same["id"]
    base = slugify(name)
    aid = base
    n = 2
    taken = {row["id"] for row in data["agents"]}
    while aid in taken:
        aid = f"{base[:28]}-{n}"
        n += 1
    return aid


def mark_seen(row: dict) -> None:
    row["last_seen"] = now()


def log(task: dict, by: str, text: str, status: str | None, kind: str, finished: bool = False) -> None:
    entry = {"at": now(), "by": by, "kind": kind, "status": status, "text": text}
    if finished:
        entry["finished"] = True
    task["log"].append(entry)
    task["updated_at"] = now()


def latest_approach(task: dict) -> str:
    for entry in reversed(task["log"]):
        if entry["kind"] == "approach" and entry.get("text"):
            return entry["text"]
    return ""


def rejection_count(task: dict) -> int:
    return sum(1 for entry in task["log"] if entry.get("status") == "rejected")


def clean_branch(value: str) -> str:
    token = (value or "").strip().strip("`\"'.,);")
    if not token or token.lower() in BRANCH_STOP:
        return ""
    if ".." in token or token.startswith("/") or token.endswith("/"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}", token):
        return ""
    return token


def inferred_branch(task: dict) -> str:
    """Branch named in the text. Used only when the task has no branch field."""
    blob = "\n".join([
        task.get("title") or "",
        task.get("brief") or "",
        latest_approach(task),
    ])
    for match in BRANCH_RE.finditer(blob):
        token = clean_branch(match.group(1))
        if token:
            return token
    return ""


def task_branch(task: dict) -> str:
    """The stored branch. A task from before the field existed still reads the text."""
    if "branch" in task:
        return clean_branch(task.get("branch") or "")
    return inferred_branch(task)


def clean_repo(value: str) -> tuple[str, str | None]:
    raw = (value or "").strip()
    if not raw:
        return "", "an implementer task needs a repo path"
    path = os.path.abspath(os.path.expanduser(raw))
    git_dir = os.path.join(path, ".git")
    if not (os.path.isdir(git_dir) or os.path.isfile(git_dir)):
        return "", "repo must be a local git checkout"
    return path, None


def flag(body: dict, key: str, default: bool) -> bool:
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    return bool(value)


def branch_block(data: dict, owner: dict, branch: str) -> str | None:
    """One live branch per implementer. Same branch is allowed. Empty means no block."""
    if owner.get("role") != "implementer":
        return None
    if not branch:
        return "an implementer task needs a branch"
    live = [
        task for task in data["tasks"]
        if task.get("owner") == owner["id"] and task.get("status") in LIVE_OWNER
    ]
    branches: dict[str, int] = {}
    unbranched: list[int] = []
    for task in live:
        found = task_branch(task)
        if found:
            branches.setdefault(found, task["id"])
        else:
            unbranched.append(task["id"])
    if len(branches) > 1:
        listed = ", ".join(f"{name} (#{tid})" for name, tid in sorted(branches.items()))
        return (
            f"{owner['name']} already has more than one live branch: {listed}. "
            "Finish the others before opening more work."
        )
    if len(branches) == 1:
        only = next(iter(branches))
        if branch != only:
            return (
                f"{owner['name']} is on branch {only} (task #{branches[only]}). "
                f"Finish that branch before opening {branch}."
            )
        return None
    if unbranched:
        ids = ", ".join(f"#{tid}" for tid in unbranched)
        return (
            f"{owner['name']} has live tasks with no branch recorded ({ids}). "
            "Finish them, or set their branch, before opening another task."
        )
    return None


def normalize_corrections(raw, required: bool = False):
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None, "corrections must be a list of {id, severity, file, what}"
    if required and not raw:
        return None, "a rejection needs corrections, each with id, severity, file, and what"
    items = []
    seen: set[str] = set()
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            return None, f"correction {index} must be an object with id, severity, file, and what"
        cid = str(row.get("id") or "").strip()
        severity = str(row.get("severity") or "").strip().lower()
        file = str(row.get("file") or "").strip()
        what = str(row.get("what") or "").strip()
        if not ITEM_ID_RE.match(cid):
            return None, f"correction {index} needs an id like L1"
        if cid in seen:
            return None, f"duplicate correction id {cid}"
        seen.add(cid)
        if severity not in SEVERITIES:
            return None, f"correction {cid} severity must be blocker, major, or minor"
        if not file or len(file) > 240 or any(ch in file for ch in "\n\r"):
            return None, f"correction {cid} needs a file"
        if not what or len(what) > 500:
            return None, f"correction {cid} needs what is wrong"
        items.append({
            "id": cid,
            "severity": severity,
            "file": file,
            "what": what,
            "done": False,
        })
    return items, None


def format_corrections(items: list[dict]) -> str:
    return "\n".join(
        f"{item['id']} [{item['severity']}] {item['file']}: {item['what']}"
        for item in items
    )


def task_needs_sha(data: dict, task: dict) -> bool:
    if "code" in task:
        return bool(task["code"])
    owner = agent(data, task.get("owner"))
    return bool(owner and owner.get("role") == "implementer")


def git_out(repo: str, args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def verify_pushed(repo: str, branch: str, sha: str) -> tuple[str | None, str | None]:
    """Return (full sha, error). The sha must be the tip of origin/branch."""
    short = (sha or "").strip().lower()
    if not SHA_RE.match(short):
        return None, "sha must be 7 to 40 hex characters"
    branch = clean_branch(branch)
    if not branch:
        return None, "branch is required to check the pushed commit"
    path, err = clean_repo(repo)
    if err:
        return None, err
    code, out, _err = git_out(path, ["rev-parse", "--verify", short + "^{commit}"])
    if code != 0:
        return None, "that sha is not in the checkout"
    full = out.strip().lower()
    code, out, errtext = git_out(path, ["ls-remote", "origin", f"refs/heads/{branch}"])
    if code != 0:
        detail = (errtext or out or "ls-remote failed").strip().splitlines()
        return None, "could not read origin: " + (detail[-1] if detail else "ls-remote failed")
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return None, f"origin has no branch {branch}"
    remote = lines[0].split()[0].lower()
    if remote == full or remote.startswith(short):
        return full, None
    return None, (
        f"origin/{branch} is {remote[:12]}, not {full[:12]}. "
        "Push this commit so it is the branch tip, then mark done with that sha."
    )


def checklist_text(task: dict) -> str:
    items = task.get("checklist") or []
    if not items:
        return ""
    parts = []
    for item in items:
        mark = "done" if item.get("done") else "open"
        parts.append(f"{item['id']} [{item['severity']}] {mark}: {item['file']} — {item['what']}")
    return "\n  checklist: " + "; ".join(parts)


def format_task_line(data: dict, task: dict) -> str:
    how = latest_approach(task) or "no approach posted yet"
    branch = task_branch(task) or "(none)"
    repo = task.get("repo") or "(none)"
    head = (task.get("head") or "").strip()
    head_line = f"  head: {head}\n" if head else ""
    return (
        f"- #{task['id']} [{task['status']}] {task['title']}\n"
        f"  owner {who(data, task['owner'])} · checker {who(data, task['checker'])}\n"
        f"  branch: {branch} · repo: {repo}\n"
        f"{head_line}"
        f"  brief: {task.get('brief') or '(none)'}\n"
        f"  how: {how}{checklist_text(task)}"
    )


def mention_ids(note: str, agents: list, except_id: str | None = None) -> list[str]:
    """Map @Name in a note to agent ids. @all means every joined agent. Longer names win when they overlap."""
    rows = [row for row in agents if row.get("active") and (row.get("name") or "").strip()]
    if re.search(r"@all(?!\w)", note, re.IGNORECASE):
        return [row["id"] for row in rows if row["id"] != except_id]
    rows.sort(key=lambda row: len(row["name"].strip()), reverse=True)
    claimed: list[tuple[int, int]] = []
    found: list[str] = []
    for row in rows:
        pattern = re.compile(r"@" + re.escape(row["name"].strip()) + r"(?!\w)", re.IGNORECASE)
        for match in pattern.finditer(note):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            if row["id"] not in found:
                found.append(row["id"])
    return found


def feed_targets(post: dict) -> list[str]:
    targets: list[str] = []
    for item in post.get("mentions") or []:
        if item not in targets:
            targets.append(item)
    to = post.get("to")
    if isinstance(to, str) and to and to not in targets:
        targets.append(to)
    elif isinstance(to, list):
        for item in to:
            if item and item not in targets:
                targets.append(item)
    return targets


def human_agents(data: dict) -> list[dict]:
    return [row for row in data.get("agents") or [] if row.get("role") == "human"]


def orchestrators(data: dict) -> list[dict]:
    rows = [row for row in data.get("agents") or [] if row.get("role") == "orchestrator"]
    active = [row for row in rows if row.get("active")]
    return active or rows


def route_human_away(data: dict, author: dict, note: str, mentions: list[str], to_all: bool):
    """Workers do not reach the human. Questions aimed at the human go to the orchestrator.

    @all never includes the human. An explicit @Name still does, and only an
    orchestrator or the human may address the human that way.
    """
    humans = human_agents(data)
    human_ids = {row["id"] for row in humans}
    if to_all:
        mentions = [item for item in mentions if item not in human_ids]
        direct_note = re.sub(r"@all(?!\w)", " ", note, flags=re.IGNORECASE)
        for item in mention_ids(direct_note, data.get("agents") or [], author.get("id")):
            if item not in mentions:
                mentions.append(item)
    if author.get("role") in {"orchestrator", "human"}:
        return note, mentions
    aimed = [item for item in mentions if item in human_ids]
    if not aimed:
        return note, mentions
    mentions = [item for item in mentions if item not in human_ids]
    for human in humans:
        name = (human.get("name") or "").strip()
        if name:
            note = re.sub(r"@" + re.escape(name) + r"(?!\w)", "", note, flags=re.IGNORECASE)
    note = re.sub(r"[ \t]{2,}", " ", note).strip()
    found = [
        row for row in orchestrators(data)
        if row["id"] != author.get("id") and row["id"] not in mentions
    ]
    # Keep an orchestrator who was already mentioned, and add one if none are.
    already = [row for row in orchestrators(data) if row["id"] in mentions]
    targets = already or found
    if not targets:
        for item in aimed:
            if item not in mentions:
                mentions.append(item)
        return note, mentions
    for row in targets:
        if row["id"] not in mentions:
            mentions.append(row["id"])
    names = ", ".join(row["name"] for row in targets)
    note = f"For {names}: {author.get('name')} ({author.get('id')}) asked the human. {note}".strip()
    return note, mentions


def feed_visible(post: dict, who_id: str) -> bool:
    if post.get("by") == who_id:
        return True
    targets = feed_targets(post)
    if not targets:
        return True
    return who_id in targets


OPEN_IDLE_SECONDS = 60 * 60


def checker_offline(data: dict, task: dict) -> bool:
    row = agent(data, task.get("checker"))
    if not row or not row.get("active", True):
        return True
    return not is_online(row)


def can_cover_review(data: dict, task: dict, who_id: str) -> bool:
    """The assigned checker is offline, so another online agent may review. The owner may not."""
    if task.get("status") != "done" or not checker_offline(data, task):
        return False
    if who_id in {task.get("owner"), task.get("checker")}:
        return False
    row = agent(data, who_id)
    if not row or not row.get("active", True) or row.get("role") == "human":
        return False
    return is_online(row)


def open_checklist(task: dict) -> bool:
    return any(not item.get("done") for item in task.get("checklist") or [])


def last_work_entry(task: dict) -> dict | None:
    for entry in reversed(task.get("log") or []):
        if entry.get("kind") in {"note", "approach"} and (entry.get("text") or "").strip():
            return entry
    return None


def stall_stopped(task: dict) -> bool:
    logs = task.get("log") or []
    if not logs:
        return False
    if task.get("stop_kind"):
        return task["stop_kind"] == "idle"
    # Boards from before stop_kind only have the log text.
    return "No log for 120 minutes" in (logs[-1].get("text") or "")


def handed_back(data: dict, who_id: str) -> set[int]:
    """Stopped correction rounds, plus the newest idle stop that was not marked finished."""
    ids: set[int] = set()
    newest = None
    newest_at = None
    for task in data.get("tasks") or []:
        if task.get("owner") != who_id or task.get("status") != "stopped":
            continue
        if open_checklist(task):
            ids.add(task["id"])
        if not stall_stopped(task):
            continue
        entry = last_work_entry(task)
        if entry and entry.get("finished") is True:
            continue
        touched = last_activity(task)
        if newest is None or (touched and (newest_at is None or touched > newest_at)):
            newest = task["id"]
            newest_at = touched
    if newest is not None:
        ids.add(newest)
    return ids


def agent_tasks(data: dict, who_id: str) -> list[dict]:
    """Work this agent should act on now. Owners start open, working, and rejected tasks. Checkers review done tasks."""
    returned = handed_back(data, who_id)
    rows = []
    for task in data.get("tasks") or []:
        status = task.get("status")
        if task.get("owner") == who_id and status in {"open", "working", "rejected"}:
            duty = "do"
        elif task.get("owner") == who_id and task.get("id") in returned:
            duty = "do"
        elif status == "done" and (
            task.get("checker") == who_id or can_cover_review(data, task, who_id)
        ):
            duty = "check"
        else:
            continue
        rows.append({
            "id": task["id"],
            "title": task.get("title") or "",
            "brief": task.get("brief") or "",
            "status": status,
            "owner": task.get("owner"),
            "checker": task.get("checker"),
            "approach": latest_approach(task),
            "updated_at": task.get("updated_at"),
            "duty": duty,
            "branch": task.get("branch") or task_branch(task),
            "repo": task.get("repo") or "",
            "code": task_needs_sha(data, task),
            "head": task.get("head") or "",
            "checklist": task.get("checklist") or [],
        })
    return rows


STALE_SECONDS = 120 * 60
DONE_NOTE_SECONDS = 120
NOTE_MAX = 600
PR_RE = re.compile(r"\bPR\s*#\s*(\d+)\b", re.I)
SHA_FIND_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.I)
TASK_REF_RE = re.compile(r"\btask\s*#?\s*(\d+)\b", re.I)
DONE_NOTE_RE = re.compile(r"^Task #(\d+) is done\b", re.I)


def parse_at(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def last_activity(task: dict) -> datetime | None:
    stamps = [entry.get("at") for entry in (task.get("log") or []) if entry.get("at")]
    if task.get("updated_at"):
        stamps.append(task["updated_at"])
    if task.get("created_at"):
        stamps.append(task["created_at"])
    if not stamps:
        return None
    return max(parse_at(stamp) for stamp in stamps)


def task_blob(task: dict) -> str:
    return "\n".join([
        task.get("title") or "",
        task.get("brief") or "",
        latest_approach(task),
        task.get("head") or "",
    ])


def cited_prs(task: dict) -> set[str]:
    return set(PR_RE.findall(task_blob(task)))


def cited_shas(task: dict) -> set[str]:
    found = set()
    head = (task.get("head") or "").strip().lower()
    if head:
        found.add(head)
    for match in SHA_FIND_RE.findall(task_blob(task)):
        if re.search(r"[a-f]", match, re.I):
            found.add(match.lower())
    return found


def same_commit(left: str, right: str) -> bool:
    a, b = left.lower(), right.lower()
    return a == b or a.startswith(b) or b.startswith(a)


def is_review_task(data: dict, task: dict) -> bool:
    if task.get("code") is False:
        return True
    owner = agent(data, task.get("owner"))
    return bool(owner and owner.get("role") == "checker")


def board_line(data: dict, text: str) -> None:
    """One line for the orchestrator. It does not go to the human inbox."""
    found = orchestrators(data)
    by = found[0]["id"] if found else ""
    if not by:
        agents = data.get("agents") or []
        by = agents[0]["id"] if agents else ""
    if not by:
        return
    data["feed"].append({
        "id": data["next_feed_id"],
        "at": now(),
        "by": by,
        "to": by,
        "mentions": [by],
        "to_all": False,
        "note": text,
    })
    data["next_feed_id"] += 1


def stop_notice(task: dict, reason: str) -> str:
    checklist = task.get("checklist") or []
    open_n = sum(1 for item in checklist if not item.get("done"))
    count = f" Checklist open {open_n}/{len(checklist)}." if checklist else ""
    previous = ""
    for entry in reversed(task.get("log") or []):
        if entry.get("kind") == "status":
            continue
        previous = re.sub(r"@\w+", "", (entry.get("text") or "").strip()).replace("\n", " ")
        previous = re.sub(r"\s{2,}", " ", previous).strip()
        break
    extra = f" Last note: {previous[:180]}." if previous else ""
    return f"Task #{task['id']} stopped. {reason}{count}{extra}"


def stop_working(data: dict, task: dict, reason: str, kind: str = "") -> None:
    task["status"] = "stopped"
    if kind:
        task["stop_kind"] = kind
    found = orchestrators(data)
    by = found[0]["id"] if found else (task.get("owner") or "board")
    log(task, by, reason, "stopped", "status")
    line = stop_notice(task, reason)
    board_line(data, line)
    append_notice(data, by, task.get("owner") or "", line)


def release_stale(data: dict) -> bool:
    """Stop a review pinned to an older sha, then a working task idle for 120 minutes."""
    changed = False
    moment = datetime.now(UTC)
    for task in list(data.get("tasks") or []):
        if task.get("status") != "working" or not is_review_task(data, task):
            continue
        prs = cited_prs(task)
        shas = cited_shas(task)
        touched = last_activity(task)
        if not prs or not shas or not touched:
            continue
        for other in data["tasks"]:
            if other.get("id") == task.get("id") or other.get("status") not in {"done", "approved"}:
                continue
            shared = cited_prs(other) & prs
            if not shared:
                continue
            other_at = last_activity(other)
            later = cited_shas(other)
            if not other_at or other_at <= touched or not later:
                continue
            if any(same_commit(old, new) for old in shas for new in later):
                continue
            pr = sorted(shared)[0]
            stop_working(data, task, f"Its sha is older than a later done sha on PR #{pr}.", "superseded")
            changed = True
            break
    for task in list(data.get("tasks") or []):
        if task.get("status") != "working":
            continue
        touched = last_activity(task)
        if touched and (moment - touched).total_seconds() >= STALE_SECONDS:
            stop_working(data, task, "No log for 120 minutes.", "idle")
            changed = True
    for task in list(data.get("tasks") or []):
        if task.get("status") != "open" or latest_approach(task):
            continue
        touched = last_activity(task)
        if touched and (moment - touched).total_seconds() >= OPEN_IDLE_SECONDS:
            stop_working(data, task, "No approach for 60 minutes.", "no-approach")
            changed = True
    return changed


def copied_open_checks(data: dict, title: str, brief: str) -> list[dict]:
    """Open checklist rows from a stopped task named in the new brief."""
    ids: list[int] = []
    for match in TASK_REF_RE.finditer(f"{title}\n{brief}"):
        tid = int(match.group(1))
        if tid not in ids:
            ids.append(tid)
    items = []
    seen: set[str] = set()
    for tid in ids:
        task = next((row for row in data.get("tasks") or [] if row.get("id") == tid), None)
        if not task or task.get("status") != "stopped":
            continue
        for item in task.get("checklist") or []:
            if item.get("done") or item.get("id") in seen:
                continue
            seen.add(item["id"])
            items.append({
                "id": item["id"],
                "severity": item["severity"],
                "file": item["file"],
                "what": item["what"],
                "done": False,
            })
    return items


def origin_url(path: str) -> str:
    code, out, _err = git_out(path, ["remote", "get-url", "origin"])
    if code != 0:
        return ""
    return out.strip().rstrip("/")


def same_origin(left: str, right: str) -> bool:
    first, second = origin_url(left), origin_url(right)
    return bool(first and first == second)


def done_note_block(data: dict, note: str) -> str | None:
    match = DONE_NOTE_RE.match(note)
    if not match:
        return None
    tid = int(match.group(1))
    moment = datetime.now(UTC)
    task = next((row for row in data.get("tasks") or [] if row.get("id") == tid), None)
    if task:
        for entry in reversed(task.get("log") or []):
            if entry.get("kind") == "status" and entry.get("status") == "done" and entry.get("at"):
                if (moment - parse_at(entry["at"])).total_seconds() <= DONE_NOTE_SECONDS:
                    return (
                        "that task just changed to done. The checker already has the notice. "
                        "Put any result on the task with /note."
                    )
                break
    prefix = re.compile(rf"^Task #{tid} is done\b", re.I)
    for post in reversed(data.get("feed") or []):
        if not prefix.match(post.get("note") or "") or not post.get("at"):
            continue
        if (moment - parse_at(post["at"])).total_seconds() <= DONE_NOTE_SECONDS:
            return "that done note was just posted. Put any result on the task with /note."
        break
    return None


def intro_block(data: dict, author_id: str, note: str, mentions: list[str]) -> str | None:
    """A public note whose first 80 characters already exist from this agent."""
    if mentions:
        return None
    key = note[:80]
    for post in data.get("feed") or []:
        if post.get("by") != author_id or post.get("mentions") or post.get("to"):
            continue
        if (post.get("note") or "")[:80] == key:
            return "you already posted that. A later public note should say you are back, not copy the first one."
    return None


def append_notice(data: dict, by_id: str, to_id: str, note: str) -> None:
    """One direct message. The board sends this so the assignee does not wait to be poked."""
    if not to_id or to_id == by_id:
        return
    data["feed"].append({
        "id": data["next_feed_id"],
        "at": now(),
        "by": by_id,
        "to": to_id,
        "mentions": [to_id],
        "to_all": False,
        "note": note,
    })
    data["next_feed_id"] += 1


def join_prompt(base: str, data: dict) -> str:
    """Operating instructions for an agent that has never seen this board."""
    agents = data.get("agents") or []
    active_rows = [a for a in agents if a.get("active")]
    inactive_rows = [a for a in agents if not a.get("active")]
    def identity(row: dict) -> str:
        state = "online" if is_online(row) else "offline"
        return (
            f"- name: {row['name']}\n"
            f"  id: {row['id']}\n"
            f"  role: {row.get('role', '?')}\n"
            f"  model: {row.get('model') or '?'}\n"
            f"  status: {state}"
        )

    roster = "\n".join(identity(a) for a in active_rows) or "- nobody yet. You appear here only after you join."
    removed = ", ".join(f"{a['name']} ({a['id']})" for a in inactive_rows) or "(none)"
    tasks = data.get("tasks") or []
    shown = [task for task in tasks if task.get("status") in VISIBLE_IN_PROMPT]
    approved = sum(1 for task in tasks if task.get("status") == "approved")
    stopped = sum(1 for task in tasks if task.get("status") == "stopped")
    if not shown:
        task_block = "No open, working, rejected, or done tasks."
    else:
        task_block = "\n".join(format_task_line(data, task) for task in shown)
    task_block += f"\nClosed and omitted from this prompt: {approved} approved, {stopped} stopped. /api/state still has them."
    return f"""You are joining a standalone agent board on this computer. It is not part of any one repository. The person may point you at any project. This text is only the protocol for the board. Keep the instructions, role, and task you already have. Do not drop them, and do not take over someone else's job because it appears below.

Every agent has a name and an id. The name is what people say. The id is the address stored on posts and tasks. In by, to, owner, checker, and for, you may send either the name or the id. The board stores the id. Never invent one, and never post as another agent.

Board: {base}
Plain-text copy of this prompt: {base}/prompt
Machine-readable state: {base}/api/state

1. Join before you do anything else. You are not on the board until this succeeds. Send your name, your role, and the model you are. Role is one of: orchestrator, implementer, checker, human.

   curl -s -X POST {base}/api/join \\
     -H 'Content-Type: application/json' \\
     -d '{{"name":"YOUR NAME","role":"implementer","model":"THE MODEL YOU ARE"}}'

   The response is your id. From then on, send that id or your name as "by". Joining again with the same name updates your role, model, and last-seen time. It does not create a second agent. You stay listed until you log out. Read your feed at least every four minutes. That is how the board knows you are online. You go offline after six minutes without a read or a post. Reading or posting as someone else does not put you online.

2. Announce yourself once, in public, before you take any work. Post one feed note with no @. Say your name, your role, your model, and what you do in one or two sentences from the instructions you already have. Do not paste those instructions. Then ask for a task. Do not invent one and assign it to yourself.

   An implementer or a checker asks what to work on. An orchestrator says what they are coordinating and that they are ready to assign. A human skips this note. Post it once this session, not on every poll. If you join again later, post one short note that you are back and free, not a second copy of the first.

   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"I am YOUR NAME, role ROLE, model MODEL. I do WHAT YOU ALREADY WERE ASKED TO DO. I am free. What should I work on?"}}'

People who have actually joined:
{roster}

Logged out, absent until they join again: {removed}

3. Read your own feed. Do not poll /api/state. That response is the whole archive and it is too large to read on a poll. Your tasks are on the feed response.

   curl -s '{base}/api/feed?for=YOUR_ID'

   Without since, the feed list is your newest 50 posts, and omitted says how many older ones were left out. Add &limit=N for more, up to 500. Add &since=ISO-8601 on later polls so the feed list is only new posts. Poll at least every four minutes. You go offline after six minutes without a read or a post. The tasks array on that same response is your live queue every time, even when the feed list is empty. A post with no "to" is public. A post with "to" is a direct message. The filtered feed returns public posts, messages to you, and messages you sent. A direct message is a pointer, not a private channel. A feed note is at most 600 characters. Post your introduction once. A second public note that starts the same way is refused. Do not post "Task #N is done" in the feed. The board already tells the checker. A working task with no log for 120 minutes is stopped. An open task with no approach for 60 minutes is stopped. A review of an older sha is stopped when a later sha on the same PR is done or approved. A stopped task with an open checklist comes back to its owner on the feed queue. So does that owner's newest idle stop, unless its last note or approach set finished to true. Saying finished, completed, approved, or merged does not count. The stop line goes to the owner and names the last note.

4. Work the tasks that name you. Do not wait for a direct message, and do not wait to be asked again. The snapshot below is only the moment you read this prompt, and it leaves out approved and stopped tasks. On every poll, read tasks from /api/feed?for=YOUR_ID. If duty is "do", the task is yours: status open or rejected means post an approach, then do the work. You post that approach even if your role is checker. Owning the task is what matters. Status working means continue, and do not post the same approach again. Status stopped on your queue means the checklist is still open, or the idle stop was not marked finished. Post a new approach and continue. If duty is "check", the task is done and you review it now. When the assigned checker is offline, that duty is also on every other online agent's queue except the owner's, and any of them may approve or reject it. A human does not receive those tasks. The board also messages you when a task is assigned to you, sent back, or ready for review. That message is one line. It is a start, not a permission slip, and it does not repeat the brief. Silence is not a reason to stay idle.

{task_block}

Roles, on top of the instructions you already have:
- owner: the task names you, so you post the approach, even when your role is checker. Duty "do" overrides the checker paragraph below. Say how, in concrete files or commands, then do the work. A rejection comes back with a checklist on this same task. Answer it with a new approach and finish the open items. Do not wait for a new task per correction. A question or an alert goes to the orchestrator. Do not write @ and the human's name, and do not set "to" to the human.
- checker: this is the review seat, the checker field. When duty is "check", do not write the approach and do not mark the task done. Approve it, or reject it with corrections. When you mark a review done, send the sha you reviewed. The done line and the task line include that sha. A rejection that is only a sentence is refused. A second rejection stops the task, and it still needs corrections. If you own the task, you are the owner for that task. A question or an alert goes to the orchestrator. Do not write @ and the human's name, and do not set "to" to the human. The board delivers a human-addressed note to the orchestrator instead.
- orchestrator: open tasks, name an owner and a different checker. An implementer task needs a branch field and the checkout path. The board does not read a branch name out of the title, the brief, or the approach. A task created before the branch field existed still uses that old reading. The board refuses a new task when that implementer already has live work on a different branch. Same branch is allowed. Several corrections belong on one checklist, on the task you are rejecting. Do not open one task per correction. When a follow-up continues a stopped task, write task #ID in the title or brief. The board copies that task's open checklist. The board already messages the owner in one line (task id, duty, who they mark it done for). Do not paste the brief again. When someone announces they are free, assign them a task or answer on the feed. Read verdicts. Approve or reject done tasks yourself. Do not mark another agent's task done to finish it. You are the only agent who contacts the human. Ask for a merge, or for one decision, in a single note that names the human. Do not ask them to approve each task. @all does not include the human, so name them on that one note.
- human: the person at the dashboard. They merge. Done tasks stay with the checker. Their inbox shows only a note from the orchestrator.

   Open a task:
   curl -s -X POST {base}/api/tasks \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","title":"SHORT TITLE","brief":"What done looks like.","owner":"OWNER_ID","checker":"CHECKER_ID","branch":"feature/name","repo":"/path/to/checkout"}}'

   Checker tasks omit branch and repo. Set "code": false when an implementer task pushes no commit.

   A question or an alert goes to the orchestrator. It does not go to the human.
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","to":"ORCHESTRATOR_ID","note":"The question."}}'

   Only the orchestrator sends this, and only when a merge or a decision is needed:
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"ORCHESTRATOR_ID","note":"@HUMAN_NAME Ready to merge PR 35 at COMMIT."}}'

5. Write back as yourself. "by" is required on every POST. Use your id or your name.

   Post how you are solving a task (owner, or the human, or the orchestrator):
   curl -s -X POST {base}/api/tasks/TASK_ID/approach \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","text":"What you changed, and what you will do next."}}'

   Mark done, approve, or reject. Approve and reject only from status done. An implementer task is done only when the sha is the tip of that branch on origin. Send the sha. Send repo and branch too if the task does not already have them. Finish every checklist item first, or pass them in "checked".
   curl -s -X POST {base}/api/tasks/TASK_ID/status \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","status":"done","sha":"COMMIT","repo":"/path/to/checkout","branch":"feature/name"}}'

   Reject with corrections. The board stores them as one checklist on this task. Do not open a new task per correction. Severity is blocker, major, or minor. A note without corrections is refused, and the task stays done.
   curl -s -X POST {base}/api/tasks/TASK_ID/status \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","status":"rejected","corrections":[{{"id":"L1","severity":"major","file":"File.swift:40","what":"The shipped path still falls back to the test double."}}]}}'

   Mark one checklist item done:
   curl -s -X POST {base}/api/tasks/TASK_ID/check \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","item":"L1"}}'

   status is one of: working, done, approved, rejected, stopped.
   A note that does not change status. Set finished to true when this note is the finish. Words such as finished, completed, approved, or merged do not count on their own. The same flag works on an approach.
   curl -s -X POST {base}/api/tasks/TASK_ID/note \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"...","finished":true}}'

   Public feed, a direct message when "to" is set, or a note that contains @Name. @Name is delivered to that agent's id. @all is delivered to every agent who is still joined, except you. Logged-out agents are not included. On your filtered feed, to or mentions is your id when someone is talking to you, including a note sent with @all.
   curl -s -X POST {base}/api/feed \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID","note":"..."}}'

6. After you act, read /api/feed?for=YOUR_ID again. Do not repeat a post you already made. If a call returns an error JSON, read the error and stop that action. The board is only on this machine. Stay on {base}.

7. Log out when the person ends your session. Treat any of these as the end: stop, disconnect, log out, close the session, or that's all for today. Logging out is the last call you make. Do not poll after it. Do not keep working.

   curl -s -X POST {base}/api/logout \\
     -H 'Content-Type: application/json' \\
     -d '{{"by":"YOUR_ID"}}'

   You leave the board. The same name can join again in a later session.
"""


def read_feed(data: dict, who: str = "", since: str = "", limit: str = "") -> tuple[int, dict]:
    """Feed and task queue for one agent. Saves only when a stale task is stopped.

    Without since, the feed list is the newest posts only, so a first read does
    not fill an agent's context with the whole history. omitted says how many
    older posts were left out.
    """
    if release_stale(data):
        save(data)
    posts = list(data.get("feed") or [])
    agent_id = ""
    if who:
        found, err = resolve(data, who)
        if not found:
            return 400, {"error": err}
        mark_seen(found)
        agent_id = found["id"]
        posts = [post for post in posts if feed_visible(post, agent_id)]
    omitted = 0
    if since:
        # An unescaped + in a query string arrives as a space.
        try:
            after = parse_at(since.strip().replace(" ", "+"))
        except ValueError:
            return 400, {"error": "since must be an ISO-8601 time, like 2026-10-14T10:00:00+01:00"}
        posts = [post for post in posts if parse_at(post["at"]) > after]
    else:
        try:
            cap = int(limit) if limit else FEED_LIMIT
        except ValueError:
            return 400, {"error": "limit must be a number"}
        cap = max(1, min(cap, FEED_LIMIT_MAX))
        if len(posts) > cap:
            omitted = len(posts) - cap
            posts = posts[-cap:]
    payload = {"feed": posts, "omitted": omitted}
    if agent_id:
        payload["tasks"] = agent_tasks(data, agent_id)
    return 200, payload


def archive_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(DATA)), "board-archive")


def archive_old(data: dict, moment: datetime | None = None, days: float | None = None) -> int:
    """Move closed tasks and feed posts older than days to board-archive/YYYY-MM.jsonl.

    A task is closed when it is approved, or stopped and not handed back to its
    owner. Each archived line is {"kind": "task" | "post", "item": ...}. The
    lines are appended before the board is saved, so a crash in between can
    repeat a line in the archive but never lose one. Returns how many moved.
    """
    days = ARCHIVE_DAYS if days is None else days
    if days <= 0:
        return 0
    moment = moment or datetime.now(UTC)
    cutoff = moment - timedelta(days=days)
    keep_ids: set[int] = set()
    for row in data.get("agents") or []:
        keep_ids |= handed_back(data, row["id"])
    lines: dict[str, list[str]] = {}

    def month_of(stamp: datetime) -> str:
        return stamp.astimezone(UTC).strftime("%Y-%m")

    kept_tasks = []
    for task in data.get("tasks") or []:
        touched = last_activity(task)
        old = (
            task.get("status") in {"approved", "stopped"}
            and task.get("id") not in keep_ids
            and touched is not None
            and touched < cutoff
        )
        if not old:
            kept_tasks.append(task)
            continue
        line = json.dumps({"kind": "task", "item": task}, ensure_ascii=False)
        lines.setdefault(month_of(touched), []).append(line)
    kept_posts = []
    for post in data.get("feed") or []:
        try:
            at = parse_at(post.get("at") or "")
        except ValueError:
            kept_posts.append(post)
            continue
        if at >= cutoff:
            kept_posts.append(post)
            continue
        line = json.dumps({"kind": "post", "item": post}, ensure_ascii=False)
        lines.setdefault(month_of(at), []).append(line)
    if not lines:
        return 0
    folder = archive_dir()
    os.makedirs(folder, exist_ok=True)
    moved = 0
    for month, rows in sorted(lines.items()):
        with open(os.path.join(folder, f"{month}.jsonl"), "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
        moved += len(rows)
    data["tasks"] = kept_tasks
    data["feed"] = kept_posts
    return moved


def apply_handler(method: str, *args) -> tuple[int, object]:
    """Run a Handler operation and return its status and body. No socket."""
    sent: dict = {}

    class Bridge:
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            sent["code"] = code
            sent["body"] = body
            sent["ctype"] = ctype

    bridge = Bridge()
    bridge._require_author = lambda data, body: Handler._require_author(bridge, data, body)
    bridge._done_ready = lambda data, task, body: Handler._done_ready(bridge, data, task, body)
    getattr(Handler, method)(bridge, *args)
    if "code" not in sent:
        return 500, {"error": "no response"}
    return sent["code"], sent["body"]


def board_call(op: str, payload: dict | None = None) -> tuple[int, object]:
    """Join, read the feed, post a note, or update a task. HTTP and MCP both use this."""
    payload = payload or {}
    with LOCK:
        data = load()
        if op == "feed":
            return read_feed(
                data,
                str(payload.get("for") or payload.get("agent") or ""),
                str(payload.get("since") or ""),
                str(payload.get("limit") or ""),
            )
        if op == "join":
            return apply_handler("_join", data, payload)
        if op == "note":
            return apply_handler("_post_feed", data, payload)
        if op == "task":
            return apply_handler(
                "_task_action",
                data,
                str(payload.get("id") or ""),
                str(payload.get("action") or "note"),
                payload,
            )
        return 404, {"error": "unknown operation"}


def host_name(value: str) -> str:
    """The host part of a Host header or an Origin, without scheme or port."""
    raw = (value or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def local_request(headers) -> str | None:
    """None when the request comes from this machine. Otherwise the reason it is refused."""
    if host_name(headers.get("Host") or "") not in LOCAL_HOSTS:
        return "the board only answers to localhost or 127.0.0.1"
    origin = headers.get("Origin")
    if origin and (origin == "null" or host_name(origin) not in LOCAL_HOSTS):
        return "requests from other sites are refused"
    return None


def sweep_forever() -> None:
    """Stop stale tasks and archive old history, even when nobody polls."""
    while True:
        threading.Event().wait(SWEEP_SECONDS)
        try:
            with LOCK:
                data = load()
                stopped = release_stale(data)
                if archive_old(data) or stopped:
                    save(data)
        except Exception as exc:  # keep sweeping after a bad row
            print(f"sweep failed: {exc}", file=sys.stderr, flush=True)


class BoardServer(ThreadingHTTPServer):
    # A stuck client must not keep the process alive or fill the accept queue.
    daemon_threads = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(10)

    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", etag: str = "") -> None:
        # Queued so the socket write happens after the board lock is released.
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self._pending = (code, raw, ctype, etag)

    def _flush(self) -> None:
        pending = getattr(self, "_pending", None)
        if not pending:
            return
        self._pending = None
        code, raw, ctype, etag = pending
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"__invalid__": True}
        return body if isinstance(body, dict) else {"__invalid__": True}

    def _require_author(self, data: dict, body: dict):
        found, err = resolve(data, body.get("by") or "", active_only=True)
        if not found:
            self._send(400, {"error": err or "by must be an agent who has joined"})
            return None
        mark_seen(found)
        return found

    def do_GET(self):
        try:
            self._do_GET()
        finally:
            self._flush()

    def _do_GET(self):
        refused = local_request(self.headers)
        if refused:
            return self._send(403, {"error": refused})
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html", "/prompt"):
            host = self.headers.get("Host") or "localhost:8791"
            base = "http://" + host
            with LOCK:
                text = join_prompt(base, load())
            if path == "/prompt":
                return self._send(200, text.encode(), "text/plain; charset=utf-8")
            with open(INDEX, encoding="utf-8") as f:
                html = f.read()
            block = "<pre id=\"join-prompt\">" + _escape(text) + "</pre>"
            html = html.replace("<!-- PROMPT -->", block, 1)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/state":
            with LOCK:
                data = load()
                # Online flags change with time, not only with saves, so the tag
                # also turns over every 30 seconds.
                etag = f'"{_REV}-{int(datetime.now(UTC).timestamp()) // 30}"'
                if self.headers.get("If-None-Match") == etag:
                    return self._send(304, b"", etag=etag)
                agents = []
                for row in data["agents"]:
                    item = dict(row)
                    item["online"] = is_online(row)
                    agents.append(item)
                tasks = []
                for task in data["tasks"]:
                    item = dict(task)
                    item["approach"] = latest_approach(task)
                    tasks.append(item)
                body = json.dumps({**data, "agents": agents, "tasks": tasks}, ensure_ascii=False).encode()
            return self._send(200, body, etag=etag)
        if path == "/api/feed":
            params = parse_qs(query)
            who = (params.get("for") or [""])[0]
            since = (params.get("since") or [""])[0]
            limit = (params.get("limit") or [""])[0]
            with LOCK:
                code, payload = read_feed(load(), who, since, limit)
            return self._send(code, payload)
        return self._send(404, {"error": "unknown path"})

    def do_POST(self):
        try:
            self._do_POST()
        finally:
            self._flush()

    def _do_POST(self):
        refused = local_request(self.headers)
        if refused:
            return self._send(403, {"error": refused})
        # A web page can send text/plain without a preflight. JSON needs one.
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, {"error": "send Content-Type: application/json"})
        path = self.path.split("?")[0]
        body = self._body()
        if body.get("__invalid__"):
            return self._send(400, {"error": "invalid JSON"})

        with LOCK:
            data = load()
            if path == "/api/join":
                return self._join(data, body)
            if path == "/api/logout":
                return self._logout(data, body)
            if path == "/api/heartbeat":
                author = self._require_author(data, body)
                if not author:
                    return None
                # last_seen lives in memory. The next real save writes it.
                return self._send(200, {**author, "online": True})
            if path == "/api/agents":
                return self._add_agent(data, body)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "agents"]:
                return self._agent_action(data, parts[2], parts[3], body)
            if path == "/api/feed":
                return self._post_feed(data, body)
            if path == "/api/tasks":
                return self._add_task(data, body)
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                return self._task_action(data, parts[2], parts[3], body)
            return self._send(404, {"error": "unknown path"})

    def _agent_action(self, data: dict, aid: str, action: str, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        found = agent(data, aid)
        if not found:
            return self._send(404, {"error": "no such agent"})
        if author["role"] not in {"human", "orchestrator"} and author["id"] != found["id"]:
            return self._send(403, {"error": "only the human or an orchestrator can remove or restore another agent"})
        if action == "remove":
            found["active"] = False
        elif action == "restore":
            found["active"] = True
        else:
            return self._send(404, {"error": "use remove or restore"})
        save(data)
        return self._send(200, found)

    def _join(self, data: dict, body: dict):
        name = (body.get("name") or "").strip()
        role = (body.get("role") or "").strip().lower()
        model = (body.get("model") or "").strip()
        if not name or len(name) > 60:
            return self._send(400, {"error": "name is required"})
        if role not in ROLES:
            return self._send(400, {"error": f"role must be one of {sorted(ROLES)}"})
        if not model or len(model) > 80:
            return self._send(400, {"error": "model is required"})
        aid = id_for_name(data, name)
        found = agent(data, aid)
        if found:
            found["name"] = name
            found["role"] = role
            found["model"] = model
            found["active"] = True
            mark_seen(found)
            row = found
            code = 200
        else:
            row = {
                "id": aid,
                "name": name,
                "role": role,
                "model": model,
                "active": True,
                "joined_at": now(),
                "last_seen": now(),
            }
            data["agents"].append(row)
            code = 201
        save(data)
        row["online"] = True
        return self._send(code, row)

    def _logout(self, data: dict, body: dict):
        found, err = resolve(data, body.get("by") or "", active_only=False)
        if not found:
            return self._send(400, {"error": err or "by must be an agent who has joined"})
        found["active"] = False
        save(data)
        found["online"] = False
        return self._send(200, found)

    def _add_agent(self, data: dict, body: dict):
        return self._send(400, {"error": "join with POST /api/join and a name, role, and model"})

    def _post_feed(self, data: dict, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        note = (body.get("note") or "").strip()
        if not note:
            return self._send(400, {"error": "note is required"})
        if len(note) > NOTE_MAX:
            return self._send(400, {
                "error": "a feed note is at most 600 characters. Put the detail on the task with /note, or on the reject checklist.",
            })
        blocked = done_note_block(data, note)
        if blocked:
            return self._send(400, {"error": blocked})
        raw_to = body.get("to")
        to_token = raw_to.strip() if isinstance(raw_to, str) else ""
        mentions = mention_ids(note, data["agents"], author["id"])
        to_all = bool(re.search(r"@all(?!\w)", note, re.IGNORECASE))
        if to_token:
            target, err = resolve(data, to_token)
            if not target:
                return self._send(400, {"error": err})
            if target["id"] not in mentions:
                mentions.append(target["id"])
        note, mentions = route_human_away(data, author, note, mentions, to_all)
        repeated = intro_block(data, author["id"], note, mentions)
        if repeated:
            return self._send(400, {"error": repeated})
        entry = {
            "id": data["next_feed_id"],
            "at": now(),
            "by": author["id"],
            "to": mentions[0] if len(mentions) == 1 else (mentions or None),
            "mentions": mentions,
            "to_all": to_all,
            "note": note,
        }
        data["next_feed_id"] += 1
        data["feed"].append(entry)
        save(data)
        return self._send(201, entry)

    def _add_task(self, data: dict, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        title = (body.get("title") or "").strip()
        if not title:
            return self._send(400, {"error": "title is required"})
        owner, owner_err = resolve(data, body.get("owner") or "", active_only=True)
        checker, checker_err = resolve(data, body.get("checker") or "", active_only=True)
        if not owner or not checker:
            return self._send(400, {"error": owner_err or checker_err or "owner and checker must be active agents"})
        if owner["id"] == checker["id"]:
            return self._send(400, {"error": "checker must be a different agent"})
        brief = (body.get("brief") or "").strip()
        code = flag(body, "code", owner.get("role") == "implementer")
        branch = clean_branch(str(body.get("branch") or ""))
        if body.get("branch") and not branch:
            return self._send(400, {"error": "branch must be a branch name, without spaces"})
        repo = ""
        if owner.get("role") == "implementer":
            blocked = branch_block(data, owner, branch)
            if blocked:
                code_no = 400 if blocked.endswith("needs a branch") else 409
                return self._send(code_no, {"error": blocked})
            if code:
                repo, repo_err = clean_repo(str(body.get("repo") or ""))
                if repo_err:
                    return self._send(400, {"error": repo_err})
        elif body.get("repo"):
            repo, repo_err = clean_repo(str(body.get("repo") or ""))
            if repo_err:
                return self._send(400, {"error": repo_err})
        checklist, list_err = normalize_corrections(body.get("checklist") or [])
        if list_err:
            return self._send(400, {"error": list_err})
        carried = copied_open_checks(data, title, brief)
        if carried:
            own = {item["id"] for item in checklist}
            checklist = [item for item in carried if item["id"] not in own] + checklist
        task = {
            "id": data["next_task_id"],
            "title": title,
            "brief": brief,
            "owner": owner["id"],
            "checker": checker["id"],
            "status": "open",
            "created_at": now(),
            "updated_at": now(),
            "log": [],
            "branch": branch,
            "repo": repo,
            "code": code if owner.get("role") == "implementer" else flag(body, "code", False),
            "checklist": checklist,
        }
        data["next_task_id"] += 1
        log(task, author["id"], brief or "Opened", "open", "status")
        data["tasks"].append(task)
        append_notice(
            data,
            author["id"],
            owner["id"],
            f"Task #{task['id']} is yours. Duty: do. Mark it done for {checker['name']} ({checker['id']}).",
        )
        save(data)
        task["approach"] = ""
        return self._send(201, task)

    def _done_ready(self, data: dict, task: dict, body: dict):
        """Validate a done move without changing the task. Returns ready, error, sha, repo, branch, checked ids."""
        checklist = task.get("checklist") or []
        raw_checked = body.get("checked") or []
        if isinstance(raw_checked, str):
            raw_checked = [part.strip() for part in raw_checked.split(",") if part.strip()]
        if not isinstance(raw_checked, list):
            return False, "checked must be a list of checklist ids", None, None, None, set()
        checked = {str(item).strip() for item in raw_checked}
        known = {item.get("id") for item in checklist}
        unknown = sorted(cid for cid in checked if cid not in known)
        if unknown:
            return False, "no such checklist item: " + ", ".join(unknown), None, None, None, set()
        open_items = [
            item["id"] for item in checklist
            if not item.get("done") and item.get("id") not in checked
        ]
        if open_items:
            return False, "checklist still open: " + ", ".join(open_items), None, None, None, set()

        full = None
        repo_path = None
        branch_name = None
        if task_needs_sha(data, task):
            if "branch" in task:
                known = clean_branch(task.get("branch") or "")
            else:
                known = inferred_branch(task)
            sent = clean_branch(str(body.get("branch") or ""))
            if body.get("branch") and not sent:
                return False, "branch must be a branch name, without spaces", None, None, None, set()
            if known and sent and known != sent:
                return False, f"this task is on branch {known}", None, None, None, set()
            branch_name = sent or known
            stored_repo = (task.get("repo") or "").strip()
            sent_repo = str(body.get("repo") or "").strip()
            if stored_repo and sent_repo:
                stored_path, stored_err = clean_repo(stored_repo)
                sent_path, sent_err = clean_repo(sent_repo)
                if sent_err:
                    return False, sent_err, None, None, None, set()
                if stored_err:
                    return False, stored_err, None, None, None, set()
                if stored_path != sent_path and not same_origin(stored_path, sent_path):
                    return False, f"this task is in repo {stored_path}", None, None, None, set()
                repo_path = sent_path
            else:
                repo_path, repo_err = clean_repo(sent_repo or stored_repo)
                if repo_err:
                    return False, repo_err, None, None, None, set()
            # The network check runs after this returns, outside the board lock.
            short = str(body.get("sha") or "").strip().lower()
            if not SHA_RE.match(short):
                return False, "sha must be 7 to 40 hex characters", None, None, None, set()
            full = short
        else:
            if body.get("repo"):
                repo_path, repo_err = clean_repo(str(body.get("repo") or ""))
                if repo_err:
                    return False, repo_err, None, None, None, set()
            if body.get("branch"):
                branch_name = clean_branch(str(body.get("branch") or ""))
                if not branch_name:
                    return False, "branch must be a branch name, without spaces", None, None, None, set()
            if is_review_task(data, task):
                short = str(body.get("sha") or "").strip().lower()
                if not SHA_RE.match(short):
                    return (
                        False,
                        "a review must include the sha you reviewed, 7 to 40 hex characters",
                        None, None, None, set(),
                    )
                full = short
        return True, None, full, repo_path, branch_name, checked

    def _task_action(self, data: dict, raw_id: str, action: str, body: dict):
        author = self._require_author(data, body)
        if not author:
            return None
        try:
            tid = int(raw_id)
        except ValueError:
            return self._send(404, {"error": "bad task id"})
        task = next((t for t in data["tasks"] if t["id"] == tid), None)
        if not task:
            return self._send(404, {"error": "no such task"})
        text = (body.get("text") or body.get("note") or "").strip()
        human = author.get("role") == "human"
        orchestrator = author["role"] == "orchestrator"
        is_owner = author["id"] == task["owner"]
        is_checker = author["id"] == task["checker"]

        if action == "note":
            if not text:
                return self._send(400, {"error": "note is required"})
            log(task, author["id"], text, None, "note", finished=flag(body, "finished", False))
        elif action == "approach":
            if not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner posts how this is being solved"})
            if not text:
                return self._send(400, {"error": "text is required"})
            if task["status"] in {"approved", "stopped"}:
                return self._send(409, {"error": "this task is closed"})
            if task["status"] in {"open", "rejected"}:
                task["status"] = "working"
            log(task, author["id"], text, task["status"], "approach", finished=flag(body, "finished", False))
        elif action == "check":
            if not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner can mark a checklist item"})
            item_id = str(body.get("item") or "").strip()
            found = next((item for item in task.get("checklist") or [] if item.get("id") == item_id), None)
            if not found:
                return self._send(400, {"error": "no such checklist item"})
            found["done"] = not flag(body, "open", False)
            log(task, author["id"], f"{item_id} {'open' if not found['done'] else 'done'}", None, "check")
        elif action == "status":
            status = body.get("status")
            if status not in STATUSES or status == "open":
                return self._send(400, {"error": "status must be working, done, approved, rejected, or stopped"})
            if task["status"] in {"approved", "stopped"}:
                return self._send(409, {"error": "this task is closed"})
            if status in {"working", "done"} and not (is_owner or human or orchestrator):
                return self._send(403, {"error": "only the owner can move this forward"})
            corrections = None
            full_sha = None
            repo_path = None
            branch_name = None
            checked_ids: set[str] = set()
            if status in {"approved", "rejected"}:
                if task["status"] != "done":
                    return self._send(409, {"error": "only a done task can be approved or rejected"})
                if not (is_checker or human or can_cover_review(data, task, author["id"])):
                    return self._send(403, {"error": "only the checker can approve or reject"})
                if status == "rejected":
                    corrections, corr_err = normalize_corrections(body.get("corrections"), required=True)
                    if corr_err:
                        return self._send(400, {"error": corr_err})
                    text = format_corrections(corrections)
                    extra = (body.get("note") or body.get("text") or "").strip()
                    if extra:
                        text = text + "\n" + extra
                    if rejection_count(task) >= 1:
                        status = "stopped"
                        text = text + "\nsecond rejection, task stopped."
            if status == "stopped" and not (human or orchestrator or is_checker):
                return self._send(403, {"error": "only the orchestrator, checker, or you can stop a task"})
            if status == "done":
                ready, done_err, full_sha, repo_path, branch_name, checked_ids = self._done_ready(
                    data, task, body
                )
                if not ready:
                    return self._send(400, {"error": done_err})
                if task_needs_sha(data, task):
                    # git ls-remote can take many seconds. Drop the lock so polls still answer.
                    LOCK.release()
                    try:
                        full_sha, sha_err = verify_pushed(repo_path or "", branch_name or "", full_sha or "")
                    finally:
                        LOCK.acquire()
                    if sha_err:
                        return self._send(400, {"error": sha_err})
                    data = load()
                    task = next((item for item in data["tasks"] if item["id"] == tid), None)
                    if not task:
                        return self._send(404, {"error": "no such task"})
                    fresh = agent(data, author["id"])
                    if fresh:
                        mark_seen(fresh)
                        author = fresh
                    if task["status"] in {"approved", "stopped"}:
                        return self._send(409, {"error": "this task changed while the push was checked"})
                    ready, done_err, _, repo_path, branch_name, checked_ids = self._done_ready(
                        data, task, body
                    )
                    if not ready:
                        return self._send(400, {"error": done_err})
            task["status"] = status
            if corrections is not None:
                task["checklist"] = corrections
            if status == "done":
                for item in task.get("checklist") or []:
                    if item.get("id") in checked_ids:
                        item["done"] = True
                if repo_path:
                    task["repo"] = repo_path
                if branch_name:
                    task["branch"] = branch_name
                if full_sha:
                    task["head"] = full_sha
            log(task, author["id"], text, status, "status")
            release_stale(data)
            if status == "done":
                head = (task.get("head") or "").strip()
                sha_bit = f" Sha: {head}." if head else ""
                append_notice(
                    data,
                    author["id"],
                    task["checker"],
                    f"Task #{tid} is done.{sha_bit} Duty: check. Approve it, or reject it with corrections.",
                )
            elif status == "rejected":
                append_notice(
                    data,
                    author["id"],
                    task["owner"],
                    f"Task #{tid} was rejected. Duty: do. The checklist is on the task. Post a new approach and finish the open items.",
                )
            elif status == "stopped":
                append_notice(
                    data,
                    author["id"],
                    task["owner"],
                    f"Task #{tid} is stopped. Read the checklist on the task.",
                )
        else:
            return self._send(404, {"error": "use approach, note, check, or status"})
        save(data)
        task["approach"] = latest_approach(task)
        return self._send(200, task)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    with LOCK:
        data = load()
        if archive_old(data):
            save(data)
    threading.Thread(target=sweep_forever, daemon=True).start()
    server = BoardServer(("127.0.0.1", port), Handler)
    print(f"dev-room at http://localhost:{port}", flush=True)
    server.serve_forever()
