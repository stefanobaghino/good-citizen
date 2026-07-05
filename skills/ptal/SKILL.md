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
4. **Short close:** PR status if relevant (e.g. "Draft PR") + the
   reviewer ask.

Keep it to ~2–4 sentences after the link. No greeting, no
"Review please:", no 🙏/pleasantry emoji.

## Reviewer routing

- **Identify the relevant Slack channel for the main reviewer** and
  @-mention them there.
- **Fall back to `#dv`** for large or cross-team changes.
- **Pull in specialized reviewers by handle** when the change touches an
  area that needs them.
- Frame extra reviewers as **welcome for knowledge sharing**, not
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
- **Emoji:** verify `:pr-open:` (and any other custom emoji) actually
  exists in the workspace before using it — a wrong name posts as literal
  `:text:`.
- **Channel:** resolve the target channel name to its ID before sending.

### How to resolve (using this session's Slack tools)

- **User ID:** `slack_search_messages` for messages by/about the person,
  then `slack_read_user_profile` to confirm identity → use their `Uxxxx`.
- **Emoji:** `slack_search_messages` for recent channel messages that use
  the intended emoji to confirm its actual `:name:`.
- **Channel ID:** `slack_search_channels` by name.

## Process

- Posting to a channel is **outward-facing: draft by default, confirm
  with the user, then post.** Send (`slack_send_message`) only when
  explicitly told.
- Do all resolution (emoji, user ID, channel ID) **before** sending.

## Reference example

```
:pr-open: https://github.com/gradle/dv/pull/68738

Fixes the <https://builds.gradle.org/…/114676267|failing nightly Bazel cross-version build>,
which was <impact>. Makes <X> the default so <outcome>. Draft PR — <@U05QV1CVA4U> is best
placed to review, and additional reviewers are welcome for knowledge sharing.
```
