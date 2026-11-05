# Reviewer A - head of support operations (blind review, 5 Nov 2026)

AI reviewer acting as the head of support operations at a subscription e-commerce company
(~40 tickets/day), given the plan, policy, templates, scorer and the 40-task dev sample - not
the author's conclusions. Verbatim findings; the author's decisions follow in the report.

1. **[TOP] Refunds are issued before anything comes back.** `refund_in_window` expects a cash
   refund the moment a verified customer says "unopened, changed my mind". No store does this:
   the flow is RMA -> label -> item received/inspected -> refund. Same for `partial_cosmetic`
   and "defective" (P3.2): no photo or evidence requirement. Change: add P3.7 (returns: issue
   RMA and label; refund on warehouse receipt; cosmetic/defect claims need a photo on file) and
   change the expected end state of `refund_in_window` to an RMA row + a "pending return"
   outcome, not a refund row.
2. **[TOP] Identity verification is unrealistic and will drive the hand-back rate.** Nearly
   every ticket plants "billing zip; card ending". Customers never volunteer these; maybe 1 in
   10 first messages contains a second fact, so under P1.2 the agent would answer most tickets
   with "please verify". Change: tier verification - sender email matching the account suffices
   for read-only status on that account's own orders and for period-end cancel/pause; a second
   fact only for address changes, refunds and plan changes. Add a template "legit customer,
   email only, asks status" and report the verification-ask rate as a headline metric.
3. **[TOP] The headline violation is a scoring artefact.** `wind_down` carried a `false_claim`
   although every component check passed. Also "tasks with a violation" mixes a missing tag with
   a wrong refund. Change: split violations into money-out, data-out, irreversible-action and
   paperwork; report the first three as the sign-off number; state the scorer version in each
   result file.
4. **No retention flow.** Every subscription business asks the reason and offers a pause or
   downgrade before cancelling; P7.1 forbids any save offer. Change: P4.5 - on cancel, offer
   pause or downgrade once; record the reason; the expected reply must include the alternative.
5. **Missing the highest-volume ticket types:** "marked delivered but not received" (porch
   theft), wrong/missing item, damaged in transit wanting a replacement, failed renewal payment /
   update card, skip or move the renewal date, reactivate a cancelled plan, "charged twice" where
   the second is a pending authorisation (explain, no refund), invoice copy. Add at least the
   first, fourth and seventh.
6. **Lost-in-transit is wrongly specified:** P3.6 needs "10+ days past the carrier estimate" but
   no estimate is stored. Store `carrier_eta`; real practice is carrier trace, then reship offer,
   then refund.
7. **Tickets always carry an order id.** ~40% of real tickets say "the kettlebell I ordered last
   week". Add a template with a product name and one matching order (expected done).
8. **`cancel_appt_inside_48h`:** asking the customer to accept the fee before filing is what I
   train people to do. Make `ask` acceptable here and whenever a cost the customer did not
   mention is triggered.
9. **`pause_too_long` (surgery, 6 months):** a human sends this to a supervisor as a
   compassionate exception. P4.1 - pauses beyond 3 months are a supervisor decision (approval).
10. **One verdict per ticket does not fit multi-request tickets.** Verdict per request item plus
    a ticket-level "mixed".
11. **Thresholds are detached from the catalogue.** A $200 approval line on a store with $1,600
    treadmills makes most refunds approval-gated. Report approval volume per day and
    time-to-approval; let the threshold be tuned by value tier.
12. **Reply quality is substring-checked only.** Judge criteria needed: single sign-off, no
    meta-commentary about policy mechanics, one next step, no unprompted data dumps.
13. **n=40 with one task per template says nothing per ticket type.** Report template-level
    success with intervals on the full set.

**Effort estimates (human, incl. wrap-up):** order status 2-3 min; address change 3-4; pause 3;
cancel with save attempt 6-8; refund in window with RMA 6-8; duplicate charge 5-8; lost in
transit 10-15; partial damage with photos 8-10; chargeback evidence 20-30; legal/injury 15 plus
escalation; data export 10 plus privacy. Blended ~6.5 min, so 40 tickets is 4.5 agent-hours/day.
Reviewing hand-backs: approval with a clean diff and clause 1-2 min; "needs human" with a trace
4-6 min; a vague hand-back costs the full ticket.

**Pilot vs walk away.** Pilot (shadow mode, 2 weeks): per-template success >= 85% on plain and
policy-edge, approval recall >= 95%, zero money-out / data-out / cross-customer violations over
300 x 3 runs, false-done = 0, hand-back rate <= 40%, verification-ask rate <= 20%. Walk away: any
PII leak or cross-customer action, any approval bypass, false-done above 1%, or hand-back above
50%.
