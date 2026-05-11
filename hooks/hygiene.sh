#!/usr/bin/env bash
# Inject hygiene rules as PreToolUse additionalContext.
#
# Usage (in the `command` field of a PreToolUse hook):
#   /path/to/hygiene.sh [doc1 doc2 ...]
#
# Reads <docN>.md from the sibling `hygiene/` directory and emits the
# concatenated contents as `hookSpecificOutput.additionalContext`. The
# sibling `hygiene/header.md` is always prepended if it exists, so you
# don't need to list it; with no extra args the header alone is emitted.
# Filter which commands trigger the hook via the `if` field in settings.json.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hygiene_dir="${script_dir}/../hygiene"

context=""
if [ -f "${hygiene_dir}/header.md" ]; then
  context="$(cat "${hygiene_dir}/header.md")"
fi
for doc in "$@"; do
  file="${hygiene_dir}/${doc}.md"
  if [ -f "$file" ]; then
    if [ -n "$context" ]; then
      context+=$'\n\n'
    fi
    context+="$(cat "$file")"
  fi
done

if [ -z "$context" ]; then
  exit 0
fi

jq -n --arg ctx "$context" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    additionalContext: $ctx
  }
}'
