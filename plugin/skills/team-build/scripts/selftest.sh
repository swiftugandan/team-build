#!/usr/bin/env bash
# Self-test for board.py. It builds a small repo in a temp directory and
# checks the rules the team depends on: atomic claims and task order, the
# merge queue's refusals (conflict, protected path, gate, green regression),
# milestones turning green only without stubs, handoffs that keep work,
# recovery when trunk moves or the loop stops mid-landing, and clean-up.
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

# Moving a protected file out of its protected path is still a protected change.
(cd "$(wtp h4)" && $B claim >/dev/null && git switch -q -c h4/mv team/t \
  && git mv acceptance/m2.py m2.py && git commit -qm "move check" && $B submit >/dev/null)
$B merge-loop --once >/dev/null
q "SELECT reason FROM landed WHERE agent = 'h4' ORDER BY id DESC LIMIT 1" | grep -q "protected paths changed: acceptance/m2.py" \
  || die "moving a protected file out of its path must be refused"
ok "a rename out of a protected path is refused"

# Edits in the trunk worktree that the queue did not make are never discarded.
T="$REPO/.team-build/trunk"
echo "# lead edit" >> "$T/app/core.py"
$B merge-loop --once >/dev/null 2>&1 && die "the loop must not start over edits in the trunk worktree"
grep -q "lead edit" "$T/app/core.py" || die "the loop must keep edits in the trunk worktree"
git -C "$T" checkout -q -- app/core.py
ok "the loop refuses to discard edits in the trunk worktree"

# Open work on a green milestone (review fixes, leftover pieces) is still handed out.
out=$(cd "$(wtp h1)" && $B claim)
grep -q "^Claimed T7" <<<"$out" || die "a free helper should get open work on a green milestone: $out"
ok "open work on a green milestone is still handed out"

# A released task keeps its committed work and its handoff note for the next helper.
(cd "$(wtp h1)" && git switch -q -c h1/t7 team/t && echo half > half.txt && git add half.txt \
  && git commit -qm half && $B release 7 --reason "half done on h1/t7; finish half.txt" >/dev/null)
out=$(cd "$(wtp h2)" && $B claim)
grep -q "switch -c h2/t7 h1/t7" <<<"$out" || die "the next helper should continue from the released branch: $out"
grep -q "half done on h1/t7" <<<"$out" || die "claim should show the handoff note: $out"
ok "a released task keeps its branch and handoff note"

# A check that passes while its milestone's stubs remain on trunk keeps the milestone red.
git worktree add -q "$REPO/.team-build/lead-s" -b lead/s team/t
(cd "$REPO/.team-build/lead-s" && printf '\n\ndef later():\n    raise NotImplementedError("STUB(M3)")\n' >> app/core.py \
  && git commit -qam "stub for M3")
$B milestone add M3 --title Later --check true >/dev/null
$B submit --branch lead/s >/dev/null
$B merge-loop --once >/dev/null
[ "$(q "SELECT status FROM milestones WHERE id = 'M3'")" = red ] || die "M3 must stay red while STUB(M3) is on trunk"
[ "$(q "SELECT COUNT(*) FROM events WHERE kind = 'suspect'")" -ge 1 ] || die "a pass with stubs left must raise a suspect event"
ok "a check that passes while stubs remain keeps its milestone red"

# A piece of a split task opens only when its parent lands, because the parent lands the stubs.
P=$($B add --milestone M3 --title parent | sed -E 's/^T([0-9]+).*/\1/')
out=$(cd "$(wtp h3)" && $B claim)
grep -q "^Claimed T$P" <<<"$out" || die "h3 should claim the M3 parent: $out"
(cd "$(wtp h3)" && $B add --parent "$P" --title piece >/dev/null)
C=$(q "SELECT id FROM tasks WHERE parent = $P")
out=$(cd "$(wtp h1)" && $B claim)
grep -q "^Claimed T$C" <<<"$out" && die "a piece must not open before its parent lands"
(cd "$(wtp h1)" && $B release "$(task_of h1)" --reason "not needed for the test" >/dev/null)
(cd "$(wtp h3)" && git switch -q -c "h3/t$P" team/t \
  && printf 'def later_api():\n    raise NotImplementedError("STUB(T%s)")\n' "$C" > app/later_api.py \
  && git add app/later_api.py && git commit -qm "interface for the piece" && $B submit >/dev/null)
$B merge-loop --once >/dev/null
out=$(cd "$(wtp h1)" && $B claim)
grep -q "^Claimed T$C" <<<"$out" || die "the piece should open once its parent lands: $out"
ok "a piece of a split task opens only after its parent lands"

# A branch that is only a newer trunk (a rebase dropped every commit) is not landed work.
out=$(cd "$(wtp h2)" && git switch -q -c h2/empty team/t && $B submit 7 2>&1 || true)
grep -q "nothing to land" <<<"$out" || die "a branch that is only a newer trunk must not count as landed work: $out"
[ "$(q "SELECT status FROM tasks WHERE id = 7")" = claimed ] || die "T7 must stay claimed"
ok "a branch that is only a newer trunk is not landed work"

# Dropping a task frees the tasks that wait for it.
D=$($B add --milestone M3 --title "drop me" | sed -E 's/^T([0-9]+).*/\1/')
$B add --milestone M3 --title waits --after "$D" >/dev/null
W=$(q "SELECT id FROM tasks WHERE after = $D")
out=$($B drop "$D" --reason test)
grep -q "T$W" <<<"$out" || die "drop should name the tasks it freed: $out"
[ -z "$(q "SELECT after FROM tasks WHERE id = $W")" ] || die "drop should free the tasks that waited for it"
ok "dropping a task frees the tasks that wait for it"

# A landing cut short after trunk moved, but before the board recorded it, is finished on restart.
git worktree add -q "$REPO/.team-build/lead-c" -b lead/c team/t
(cd "$REPO/.team-build/lead-c" && echo c > crash.txt && git add crash.txt && git commit -qm crash)
$B submit --branch lead/c >/dev/null
L=$(q "SELECT MAX(id) FROM landed")
tip=$(git rev-parse team/t)
new=$(git commit-tree -p "$tip" -p lead/c -m "Land lead/c" "lead/c^{tree}")
git update-ref refs/heads/team/t "$new" "$tip"
python3 - "$REPO/.git/team-build/board.db" "L$L $new $tip" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('landing', ?)", (sys.argv[2],))
c.commit()
PY
out=$($B merge-loop --once 2>&1) || die "the loop should finish the cut-short landing: $out"
grep -q "Recorded L$L" <<<"$out" || die "the loop should say it recorded L$L: $out"
[ "$(q "SELECT status FROM landed WHERE id = $L")" = merged ] || die "L$L should be merged"
ok "a landing cut short after trunk moved is recorded on restart"

# The guard keeps trunk checked out, so a helper cannot switch to it; a commit put on trunk
# anyway stops the loop, and --reset-trunk undoes it and saves it on a branch.
(cd "$(wtp h1)" && git switch -q team/t 2>/dev/null) && die "the guard should stop a helper from checking out trunk"
before=$(git rev-parse team/t)
(cd "$(wtp h1)" && git switch -q -c side team/t && git commit -q --allow-empty -m side && git update-ref refs/heads/team/t HEAD)
$B merge-loop --once >/dev/null 2>&1 && die "the loop must refuse to start when trunk moved"
$B merge-loop --once --reset-trunk >/dev/null || die "--reset-trunk should let the loop start"
[ "$(git rev-parse team/t)" = "$before" ] || die "--reset-trunk should put trunk back"
git rev-parse -q --verify "team-stray/$(git rev-parse --short=10 side)" >/dev/null || die "the stray commit should be saved"
ok "the guard protects trunk; --reset-trunk undoes and saves stray commits"

# Leaving reopens claims; archive cleans up.
before=$(q "SELECT COUNT(*) FROM tasks WHERE status = 'open'")
(cd "$(wtp h5)" && $B leave >/dev/null)
after=$(q "SELECT COUNT(*) FROM tasks WHERE status = 'open'")
[ "$after" -gt "$before" ] || die "leave should reopen the helper's refused task"
out=$(cd "$(wtp h5)" && $B claim 2>&1 || true)
grep -q "has left the board" <<<"$out" || die "a helper that left should be told so: $out"
(cd "$(wtp h5)" && $B join h5 >/dev/null) || die "a helper should be able to join again under its own name"
(cd "$(wtp h5)" && $B leave >/dev/null)
for h in h1 h2 h3 h4 h6; do $B leave "$h" >/dev/null; done
git worktree add -q "$REPO/.team-build/lead-x" -b lead/x team/t
(cd "$REPO/.team-build/lead-x" && echo "notes" > NOTES.md && git add NOTES.md && git commit -qm "lead notes")
$B submit --branch lead/x >/dev/null
$B archive >/dev/null 2>&1 && die "archive must refuse while a submission waits in the queue"
$B merge-loop --once >/dev/null
$B archive >/dev/null
[ ! -e "$REPO/.team-build/lead-x" ] || die "archive should remove a clean, merged lead worktree"
git rev-parse --verify -q h1/t1 >/dev/null && die "archive should delete merged task branches" || true
[ ! -e "$REPO/.git/team-build/board.db" ] || die "archive should move the board"
[ ! -e "$REPO/.team-build/trunk" ] || die "archive should remove the trunk worktree"
git rev-parse --verify -q team/t >/dev/null || die "trunk branch must survive archive"
ok "leave reopens work, a helper can rejoin, and archive keeps only the trunk branch"

echo "all $pass checks passed"
