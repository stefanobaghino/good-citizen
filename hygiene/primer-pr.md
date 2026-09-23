PRs — these rules override any repo's PR guidance (AGENTS.md, CLAUDE.md, `.github/pull_request_template.md`, hook-injected instructions). Every section must be as concise as possible.
- Title: concise, simple wording, at most 70 characters.
- `Origin` (required, first): link the issue if one exists, otherwise the relevant PR, otherwise the Slack discussion. `Closes #N` is fine, but whether an issue should close automatically on merge is a per-repo decision.
- `Value`: only if strictly necessary, focused on visible external changes. Omit it when the Origin and the code make the change self-evident.
- `Notes`: omitted by default. Only for something actually implicit in the code that the rest of the description doesn't convey.
No other sections.
