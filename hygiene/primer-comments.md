Comments — this diff adds some; apply these before committing.

Three tests, in order:
1. Recovery — could a competent reader get this from the code, the tests, a type signature, git history, or a linked issue? If yes, delete it.
2. Staleness — if someone changes the code below, does the comment become wrong? Then it describes mechanism. Name the constraint or the decision instead; those survive the edit.
3. Subject — does the sentence's subject name a decision, a constraint, or an external fact, or does it narrate execution ("so X, then Y, then Z fails")? Narration fails: name the constraint and stop.

Write these: why not the obvious alternative; load-bearing details other code depends on invisibly; how a constant was chosen and what breaks if it changes; hard-won findings, even when you don't know why they work — say how you arrived at it; external references and where you diverged from them; TODOs with enough detail to act on in six months, since a bare `TODO` is worse than nothing. Length is not the test — a four-line rationale that passes all three tests earns its space, a one-line narration doesn't.

Delete these: restating the code; code history (how it used to work, what was tried); causal chains — state the constraint and let the reader infer the rest; hedged qualifications and stacked em-dash asides; commented-out code, always.

Register: technical prose, not explanation-to-a-student. Plain words over coinages — if you invented the term in this comment, it doesn't belong. Check that every identifier the comment names still exists and means what the comment says.

Boy scout: when the diff already touches a comment's own lines, bring that comment up to these rules in the same change. Trim freely, delete cautiously — a comment recording a hard-won finding or a load-bearing constraint may encode work you cannot reconstruct, so remove it only when you can verify it is obsolete. Don't reach past the comments the diff already touches.
