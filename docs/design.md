# team-build design

This is the design record for the team-build skill: where each part comes from, why it works as it does, how it was tested, and its limits. The repo README covers install and use.

## Sources

The design follows two posts:

- "Safely scaling multi-agent teams with Claude Fable 5.2" (claude.dev, 2026-11-09): https://claude.dev/blog/safely-scaling-multi-agent-teams-with-fable-5-2/ . A lead and 999 helpers reproduced a Lean proof of Fermat's Last Theorem in 28.7 hours, using only a shared SQL database and git.
- "How the Claude Code team thinks about verification" (claude.dev, 2026-11-16): https://claude.dev/blog/how-the-claude-code-team-thinks-about-verification/ . It supplies the software version of the Lean checker: end-to-end checks, gates the change cannot modify, and no weakened tests.

The first post is about mathematics, where Lean gives an objective pass or fail. Software has no such checker by default, so this skill makes one: each milestone gets an acceptance check before any helper starts.

## How the article's practices map to this skill

| Article practice | Mechanism here |
|---|---|
| 60 statements, each a Lean placeholder until the checker accepts a proof | Milestones with acceptance checks (`board milestone add`), and a walking skeleton where every interface is a `STUB(...)` that fails loudly. `board preflight` proves the gate passes and every check fails before work starts. |
| Each agent has its own sandbox and its own git branch | `board hire <name>` makes the helper's worktree at `.team-build/<name>` and binds the name to it before the agent starts. The helper makes one branch per task (`<helper>/t<id>`). |
| Coordination only through a SQL database and git, no messaging | The board: SQLite in the git common dir (`.git/team-build/board.db`), WAL mode, 5 s busy timeout. Helpers never message each other. |
| Five tables: Announce, Assign, Notes, Subtasks, Landed | Tables `announce`, `assign`, `notes`, `tasks`, `landed`, plus `milestones`, `agents`, `events` and `config`. The schema is at the top of `scripts/board.py`. |
| The lead writes Announce and Assign; helpers write the rest | `announce`, `assign`, `milestone`, `merge-loop`, `recheck`, `reap`, `archive` and `config` refuse non-lead callers. Identity comes from the worktree path, so a helper cannot act as another by mistake. It is not a security boundary: every agent runs as the same user. The queue therefore reports every protected change it lands (`protected` events), and the lead's checkout refuses helper commands such as `claim`, `brief` and helper notes. |
| A separate job merges into main and refuses on conflict or fault; a grader checks main | `board merge-loop`: one landing at a time on a detached HEAD in `.team-build/trunk`. It runs the gate and every green milestone's check, then moves the trunk ref with a compare-and-swap. A crash never leaves an unchecked commit on trunk. |
| Crews per statement, each with a captain who posts a plan and splits work | `board assign <helper> <milestone> --captain`. HELPER.md gives the captain duties: verdict, plan, tasks. |
| A fixed work loop: find the plan, claim, write, check, submit, mark done, post an update | HELPER.md "The loop". `board claim` is one atomic transaction (`BEGIN IMMEDIATE`). |
| Recursive task tree; a piece can assume the pieces beneath it | `board add --parent`. The splitter codes against `STUB(T<id>)` stubs and submits its part while others fill the pieces. |
| "Do not wait for me": helpers self-organize | `board claim` falls through to any red milestone. Helpers never block on answers. |
| Verdicts were too pessimistic: 47 of 60 judged infeasible, most proved | The lead checks verdicts against measured throughput (`board status`) before it parks anything, and unparks when helpers come free. |
| "There is no reason to idle"; the lead moved helpers to hard statements | Step 5 of SKILL.md: move crews when milestones go green, keep `max_helpers` running, concentrate on the last red milestones. |
| 19 time reminders kept the lead going | The lead announces time left and progress every few hours. |
| One shared machine hit its limits at about 100 agents | `max_helpers` defaults to 6. Helpers run only targeted checks; the full gate runs only in the queue, one landing at a time. |
| Agents restarted from the last save | Agents are disposable. All state lives in the board and git. A helper exits after 4 submissions, and the lead spawns a fresh one. If the lead's session restarts, it resumes from `board status`. |
| Offline monitor reviews team actions | The `events` table logs every action. Before finish, a fresh agent reviews the whole trunk diff. |

From the verification post:

| Verification practice | Mechanism here |
|---|---|
| Prefer end-to-end tests; unit tests can pass while the feature is broken | Acceptance checks drive the product the way a user does, where possible. |
| A change cannot modify the check that judges it | The gate command lives in the board, outside the repo. Protected paths (`--protect`) are refused unless the lead submits them. |
| Cheap checks first; heavy runs in one place | Helpers run targeted tests; the merge queue runs the gate. |
| No skips, sleeps or retries to hide flaky tests | HELPER.md forbids weakening tests or checks. |
| After three failed rounds, escalate | A task refused three times goes to the lead as a `stuck` note. |

## Parts of the article this skill leaves out

The article's run also had a hardened VM per agent with no network, an LLM classifier that blocked unsafe tool calls, and an offline safety monitor. Those defend against a misaligned research model. In Claude Code, the user's permission mode (for example auto mode) does the per-call screening. This skill keeps the structural part: separate worktrees, no pushes, and one writer of trunk.

## Files

The skill is in `plugin/skills/team-build/`:

- `SKILL.md`: the lead's steps.
- `HELPER.md`: the helper's loop and the captain's duties. Each spawned helper reads it.
- `scripts/board.py`: the board and the merge queue. Python 3.9+, standard library only.
- `scripts/selftest.sh`: checks the board's rules in a temp repo. `make test` runs it; run it after every change to `board.py`.

## Where things live during a run

- Board: `<repo>/.git/team-build/board.db`, with gate logs in `.git/team-build/logs/`.
- Trunk worktree: `<repo>/.team-build/trunk`, added to `.git/info/exclude`.
- Trunk branch: `team/<name>`. It holds the result after `board archive`.
- Helper worktrees: `<repo>/.team-build/<name>`, made by `board hire`.

## Why the board makes the worktrees

The lead does not spawn helpers with the Agent tool's `isolation: "worktree"`. That option depends on the machine: with the worktrunk plugin installed, a `WorktreeCreate` hook routes it through `wt`, fails when `wt` is missing, and resolves the repo from the session's launch folder. It also names the branch after Claude Code's internal agent ID. The worktrunk skill gives the same advice for parallel sub-agents: make each worktree from the parent and pass its path in the prompt. `board hire` does this with plain `git worktree add`, so it works on every machine, and the helper's identity exists before the helper starts.

The cost is that a helper's shell does not stay in its worktree between commands. The spawn prompt and HELPER.md tell it to start every command with `cd <worktree> && `. The board refuses `claim` and `submit` from the lead's checkout, and `board status` reports when the main checkout changed branch or has edits, which is the sign that a helper worked in the wrong place.

## How it was tested

`scripts/selftest.sh` (29 checks) covers the board's rules with real processes in a temp repo:

- one name per worktree, rejoin, `claim --wait`, and six parallel claims with no double claim;
- the queue's refusals: conflict, protected path (including a rename out of one), gate failure and green regression;
- a milestone that turns green only when no `STUB(...)` marker of it remains on trunk;
- open work on green milestones, release with the work kept on its branch, and split pieces that wait for their parent;
- recovery: trunk moved outside the queue (`--reset-trunk`), a landing cut short after trunk moved, and edits in the trunk worktree that the queue must not discard;
- commit hooks that must not block the queue, a broken trunk that blames no submitter, protected landings that are reported, a stop signal that stops the running gate, liveness by lock, and `init` in a repo with no commits;
- archive clean-up.

A live trial on 2026-11-17 ran the skill as written on a small Python CLI with four milestones. Six Sonnet helpers ran in two waves, and the lead landed one protected change. All four milestones went green, with 7 landings and 0 refusals, and the main checkout stayed untouched. The trial found four problems, all fixed:

- `isolation: "worktree"` failed on a machine with the worktrunk plugin but no `wt` binary. The fix is `board hire`.
- Crew members who started before their captain had split the milestone found no work and left within seconds. The fix is `board claim --wait`.
- `B="python3 .../board.py"; $B` does not split words in zsh. The fix is to type the full board command each time; `board hire` puts the exact command in each helper's prompt.
- `board archive` left the lead's own worktree behind. Archive now cleans every merged worktree under `.team-build/`.

Two independent reviews then read the package: a correctness review of `board.py`, and a walk-through that followed SKILL.md and HELPER.md literally against the script in throwaway repos. Together they found 21 problems. The main ones were a check that an unprotected test config could skip, handoffs that lost committed work, split pieces claimable before their stubs landed, review fixes that no helper could claim, and recovery paths that either refused checked work or accepted unchecked work. Each one is fixed, and the self-test covers it. A second round of the same two reviews found 16 more, mostly in the new recovery code and at the trust boundary between lead and helpers: a repo commit hook that blocked every landing, a broken trunk that blamed every submitter, a helper that could act as the lead from the main checkout, checks fooled through `sitecustomize.py` or a nested `conftest.py`, and orphaned gates after SIGHUP. Those are fixed and covered too.

Real helpers have not yet exercised a queue refusal: they rebased before each submit, so no conflict reached the queue. Recursive splitting across several helpers has not been exercised either: the trial tasks were too small. The self-test covers the board side of both.

## Limits

- One machine. All agents must reach the board file in the repo's `.git` directory, so remote agents cannot join.
- One run per repo at a time. `board archive` ends a run.
- The trunk and helper worktrees sit inside the user's checkout (`.team-build/`). Tools that search up the directory tree can reach the main checkout from there: Node resolves `node_modules` upward, and ESLint and tsconfig lookups cascade upward. A worktree with no install of its own can then pass the gate on the main checkout's dependencies. Put the install step in the gate and in the setup command.
- A check can still be fooled by code that special-cases the check's own inputs. The defences are end-to-end checks, the stub rule (`suspect` events), and the independent review at the finish.
- The merge queue lands one submission at a time, and each landing runs the gate plus the check of every green milestone (`check_jobs` at once, default 4). Slow checks therefore slow every landing late in a run: keep both the gate and the checks fast. Checks also run side by side in the same directory, so they must not share fixed ports or temp paths.
