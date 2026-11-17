#!/usr/bin/env python3
"""Board for the team-build skill.

The board is the only channel between the lead and the helpers. It is a
SQLite database in the git common directory, so every worktree of the repo
opens the same file. The merge queue (`merge-loop`) is the only writer of the
trunk branch.

Identity comes from the worktree: the main checkout is the lead, and each
helper binds its name to its own worktree with `join`.

Run `board.py -h` for the commands. Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

NO_WORK = 3
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MILESTONE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
NOTE_KINDS = ("plan", "verdict", "progress", "question", "answer", "stuck", "free", "handoff")
# Events the lead acts on. `board watch` prints only these.
ATTENTION = (
    "merged", "refused", "green", "red", "suspect", "question", "stuck", "free",
    "verdict", "plan", "handoff", "left", "loop",
)
CONFIG_KEYS = ("gate", "protect", "budget_hours", "max_helpers", "gate_timeout", "check_timeout")
TAIL_LINES = 40

SCHEMA = """
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE agents (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL CHECK (role IN ('lead', 'helper')),
    worktree      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'left')),
    joined_at     TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    announce_seen INTEGER NOT NULL DEFAULT 0,
    notes_seen    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE milestones (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    check_cmd  TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 100,
    status     TEXT NOT NULL DEFAULT 'red' CHECK (status IN ('red', 'green', 'parked', 'dropped')),
    reason     TEXT,
    green_sha  TEXT,
    updated_at TEXT NOT NULL
);
-- The lead's orders to everyone.
CREATE TABLE announce (
    id   INTEGER PRIMARY KEY,
    at   TEXT NOT NULL,
    body TEXT NOT NULL
);
-- Who works on which milestone. Written by the lead, before or after a helper joins.
CREATE TABLE assign (
    agent     TEXT PRIMARY KEY,
    milestone TEXT NOT NULL REFERENCES milestones (id),
    captain   INTEGER NOT NULL DEFAULT 0,
    at        TEXT NOT NULL
);
-- Open board: plans, verdicts, progress, questions, answers, handoffs.
CREATE TABLE notes (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    author    TEXT NOT NULL,
    milestone TEXT,
    task      INTEGER,
    kind      TEXT NOT NULL,
    body      TEXT NOT NULL
);
-- The to-do list. Anyone adds; the first claimer owns. `parent` forms the task tree.
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY,
    milestone   TEXT NOT NULL REFERENCES milestones (id),
    parent      INTEGER REFERENCES tasks (id),
    after       INTEGER REFERENCES tasks (id),  -- not claimable until this task lands
    base        TEXT,                           -- trunk tip when the task was last claimed
    title       TEXT NOT NULL,
    spec        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'claimed', 'queued', 'refused', 'landed', 'dropped')),
    owner       TEXT,
    branch      TEXT,
    refusals    INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Submissions to the merge queue and their outcome.
CREATE TABLE landed (
    id           INTEGER PRIMARY KEY,
    task         INTEGER REFERENCES tasks (id),
    agent        TEXT NOT NULL,
    branch       TEXT NOT NULL,
    sha          TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'merging', 'merged', 'refused')),
    reason       TEXT,
    merged_sha   TEXT,
    log          TEXT,
    submitted_at TEXT NOT NULL,
    decided_at   TEXT
);
-- Append-only audit log. `board watch` streams it to the lead.
CREATE TABLE events (
    id    INTEGER PRIMARY KEY,
    at    TEXT NOT NULL,
    actor TEXT,
    kind  TEXT NOT NULL,
    body  TEXT NOT NULL
);
CREATE INDEX tasks_by_status ON tasks (status, milestone);
CREATE INDEX landed_by_status ON landed (status);
"""


class Fail(Exception):
    """An error the caller can fix. Printed without a traceback."""


class Interrupted(Exception):
    """The merge loop was told to stop while a gate or check ran."""


# SIGTERM sets `requested`; run_cmd then kills the running gate's process
# group, so a stopped loop never leaves a gate running in the trunk worktree.
STOP = {"requested": False, "pgid": None}

# A task is ready to claim when the task it waits for has landed.
READY = "(t.after IS NULL OR EXISTS (SELECT 1 FROM tasks d WHERE d.id = t.after AND d.status = 'landed'))"


# --- small helpers ---------------------------------------------------------


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_minutes(stamp: str) -> float:
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def run_git(args, cwd=None, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Fail("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def git(*args, cwd=None) -> str:
    return run_git(args, cwd=cwd).stdout.strip()


def common_dir() -> Path:
    r = run_git(["rev-parse", "--git-common-dir"], check=False)
    if r.returncode != 0:
        raise Fail("not inside a git repository.")
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = Path.cwd() / p
    return Path(os.path.realpath(str(p)))


def worktree_root() -> str:
    return os.path.realpath(git("rev-parse", "--show-toplevel"))


def board_dir() -> Path:
    return common_dir() / "team-build"


def board_path() -> Path:
    return board_dir() / "board.db"


def open_board(must_exist=True) -> sqlite3.Connection:
    path = board_path()
    if must_exist and not path.exists():
        raise Fail("no board in this repository. The lead makes one with `board init`.")
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def tx(conn):
    """One write transaction. BEGIN IMMEDIATE takes the write lock first,
    so a read followed by an update inside it is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextlib.contextmanager
def trunk_lock():
    """Only one process at a time may use the trunk worktree."""
    with open(board_dir() / "trunk.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cfg(conn, key, default=None):
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_cfg(conn, key, value):
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def protect_globs(conn):
    return [g for g in cfg(conn, "protect", "").split("\n") if g.strip()]


def is_protected(path, globs) -> bool:
    """A glob matches as fnmatch (`*` crosses directories); a plain directory
    name such as `checks` or `checks/` protects everything under it."""
    for g in globs:
        if fnmatch(path, g) or path == g.rstrip("/") or path.startswith(g.rstrip("/") + "/"):
            return True
    return False


def log_event(conn, actor, kind, body):
    conn.execute(
        "INSERT INTO events (at, actor, kind, body) VALUES (?, ?, ?, ?)",
        (now(), actor, kind, body),
    )


def whoami(conn, required=True):
    wt = worktree_root()
    row = conn.execute(
        "SELECT * FROM agents WHERE worktree = ? AND state = 'active'", (wt,)
    ).fetchone()
    if row is None:
        if required:
            gone = conn.execute(
                "SELECT name FROM agents WHERE worktree = ? AND role = 'helper' ORDER BY joined_at DESC LIMIT 1", (wt,)
            ).fetchone()
            if gone:
                raise Fail("%s has left the board (it left, or the lead released it). Run `board join %s` "
                           "to come back, then `board claim`." % (gone["name"], gone["name"]))
            root = cfg(conn, "root")
            if wt.startswith(os.path.join(root, ".team-build") + os.sep):
                raise Fail("%s is not a helper worktree. Run board commands from the main checkout: %s" % (wt, root))
            raise Fail("this worktree has not joined the board. Run `board join <name>` first.")
        return None
    conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now(), row["name"]))
    return row


def require_lead(me):
    if me["role"] != "lead":
        raise Fail("only the lead can do this.")


def require_helper(me, what):
    if me["role"] == "lead":
        raise Fail("this is the lead's checkout, and the lead does not %s. "
                   "Helpers: run this from your own worktree (cd <your worktree> && ...)." % what)


def checkout_branch(path) -> str:
    r = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path, check=False)
    return r.stdout.strip() if r.returncode == 0 else "(detached)"


def get_task(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise Fail("no task T%s." % task_id)
    return row


def get_milestone(conn, mid):
    row = conn.execute("SELECT * FROM milestones WHERE id = ?", (mid,)).fetchone()
    if row is None:
        raise Fail("no milestone %s." % mid)
    return row


def assignment(conn, name):
    return conn.execute("SELECT * FROM assign WHERE agent = ?", (name,)).fetchone()


def rev(ref, cwd=None):
    return git("rev-parse", "--verify", ref + "^{commit}", cwd=cwd)


def is_ancestor(a, b, cwd=None) -> bool:
    return run_git(["merge-base", "--is-ancestor", a, b], cwd=cwd, check=False).returncode == 0


def dirty(cwd) -> str:
    return git("status", "--porcelain", "--untracked-files=no", cwd=cwd)


def pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def loop_pid(conn):
    pid = cfg(conn, "merge_pid")
    if pid and pid_alive(int(pid)):
        return int(pid)
    return None


def run_cmd(cmd, cwd, timeout, log_path) -> int:
    """Run a shell command, append its output to log_path, kill its whole
    process group on timeout. Returns the exit code (124 on timeout)."""
    if STOP["requested"]:
        raise Interrupted()
    with open(log_path, "ab") as log:
        log.write(("\n$ %s\n" % cmd).encode())
        log.flush()
        p = subprocess.Popen(
            cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        STOP["pgid"] = p.pid
        try:
            if STOP["requested"]:
                kill_group(p.pid)
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(p.pid)
            p.wait()
            log.write(("\n[team-build] timed out after %d s\n" % timeout).encode())
            rc = 124
        except BaseException:
            # Ctrl-C and other exits: the command runs in its own session, so stop it here.
            kill_group(p.pid)
            raise
        finally:
            STOP["pgid"] = None
    if STOP["requested"]:
        raise Interrupted()
    return rc


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def untracked_files(cwd):
    out = git("status", "--porcelain", "--untracked-files=all", cwd=cwd)
    return [ln[3:] for ln in out.splitlines() if ln.startswith("?? ")]


def milestone_stubs(conn, ref, mid):
    """STUB markers on ref that belong to a milestone: STUB(<milestone>) and STUB(T<id>) of its tasks."""
    ids = [mid] + ["T%d" % r["id"] for r in conn.execute("SELECT id FROM tasks WHERE milestone = ?", (mid,))]
    r = run_git(["grep", "-n", "-E", "STUB\\((%s)\\)" % "|".join(ids), ref], check=False)
    return [ln[len(ref) + 1:] if ln.startswith(ref + ":") else ln for ln in r.stdout.splitlines()]


def saved_branch(conn, t):
    """The branch with a task's unlanded commits, so whoever claims it next continues from there."""
    if t["owner"]:
        cand = "%s/t%d" % (t["owner"], t["id"])
        ref = "refs/heads/" + cand
        if run_git(["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0 \
                and not is_ancestor(ref, cfg(conn, "trunk")):
            return cand
    return t["branch"]


def reopen(conn, t):
    conn.execute(
        "UPDATE tasks SET status = 'open', owner = NULL, branch = ?, updated_at = ? WHERE id = ?",
        (saved_branch(conn, t), now(), t["id"]),
    )


def tail(path, n=TAIL_LINES) -> str:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:])


def failure_lines(path, n=4) -> str:
    """The lines that say why a check failed: STUB markers first, then
    error and assertion lines, else the last lines."""
    try:
        lines = [ln.strip() for ln in Path(path).read_text(errors="replace").splitlines() if ln.strip()]
    except FileNotFoundError:
        return ""
    for pattern in (r"STUB\(", r"(?i)error|exception|assert|fail|not found|no such"):
        hits = [ln for ln in lines if re.search(pattern, ln) and not ln.startswith("$ ")]
        if hits:
            return "\n".join(list(dict.fromkeys(hits))[:n])
    return "\n".join(lines[-n:])


def stub_count(ref) -> int:
    r = run_git(["grep", "-c", "-F", "STUB(", ref], check=False)
    total = 0
    for line in r.stdout.splitlines():
        try:
            total += int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            pass
    return total


def indent(text, prefix="  "):
    return "\n".join(prefix + line for line in text.splitlines())


def first_line(text, limit=160):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


# --- lead: set-up ------------------------------------------------------------


def cmd_init(a):
    common = common_dir()
    if common.name != ".git":
        raise Fail("team-build needs a normal (non-bare) repository.")
    root = os.path.realpath(str(common.parent))
    if worktree_root() != root:
        raise Fail("run init from the main checkout: %s" % root)
    if not NAME_RE.match(a.name):
        raise Fail("--name must be lowercase letters, digits and dashes.")
    if board_path().exists():
        conn = open_board()
        raise Fail(
            "a board for run '%s' exists. Finish it with `board archive` first." % cfg(conn, "name")
        )
    trunk = "team/" + a.name
    if run_git(["rev-parse", "--verify", "--quiet", "refs/heads/" + trunk], check=False).returncode == 0:
        raise Fail("branch %s exists. Pick another --name." % trunk)
    base = rev(a.base)
    trunk_dir = Path(root) / ".team-build" / "trunk"

    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if "/.team-build/" not in text.splitlines():
        with open(exclude, "a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + "/.team-build/\n")

    git("worktree", "add", "-b", trunk, str(trunk_dir), base)
    bdir = board_dir()
    (bdir / "logs").mkdir(parents=True, exist_ok=True)
    conn = open_board(must_exist=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    with tx(conn):
        for key, value in (
            ("name", a.name), ("trunk", trunk), ("trunk_dir", str(trunk_dir)), ("root", root),
            ("base", base), ("gate", a.gate), ("protect", "\n".join(a.protect or [])),
            ("budget_hours", a.budget_hours), ("max_helpers", a.max_helpers),
            ("gate_timeout", a.gate_timeout), ("check_timeout", a.check_timeout),
            ("started_at", now()), ("root_branch", checkout_branch(root)),
        ):
            set_cfg(conn, key, value)
        conn.execute(
            "INSERT INTO agents (name, role, worktree, joined_at, last_seen) VALUES ('lead', 'lead', ?, ?, ?)",
            (root, now(), now()),
        )
        log_event(conn, "lead", "init", "run %s, trunk %s from %s" % (a.name, trunk, base[:10]))
    pending = git("status", "--porcelain", cwd=root)
    if pending:
        print("Note: these changes in the main checkout are not committed, so they are not on trunk:")
        print(indent("\n".join(pending.splitlines()[:10])))
    print("Board ready for run '%s'." % a.name)
    print("Trunk branch %s starts at %s." % (trunk, base[:10]))
    print("Trunk worktree: %s" % trunk_dir)
    print("Next: build the walking skeleton in the trunk worktree, commit it there,")
    print("then `board milestone add ...` for each milestone and `board preflight`.")


def cmd_config(a):
    conn = open_board()
    me = whoami(conn)
    if a.key is None:
        for row in conn.execute("SELECT key, value FROM config ORDER BY key"):
            print("%s = %s" % (row["key"], row["value"].replace("\n", ", ")))
        return
    require_lead(me)
    if a.key not in CONFIG_KEYS:
        raise Fail("you can set only: %s" % ", ".join(CONFIG_KEYS))
    if a.value is None:
        print(cfg(conn, a.key, ""))
        return
    value = a.value
    if a.key == "protect":
        value = "\n".join(g.strip() for g in a.value.split(",") if g.strip())
    elif a.key in ("max_helpers", "gate_timeout", "check_timeout"):
        if not value.isdigit() or int(value) < 1:
            raise Fail("%s must be a whole number above 0 (seconds for timeouts)." % a.key)
    elif a.key == "budget_hours":
        try:
            if float(value) <= 0:
                raise ValueError
        except ValueError:
            raise Fail("budget_hours must be a number of hours above 0.") from None
    with tx(conn):
        set_cfg(conn, a.key, value)
        log_event(conn, "lead", "config", "%s = %s" % (a.key, value.replace("\n", ", ")))
    print("%s set." % a.key)


def cmd_milestone(a):
    conn = open_board()
    me = whoami(conn)
    require_lead(me)
    if a.action == "add":
        if not MILESTONE_RE.match(a.id):
            raise Fail("milestone IDs use letters, digits, '-' and '_' (for example M01).")
        if not a.title or not a.check:
            raise Fail("`milestone add` needs --title and --check.")
        if conn.execute("SELECT 1 FROM milestones WHERE id = ?", (a.id,)).fetchone():
            raise Fail("milestone %s exists. Use `board milestone set %s` to change it." % (a.id, a.id))
        with tx(conn):
            conn.execute(
                "INSERT INTO milestones (id, title, check_cmd, priority, updated_at) VALUES (?, ?, ?, ?, ?)",
                (a.id, a.title, a.check, a.priority if a.priority is not None else 100, now()),
            )
            log_event(conn, "lead", "milestone", "%s added: %s" % (a.id, a.title))
        print("Milestone %s added (red)." % a.id)
        return
    m = get_milestone(conn, a.id)
    changes = []
    with tx(conn):
        if a.status:
            if a.status == "green":
                raise Fail("only the merge queue or `board recheck` marks a milestone green.")
            conn.execute(
                "UPDATE milestones SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                (a.status, a.reason, now(), m["id"]),
            )
            changes.append("status %s" % a.status)
        for field, column in (("title", "title"), ("check", "check_cmd"), ("priority", "priority")):
            value = getattr(a, field)
            if value is not None:
                conn.execute(
                    "UPDATE milestones SET %s = ?, updated_at = ? WHERE id = ?" % column,
                    (value, now(), m["id"]),
                )
                changes.append(field)
        if not changes:
            raise Fail("nothing to change. Give --status, --title, --check or --priority.")
        body = "%s: %s" % (m["id"], ", ".join(changes))
        if a.reason:
            body += " (%s)" % a.reason
        log_event(conn, "lead", "milestone", body)
    print("Milestone %s updated: %s." % (m["id"], ", ".join(changes)))


def run_milestone_checks(conn, milestones, tdir, label):
    """Run each milestone's check on the trunk worktree. Returns [(row, rc, log)]."""
    timeout = int(cfg(conn, "check_timeout"))
    results = []
    for m in milestones:
        log = board_dir() / "logs" / ("%s-%s-%s.log" % (label, m["id"], int(time.time())))
        rc = run_cmd(m["check_cmd"], tdir, timeout, log)
        results.append((m, rc, log))
    return results


def cmd_preflight(a):
    conn = open_board()
    me = whoami(conn)
    require_lead(me)
    tdir = cfg(conn, "trunk_dir")
    ok = True
    with trunk_lock():
        if dirty(tdir):
            raise Fail("the trunk worktree has uncommitted changes. Commit the skeleton first.")
        new_files = untracked_files(tdir)
        if new_files:
            raise Fail("the trunk worktree has files that are not committed: %s. "
                       "`git add` and commit them, or remove them." % ", ".join(new_files[:10]))
        log = board_dir() / "logs" / ("preflight-gate-%s.log" % int(time.time()))
        rc = run_cmd(cfg(conn, "gate"), tdir, int(cfg(conn, "gate_timeout")), log)
        print("gate: %s (exit %d) log %s" % ("PASS" if rc == 0 else "FAIL", rc, log))
        if rc != 0:
            ok = False
            print(indent(tail(log, 20)))
        rows = conn.execute(
            "SELECT * FROM milestones WHERE status IN ('red', 'green') ORDER BY priority, id"
        ).fetchall()
        if not rows:
            ok = False
            print("no milestones. Add them with `board milestone add`.")
        for m, rc, log in run_milestone_checks(conn, rows, tdir, "preflight"):
            if m["status"] == "green":
                if rc != 0:
                    ok = False
                    print("%s: green, but its check fails now (exit %d). Run `board recheck %s`." % (m["id"], rc, m["id"]))
                else:
                    print("%s: green, and its check passes." % m["id"])
            elif rc == 0:
                ok = False
                print("%s: PASSES already. The check is too weak, or the work exists. "
                      "Fix the check, or run `board recheck %s` to mark it green." % (m["id"], m["id"]))
            else:
                print("%s: fails as expected (exit %d). Why:" % (m["id"], rc))
                print(indent(failure_lines(log), "    "))
        # The gate and checks may rewrite or add files (lock files, formatters, reports).
        git("reset", "--quiet", "--hard", "HEAD", cwd=tdir)
        git("clean", "-ffdq", cwd=tdir)
    print("STUB markers on trunk: %d" % stub_count(cfg(conn, "trunk")))
    with tx(conn):
        log_event(conn, "lead", "preflight", "ok" if ok else "failed")
    if not ok:
        print("Preflight failed.")
        return 1
    print("Preflight passed. Read each failure above: it must fail at a STUB, "
          "not because the check itself is broken.")
    return 0


def cmd_announce(a):
    conn = open_board()
    require_lead(whoami(conn))
    with tx(conn):
        cur = conn.execute("INSERT INTO announce (at, body) VALUES (?, ?)", (now(), a.text))
        log_event(conn, "lead", "announce", first_line(a.text))
    print("Announcement #%d posted." % cur.lastrowid)


def cmd_assign(a):
    conn = open_board()
    require_lead(whoami(conn))
    if not NAME_RE.match(a.agent):
        raise Fail("helper names use lowercase letters, digits and dashes.")
    with tx(conn):
        if a.milestone == "-":
            conn.execute("DELETE FROM assign WHERE agent = ?", (a.agent,))
            log_event(conn, "lead", "assign", "%s unassigned" % a.agent)
            print("%s unassigned. It takes any open work." % a.agent)
            return
        get_milestone(conn, a.milestone)
        conn.execute(
            "INSERT INTO assign (agent, milestone, captain, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (agent) DO UPDATE SET milestone = excluded.milestone, "
            "captain = excluded.captain, at = excluded.at",
            (a.agent, a.milestone, 1 if a.captain else 0, now()),
        )
        log_event(conn, "lead", "assign", "%s -> %s%s" % (a.agent, a.milestone, " (captain)" if a.captain else ""))
    print("%s assigned to %s%s." % (a.agent, a.milestone, " as captain" if a.captain else ""))


def cmd_hire(a):
    """Make a worktree for a new helper and bind the name to it, so the
    helper's identity exists before the agent starts."""
    conn = open_board()
    require_lead(whoami(conn))
    if not NAME_RE.match(a.name) or a.name in ("trunk", "lead"):
        raise Fail("helper names use lowercase letters, digits and dashes, and cannot be 'trunk' or 'lead'.")
    if conn.execute("SELECT 1 FROM agents WHERE name = ?", (a.name,)).fetchone():
        raise Fail("the name %s was used in this run. Pick a new name." % a.name)
    wt = Path(cfg(conn, "root")) / ".team-build" / a.name
    if wt.exists():
        raise Fail("%s exists. Pick a new name." % wt)
    git("worktree", "add", "--quiet", "--detach", str(wt), cfg(conn, "trunk"), cwd=cfg(conn, "root"))
    wt = os.path.realpath(str(wt))
    with tx(conn):
        conn.execute(
            "INSERT INTO agents (name, role, worktree, joined_at, last_seen) VALUES (?, 'helper', ?, ?, ?)",
            (a.name, wt, now(), now()),
        )
        log_event(conn, "lead", "hired", "%s at %s" % (a.name, wt))
    board = os.path.realpath(__file__)
    helper_md = os.path.join(os.path.dirname(os.path.dirname(board)), "HELPER.md")
    print("Worktree for %s: %s" % (a.name, wt))
    print("Spawn prompt:")
    cmd = "python3 " + shlex.quote(board)
    cd = "cd %s && " % shlex.quote(wt)
    print(
        "You are helper {n} in a team build. Your worktree is {wt}. Start every shell command "
        "with `{cd}`, and read and edit files only under that path. "
        "The board command is `{cmd}`; type it in full each time. "
        "Read {md} and follow it. Start with: {cd}{cmd} join {n}".format(
            n=a.name, wt=wt, md=helper_md, cmd=cmd, cd=cd)
    )


# --- everyone: work loop -----------------------------------------------------


def cmd_join(a):
    conn = open_board()
    wt = worktree_root()
    if wt == cfg(conn, "root"):
        raise Fail("the main checkout belongs to the lead. Helpers join from their own worktree.")
    if wt == os.path.realpath(cfg(conn, "trunk_dir")):
        raise Fail("the trunk worktree belongs to the merge queue.")
    if not NAME_RE.match(a.name):
        raise Fail("helper names use lowercase letters, digits and dashes.")
    with tx(conn):
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (a.name,)).fetchone()
        if row is not None:
            if row["worktree"] != wt:
                raise Fail("the name %s is taken. Ask the lead for a new name." % a.name)
            if row["state"] == "left":
                conn.execute("UPDATE agents SET state = 'active', last_seen = ? WHERE name = ?", (now(), a.name))
                log_event(conn, a.name, "joined", "%s joined again from %s" % (a.name, wt))
        else:
            other = conn.execute(
                "SELECT name FROM agents WHERE worktree = ? AND state = 'active'", (wt,)
            ).fetchone()
            if other:
                raise Fail("this worktree already joined as %s." % other["name"])
            conn.execute(
                "INSERT INTO agents (name, role, worktree, joined_at, last_seen) VALUES (?, 'helper', ?, ?, ?)",
                (a.name, wt, now(), now()),
            )
            log_event(conn, a.name, "joined", "%s joined from %s" % (a.name, wt))
    brief(conn, whoami(conn), show_all=True)


def brief(conn, me, show_all=False):
    name = me["name"]
    top_a = conn.execute("SELECT COALESCE(MAX(id), 0) FROM announce").fetchone()[0]
    top_n = conn.execute("SELECT COALESCE(MAX(id), 0) FROM notes").fetchone()[0]
    trunk = cfg(conn, "trunk")
    asg = assignment(conn, name)
    role = "lead" if me["role"] == "lead" else ("captain" if asg and asg["captain"] else "helper")
    print("You are %s (%s). Trunk branch: %s." % (name, role, trunk))
    if asg:
        print("Assignment: %s%s." % (asg["milestone"], " (you are its captain)" if asg["captain"] else ""))
    else:
        print("Assignment: none. Take open work on any red milestone.")
    globs = protect_globs(conn)
    if globs:
        print("Protected paths (only the lead may change them): %s" % ", ".join(globs))
    print("The merge queue runs the gate: %s" % cfg(conn, "gate"))

    since = 0 if show_all else me["announce_seen"]
    rows = conn.execute("SELECT * FROM announce WHERE id > ? AND id <= ? ORDER BY id", (since, top_a)).fetchall()
    if rows:
        print("\nAnnouncements%s:" % ("" if show_all else " (new)"))
        for r in rows:
            print("[#%d %s]" % (r["id"], r["at"]))
            print(indent(r["body"]))
    else:
        print("\nNo new announcements.")

    if asg:
        m = get_milestone(conn, asg["milestone"])
        print("\nMilestone %s: %s [%s]" % (m["id"], m["title"], m["status"]))
        print("  Check: %s" % m["check_cmd"])
        counts = dict(
            (r["status"], r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE milestone = ? GROUP BY status", (m["id"],)
            )
        )
        print("  Tasks: " + ", ".join("%s %d" % (s, counts.get(s, 0)) for s in ("open", "claimed", "queued", "refused", "landed")))
        for kind in ("plan", "verdict"):
            n = conn.execute(
                "SELECT * FROM notes WHERE milestone = ? AND kind = ? ORDER BY id DESC LIMIT 1", (m["id"], kind)
            ).fetchone()
            if n:
                print("  Latest %s (by %s, %s):" % (kind, n["author"], n["at"]))
                print(indent(n["body"], "    "))
            elif kind == "plan":
                print("  No plan yet." + (" Make it: you are the captain." if asg["captain"] else ""))

    answers = conn.execute(
        "SELECT n.* FROM notes n LEFT JOIN tasks t ON t.id = n.task "
        "WHERE n.id > ? AND n.id <= ? AND n.kind = 'answer' "
        "AND (t.owner = ? OR n.milestone = ? OR (n.task IS NULL AND n.milestone IS NULL)) "
        "ORDER BY n.id",
        (me["notes_seen"], top_n, name, asg["milestone"] if asg else ""),
    ).fetchall()
    for n in answers:
        print("\nAnswer from %s%s:" % (n["author"], " on T%d" % n["task"] if n["task"] else ""))
        print(indent(n["body"]))

    mine = conn.execute(
        "SELECT * FROM tasks WHERE owner = ? AND status IN ('claimed', 'refused', 'queued') ORDER BY id", (name,)
    ).fetchall()
    if mine:
        print("\nYour tasks:")
        for t in mine:
            line = "  T%d [%s] %s - %s" % (t["id"], t["milestone"], t["status"], t["title"])
            if t["status"] == "refused":
                line += "\n    refused: %s" % first_line(t["last_reason"])
            print(line)

    with tx(conn):
        conn.execute(
            "UPDATE agents SET announce_seen = MAX(announce_seen, ?), notes_seen = MAX(notes_seen, ?) WHERE name = ?",
            (top_a, top_n, name),
        )


def cmd_brief(a):
    conn = open_board()
    brief(conn, whoami(conn), show_all=a.all)


def print_task(conn, t, me_name, resumed=False):
    trunk = cfg(conn, "trunk")
    m = get_milestone(conn, t["milestone"])
    print("%s T%d [%s] %s" % ("You already hold" if resumed else "Claimed", t["id"], t["milestone"], t["title"]))
    if t["parent"]:
        p = get_task(conn, t["parent"])
        print("Parent: T%d %s" % (p["id"], p["title"]))
    kids = conn.execute("SELECT id, status, title FROM tasks WHERE parent = ? ORDER BY id", (t["id"],)).fetchall()
    for k in kids:
        print("Child: T%d [%s] %s" % (k["id"], k["status"], k["title"]))
    if t["after"]:
        print("Waited for: T%d (landed)" % t["after"])
    print("Spec:")
    print(indent(t["spec"] or "(none: read the milestone plan with `board brief`)"))
    h = conn.execute(
        "SELECT * FROM notes WHERE task = ? AND kind = 'handoff' ORDER BY id DESC LIMIT 1", (t["id"],)
    ).fetchone()
    if h:
        print("Handoff from %s:" % h["author"])
        print(indent(h["body"]))
    if t["last_reason"]:
        print("Last refusal (%d so far):" % t["refusals"])
        print(indent(t["last_reason"]))
    print("Milestone check: %s" % m["check_cmd"])
    mine = "%s/t%d" % (me_name, t["id"])
    g = "git -C %s" % shlex.quote(worktree_root())
    if t["branch"] == mine:
        steps = ["%s switch %s" % (g, mine), "%s rebase %s" % (g, trunk)]
    elif t["branch"]:
        steps = ["%s switch -c %s %s" % (g, mine, t["branch"]), "%s rebase %s" % (g, trunk)]
    else:
        steps = ["%s switch -c %s %s" % (g, mine, trunk)]
    print("Start with:")
    print(indent("\n".join(steps)))


def cmd_claim(a):
    """Claim once, or with --wait poll until work appears. A captain may
    still be splitting a milestone, and refusals come back later, so a free
    helper waits while any milestone is red instead of leaving at once."""
    conn = open_board()
    deadline = time.time() + max(a.wait, 0) * 60
    told = False
    while True:
        rc = try_claim(conn, a)
        if rc != NO_WORK:
            return rc
        # Work can still appear while a milestone is red (captains split it) or a task is
        # claimed (its helper may split it), or is open but waiting for another task.
        pending = conn.execute(
            "SELECT (SELECT COUNT(*) FROM milestones WHERE status = 'red') + "
            "(SELECT COUNT(*) FROM tasks WHERE status = 'claimed') + "
            "(SELECT COUNT(*) FROM tasks t JOIN milestones m ON m.id = t.milestone "
            "WHERE t.status = 'open' AND m.status IN ('red', 'green'))"
        ).fetchone()[0]
        if time.time() >= deadline or pending == 0:
            print("No open work%s%s." % (
                " on " + a.milestone if a.milestone else "",
                "" if pending else ": every milestone is green, parked or dropped, and no task is in progress"))
            return NO_WORK
        if not told:
            print("No open work yet. Waiting up to %d min for new tasks or refusals..." % a.wait, flush=True)
            told = True
        time.sleep(10)


def try_claim(conn, a):
    with tx(conn):
        me = whoami(conn)
        require_helper(me, "claim tasks")
        name = me["name"]
        held = conn.execute(
            "SELECT * FROM tasks WHERE owner = ? AND status = 'claimed' ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        if held:
            row, resumed = held, True
        else:
            resumed = False
            row = conn.execute(
                "SELECT * FROM tasks WHERE owner = ? AND status = 'refused' ORDER BY id LIMIT 1", (name,)
            ).fetchone()
            asg = assignment(conn, name)
            target = a.milestone or (asg["milestone"] if asg else None)
            if row is None and target:
                row = conn.execute(
                    "SELECT t.* FROM tasks t JOIN milestones m ON m.id = t.milestone "
                    "WHERE t.status = 'open' AND t.milestone = ? AND m.status IN ('red', 'green') AND " + READY +
                    " ORDER BY t.id LIMIT 1",
                    (target,),
                ).fetchone()
            if row is None and not a.milestone:
                # Red milestones first; then open work on green ones (review fixes, leftover pieces).
                row = conn.execute(
                    "SELECT t.* FROM tasks t JOIN milestones m ON m.id = t.milestone "
                    "WHERE t.status = 'open' AND m.status IN ('red', 'green') AND " + READY +
                    " ORDER BY m.status = 'green', m.priority, t.id LIMIT 1"
                ).fetchone()
            if row is None:
                return NO_WORK
            conn.execute(
                "UPDATE tasks SET status = 'claimed', owner = ?, base = ?, updated_at = ? WHERE id = ?",
                (name, rev(cfg(conn, "trunk")), now(), row["id"]),
            )
            log_event(conn, name, "claimed", "T%d [%s] %s" % (row["id"], row["milestone"], row["title"]))
            row = get_task(conn, row["id"])
    print_task(conn, row, name, resumed)
    return 0


def cmd_add(a):
    conn = open_board()
    with tx(conn):
        me = whoami(conn)
        milestone = a.milestone
        if a.parent:
            p = get_task(conn, a.parent)
            milestone = milestone or p["milestone"]
        if not milestone:
            asg = assignment(conn, me["name"])
            milestone = asg["milestone"] if asg else None
        if not milestone:
            raise Fail("give --milestone.")
        m = get_milestone(conn, milestone)
        if m["status"] == "dropped":
            raise Fail("milestone %s is dropped." % milestone)
        after = a.after if a.after is not None else a.parent
        if after:
            get_task(conn, after)
        cur = conn.execute(
            "INSERT INTO tasks (milestone, parent, after, title, spec, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (milestone, a.parent, after or None, a.title, a.spec or "", me["name"], now(), now()),
        )
        tid = cur.lastrowid
        log_event(conn, me["name"], "added", "T%d [%s] %s%s" % (tid, milestone, a.title, " under T%d" % a.parent if a.parent else ""))
    print("T%d added to %s.%s" % (tid, milestone, " It opens when T%d lands." % after if after else ""))


def cmd_release(a):
    conn = open_board()
    with tx(conn):
        me = whoami(conn)
        t = get_task(conn, a.task)
        if t["owner"] != me["name"]:
            require_lead(me)
        if t["status"] not in ("claimed", "refused"):
            raise Fail("T%d is %s. Only a claimed or refused task can be released." % (t["id"], t["status"]))
        reopen(conn, t)
        conn.execute(
            "INSERT INTO notes (at, author, milestone, task, kind, body) VALUES (?, ?, ?, ?, 'handoff', ?)",
            (now(), me["name"], t["milestone"], t["id"], a.reason),
        )
        log_event(conn, me["name"], "handoff", "T%d released: %s" % (t["id"], first_line(a.reason)))
    print("T%d is open again. Your handoff note is on the board." % t["id"])


def cmd_drop(a):
    conn = open_board()
    with tx(conn):
        require_lead(whoami(conn))
        t = get_task(conn, a.task)
        if t["status"] in ("landed", "queued"):
            raise Fail("T%d is %s and cannot be dropped." % (t["id"], t["status"]))
        conn.execute(
            "UPDATE tasks SET status = 'dropped', last_reason = ?, updated_at = ? WHERE id = ?",
            (a.reason, now(), t["id"]),
        )
        freed = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE after = ? AND status = 'open'", (t["id"],))]
        conn.execute("UPDATE tasks SET after = NULL WHERE after = ?", (t["id"],))
        log_event(conn, "lead", "dropped", "T%d: %s" % (t["id"], a.reason))
    print("T%d dropped." % t["id"])
    if freed:
        print("These tasks waited for it and are open now: %s. Drop them too if they need T%d."
              % (", ".join("T%d" % i for i in freed), t["id"]))


def cmd_note(a):
    conn = open_board()
    with tx(conn):
        me = whoami(conn)
        milestone = a.milestone
        if a.task:
            t = get_task(conn, a.task)
            milestone = milestone or t["milestone"]
        if not milestone:
            asg = assignment(conn, me["name"])
            milestone = asg["milestone"] if asg else None
        if milestone:
            get_milestone(conn, milestone)
        cur = conn.execute(
            "INSERT INTO notes (at, author, milestone, task, kind, body) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), me["name"], milestone, a.task, a.kind, a.text),
        )
        where = " ".join(x for x in (milestone or "", "T%d" % a.task if a.task else "") if x)
        log_event(conn, me["name"], a.kind, "%s%s" % ("[%s] " % where if where else "", first_line(a.text, 300)))
    print("Note #%d posted (%s)." % (cur.lastrowid, a.kind))


def cmd_submit(a):
    conn = open_board()
    me = whoami(conn)
    trunk = cfg(conn, "trunk")
    if me["role"] == "lead":
        if not a.branch:
            require_helper(me, "submit without --branch")
        branch = a.branch
        sha = rev(branch)
        task_id = a.task
    else:
        wt = worktree_root()
        if dirty(wt):
            raise Fail("you have uncommitted changes. Commit them first.")
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt)
        if branch == "HEAD":
            raise Fail("you are on a detached HEAD. Switch to your task branch.")
        sha = rev("HEAD", cwd=wt)
        task_id = a.task
        if task_id is None:
            held = conn.execute(
                "SELECT id FROM tasks WHERE owner = ? AND status = 'claimed' ORDER BY id LIMIT 1", (me["name"],)
            ).fetchone()
            if held is None:
                raise Fail("you hold no claimed task. Give the task ID.")
            task_id = held["id"]
        untracked = untracked_files(wt)
        if untracked:
            print("Note: untracked files are not part of the submission: %s" % ", ".join(untracked[:10]))
    if is_ancestor(sha, trunk):
        if me["role"] != "lead" and task_id is not None:
            t = get_task(conn, task_id)
            # The queue's own landings form trunk's first-parent line. A HEAD on that line is
            # just a newer trunk (for example after a rebase dropped every commit), not work.
            landings = git("rev-list", "--first-parent", "%s..%s" % (t["base"], trunk)).split() if t["base"] else []
            if t["owner"] == me["name"] and t["status"] == "claimed" and t["base"] \
                    and sha != t["base"] and not is_ancestor(sha, t["base"]) and sha not in landings:
                with tx(conn):
                    conn.execute("UPDATE tasks SET status = 'landed', branch = ?, updated_at = ? WHERE id = ?",
                                 (branch, now(), t["id"]))
                    log_event(conn, me["name"], "merged", "T%d is on %s through another landing" % (t["id"], trunk))
                print("T%d is already on %s through another landing, so it is marked landed. Claim the next task."
                      % (t["id"], trunk))
                return 0
        raise Fail("nothing to land: %s is already on %s." % (sha[:10], trunk))
    with tx(conn):
        if task_id is not None:
            t = get_task(conn, task_id)
            if me["role"] != "lead" and (t["owner"] != me["name"] or t["status"] != "claimed"):
                raise Fail("T%d is not claimed by you (it is %s, owner %s)." % (t["id"], t["status"], t["owner"]))
            if me["role"] == "lead" and t["status"] not in ("open", "refused"):
                raise Fail("T%d is %s%s. The lead can take over only an open or refused task." % (
                    t["id"], t["status"], " by " + t["owner"] if t["owner"] else ""))
            conn.execute(
                "UPDATE tasks SET status = 'queued', owner = ?, branch = ?, updated_at = ? WHERE id = ?",
                (me["name"], branch, now(), t["id"]),
            )
        cur = conn.execute(
            "INSERT INTO landed (task, agent, branch, sha, submitted_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, me["name"], branch, sha, now()),
        )
        lid = cur.lastrowid
        depth = conn.execute("SELECT COUNT(*) FROM landed WHERE status IN ('queued', 'merging')").fetchone()[0]
        log_event(conn, me["name"], "queued", "L%d %s%s @ %s" % (lid, "T%d " % task_id if task_id else "", branch, sha[:10]))
    print("Queued L%d (%s @ %s). %d in the queue." % (lid, branch, sha[:10], depth))
    if me["role"] != "lead":
        print("Do not wait. Run `board brief`, then `board claim` for the next task.")
        print("If the queue refuses this, `board claim` gives the task back to you first.")


def cmd_mine(a):
    conn = open_board()
    me = whoami(conn)
    rows = conn.execute(
        "SELECT * FROM tasks WHERE owner = ? AND status IN ('claimed', 'refused', 'queued') ORDER BY id", (me["name"],)
    ).fetchall()
    print("Tasks you hold:" if rows else "You hold no tasks.")
    for t in rows:
        print("  T%d [%s] %s - %s" % (t["id"], t["milestone"], t["status"], t["title"]))
        if t["status"] == "refused":
            print(indent(t["last_reason"] or "", "      "))
    subs = conn.execute(
        "SELECT * FROM landed WHERE agent = ? ORDER BY id DESC LIMIT 10", (me["name"],)
    ).fetchall()
    if subs:
        print("Your recent submissions:")
        for s in subs:
            print("  L%d %s %s @ %s - %s%s" % (
                s["id"], "T%d" % s["task"] if s["task"] else "-", s["branch"], s["sha"][:10], s["status"],
                ": " + first_line(s["reason"]) if s["reason"] else "",
            ))


def release_agent(conn, name, actor, why):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE owner = ? AND status IN ('claimed', 'refused')", (name,)
    ).fetchall()
    for t in rows:
        reopen(conn, t)
    held = [t["id"] for t in rows]
    conn.execute("UPDATE agents SET state = 'left' WHERE name = ?", (name,))
    body = "%s left (%s)" % (name, why)
    if held:
        body += "; reopened " + ", ".join("T%d" % i for i in held)
    log_event(conn, actor, "left", body)
    return held


def cmd_leave(a):
    conn = open_board()
    with tx(conn):
        me = whoami(conn)
        name = a.name or me["name"]
        if name != me["name"]:
            require_lead(me)
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise Fail("no agent %s." % name)
        if row["role"] == "lead":
            raise Fail("the lead does not leave. Use `board archive` to end the run.")
        if row["state"] == "left":
            print("%s already left." % name)
            return
        held = release_agent(conn, name, me["name"], a.reason or ("left" if name == me["name"] else "released by the lead"))
    print("%s left.%s" % (name, " Reopened: " + ", ".join("T%d" % i for i in held) if held else ""))


def cmd_reap(a):
    conn = open_board()
    with tx(conn):
        require_lead(whoami(conn))
        silent = [
            r["name"] for r in conn.execute("SELECT name, last_seen FROM agents WHERE role = 'helper' AND state = 'active'")
            if age_minutes(r["last_seen"]) > a.ttl
        ]
        for name in silent:
            release_agent(conn, name, "lead", "silent for more than %d min" % a.ttl)
    print("Reaped: %s" % (", ".join(silent) if silent else "nobody"))


# --- status and events -------------------------------------------------------


def cmd_status(a):
    conn = open_board()
    whoami(conn, required=False)
    trunk = cfg(conn, "trunk")
    tip = rev(trunk)
    hours = age_minutes(cfg(conn, "started_at")) / 60.0
    pid = loop_pid(conn)
    q = dict((r["status"], r["n"]) for r in conn.execute("SELECT status, COUNT(*) AS n FROM landed GROUP BY status"))
    print("Run %s: trunk %s @ %s. %.1f h of %s h budget. Max helpers %s." % (
        cfg(conn, "name"), trunk, tip[:10], hours, cfg(conn, "budget_hours"), cfg(conn, "max_helpers")))
    print("Merge queue: %s. Waiting %d, merging %d, merged %d, refused %d. STUB markers on trunk: %d." % (
        "running (pid %d)" % pid if pid else "NOT RUNNING", q.get("queued", 0), q.get("merging", 0),
        q.get("merged", 0), q.get("refused", 0), stub_count(trunk)))
    last_hour = conn.execute(
        "SELECT COUNT(*) FROM landed WHERE status = 'merged' AND (julianday('now') - julianday(decided_at)) * 24 <= 1"
    ).fetchone()[0]
    print("Throughput: %d landed in the last hour, %.1f/h over the run." % (last_hour, (q.get("merged", 0) / hours) if hours > 0 else 0))
    root = cfg(conn, "root")
    branch_now = checkout_branch(root)
    changed = len(dirty(root).splitlines())
    if branch_now != cfg(conn, "root_branch") or changed:
        print("Main checkout: on %s (was %s at init), %d changed tracked files. "
              "A helper may have worked outside its worktree." % (branch_now, cfg(conn, "root_branch"), changed))

    print("\n%-8s %-7s %4s %5s %5s %5s %5s %5s  %-22s %s" % (
        "MILE", "STATUS", "PRI", "OPEN", "CLMD", "QUEUE", "REFUS", "LAND", "CREW", "VERDICT"))
    for m in conn.execute("SELECT * FROM milestones ORDER BY priority, id"):
        c = dict((r["status"], r["n"]) for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE milestone = ? GROUP BY status", (m["id"],)))
        crew = [
            ("*" if r["captain"] else "") + r["agent"] for r in conn.execute(
                "SELECT s.agent, s.captain FROM assign s JOIN agents g ON g.name = s.agent "
                "WHERE s.milestone = ? AND g.state = 'active' ORDER BY s.captain DESC, s.agent", (m["id"],))
        ]
        v = conn.execute(
            "SELECT body FROM notes WHERE milestone = ? AND kind = 'verdict' ORDER BY id DESC LIMIT 1", (m["id"],)
        ).fetchone()
        print("%-8s %-7s %4d %5d %5d %5d %5d %5d  %-22s %s" % (
            m["id"], m["status"], m["priority"], c.get("open", 0), c.get("claimed", 0), c.get("queued", 0),
            c.get("refused", 0), c.get("landed", 0), ",".join(crew)[:22], first_line(v["body"], 50) if v else ""))
        if m["status"] in ("parked", "dropped") and m["reason"]:
            print("         reason: %s" % first_line(m["reason"], 100))

    waiting = conn.execute(
        "SELECT t.id, t.after FROM tasks t WHERE t.status = 'open' AND NOT " + READY + " ORDER BY t.id"
    ).fetchall()
    if waiting:
        print("Waiting for another task to land: %s" % ", ".join("T%d (after T%d)" % (w["id"], w["after"]) for w in waiting))

    helpers = conn.execute("SELECT * FROM agents WHERE role = 'helper' AND state = 'active' ORDER BY name").fetchall()
    print("\nActive helpers: %d" % len(helpers))
    for h in helpers:
        asg = assignment(conn, h["name"])
        t = conn.execute(
            "SELECT id, status FROM tasks WHERE owner = ? AND status IN ('claimed', 'refused') ORDER BY id LIMIT 1", (h["name"],)
        ).fetchone()
        print("  %-8s %-10s %-14s seen %3.0f min ago" % (
            h["name"], (asg["milestone"] + ("*" if asg["captain"] else "")) if asg else "-",
            "T%d %s" % (t["id"], t["status"]) if t else "idle", age_minutes(h["last_seen"])))

    print("\nOpen questions and stuck notes:")
    rows = conn.execute(
        "SELECT * FROM notes n WHERE kind IN ('question', 'stuck') AND NOT EXISTS ("
        "SELECT 1 FROM notes r WHERE r.kind = 'answer' AND r.id > n.id AND "
        "((n.task IS NOT NULL AND r.task = n.task) OR (n.task IS NULL AND r.milestone IS n.milestone))) "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for n in rows:
        print("  #%d %s %s%s: %s" % (n["id"], n["kind"], n["author"], " T%d" % n["task"] if n["task"] else "", first_line(n["body"], 120)))
    if not rows:
        print("  none")

    print("\nRecent refusals:")
    rows = conn.execute("SELECT * FROM landed WHERE status = 'refused' ORDER BY id DESC LIMIT 5").fetchall()
    for s in rows:
        print("  L%d %s by %s: %s" % (s["id"], "T%d" % s["task"] if s["task"] else "-", s["agent"], first_line(s["reason"], 120)))
    if not rows:
        print("  none")


def cmd_watch(a):
    conn = open_board()
    require_lead(whoami(conn))
    cursor = int(cfg(conn, "watch_cursor", 0) or 0)
    if a.from_now or cursor == 0:
        cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    while True:
        rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id", (cursor,)).fetchall()
        for e in rows:
            if e["kind"] in ATTENTION:
                print("%s %s %s: %s" % (e["at"][11:19], e["kind"], e["actor"] or "-", first_line(e["body"], 300)), flush=True)
            cursor = e["id"]
        if rows:
            with tx(conn):
                set_cfg(conn, "watch_cursor", cursor)
        time.sleep(2)


def cmd_sql(a):
    path = board_path()
    if not path.exists():
        raise Fail("no board in this repository.")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        cur = conn.execute(a.query)
    except sqlite3.Error as e:
        raise Fail("SQL error: %s" % e) from None
    if cur.description:
        print("\t".join(d[0] for d in cur.description))
        for row in cur:
            print("\t".join("" if v is None else str(v).replace("\n", "\\n") for v in row))


def cmd_whoami(a):
    conn = open_board()
    me = whoami(conn, required=False)
    if me is None:
        print("This worktree has not joined. Worktree: %s" % worktree_root())
        return 1
    print("%s (%s) at %s" % (me["name"], me["role"], me["worktree"]))


# --- merge queue -------------------------------------------------------------


def refuse(conn, sub, reason, log_path, actor="queue"):
    with tx(conn):
        conn.execute(
            "UPDATE landed SET status = 'refused', reason = ?, log = ?, decided_at = ? WHERE id = ?",
            (reason, str(log_path) if log_path else None, now(), sub["id"]),
        )
        if sub["task"]:
            t = get_task(conn, sub["task"])
            if t["status"] == "queued":
                owner = conn.execute(
                    "SELECT role, state FROM agents WHERE name = ?", (t["owner"],)
                ).fetchone() if t["owner"] else None
                if owner is not None and owner["role"] == "helper" and owner["state"] == "active":
                    conn.execute(
                        "UPDATE tasks SET status = 'refused', refusals = refusals + 1, last_reason = ?, updated_at = ? WHERE id = ?",
                        (reason, now(), t["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = 'open', owner = NULL, refusals = refusals + 1, "
                        "last_reason = ?, updated_at = ? WHERE id = ?",
                        (reason, now(), t["id"]),
                    )
        log_event(conn, actor, "refused", "L%d %sby %s: %s" % (
            sub["id"], "T%d " % sub["task"] if sub["task"] else "", sub["agent"], first_line(reason, 200)))


def reset_trunk_dir(tdir, tip):
    """Put the trunk worktree exactly at tip: no merge in progress, no changes,
    no files left behind by an earlier gate. Ignored files (dependency caches) stay."""
    run_git(["merge", "--abort"], cwd=tdir, check=False)
    git("checkout", "--quiet", "--detach", tip, cwd=tdir)
    git("reset", "--quiet", "--hard", tip, cwd=tdir)
    git("clean", "-ffdq", cwd=tdir)


def prepare_trunk_dir(conn, tdir, tip):
    """Detach the trunk worktree at tip. Discard only what the queue made: a
    landing cut short by a crash leaves the `merging` marker set. Anything
    else (edits, commits on the detached HEAD) stops with instructions."""
    if not cfg(conn, "merging"):
        if dirty(tdir) or untracked_files(tdir):
            raise Fail("the trunk worktree %s has changes or new files that the queue did not make. "
                       "Move them to a lead branch, or discard them with `git -C %s reset --hard && git -C %s clean -fd`, "
                       "then try again." % (tdir, tdir, tdir))
        head = rev("HEAD", cwd=tdir)
        if not is_ancestor(head, tip):
            raise Fail("the trunk worktree %s holds commit %s, which is not on %s. Save it with "
                       "`git -C %s branch lead/<topic> %s` and submit that branch, then try again."
                       % (tdir, head[:10], cfg(conn, "trunk"), tdir, head[:10]))
    reset_trunk_dir(tdir, tip)
    with tx(conn):
        set_cfg(conn, "merging", "")


def merge_one(conn) -> bool:
    with tx(conn):
        sub = conn.execute("SELECT * FROM landed WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if sub is None:
            return False
        conn.execute("UPDATE landed SET status = 'merging' WHERE id = ?", (sub["id"],))
    # Clear the marker on every outcome except a crash: refusals and the
    # trunk-moved stop all leave the trunk worktree clean at a known commit.
    finished = False
    try:
        result = attempt_landing(conn, sub)
        finished = True
        return result
    except Interrupted:
        # Stopped during a gate or check: undo the attempt, keep the submission queued.
        with trunk_lock():
            reset_trunk_dir(cfg(conn, "trunk_dir"), rev(cfg(conn, "trunk")))
        with tx(conn):
            conn.execute("UPDATE landed SET status = 'queued' WHERE id = ? AND status = 'merging'", (sub["id"],))
        finished = True
        return False
    except Fail:
        finished = True
        raise
    finally:
        if finished:
            with tx(conn):
                set_cfg(conn, "merging", "")


def record_landing(conn, sub, new, log_path, note=None):
    with tx(conn):
        set_cfg(conn, "trunk_tip", new)
        set_cfg(conn, "landing", "")
        conn.execute(
            "UPDATE landed SET status = 'merged', merged_sha = ?, log = ?, reason = ?, decided_at = ? WHERE id = ?",
            (new, str(log_path) if log_path else None, note, now(), sub["id"]),
        )
        if sub["task"]:
            conn.execute(
                "UPDATE tasks SET status = 'landed', updated_at = ? WHERE id = ? AND status = 'queued'",
                (now(), sub["task"]),
            )
        log_event(conn, "queue", "merged", "L%d %sby %s -> %s%s" % (
            sub["id"], "T%d " % sub["task"] if sub["task"] else "", sub["agent"], new[:10],
            " (%s)" % note if note else ""))


def judge(conn, rows, tdir, sha, log_path, actor):
    """Run each milestone's check with sha checked out in tdir, and record red or
    green. A check that passes while the milestone's STUB markers remain on trunk
    stays red, and the lead gets a `suspect` event: the check may be too weak."""
    out = []
    for m in rows:
        rc = run_cmd(m["check_cmd"], tdir, int(cfg(conn, "check_timeout")), log_path)
        stubs = milestone_stubs(conn, sha, m["id"]) if rc == 0 else []
        state = "green" if rc == 0 and not stubs else "red"
        with tx(conn):
            if state != m["status"] and m["status"] in ("red", "green"):
                conn.execute(
                    "UPDATE milestones SET status = ?, green_sha = ?, updated_at = ? WHERE id = ?",
                    (state, sha if state == "green" else None, now(), m["id"]),
                )
                log_event(conn, actor, state, "%s %s at %s" % (m["id"], m["title"], sha[:10]))
            if stubs:
                log_event(conn, actor, "suspect", "%s check passes, but %d STUB marker(s) remain: %s. "
                          "The check may be too weak." % (m["id"], len(stubs), "; ".join(x[:80] for x in stubs[:3])))
        out.append((m, rc, stubs))
    return out


def red_to_check(conn, task_id):
    """Red milestones worth a check after a landing: the task's own milestone and
    milestones whose tasks have all landed. After a landing with no task (the
    lead's), every red milestone."""
    if task_id is None:
        return conn.execute("SELECT * FROM milestones WHERE status = 'red' ORDER BY priority, id").fetchall()
    return conn.execute(
        "SELECT * FROM milestones m WHERE m.status = 'red' AND (m.id = ? OR ("
        "EXISTS (SELECT 1 FROM tasks t WHERE t.milestone = m.id AND t.status = 'landed') AND "
        "NOT EXISTS (SELECT 1 FROM tasks t WHERE t.milestone = m.id AND t.status IN ('open', 'claimed', 'queued', 'refused'))"
        ")) ORDER BY priority, id",
        (get_task(conn, task_id)["milestone"],),
    ).fetchall()


def attempt_landing(conn, sub) -> bool:
    trunk = cfg(conn, "trunk")
    tdir = cfg(conn, "trunk_dir")
    log_path = board_dir() / "logs" / ("L%d.log" % sub["id"])
    with trunk_lock():
        with tx(conn):
            set_cfg(conn, "merging", "L%d" % sub["id"])
        tip = rev(trunk)
        if tip != cfg(conn, "trunk_tip"):
            with tx(conn):
                conn.execute("UPDATE landed SET status = 'queued' WHERE id = ?", (sub["id"],))
                log_event(conn, "queue", "loop", "stopped: %s moved outside the merge queue (%s -> %s)" % (
                    trunk, (cfg(conn, "trunk_tip") or "?")[:10], tip[:10]))
            raise Fail("%s moved outside the merge queue (expected %s, found %s). Restart the loop with "
                       "--reset-trunk to undo it, or --accept-trunk to keep it." % (trunk, cfg(conn, "trunk_tip"), tip))
        reset_trunk_dir(tdir, tip)
        sha = sub["sha"]
        if run_git(["cat-file", "-e", sha + "^{commit}"], check=False).returncode != 0:
            refuse(conn, sub, "missing commit %s. Commit again and resubmit." % sha[:10], None)
            return True
        if is_ancestor(sha, tip):
            # Another landing carried this commit, for example a branch built on top of it.
            record_landing(conn, sub, tip, None, "already on %s through another landing" % trunk)
            return True
        submitter = conn.execute("SELECT role FROM agents WHERE name = ?", (sub["agent"],)).fetchone()
        if submitter is None or submitter["role"] != "lead":
            changed = git("diff", "--name-only", "--no-renames", "%s...%s" % (tip, sha)).splitlines()
            globs = protect_globs(conn)
            hit = [f for f in changed if is_protected(f, globs)]
            if hit:
                refuse(conn, sub, "protected paths changed: %s. Only the lead may change them. "
                       "Remove these changes, and post a question note if a check or gate is wrong." % ", ".join(hit), None)
                return True
        title = ""
        if sub["task"]:
            title = "T%d %s" % (sub["task"], get_task(conn, sub["task"])["title"])
        msg = "Land %s (%s, L%d)" % (title or sub["branch"], sub["agent"], sub["id"])
        r = run_git(["merge", "--no-ff", "--no-edit", "-m", msg, sha], cwd=tdir, check=False)
        if r.returncode != 0:
            conflicted = git("diff", "--name-only", "--diff-filter=U", cwd=tdir).splitlines()
            reset_trunk_dir(tdir, tip)
            refuse(conn, sub, "merge conflict in: %s. Rebase onto %s, resolve, rerun your checks, then submit again."
                   % (", ".join(conflicted) or first_line(r.stdout + r.stderr), trunk), None)
            return True
        new = rev("HEAD", cwd=tdir)
        rc = run_cmd(cfg(conn, "gate"), tdir, int(cfg(conn, "gate_timeout")), log_path)
        if rc != 0:
            reset_trunk_dir(tdir, tip)
            refuse(conn, sub, "gate failed (exit %d) after merging onto %s. Log: %s\nLast lines:\n%s"
                   % (rc, tip[:10], log_path, tail(log_path)), log_path)
            return True
        for m in conn.execute("SELECT * FROM milestones WHERE status = 'green' ORDER BY id").fetchall():
            rc = run_cmd(m["check_cmd"], tdir, int(cfg(conn, "check_timeout")), log_path)
            if rc != 0:
                reset_trunk_dir(tdir, tip)
                refuse(conn, sub, "milestone %s was green and its check fails after this change (exit %d). Log: %s\nLast lines:\n%s"
                       % (m["id"], rc, log_path, tail(log_path)), log_path)
                return True
        # Record the landing before trunk moves: a crash between the two is finished on restart.
        with tx(conn):
            set_cfg(conn, "landing", "L%d %s %s" % (sub["id"], new, tip))
        r = run_git(["update-ref", "refs/heads/" + trunk, new, tip], check=False)
        if r.returncode != 0:
            reset_trunk_dir(tdir, tip)
            with tx(conn):
                set_cfg(conn, "landing", "")
                conn.execute("UPDATE landed SET status = 'queued' WHERE id = ?", (sub["id"],))
                log_event(conn, "queue", "loop", "stopped: %s moved during the gate" % trunk)
            raise Fail("%s moved while the gate ran. Restart the loop with --reset-trunk to undo it, "
                       "or --accept-trunk to keep it." % trunk)
        record_landing(conn, sub, new, log_path)
        judge(conn, red_to_check(conn, sub["task"]), tdir, new, log_path, "queue")
        # The gate and checks may rewrite or add files; leave the worktree clean.
        reset_trunk_dir(tdir, new)
    return True


def ensure_guard(conn):
    """Keep the trunk branch checked out, with no files, in a guard worktree.
    Git then refuses `git switch <trunk>` in every other worktree, so only the
    queue moves trunk."""
    guard = Path(cfg(conn, "root")) / ".team-build" / ".guard"
    if not guard.exists():
        git("worktree", "add", "--quiet", "--no-checkout", str(guard), cfg(conn, "trunk"), cwd=cfg(conn, "root"))


def cmd_merge_loop(a):
    conn = open_board()
    require_lead(whoami(conn))
    with tx(conn):
        pid = loop_pid(conn)
        if pid and pid != os.getpid():
            raise Fail("a merge loop already runs (pid %d)." % pid)
        set_cfg(conn, "merge_pid", os.getpid())
        conn.execute("UPDATE landed SET status = 'queued' WHERE status = 'merging'")
    trunk = cfg(conn, "trunk")
    tdir = cfg(conn, "trunk_dir")

    def on_term(*_):
        STOP["requested"] = True
        if STOP["pgid"]:
            kill_group(STOP["pgid"])
    signal.signal(signal.SIGTERM, on_term)
    try:
        with trunk_lock():
            tip = rev(trunk)
            last = cfg(conn, "trunk_tip")
            pending = cfg(conn, "landing")
            if pending:
                lid, new, old = pending.split()
                if tip == new and last == old:
                    sub = conn.execute("SELECT * FROM landed WHERE id = ?", (int(lid[1:]),)).fetchone()
                    record_landing(conn, sub, new, board_dir() / "logs" / ("%s.log" % lid),
                                   "recorded after the loop stopped")
                    print("Recorded %s: it reached trunk just before the loop stopped." % lid, flush=True)
                    last = new
                else:
                    with tx(conn):
                        set_cfg(conn, "landing", "")
            if last and last != tip:
                if a.reset_trunk:
                    stray = "team-stray/" + tip[:10]
                    git("branch", "-f", stray, tip)
                    git("update-ref", "refs/heads/" + trunk, last, tip)
                    with tx(conn):
                        log_event(conn, "lead", "loop", "trunk reset to %s; stray commits saved on %s" % (last[:10], stray))
                    print("Put %s back at %s. The stray commits are on branch %s; submit it with "
                          "`board submit --branch %s` if you want them." % (trunk, last[:10], stray, stray), flush=True)
                    tip = last
                elif not a.accept_trunk:
                    raise Fail("%s is at %s, but the queue last landed %s: commits reached trunk outside the queue. "
                               "Restart with --reset-trunk to put trunk back and save those commits on a branch you can "
                               "submit, or with --accept-trunk to keep them without the gate." % (trunk, tip[:10], last[:10]))
            prepare_trunk_dir(conn, tdir, tip)
            ensure_guard(conn)
            with tx(conn):
                set_cfg(conn, "trunk_tip", tip)
                log_event(conn, "queue", "loop", "started (pid %d) at %s" % (os.getpid(), tip[:10]))
        print("Merge loop running (pid %d). Trunk %s at %s." % (os.getpid(), trunk, tip[:10]), flush=True)
        while not STOP["requested"]:
            if merge_one(conn):
                last = conn.execute(
                    "SELECT * FROM events WHERE kind IN ('merged', 'refused', 'green', 'suspect') ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if last:
                    print("%s %s" % (last["kind"], last["body"]), flush=True)
                continue
            if a.once or STOP["requested"]:
                break
            time.sleep(2)
    finally:
        with tx(conn):
            if cfg(conn, "merge_pid") == str(os.getpid()):
                set_cfg(conn, "merge_pid", "")
            log_event(conn, "queue", "loop", "stopped")
    return 0


def cmd_recheck(a):
    conn = open_board()
    require_lead(whoami(conn))
    trunk = cfg(conn, "trunk")
    tdir = cfg(conn, "trunk_dir")
    if a.milestones:
        rows = [get_milestone(conn, m) for m in a.milestones]
    else:
        rows = conn.execute("SELECT * FROM milestones WHERE status IN ('red', 'green') ORDER BY priority, id").fetchall()
    log = board_dir() / "logs" / ("recheck-%s.log" % int(time.time()))
    with trunk_lock():
        tip = rev(trunk)
        on_branch = run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=tdir, check=False).returncode == 0
        if on_branch:
            # Before the first merge loop the lead builds the skeleton here, on the branch.
            if dirty(tdir) or untracked_files(tdir):
                raise Fail("the trunk worktree has uncommitted changes or new files. Commit or remove them first.")
        else:
            prepare_trunk_dir(conn, tdir, tip)
        results = judge(conn, rows, tdir, tip, log, "lead")
        git("reset", "--quiet", "--hard", "HEAD", cwd=tdir)
        git("clean", "-ffdq", cwd=tdir)
    all_green = True
    for m, rc, stubs in results:
        ok = rc == 0 and not stubs
        all_green = all_green and ok
        why = "" if ok else ("  %d STUB marker(s) remain, so it stays red" % len(stubs) if stubs else "  log " + str(log))
        print("%-8s %s (exit %d)%s" % (m["id"], "GREEN" if ok else "RED", rc, why))
    print("Trunk %s @ %s. STUB markers: %d." % (trunk, tip[:10], stub_count(trunk)))
    return 0 if all_green else 1


def cmd_archive(a):
    conn = open_board()
    require_lead(whoami(conn))
    if loop_pid(conn):
        raise Fail("the merge loop still runs (pid %d). Stop it first." % loop_pid(conn))
    active = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE role = 'helper' AND state = 'active'")]
    if active:
        raise Fail("helpers are still active: %s. Wait for them, then `board leave <name>` for each." % ", ".join(active))
    waiting = [r["id"] for r in conn.execute("SELECT id FROM landed WHERE status IN ('queued', 'merging') ORDER BY id")]
    if waiting:
        raise Fail("submissions still wait in the queue: %s. Run `board merge-loop --once` to decide them first."
                   % ", ".join("L%d" % i for i in waiting))
    trunk = cfg(conn, "trunk")
    tdir = cfg(conn, "trunk_dir")
    tip = rev(trunk)
    if Path(tdir).exists():
        head = rev("HEAD", cwd=tdir)
        if head != tip:
            raise Fail("the trunk worktree is at %s, not at %s. Check it before you archive." % (head[:10], tip[:10]))
        git("worktree", "remove", "--force", tdir)
        print("Removed the trunk worktree. Branch %s stays at %s." % (trunk, tip[:10]))
    guard = Path(cfg(conn, "root")) / ".team-build" / ".guard"
    if guard.exists():
        git("worktree", "remove", "--force", str(guard))
    # Helper worktrees, plus any other worktree under .team-build/ (for
    # example the lead's branch for a protected change).
    team_dir = os.path.realpath(os.path.join(cfg(conn, "root"), ".team-build")) + os.sep
    paths = [h["worktree"] for h in conn.execute("SELECT worktree FROM agents WHERE role = 'helper' ORDER BY name")]
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            p = os.path.realpath(line[len("worktree "):])
            if p.startswith(team_dir) and p not in paths:
                paths.append(p)
    for wt in paths:
        if not Path(wt).exists():
            continue
        head = run_git(["rev-parse", "HEAD"], cwd=wt, check=False).stdout.strip()
        if run_git(["status", "--porcelain"], cwd=wt, check=False).stdout.strip() or not head or not is_ancestor(head, tip):
            print("Kept %s: it has changes or commits that are not on trunk." % wt)
            continue
        r = run_git(["worktree", "remove", wt], check=False)
        print(("Removed %s." if r.returncode == 0 else "Could not remove %s.") % wt)
    branches = set(r["branch"] for r in conn.execute("SELECT branch FROM landed UNION SELECT branch FROM tasks WHERE branch IS NOT NULL"))
    deleted = []
    for b in sorted(branches):
        if b == trunk or run_git(["rev-parse", "--verify", "--quiet", "refs/heads/" + b], check=False).returncode != 0:
            continue
        if is_ancestor("refs/heads/" + b, tip) and run_git(["branch", "-D", b], check=False).returncode == 0:
            deleted.append(b)
    print("Deleted %d merged task branches." % len(deleted))
    with tx(conn):
        log_event(conn, "lead", "archive", "run archived at %s" % tip[:10])
    name = cfg(conn, "name")
    conn.close()
    dest = board_dir() / "archive" / ("%s-%s" % (name, datetime.now().strftime("%Y%m%d-%H%M%S")))
    dest.mkdir(parents=True)
    for item in ("board.db", "board.db-wal", "board.db-shm", "logs", "trunk.lock"):
        src = board_dir() / item
        if src.exists():
            src.rename(dest / item)
    print("Board archived to %s" % dest)
    print("Trunk branch %s at %s holds the result." % (trunk, tip[:10]))


# --- command line ------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="board", description="Coordination board for a team build.")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    s = sub.add_parser("init", help="lead: make the board, the trunk branch and its worktree")
    s.add_argument("--name", required=True, help="run name; the trunk branch is team/<name>")
    s.add_argument("--base", default="HEAD", help="commit the trunk starts from (default HEAD)")
    s.add_argument("--gate", required=True, help="shell command every landing must pass")
    s.add_argument("--protect", action="append", help="glob of paths only the lead may change (repeat)")
    s.add_argument("--budget-hours", type=float, default=8.0)
    s.add_argument("--max-helpers", type=int, default=6)
    s.add_argument("--gate-timeout", type=int, default=900, help="seconds (default 900)")
    s.add_argument("--check-timeout", type=int, default=600, help="seconds per milestone check (default 600)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("config", help="show the config; lead: set gate, protect (comma list), "
                       "budget_hours, max_helpers, gate_timeout, check_timeout")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("milestone", help="lead: add or change a milestone")
    s.add_argument("action", choices=("add", "set"))
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--check", help="shell command that exits 0 only when the milestone works")
    s.add_argument("--priority", type=int, help="lower runs first (default 100)")
    s.add_argument("--status", choices=("red", "parked", "dropped"))
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_milestone)

    s = sub.add_parser("preflight", help="lead: gate must pass and every milestone check must fail on trunk")
    s.set_defaults(fn=cmd_preflight)

    s = sub.add_parser("announce", help="lead: post an order to every helper")
    s.add_argument("text")
    s.set_defaults(fn=cmd_announce)

    s = sub.add_parser("assign", help="lead: put a helper on a milestone ('-' to unassign)")
    s.add_argument("agent")
    s.add_argument("milestone")
    s.add_argument("--captain", action="store_true")
    s.set_defaults(fn=cmd_assign)

    s = sub.add_parser("hire", help="lead: make a worktree for a new helper and print its spawn prompt")
    s.add_argument("name")
    s.set_defaults(fn=cmd_hire)

    s = sub.add_parser("join", help="helper: confirm your name from your worktree and read your orders")
    s.add_argument("name")
    s.set_defaults(fn=cmd_join)

    s = sub.add_parser("brief", help="new announcements, your assignment, plan and tasks")
    s.add_argument("--all", action="store_true", help="show every announcement")
    s.set_defaults(fn=cmd_brief)

    s = sub.add_parser("claim", help="take a task: your refused work first, then your milestone, then any red milestone")
    s.add_argument("--milestone")
    s.add_argument("--wait", type=int, default=0, metavar="MIN",
                   help="if there is no work, wait up to MIN minutes for new tasks while a milestone is red")
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("add", help="add a task (use --parent to split a task)")
    s.add_argument("--title", required=True)
    s.add_argument("--spec", help="what done means for this task and how to check it")
    s.add_argument("--milestone")
    s.add_argument("--parent", type=int, help="split: the new task is a piece of this one, and opens when it lands")
    s.add_argument("--after", type=int, help="open only when this task lands (0: at once, even with --parent)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("release", help="give a task back to the pool with a handoff note")
    s.add_argument("task", type=int)
    s.add_argument("--reason", required=True, help="what is done, which branch, what is next")
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser("drop", help="lead: drop a task")
    s.add_argument("task", type=int)
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_drop)

    s = sub.add_parser("note", help="post a note: " + ", ".join(NOTE_KINDS))
    s.add_argument("--kind", required=True, choices=NOTE_KINDS)
    s.add_argument("--milestone")
    s.add_argument("--task", type=int)
    s.add_argument("text")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("submit", help="queue your committed task branch for landing")
    s.add_argument("task", nargs="?", type=int)
    s.add_argument("--branch", help="lead only: the branch to land")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("mine", help="your tasks and submissions")
    s.set_defaults(fn=cmd_mine)

    s = sub.add_parser("leave", help="leave the team and reopen your unfinished tasks (lead: name a helper)")
    s.add_argument("name", nargs="?")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_leave)

    s = sub.add_parser("reap", help="lead: reopen tasks of helpers that went silent")
    s.add_argument("--ttl", type=int, default=90, help="minutes of silence (default 90)")
    s.set_defaults(fn=cmd_reap)

    s = sub.add_parser("status", help="run overview: milestones, crews, queue, questions, refusals")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("watch", help="lead: stream events that need attention (for the Monitor tool)")
    s.add_argument("--from-now", action="store_true", help="skip events from before this watch")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("merge-loop", help="lead: run the merge queue (the only writer of trunk)")
    s.add_argument("--once", action="store_true", help="stop when the queue is empty")
    s.add_argument("--accept-trunk", action="store_true", help="keep commits that reached trunk outside the queue")
    s.add_argument("--reset-trunk", action="store_true",
                   help="put trunk back at the last landing; save stray commits on a team-stray/ branch")
    s.set_defaults(fn=cmd_merge_loop)

    s = sub.add_parser("recheck", help="lead: run milestone checks on trunk and update red/green")
    s.add_argument("milestones", nargs="*")
    s.set_defaults(fn=cmd_recheck)

    s = sub.add_parser("archive", help="lead: end the run, clean up worktrees and merged branches")
    s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("sql", help="run a read-only SQL query on the board")
    s.add_argument("query")
    s.set_defaults(fn=cmd_sql)

    s = sub.add_parser("whoami", help="show the agent bound to this worktree")
    s.set_defaults(fn=cmd_whoami)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
    except Fail as e:
        print("board: %s" % e, file=sys.stderr)
        return 1
    except sqlite3.OperationalError as e:
        print("board: database error: %s. Try again." % e, file=sys.stderr)
        return 1
    except sqlite3.IntegrityError as e:
        print("board: the board refused the change: %s." % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
