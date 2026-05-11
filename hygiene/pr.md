# PR descriptions

- **Lead with the experience, not the implementation.** Describe the user-visible change or the problem solved. Skip "wraps X", "centralizes Y", "adds helper Z" — the diff shows the mechanics.
- Open with the motivation (link the source), then state the resulting behavior change. No headers needed for short PRs.
- **No test plan section** unless the PR adds verification work beyond what CI runs automatically. Do not enumerate `make test`, `go vet`, `npm test`, etc.
- **No follow-up / next-steps section** unless those follow-ups exist as filed issues to link to. Conversation-only ideas (e.g. "we should also do X later") belong nowhere on the PR.
- Do not embed manual-verification checklists for the reviewer unless the change genuinely cannot be CI-verified; if you mention manual checks, do them first and report the outcome, do not leave them as an open box.
