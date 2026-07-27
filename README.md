# dv-user-sbaghino-tools

Personal Claude Code tweaks.

## PR / commit / issue hygiene hook

A single `PreToolUse` hook on `Bash` that validates the artifact Claude is about
to create — a `git commit`, `gh pr create`, or `gh issue create|comment|edit` —
and denies violations with the exact fix, at the moment of action. Judgment-only
guidance still gets injected, but as a ~113-token primer, once per session.

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

### How it works

`hooks/hygiene-dispatch.py` does its own quote/heredoc-aware command matching
(sidestepping the upstream bug entirely), then:

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
- **Fail open** — any dispatcher *error* exits 0 silently. Only documented rule
  violations may deny.
- **Primer** — the genuinely judgment-based rules (tone, structure, what belongs in
  a PR body) are injected from `hygiene/primer-*.md`, at most once per session per
  category. Re-armed when the marker is older than 4 h or the transcript gained a
  `compact_boundary` after it.

### The rules

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
hooks/hygiene-dispatch.py      # the validate-and-deny dispatcher (single hook)
hooks/verify-hygiene-hooks.sh  # post-upgrade re-verification harness
hygiene/primer-shared.md       # judgment primer, always included
hygiene/primer-commit.md       # + when a commit is in the command
hygiene/primer-pr.md           # + for gh pr create
hygiene/primer-issue.md        # + for gh issue create|comment|edit
```

The dispatcher resolves the primers relative to itself, so the repo stays
self-contained. Machine-local state (primer/ack/loop-guard markers) lives in
`~/.claude/hooks/.state/` and self-cleans after 7 days. Python 3 stdlib only; no
dependencies, no `jq`.

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
             { "type": "command", "command": "<REPO>/hooks/hygiene-dispatch.py" }
           ]
         }
       ]
     }
   }
   ```

   No `if` gate — the dispatcher matches commands itself.

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
cheap pre-filter in front of the dispatcher, keeping in-script matching as defense
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
