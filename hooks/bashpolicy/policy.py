"""Rule registry, criticality tiers, and the decision fold.

Two things this buys over running one hook per concern:

  * Rules are isolated. Each runs in its own try/except, so a bug in a
    hygiene rule cannot take down the force-push rule that used to live
    in a separate process.
  * Criticality is explicit. An ADVISORY rule that errors is skipped
    (fail open, as a style gate should). A CRITICAL rule that errors
    denies (fail closed) — the old `git-history-guard.py` failed open on
    its own bugs, silently removing push protection.

The fold mirrors Claude Code's own precedence for multiple PreToolUse
hooks (deny > defer > ask > allow, order-independent), so collapsing two
hooks into one does not change which decision wins.
"""

CRITICAL = "critical"
ADVISORY = "advisory"

# Mirrors the harness ladder; `defer` is never produced here, only ranked.
PRECEDENCE = {"allow": 0, "ask": 1, "defer": 2, "deny": 3}
BLOCKING = ("deny", "ask", "defer")


class Finding:
    """One rule's verdict on one invocation."""

    __slots__ = ("decision", "rule", "msg", "tier", "context", "data")

    def __init__(self, decision, rule, msg="", tier=ADVISORY, context=None, **data):
        if decision not in PRECEDENCE:
            raise ValueError(f"unknown decision {decision!r}")
        self.decision = decision
        self.rule = rule
        self.msg = msg
        self.tier = tier
        self.context = context
        self.data = data


class Verdict:
    """What the hook will emit."""

    __slots__ = ("decision", "reason", "context", "matched", "findings")

    def __init__(self, decision=None, reason=None, context=None, matched=False,
                 findings=()):
        self.decision = decision
        self.reason = reason
        self.context = context
        self.matched = matched
        self.findings = list(findings)


class Rule:
    __slots__ = ("name", "tier", "prefixes", "fn")

    def __init__(self, name, tier, prefixes, fn):
        self.name = name
        self.tier = tier
        self.prefixes = [tuple(p) for p in prefixes]
        self.fn = fn

    def select(self, invocations):
        """The invocations this rule applies to."""
        return [inv for inv in invocations
                if any(inv.matches(p) for p in self.prefixes)]


REGISTRY = []


def rule(name, tier, prefixes):
    """Register a rule.

    The function receives every matching invocation at once — the
    hygiene rules need the whole batch to aggregate findings and apply
    their ack-and-retry and loop-guard state, while the git-history
    rules simply iterate.
    """
    def wrap(fn):
        REGISTRY.append(Rule(name, tier, prefixes, fn))
        return fn
    return wrap


def watched_prefixes():
    """Every command prefix any rule cares about."""
    return [p for r in REGISTRY for p in r.prefixes]


def evaluate(invocations, ctx, registry=None):
    """Run every applicable rule and fold the findings into a Verdict."""
    rules = REGISTRY if registry is None else registry
    findings = []
    matched = False
    for r in rules:
        selected = r.select(invocations)
        if not selected:
            continue
        matched = True
        try:
            produced = r.fn(selected, ctx) or []
        except Exception as exc:
            if r.tier == CRITICAL:
                findings.append(Finding(
                    "deny", r.name, tier=CRITICAL,
                    msg=(f"Safety rule `{r.name}` could not be evaluated "
                         f"({type(exc).__name__}: {exc}). Refusing rather than "
                         "letting the command through unchecked."),
                ))
            # An advisory rule that errors is skipped: a style gate must
            # never block work on its own bug.
            continue
        findings.extend(produced)

    blocking = [f for f in findings if f.decision in BLOCKING]
    if blocking:
        decision = max((f.decision for f in blocking), key=lambda d: PRECEDENCE[d])
        return Verdict(decision=decision, reason=render(blocking), matched=True,
                       findings=findings)

    # `allow` is opt-in, never a consequence of merely matching. The
    # hygiene rules assert it on a validated artifact (that is what keeps
    # `git commit` from prompting); the git-history rules stay silent on
    # a pass, so folding them in must not start auto-approving pushes.
    allows = [f for f in findings if f.decision == "allow"]
    if allows:
        contexts = [f.context for f in allows if f.context]
        return Verdict(decision="allow", matched=True, findings=findings,
                       context="\n\n".join(contexts) or None)
    return Verdict(matched=matched, findings=findings)


def render(blocking):
    """One message listing every blocking finding.

    Both hooks could fire on `git commit`; separately, only one reason
    ever reached the transcript. Collected here, a single denial can
    report the signing problem and the message problem together.
    """
    if len(blocking) == 1:
        return blocking[0].msg
    lines = [f"{len(blocking)} policy finding(s) — fix all and re-run:"]
    for i, f in enumerate(blocking, 1):
        lines.append(f"{i}. [{f.rule}] {f.msg}")
    return "\n".join(lines)
