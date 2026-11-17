#!/usr/bin/env bash
# Self-test for board.py. It builds a small repo in a temp directory and
# checks the rules the team depends on: atomic claims, the merge queue's
# refusals (conflict, protected path, gate, green regression, trunk moved
# outside the queue), milestones turning green, and clean-up.
# Run it after every change to board.py:  bash selftest.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
B="python3 $HERE/board.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REPO="$WORK/repo"
pass=0

ok() { pass=$((pass + 1)); echo "ok $pass - $1"; }
die() { echo "FAIL: $1" >&2; exit 1; }
q() { (cd "$REPO" && $B sql "$1" | tail -n +2); }

# --- a small project with two milestones -----------------------------------
mkdir -p "$REPO" && cd "$REPO"
git init -q -b main
git config user.name selftest && git config user.email selftest@example.invalid
mkdir -p app tests acceptance
printf '__pycache__/\n' > .gitignore
touch app/__init__.py
cat > app/core.py <<'EOF'
def greet(name):
    raise NotImplementedError("STUB(M1)")


def shout(text):
    raise NotImplementedError("STUB(M2)")
EOF
printf 'commands:\n' > app/registry.txt
cat > tests/test_smoke.py <<'EOF'
import unittest


class Smoke(unittest.TestCase):
    def test_import(self):
        import app.core  # noqa: F401
EOF
printf 'from app.core import greet\nassert greet("x") == "hello x"\n' > acceptance/m1.py
printf 'from app.core import shout\nassert shout("x") == "X!"\n' > acceptance/m2.py
git add -A && git commit -qm skeleton

$B init --name t --gate "python3 -m unittest discover -s tests -q" --protect "acceptance/*" >/dev/null
$B milestone add M1 --title Greet --check "PYTHONPATH=. python3 acceptance/m1.py" --priority 10 >/dev/null
$B milestone add M2 --title Shout --check "PYTHONPATH=. python3 acceptance/m2.py" --priority 20 >/dev/null
$B preflight >/dev/null || die "preflight should pass on the skeleton"
ok "init and preflight pass on a skeleton with failing checks"

# --- helpers: five hired by the lead, one joins from a worktree it made -------
wtp() { if [ "$1" = h6 ]; then echo "$WORK/h6"; else echo "$REPO/.team-build/$1"; fi; }
for h in h1 h2 h3 h4 h5; do
  out=$($B hire "$h"); grep -q "join $h" <<<"$out" || die "hire should print a spawn prompt for $h"
  (cd "$(wtp "$h")" && $B join "$h" >/dev/null)
done
git worktree add -q "$(wtp h6)" -b wt-h6 main
(cd "$(wtp h6)" && $B join h6 >/dev/null)
$B hire h1 >/dev/null 2>&1 && die "a name must not be hired twice"
(cd "$(wtp h1)" && $B join h2 >/dev/null 2>&1) && die "a helper must not join under another helper's name"
(cd "$REPO" && $B join h7 >/dev/null 2>&1) && die "the main checkout must not join as a helper"
ok "hire and join bind one name to one worktree"

# A free helper waits for the captain's tasks instead of leaving.
(cd "$(wtp h1)" && $B claim --wait 1 > "$WORK/wait.out") &
python3 -c "import time; time.sleep(3)"
$B add --milestone M1 --title "task 0" >/dev/null
wait
grep -q "Claimed T1" "$WORK/wait.out" || die "claim --wait should pick up a task added while it waits: $(cat "$WORK/wait.out")"
ok "claim --wait picks up work that appears while it waits"

for i in 1 2 3 4 5 6 7 8; do $B add --milestone M1 --title "task $i" >/dev/null; done
out=$($B claim 2>&1 || true); grep -q "lead does not claim" <<<"$out" || die "claim from the lead's checkout must be refused"
ok "the lead's checkout cannot claim tasks"

# --- six helpers claim at the same time ---------------------------------------
for h in h1 h2 h3 h4 h5 h6; do
  (cd "$(wtp "$h")" && $B claim | head -1 > "$WORK/$h.claim") &
done
wait
claimed=$(cat "$WORK"/h?.claim | sed -E 's/.*T([0-9]+).*/\1/' | sort -u | wc -l | tr -d ' ')
[ "$claimed" = 6 ] || die "expected 6 distinct claims, got $claimed: $(cat "$WORK"/h?.claim)"
[ "$(q "SELECT COUNT(*) FROM tasks WHERE status = 'claimed'")" = 6 ] || die "board should show 6 claimed tasks"
ok "parallel claims never give one task to two helpers"

task_of() { q "SELECT id FROM tasks WHERE owner = '$1' AND status IN ('claimed', 'refused')"; }
start() { (cd "$(wtp "$1")" && git switch -q -c "$1/t$(task_of "$1")" team/t); }

# h1 does M1, h2 does M2 and registers it, h3 registers on the same line.
start h1; (cd "$(wtp h1)" && python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("app/core.py")
p.write_text(p.read_text().replace('raise NotImplementedError("STUB(M1)")', 'return "hello " + name'))
EOF
git -C "$(wtp h1)" commit -qam "greet" && $B submit >/dev/null)
start h2; (cd "$(wtp h2)" && python3 - <<'EOF'
import pathlib
p = pathlib.Path("app/core.py")
p.write_text(p.read_text().replace('raise NotImplementedError("STUB(M2)")', 'return text.upper() + "!"'))
EOF
printf 'commands:\nshout\n' > app/registry.txt && git commit -qam "shout" && $B submit >/dev/null)
start h3; (cd "$(wtp h3)" && printf 'commands:\nwhisper\n' > app/registry.txt && git commit -qam "whisper" && $B submit >/dev/null)
# h4 edits a protected acceptance check.
start h4; (cd "$(wtp h4)" && printf 'assert True\n' > acceptance/m1.py && git commit -qam "weaken" && $B submit >/dev/null)
# h5 breaks the gate.
start h5; (cd "$(wtp h5)" && printf 'import unittest\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail("broken")\n' > tests/test_bad.py && git add -A && git commit -qm "bad" && $B submit >/dev/null)

(cd "$(wtp h6)" && $B submit >/dev/null 2>&1) && die "submit with nothing new must fail"
ok "submit refuses an empty submission"

$B merge-loop --once >/dev/null
st() { q "SELECT status FROM landed WHERE agent = '$1' ORDER BY id DESC LIMIT 1"; }
[ "$(st h1)" = merged ] || die "h1 should land"
[ "$(st h2)" = merged ] || die "h2 should land"
[ "$(st h3)" = refused ] || die "h3 should be refused (conflict)"
q "SELECT reason FROM landed WHERE agent = 'h3'" | grep -q "merge conflict in: app/registry.txt" || die "h3 reason should name the conflicting file"
[ "$(st h4)" = refused ] || die "h4 should be refused (protected)"
q "SELECT reason FROM landed WHERE agent = 'h4'" | grep -q "protected paths changed: acceptance/m1.py" || die "h4 reason should name the protected path"
[ "$(st h5)" = refused ] || die "h5 should be refused (gate)"
q "SELECT reason FROM landed WHERE agent = 'h5'" | grep -q "gate failed" || die "h5 reason should say the gate failed"
ok "queue lands good work and refuses conflict, protected path and gate failure"

[ "$(q "SELECT status FROM milestones WHERE id = 'M1'")" = green ] || die "M1 should be green"
[ "$(q "SELECT status FROM milestones WHERE id = 'M2'")" = red ] || die "M2 has no task of its own, so it stays red until recheck"
$B recheck M2 >/dev/null || die "recheck M2 should pass"
[ "$(q "SELECT status FROM milestones WHERE id = 'M2'")" = green ] || die "recheck should mark M2 green"
ok "milestones turn green from the queue and from recheck"

# A refused task comes back to its owner first.
(cd "$(wtp h3)" && $B claim | head -1) | grep -q "T$(q "SELECT task FROM landed WHERE agent = 'h3'")" || die "h3 should get its refused task back"
ok "claim returns the helper's refused task first"

# h3 rebases, resolves, resubmits; then a change that breaks green M1 is refused.
(cd "$(wtp h3)" && { git rebase -q team/t >/dev/null 2>&1 || true; } \
  && printf 'commands:\nshout\nwhisper\n' > app/registry.txt && git add app/registry.txt \
  && GIT_EDITOR=true git rebase --continue >/dev/null && $B submit >/dev/null)
(cd "$(wtp h6)" && git switch -q -c "h6/t$(task_of h6)" team/t && python3 - <<'EOF'
import pathlib
p = pathlib.Path("app/core.py")
p.write_text(p.read_text().replace('return "hello " + name', 'return "hi " + name'))
EOF
git commit -qam "regress" && $B submit >/dev/null)
$B merge-loop --once >/dev/null
[ "$(st h3)" = merged ] || die "h3's rebased resubmission should land"
[ "$(st h6)" = refused ] || die "h6 should be refused for breaking green M1"
q "SELECT reason FROM landed WHERE agent = 'h6'" | grep -q "milestone M1 was green" || die "h6 reason should name M1"
ok "resubmission after rebase lands; a regression of a green milestone is refused"

# Trunk moved outside the queue: the loop stops and lands nothing.
(cd "$(wtp h1)" && git switch -q -c side team/t && git commit -q --allow-empty -m side && git update-ref refs/heads/team/t HEAD)
$B merge-loop --once >/dev/null 2>&1 && die "the loop must refuse to start when trunk moved"
$B merge-loop --once --accept-trunk >/dev/null || die "--accept-trunk should let the loop start"
ok "the loop detects commits to trunk that it did not land"

# Leaving reopens claims; archive cleans up.
before=$(q "SELECT COUNT(*) FROM tasks WHERE status = 'open'")
(cd "$(wtp h5)" && $B leave >/dev/null)
after=$(q "SELECT COUNT(*) FROM tasks WHERE status = 'open'")
[ "$after" -gt "$before" ] || die "leave should reopen the helper's refused task"
for h in h1 h2 h3 h4 h6; do $B leave "$h" >/dev/null; done
git worktree add -q "$REPO/.team-build/lead-x" -b lead/x team/t
$B archive >/dev/null
[ ! -e "$REPO/.team-build/lead-x" ] || die "archive should remove a clean, merged lead worktree"
git rev-parse --verify -q h1/t1 >/dev/null && die "archive should delete merged task branches" || true
[ ! -e "$REPO/.git/team-build/board.db" ] || die "archive should move the board"
[ ! -e "$REPO/.team-build/trunk" ] || die "archive should remove the trunk worktree"
git rev-parse --verify -q team/t >/dev/null || die "trunk branch must survive archive"
ok "leave reopens work; archive removes worktrees and keeps the trunk branch"

echo "all $pass checks passed"
