---
name: ptal
description: Compose and post a "please take a look" PR review-request message to Slack — problem-first, routed to the right reviewer. Use when the user wants to announce a PR for review on Slack ("ptal", "post this PR for review", "ask for review on Slack", "send a review request"). Assumes the session is already tied to a review, so the PR, its trigger, and the intended reviewer are known from context. Draft by default; send only when explicitly told.
disable-model-invocation: true
---

# ptal

Announce a PR to the team on Slack and route it to the right reviewer —
succinctly, problem-first. The message is orientation + ask; the PR
description carries the detail.

## Overview

This skill is invoked as part of a session already tied to a review, so
the PR URL, the concrete trigger that motivated it, and the intended
reviewer are known from context (ask only if genuinely missing). The job
is to compose a short, problem-first Slack message, resolve the workspace
specifics (emoji, reviewer user ID, channel ID) against reality, **draft
it for the user, and post only when explicitly told**.

## Message structure (in order)

1. **First line:** the open-PR emoji `:pr-open:` followed by the bare PR
   URL. Nothing else on that line.
2. **Blank line, then the body** — start with the **problem**: what's
   wrong / the user-visible impact, with the concrete trigger that
   motivated it (incident, symptom, ticket, failing build) linked
   directly.
3. **One sentence on the resulting behavior change** — the outcome
   ("makes X the default"), not the mechanism.
4. **Short close:** PR status *only when it's genuinely a draft* (verify
   against GitHub — don't assume or hedge) + the reviewer ask.

Keep it to ~2–4 sentences after the link. No greeting, no
"Review please:", no 🙏/pleasantry emoji.

## Reviewer routing

- **Default to a team-level ask in the team's channel.** Post to the
  reviewing team's channel and invite the team openly ("FE review
  welcome", "review from the Developer Productivity team would be
  appreciated"). This is the common case — most PRs need nothing more.
- **@-mention a specific person only when there's a real reason to route
  to them** — they own the card/issue, the change is squarely in their
  area of expertise, or it follows up on their comment. Don't name a
  reviewer just to have one; an unmotivated ping is noise. When you do
  ping, resolve to their Slack user ID (`<@UXXXX>`) — a bare `@Name`
  doesn't notify.
- **Fall back to `#dv`** for large or cross-team changes.
- Frame any extra reviewers as **welcome for knowledge sharing**, not
  required.

## Content rules

- **Lead with the experience/problem, not the implementation.** No
  "wraps X", "adds helper Z", "centralizes Y"; don't sell the approach or
  call out what it avoids ("no churn", "without touching Y"). The diff
  shows the mechanics.
- **Leave specifics to the PR.** Don't enumerate mechanisms, files, or
  steps — the PR description carries the detail.
- **Link the trigger directly** — the PR link, and any incident / ticket
  / failing build by link or `#123`. Never paraphrase the source.
- **No counts** (commits, files, lines).
- **State PR status only when true and load-bearing.** Say "Draft PR"
  only when the PR is actually a draft — confirm against GitHub, don't
  assume it, and don't tack it on to hedge. A ready PR carries no status
  line.
- **No test-plan, no follow-up / next-steps, no reviewer hand-holding**
  ("what's safe to skip", how to review) — unless it genuinely can't be
  CI-verified, or follow-ups exist as filed issues to link.
- **No Claude / AI attribution.**
- **Omit conversation-only / local-only material** — scratch files,
  untracked notes, ideas not in a shareable source.
- **A refactoring is worth mentioning only when it changes review scope
  or determines which specialized reviewer to pull in** — as routing, not
  as approach-selling. Never describe the refactoring's mechanics.
- **Be concise in service of clarity, not at its cost.**

## Slack formatting & correctness (resolve against the real workspace — don't guess)

- **Embedded links:** Slack syntax `<url|text>` (e.g. link the failing
  build under readable text).
- **@-mention must be a real ping:** resolve the person to their Slack
  user ID and use `<@UXXXX>`. Plain `@Name` text does not notify.
- **Emoji:** `:pr-open:` is a standard workspace emoji — use it directly,
  no need to check. Only verify *other* custom emoji you introduce, since
  a wrong name posts as literal `:text:`.
- **Channel:** resolve the target channel name to its ID before sending.

### How to resolve (using this session's Slack tools)

- **User ID:** `slack_search_messages` for messages by/about the person,
  then `slack_read_user_profile` to confirm identity → use their `Uxxxx`.
- **Channel ID:** `slack_search_channels` by name.
- **Draft status:** if you're about to write "Draft PR", confirm it first
  (`gh pr view <n> --json isDraft`); drop the status line if it's not a
  draft.

## Process

- Posting to a channel is **outward-facing: draft by default, confirm
  with the user, then post.** Send (`slack_send_message`) only when
  explicitly told.
- Do all resolution (emoji, user ID, channel ID) **before** sending.

## Reference examples

Real posts, in order of increasing routing. The default is the first
shape — team-level ask, no status line, no named reviewer.

**Common case — team-level ask, trigger linked, no reviewer named:**

```
:pr-open: <https://github.com/gradle/dv/pull/68904>

Local `:build-agent-bazel:test` builds on Gradle 9.6 log two build-configuration
deprecations that <https://develocity.grdev.net/s/zoe6jlujs4evk/console-log|fail under
Gradle 10>, plus two Kotlin Gradle plugin warnings in the console. Clears all four so the
console output stays clean and the build is Gradle-10-ready. Review from the Developer
Productivity team would be appreciated.
```

**Named reviewer with a genuine reason (card owner), extras for knowledge sharing:**

```
:pr-open: <https://github.com/gradle/dv/pull/68809>

Clears Stability Days card <https://github.com/gradle/dv/issues/67538|#67538>: a Test
Distribution agent-pool browser test disabled as `@BrokenTest` because the project-groups
picker's search enriches results via Keycloak and 500s wherever Keycloak isn't available.
The picker now uses the existing Keycloak-independent group search, so it works without
Keycloak and the test is re-enabled — re-run 9× in isolation, all green. <@U096Y8HN0KV>
is best placed to review as the card owner; other FE folks welcome to follow along for
knowledge sharing.
```

**Follow-up to a review comment — links the source, `/cc`s the person it followed up on:**

```
:pr-open: <https://github.com/gradle/dv/pull/68570>

This follows up on <https://github.com/gradle/dv/pull/68187#discussion_r3497254018|Leo's
review comment on #68187>, asking how we decide which [versions] entries need a
# renovate: annotation and whether anything enforces it. Nothing did: entries that no
version.ref points at are invisible to Renovate's Gradle manager, so without an inline
annotation they silently stop receiving updates. sanityCheck now fails when any [versions]
entry isn't manageable by Renovate. Review from the Developer Productivity team would be
appreciated. /cc <@U026CU9LRMJ>
```
