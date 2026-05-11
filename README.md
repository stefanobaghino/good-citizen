# dv-user-sbaghino-tools

Personal Claude Code tweaks. Currently ships one thing:

## PR & Commit hygiene hooks

`PreToolUse` hooks that inject hygiene rules into Claude's context right before it runs a `git commit` or `gh pr create|edit`. The rules land at the moment of action, not at session start where they fade.

### Layout

```
hooks/hygiene.sh        # concatenates the named hygiene docs and emits them as additionalContext
hygiene/header.md       # always prepended; cross-cutting rules
hygiene/commit.md       # commit-message-only rules
hygiene/pr.md           # PR-description-only rules
```

The script takes zero or more doc names, reads `hygiene/<name>.md` for each, and concatenates them as the injected context. `hygiene/header.md` is always prepended, so don't list it explicitly; running with no args emits just the header. Which commands trigger which docs is decided by the `if` field in `settings.json`. Drop a new file in `hygiene/` and reference it by name to extend.

### Installation

1. **Clone this repo somewhere stable** (the path is referenced from your Claude settings):
   ```sh
   git clone git@github.com:gradle/dv-user-sbaghino-tools.git ~/src/dv-user-sbaghino-tools
   ```

2. **Verify `jq` is installed** (the script uses it to emit JSON safely):
   ```sh
   jq --version  # any recent version
   ```

3. **Add hook entries to `~/.claude/settings.json`.** Merge with whatever is already there — don't replace. Substitute your clone path for `<REPO>`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "if": "Bash(git commit*)",
               "command": "<REPO>/hooks/hygiene.sh commit"
             },
             {
               "type": "command",
               "if": "Bash(gh pr create*)",
               "command": "<REPO>/hooks/hygiene.sh pr"
             },
             {
               "type": "command",
               "if": "Bash(gh pr edit*)",
               "command": "<REPO>/hooks/hygiene.sh pr"
             },
             {
               "type": "command",
               "if": "Bash(gh pr comment*)",
               "command": "<REPO>/hooks/hygiene.sh"
             },
             {
               "type": "command",
               "if": "Bash(gh issue comment*)",
               "command": "<REPO>/hooks/hygiene.sh"
             }
           ]
         }
       ]
     }
   }
   ```

   The `if` fields use Claude Code's permission-rule syntax (prefix-matched against the bash command) and are the only gate — the script itself just concatenates whatever docs you name.

4. **Reload settings.** Open the `/hooks` menu inside Claude Code once (or restart). New settings files aren't watched until they exist at session start.

5. **Verify.** In a session, ask Claude to draft a commit. Before it runs `git commit`, the hygiene rules should be present in its context.

### Extending

- New rule set: drop `hygiene/<name>.md`, add a hook entry whose `if` matches the command and pass the doc name(s) to `hygiene.sh`.
- The script accepts an arbitrary number of doc-name args; they're concatenated in order with blank lines between, after the always-included `header.md`.

### Testing the script directly

```sh
./hooks/hygiene.sh commit
```

Prints a JSON envelope with `header.md` followed by `commit.md` in `hookSpecificOutput.additionalContext`. With no args (or no matching files), exits 0 silently.
