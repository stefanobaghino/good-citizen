# dv-user-sbaghino-tools

Personal Claude Code tweaks.

## Bash policy hook

A single `PreToolUse` hook on `Bash` that polices how specific CLI tools are
used, in two domains:

- **Artifact hygiene** — validates the `git commit`, `gh pr create`, or
  `gh issue create|comment|edit` Claude is about to run and denies violations
  with the exact fix, at the moment of action. Judgment-only guidance still gets
  injected, but as a ~113-token primer, once per session.
- **Git history safety** — refuses force pushes and unsigned commits, and asks
  before rewriting history that is already published.

Both used to be separate hooks (`hooks/hygiene-dispatch.py` here and a personal
`git-history-guard.py`). They parsed the same command string twice, and the
safety-critical one carried the weaker parser: `shlex.split`, which cannot see
into heredocs and *allowed* any command it failed to parse. One hook, one parse,
one rule registry — see [Why one hook](#why-one-hook).

### Why it replaced the old prose-injection hooks

The previous setup registered five `PreToolUse` hooks, each gated with an
`if: "Bash(git commit*)"`-style filter and each injecting a prose rule file via
`additionalContext`. It had three compounding problems:

1. **Upstream `if`-filter false positives**
   ([anthropics/claude-code#75245](https://github.com/anthropics/claude-code/issues/75245)) —
   differently-patterned `if` gates fire together on commands that match none of
   them. On recent CLIs, command substitution `$(…)`, `${var}` expansion, and even
   a bare `for` loop trip every gate at once, injecting the full rule battery with a
   0% true-positive rate.
2. **Permission bypass as a side effect** — the hooks answered `allow` +
   `additionalContext`, so a false positive silently auto-approved an unrelated
   Bash command.
3. **Injected prose is still just advice** — it gets skipped or compacted away in
   long sessions, the same failure mode as CLAUDE.md guidance.

The fix for all three: stop asking the model to remember rules, and check the
artifact instead.

### Why one hook

Claude Code runs **all matching hooks in parallel** and folds their answers with
a fixed precedence — `deny` > `defer` > `ask` > `allow`, order-independent — so
two hooks were never a safety mechanism, just two copies of the same parser.
Merging them buys three things:

- **The safety rules inherit the better parser.** `git push --force 'unbalanced`
  used to make `shlex.split` raise, and the old guard returned — allowing an
  unchecked force push. It is now denied.
- **Normalized command matching.** Rules match on a command *path* with git's
  global options skipped, so `git -C /elsewhere commit` is policed too. The old
  hygiene matcher compared raw leading words and missed that form.
- **One denial reports everything.** Both hooks could fire on `git commit`, but
  only one reason ever reached the transcript. A single denial now lists the
  signing problem and the message problem together.

Criticality is explicit, which the split hooks could not express:

| Tier | Rules | On a rule's own error |
|---|---|---|
| `CRITICAL` | git history safety, commit signing | **deny** (fail closed) |
| `ADVISORY` | artifact hygiene | skipped (fail open) |

Each rule runs in its own `try`, so a bug in a hygiene rule cannot take down the
force-push rule — and a crash in the safety rules no longer silently removes
push protection, which is how the standalone guard behaved.

### How it works

`hooks/bash-policy.py` does its own quote/heredoc-aware command matching
(sidestepping the upstream `if`-filter bug entirely), then:

- **Matches** the watched commands only at the start of a top-level command
  segment. The same text inside a string or `$(…)` never matches. Everything else
  gets **no output at all** — zero context cost, normal permission flow (never a
  blanket `allow`).
- **Extracts** the drafted message/body from `-m`/`--body`/`--title`,
  `--file`/`--body-file` (including `-F -` heredocs and the
  `--message "$(cat <<'EOF' … EOF)"` pattern), then runs the rule tiers below.
  Violations → one `deny` listing every finding and its concrete fix.
- **Ack-and-retry (Tier C)** — intent-dependent findings (a bare `@name` or `#123`
  that would ping/auto-link) deny once; re-running the *identical* command passes.
- **Loop guard** — after 2 denies for the same command family with no shrinking of
  the violation set, the third attempt is allowed with findings downgraded to a
  warning. The hook can slow a bad artifact down, never wedge a session.
- **Fail open** — an error outside the rule tiers (bad stdin, import failure)
  exits 0 silently, and an ADVISORY rule's own error is skipped. Only documented
  rule violations and CRITICAL rule failures may deny.
- **Primer** — the genuinely judgment-based rules (tone, structure, what belongs in
  a PR body) are injected from `hygiene/primer-*.md`, at most once per session per
  category. Re-armed when the marker is older than 4 h or the transcript gained a
  `compact_boundary` after it.

### The rules — git history safety (CRITICAL)

| Command | Decision | Why |
|---|---|---|
| `git push --force` / `-f` / `+refspec` | **deny** | can discard commits pushed by others |
| `git push --force-with-lease` | **ask** | still rewrites published history; hints `--force-if-includes` when missing |
| `git push` with an unsigned outgoing commit | **deny** | would break CI |
| `git commit --no-gpg-sign` | **deny** | unsigned by request |
| `git commit` with signing not configured / disabled | **deny** | would be unsigned |
| `git commit --amend` / `git rebase` when HEAD is on a remote | **ask** | rewrites published history; the default is a follow-up commit |

Signature checking is one `git log --format='%H %G?'` call over at most 100
outgoing commits, replacing the old `rev-list` plus one `cat-file` per commit —
up to 101 subprocesses, which is what made that hook need a wall-clock timeout
in the first place. Only `%G?` == `N` counts as unsigned: a signature that
exists but cannot be verified locally (no public key, `%G?` == `E`) is signed,
and treating it otherwise would block every push on a machine without the
signer's key. git runs with the payload's `cwd`, so `git -C <other repo>` is
inspected in the repo it targets rather than wherever the hook process sits.

### The rules — artifact hygiene (ADVISORY)

| Tier | Meaning | Examples |
|---|---|---|
| **A** — deterministic, always deny | Mechanically decidable | commit subject > 70 chars or trailing period; `Co-Authored-By`/`Signed-off-by`/`Acked-by`/`Reviewed-by` trailers; `-s`/`--signoff`; AI-attribution lines; multi-issue or negated closing keywords; missing blank line after `</summary>`; open `- [ ]` checkboxes in PR bodies; inline `-m`/`--body` with shell-hazard chars or > 200 chars; local-only paths (`/tmp`, `/Users`, …) |
| **B** — high-confidence heuristics | Deny, tuned for near-zero false positives | hard-wrapped paragraphs landing a Markdown token at column 0; PR bodies enumerating branch commits; "Follow-ups" sections with no linked issues; test-plan sections that only restate CI commands |
| **C** — intent-dependent | Deny once, identical retry passes | bare `@mentions`; bare `#N` with no referencing intent nearby; ambiguous hex tokens that auto-link as commit SHAs |

Code blocks and inline code spans are excluded from prose checks throughout.

**Known limitation.** A watched command wrapped in command substitution — e.g.
`URL=$(gh issue create …)` to capture the created URL — is *not* validated: the
matcher deliberately never looks inside `$(…)`, because doing so is exactly what
made the old `if`-filter misfire on unrelated `$(…)` commands. This is an accepted
false negative (the design never denies a command it isn't sure about). Run the
watched command at the top level if you want it checked.

### Layout

```
hooks/bash-policy.py           # the hook entry point (single registration)
hooks/bashpolicy/shell.py      # command splitting/tokenizing, shared by all rules
hooks/bashpolicy/policy.py     # rule registry, criticality tiers, decision fold
hooks/bashpolicy/githist.py    # git history safety rules (CRITICAL)
hooks/bashpolicy/hygiene.py    # commit/PR/issue hygiene rules (ADVISORY)
hooks/bashpolicy/state.py      # markers, config, session primers
hooks/test-bash-policy.py      # unit suite — no live session, no cost
hooks/verify-hygiene-hooks.sh  # post-upgrade re-verification harness (live session)
hygiene/primer-shared.md       # judgment primer, always included
hygiene/primer-commit.md       # + when a commit is in the command
hygiene/primer-pr.md           # + for gh pr create
hygiene/primer-issue.md        # + for gh issue create|comment|edit
```

Adding coverage for another CLI tool means one entry in the registry: a
`@rule(name, tier, prefixes)` function that receives the already-parsed matching
invocations and returns findings.

The hook resolves the primers relative to itself, so the repo stays
self-contained. Machine-local state (primer/ack/loop-guard markers) lives in
`~/.claude/hooks/.state/` and self-cleans after 7 days — `$BASH_POLICY_HOME`
overrides that root, which is how the test suite stays out of real state.
Python 3 stdlib only; no dependencies, no `jq`.

### Installation

1. **Clone this repo somewhere stable** (the path is referenced from your Claude
   settings):
   ```sh
   git clone git@github.com:gradle/dv-user-sbaghino-tools.git ~/src/dv-user-sbaghino-tools
   ```

2. **Register one hook in `~/.claude/settings.json`.** Merge with whatever is
   already there — don't replace. Substitute your clone path for `<REPO>`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "command": "<REPO>/hooks/bash-policy.py",
               "timeout": 30,
               "statusMessage": "Checking Bash command policy"
             }
           ]
         }
       ]
     }
   }
   ```

   No `if` gate — the hook matches commands itself. Set the `timeout`
   explicitly: the default for `command` hooks is **600s**, so a wedged hook
   would otherwise sit for ten minutes before the harness kills it.

   This single entry replaces both predecessors. If you were running
   `hooks/hygiene-dispatch.py` and/or a personal `git-history-guard.py`, remove
   those entries **in the same edit** — leaving one wired alongside this hook
   just double-parses every command and double-reports every finding.

3. **Reload settings.** Open the `/hooks` menu inside Claude Code once (or restart).

4. **Verify.** Ask Claude to draft a commit whose message carries a
   `Co-Authored-By: Claude` trailer; the hook should deny it with the fix. A clean
   commit should pass, with the primer attached once per session.

### Configuration (optional)

For repos whose contribution guide *requires* `Signed-off-by` (e.g. DCO projects),
create `~/.claude/hooks/hygiene-config.json`:

```json
{ "signoff_cwd_substrings": ["/src/linux"] }
```

Any cwd containing one of these fragments suppresses the trailer/`-s` denial there.
Absent file = defaults (deny all trailers).

### Tests

```sh
python3 hooks/test-bash-policy.py
```

88 assertions, no live session and no cost: it feeds the hook `PreToolUse`
payloads on stdin and asserts on the JSON it prints, with state and primers
redirected away from `~/.claude` and throwaway git repos as fixtures. Covers
every decision either predecessor hook made, plus what the merge introduces —
the precedence fold, the criticality tiers, and per-rule isolation (an advisory
rule that raises must not mask a critical one).

One case deserves its own note, because it is the failure mode the merge could
plausibly have introduced: **matching a command must not imply `allow`.** The
hygiene rules assert `allow` deliberately — that is what keeps a validated
`git commit` from falling through to a permission prompt — while the history
rules stay silent when they pass. If `allow` were emitted merely because some
rule matched, folding the history rules in would have started auto-approving
every `git push`. `allow` is opt-in per rule, and there is a test for it.

### Post-upgrade verification

`hooks/verify-hygiene-hooks.sh` re-checks the behavior after a CLI upgrade: it
drives a throwaway headless session (`claude -p`, Haiku,
`--dangerously-skip-permissions`) through eight negative controls and one real
commit, then greps the transcript and prints PASS/FAIL.

```sh
./hooks/verify-hygiene-hooks.sh
```

PASS = zero injections/denials on the negative controls, primer exactly once on the
positive control. If upstream ever fixes the `if` filter, the gates can return as a
cheap pre-filter in front of the hook, keeping in-script matching as defense
in depth.

## Skills

### `/ptal`

Composes a "please take a look" PR review-request message for Slack — problem-first,
routed to the right reviewer, with the same hygiene rules the hook enforces (no
counts, no AI attribution, link the trigger, lead with the experience). Drafts by
default and posts only when told.

```
skills/ptal/SKILL.md
```

Install by symlinking into your skills dir, then reload (`/skills` or restart):

```sh
ln -s "$PWD/skills/ptal" ~/.claude/skills/ptal
```
