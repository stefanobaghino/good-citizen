# Hygiene rules

Apply unless the surrounding repo's contribution guide explicitly overrides.

- **No Claude attribution.** No `Co-Authored-By: Claude …` trailer in commits. No `🤖 Generated with Claude Code` line, no Claude mention, in PR descriptions.
- **Link the trigger.** When the content is driven by a specific message, ticket, incident, or doc, point at it directly — by link or plain reference (`#123`, URL) where a link isn't supported. Do not paraphrase the source.
- **Omit local-only and conversation-only material.** No references to scratch files, untracked working docs, or follow-up ideas that don't exist in a sharable source (see "Link the trigger" for what counts). The artifact is permanent; do not pollute it with ephemera.
- **Create context; favor readability.** A short motivation plus a short statement of the resulting behavior is usually enough. Be concise in service of clarity, not at its cost.
