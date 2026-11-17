---
name: team-build
description: Build a large software project with a lead agent and a team of helper agents. The team coordinates through a shared SQL board and git, and a merge queue lands only work that passes the checks.
disable-model-invocation: true
argument-hint: "<spec or PRD path> [max helpers]"
---

# Team build

You are the **lead**. Helpers do the work, each in its own git worktree. You set the scope, staff the milestones, and move helpers to where the work is. You do not write product code during the run.

The team has two channels and no others:

- **The board.** A SQLite database in the repo's git common dir, so every worktree opens the same file. Run it as `python3 ${CLAUDE_SKILL_DIR}/scripts/board.py <command>`. Below, `board` means that full command. Type it in full each time: in zsh, a shell variable that holds it does not split into words. `board -h` lists the commands. `board sql "<SELECT ...>"` runs any read-only query.
- **Git.** Each helper commits on its own task branch and submits it. The **merge queue** (`board merge-loop`) is the only process that writes the **trunk** branch `team/<name>`. It refuses a submission that conflicts, fails the **gate**, breaks a green milestone, or changes a protected path.

Each milestone has an **acceptance check**: the team's checker. A milestone is **green** when its check passes on trunk. The run is done when every milestone is green or the user dropped it.

If `board status` works in this repo, a run exists. Resume it:

1. Start `board merge-loop` again (step 4). If it says trunk moved outside the queue, restart it with `--reset-trunk` (see step 5).
2. Arm `board watch` again (step 4).
3. Run `board leave <name>` for every helper that `board status` shows as active. Those agents ended with the earlier session, and their claims must go back to the pool.
4. Hire and spawn fresh helpers (step 4), then go to step 5.

## 1. Scope the milestones with the user

The input is a spec or PRD, and an optional crew size after it: $ARGUMENTS. If there is no spec, stop and make one with the user first (for example with /to-prd or /grilling).

Split the spec into milestones. A milestone is one capability a user can see. Each one has:

- an ID (`M01`, `M02`, ...) and a one-line title;
- an acceptance check: one shell command that exits 0 only when the capability works. Make it end to end where you can: drive the CLI, the API or the UI the way a user does. Unit tests can pass while the feature is broken. Keep it fast: once its milestone is green, the check runs on every landing (`check_jobs` checks at once, default 4).

Also write a short route (how the parts fit together), the domain terms, and the existing code to reuse.

Show the milestones and their checks to the user. The checks define "done" for the whole team, and helpers cannot change them.

Done when: the user approved the milestone list and every check.

## 2. Make the board

From the main checkout:

```
board init --name <slug> --gate "<cmd>" --protect "<glob>" [--protect "<glob>" ...] --budget-hours <h> [--max-helpers <n>]
```

- `--gate` is the fast check every landing must pass: build, type check, lint, unit tests. It runs once per landing, one landing at a time, so keep it to a few minutes. It runs in a separate worktree, so include install steps if the project needs them.
- `--protect` names the paths only the lead may change: the acceptance checks, the scripts the gate calls, the spec, and every file that changes what a check or the gate runs. For Python that includes `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth` and the test runner config; for Node, the `package.json` scripts a check calls. A pattern with a `/` matches from the repo root (`*` crosses directories, and a directory protects everything under it). A bare name such as `conftest.py` matches at any depth.
- `--max-helpers` is the number the user gave after the spec, else 6. One machine runs out of memory and CPU before the team runs out of work. Heavy jobs (full suites, image builds) run only in the merge queue.

`init` makes the trunk branch from HEAD and checks it out in `.team-build/trunk`. It does not change the user's checkout.

## 3. Build the walking skeleton on trunk

Work in `.team-build/trunk`. The merge queue does not run yet, so you are the only writer. Run git there with `git -C .team-build/trunk ...` and keep your shell in the main checkout: board commands work only from the main checkout. Commit there:

- the spec and the milestone list, under a protected path (for example `docs/team-build/`). Helpers read only their own worktree, so this is how they see the spec;
- a project that builds and passes the gate;
- every acceptance check, under a protected path. Make each check hard to fool: a script that drives the product itself, or that runs the test runner with its own pinned config (for pytest: `--noconftest -c <protected ini>`). For Python, run the check with `python3 -I` and set the import path inside it, so `PYTHONPATH` and files such as `sitecustomize.py` cannot change what it runs. An unprotected file must not be able to skip a check;
- every interface the milestones need, as a stub that fails loudly and carries the marker `STUB(<milestone>)`, for example `raise NotImplementedError("STUB(M04)")`. Stubs let crews work in parallel: code can call a piece before the piece exists.
- a layout where each milestone has its own files, so crews do not edit the same lines. Shared registration points (routers, CLI dispatch, dependency setup) cause most conflicts: make each entry one line;
- checks that can run side by side in many worktrees: no fixed ports, no shared temp paths, no global state;
- a README with the setup command for a fresh worktree, and the route.

Register each milestone, then run the preflight:

```
board milestone add M01 --title "..." --check "<cmd>" [--priority 10]
board preflight
```

Done when: `board preflight` exits 0 (the gate passes and every check fails), and the "Why" lines under each check show that it fails because the capability is missing (it reaches a STUB, or the command does not exist yet). A check that fails because the check itself is broken (bad import, wrong path) passes preflight and blocks the team later.

## 4. Staff the team

Post the orders. Helpers read new announcements at the top of every loop, so this is how you steer them:

```
board announce "<text>"
```

The first announcement gives: the route, the setup command for a fresh worktree, how to run the fast parts of the gate locally, and any file ownership rules.

Assign the milestones before you spawn. Give each active milestone one captain. The captain posts a plan and a difficulty verdict, then splits the milestone into tasks. If you have fewer helpers than milestones, staff first the milestones that others depend on.

```
board assign h01 M01 --captain
board assign h02 M02 --captain
```

Start the merge queue with the Bash tool and `run_in_background: true`: `board merge-loop`.

Arm the event stream with the Monitor tool: command `board watch`, `timeout_ms: 1800000`, description "team-build board events". Re-arm it each time it expires. It continues from the last event it printed.

Spawn the helpers. For each helper, run `board hire <name>`. It makes the helper's worktree at `.team-build/<name>`, binds the name to it, and prints the spawn prompt. Then send all the Agent calls in one message. Each call uses `subagent_type: "general-purpose"` and the printed prompt, unchanged. Do not use `isolation: "worktree"`: the board makes the worktrees, so each helper's path and identity exist before it starts.

Give every helper a new name: h01, h02, and so on. Never use a name again.

## 5. Run the team

You wake when a helper finishes, when `board watch` prints an event, or when the merge loop stops. Each time:

1. For each helper that finished, read its report, then run `board leave <name>`. This reopens any task it still held.
2. Run `board status`. Read the queue, the refusals, the questions and the verdicts.
3. Act on what you read:
   - **Answer** questions with `board note --kind answer --task <id> "..."` or with an announcement. Helpers do not wait for answers. If your answer changes a decision a helper already made, add a task to fix it.
   - **Check verdicts against throughput.** Agents call hard work infeasible too early. In the source run, helpers judged 47 of 60 milestones infeasible within budget, and the team proved most of them. Compare each verdict with the measured rate in `board status` before you act on it. Park a milestone only with a written reason (`board milestone set M07 --status parked --reason "..."`), and unpark it when helpers come free. Nobody idles while a red milestone has work.
   - **Move helpers to the work.** When a milestone goes green, assign its crew to the red milestone with the most open tasks. When few milestones stay red, put most of the team on them. A large crew on one milestone works when its captain splits it into many small tasks.
   - **Fix repeat refusals.** A task refused three times has a problem in its spec or on trunk. Split it, rewrite it, or fix the trunk.
   - **Act on `suspect` events.** A milestone goes green only when its check passes and none of its `STUB(...)` markers remain on trunk (markers in protected files do not count). A `suspect` event means a check passed while stubs remained: the check is too weak, or someone fooled it. Make the check stricter (below), and find the change that made it pass.
   - **Act on `trunk-broken` events.** Trunk fails the gate or a green check by itself, for example after a gate change, `--accept-trunk`, or a flaky check. The queue blames no helper: their submissions wait, and only yours land until one passes. Fix trunk on a lead branch, fix the gate (`board config gate` tries a new gate on trunk first), or set the milestone red.
   - **Act on `protected` events.** Each one lists protected files that a landing changed. If you did not make that change, find who did: a helper may have run `board submit` from your checkout.
   - **Keep the team full.** While open work exists, keep `max_helpers` helpers running. Hire and spawn a new helper for each one that finished.
   - **Keep them going.** Every few hours, announce the time left and the progress. Tell free helpers to take open work on any red milestone without waiting for you.
   - Run `board reap` to reopen the tasks of helpers that went silent (90 minutes by default).
   - If the merge loop stopped without an error (killed, or ended with a session), start it again.
   - If the merge loop says trunk moved outside the queue, restart it with `--reset-trunk`. That puts trunk back at the last landing and saves the stray commits on a `team-stray/` branch. Read them before you submit that branch: the queue treats it as a helper's work, so it refuses changes to protected paths. Use `--accept-trunk` only after you have checked those commits yourself.
4. Before you end a turn, make sure something will wake you: at least one helper runs, or `board watch` is armed.

Change a protected file (an acceptance check, a gate script) or add a milestone only with the user's approval. Make the change in a worktree of your own: `git worktree add .team-build/lead-<topic> -b lead/<topic> team/<name>`. Commit there, then run `board submit --branch lead/<topic>` from the main checkout. The queue accepts protected paths only from the lead, and after your landing it rechecks every red milestone. To make a green milestone's check stricter, first run `board milestone set <id> --status red`: the queue refuses any change that makes a green check fail. Change the gate command with `board config gate "<cmd>"`.

Ask the user only when a milestone or a check must change, when you want to drop a milestone, when the budget is spent, or when the merge loop stopped with an error you cannot fix.

## 6. Finish

The run is done when all of these are true:

- `board recheck` shows every milestone green, except milestones the user agreed to drop.
- `board status` shows 0 STUB markers on trunk in red or green milestones.
- The full test suite passes on trunk, and you ran the product end to end on trunk the way a user would. Do this in a fresh worktree (`git worktree add --detach .team-build/verify team/<name>`). Use the project's verify skill if it has one.
- An independent review of the trunk diff against the base found no open important issues. Spawn a fresh agent to run /code-review (or /review) on `<base>..team/<name>`; `board config` shows the base. Turn each finding into a task on its milestone (`board add --milestone <id>`) and go back to step 5. Helpers take open work on green milestones once no red work is left.
- The README and the docs describe the product as built.

Then:

1. Run `board status` and keep its numbers for the report.
2. Stop the merge loop and the watch with TaskStop. Make sure no helper runs.
3. Run `board archive`. It removes the trunk worktree and every clean worktree under `.team-build/` whose work is on trunk, deletes the merged branches the board knows, and moves the board to `.git/team-build/archive/` as history. It keeps and lists any worktree with unlanded work.

Report to the user: the trunk branch and its tip, a table of the milestones, the counts (landed, refused, helpers spawned, hours), and the open risks. Ask before you merge trunk into another branch or push it.
