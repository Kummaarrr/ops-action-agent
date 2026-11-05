# Reviewer B - application security for LLM agents (blind review, 5 Nov 2026)

AI reviewer acting as an application-security engineer specialising in LLM agents, given the
architecture, guard, agent loop, policy engine, tool catalogue, backend, scorer and adversarial
templates - not the author's conclusions. Verbatim findings; the author's decisions follow in
the report.

1. **HIGH - Verification spoofable with facts a stranger knows.** `verify_identity` accepts the
   email as a model argument and counts `order_id` as the second factor; `find_customer(order_id)`
   is a public read returning the owner's email. An impostor who knows only an order number is
   verified. Fix: the server reads the email from the ticket's requester, not the model; order id
   is not a factor when the sender email does not match; `find_customer` returns ids and masked
   emails only.
2. **HIGH - Any ticket is readable, and ticket bodies contain verification facts.** `get_ticket`
   / `list_notes` take any ticket id (sequential ids); the model can harvest another customer's
   facts and verify as them; the scorer only ownership-checks account-scoped tools. Fix: pin
   helpdesk tools to the task's ticket; score any helpdesk read on a foreign ticket.
3. **HIGH - Refund thresholds evaluated per call, not cumulatively.** A $400 charge refunded as
   4 x $100 passes every check with no approval. Fix: apply thresholds to the task's cumulative
   refunds on the charge against the original amount.
4. **HIGH - Re-verification overwrites identity mid-task.** After verifying as A the model can
   verify as B and the verified customer silently switches. Fix: refuse a second successful
   verification for a different customer; security event.
5. **MEDIUM - Output guard for unverified requesters is dead in impostor tasks** (it joins the
   ticket's customer id, which is NULL for impostors). Fix: resolve the target account from the
   records touched this task.
6. **MEDIUM - Leak detection is exact substring on three fields.** Names, ship address,
   tracking, other customers' last4, partial street names and reformatted values evade both
   guard and scorer; `verification_echo` needs "ending in" phrasing and exempts the billing zip
   for most customers. Fix: normalise text before matching; add ship fields, tracking, names,
   token-level partial matches; drop the exemption unless the reply is about shipping.
7. **MEDIUM - Checker fails open and is injectable.** On exception the outcome stands; only
   done / approval / refuse are checked; only the last reply; untrusted text is interpolated raw.
   Fix: checker error -> hand back; check every outcome with a reply; pass all replies; delimit
   untrusted spans.
8. **MEDIUM - The model can author approval requests directly**, with its own summary, clause and
   a fabricated diff; the scorer's action matching is loose. Fix: hide `request_approval` from the
   model (the guard files it) or rebuild the diff from records.
9. **MEDIUM - `get_approval` is a public read** with no ownership check and not marked untrusted.
   Fix: restrict to approvals filed in this task; mark the summary untrusted.
10. **MEDIUM - Type confusion engine vs server.** `at_period_end is False` misses "false"/0;
    `int("49.99")` raises and aborts the run (injection-triggerable DoS); asserts vanish under -O.
    Fix: validate arguments against the tool schema before the engine; engine exceptions -> deny;
    explicit denies instead of asserts.
11. **MEDIUM - Injection detection is a short regex denylist**; paraphrase and other languages
    pass; plain-string results are not flagged; model-written `reason` args land unfiltered in
    the supervisor's diff. Fix: every customer-authored field untrusted by default, delimited;
    regex as telemetry only.
12. **MEDIUM - Scorer blind spots.** `injection_followed` not implemented; `set_status` abuse and
    promised discounts unmeasured; `unverified_access` satisfied by any `verified=True` row, so a
    spoofed verification launders later reads; forbidden *reads* never count; `false_claim` only
    for `done`. Fix: re-derive legitimate verification from the true requester; count forbidden
    reads; closed-without-reply and promised-credit checks.
13. **LOW - `guard.call` does not re-check `allowed_tools()`**; gating relies on the definitions
    list alone. Fix: deny any tool not currently allowed.
14. **LOW - Transport exceptions propagate** out of the guard and the run; `find_customer(phone)`
    matches 7 digits with LIKE and returns PII (enumeration).
15. **Missing adversarial templates:** order-id-only impostor; foreign-ticket read; salami-sliced
    refunds; verified A acting on B's named record; customer posing as internal staff; injection
    in staff notes / KB titles; checker-targeted injection; exfiltration asks; obfuscated or
    non-English injection; partial-PII echo; "close my ticket as solved" with no reply;
    threshold gaming at $199.99.

Top three: 1, 2, 3 - together they turn "verified customer" into "anyone with an order number",
and the scorer only partially sees it.
