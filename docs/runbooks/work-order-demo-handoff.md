# Work Order demo / manual handoff runbook

## 1. Purpose

This runbook pins the **repeatable demo script and verification checklist** for the
Work Order step of the PureIron sales demo. It exists so that "Work Order" is shown
as a **manual handoff off the back of alarm + Agent troubleshooting**, and is never
demoed or described as an implemented backend feature.

Concretely it ties together the parts that *are* real backend features:

- alarm raise → ack → action-note → journal history (`/api/alarms/*`, alarm journal)
- Agent troubleshooting explanation grounded on read-only context (tag / alarm / history / health)

and then continues, **by hand**, into whatever work-order / CMMS / paper process the
customer already uses. PureIron v1 stops at the human decision; it does not create,
store, approve, or dispatch a work order.

Scope guardrails and status are in [mvp-gap](../wiki/reports/mvp-gap.md) (flow #8, P2 item).
The demo narrative and the automated-vs-manual split are canonical in
[testing](../wiki/sot/testing.md) (§5, AC-18). Product framing is in
[product](../wiki/sot/product.md) (Work Order Demo) and
[first-contract](../wiki/gtm/first-contract.md) (데모 시나리오).

## 2. What is real vs. what is a manual handoff

| Demo step | Backing | Real backend feature? |
|---|---|---|
| Alarm raised on threshold breach | `alarms/engine.py`, AC-5 | Yes |
| Operator acknowledges with attribution | `POST /api/alarms/{name}/ack`, AC-6 | Yes |
| Action note captured | `POST /api/alarms/{name}/notes` | Yes |
| Action / ack / note history reviewable | `GET /api/alarms/journal` (alarm journal) | Yes |
| Agent explains cause candidates + remediation | agent troubleshooting tools, AC-18c | Yes (needs LLM key) |
| **Work order created / approved / dispatched** | **none** | **No — manual handoff** |

There is no Work Order DB schema, API, router, or approval workflow in v1, and adding
one is explicitly out of scope. The MES module (`backend/src/pureiron/mes/`) is
recipe/plan/task, **not** work order. Do not present Work Order as something PureIron
stores or approves.

## 3. Non-goals

Do not use this runbook to introduce any of these:

- Work Order DB schema / API / router implementation
- MES / CMMS integration
- automatic work-order generation or approval workflow
- a new mobile Work Order UI
- any write/control or auto-actuation

If a customer wants a real work-order feature, record it as a separate post-PoC scope
item; do not build it inside the demo.

## 4. Repeatable demo script

Run this after the alarm + Agent part of the AC-18 demo narrative
([testing](../wiki/sot/testing.md) §5). Roughly 3-4 minutes.

1. **Trigger the alarm.** Use the Simulator scenario (or a live approved tag) to
   breach a setpoint so an alarm goes `Active/Unacked`. Confirm it shows in the
   dashboard / mobile alarm view (`GET /api/alarms/active`).
2. **Acknowledge with attribution.** Ack the alarm as the operator
   (`POST /api/alarms/{name}/ack`). Confirm it moves `Active/Unacked → Active/Acked`
   and the operator identity is recorded.
3. **Ask the Agent.** Ask the troubleshooting Agent for cause candidates and a
   remediation scenario. Confirm the answer is grounded on read-only context (tag /
   alarm / history / device health) and that no control action is auto-executed —
   a risky write stops at pending approval (AC-18b).
4. **Capture the action note.** Record the decided action as an action note on the
   alarm (`POST /api/alarms/{name}/notes`). This is the durable "what we did / will
   do" record.
5. **Manual work-order handoff.** State out loud that PureIron now hands off to the
   customer's existing work-order / CMMS / maintenance process **by hand** — e.g.
   copy the alarm + action note into their work-order sheet or ticket. Emphasize:
   *PureIron records the cause and the decision; issuing and approving the work order
   stays with the customer's people/process in v1.*
6. **Show the trail.** Open the alarm journal (`GET /api/alarms/journal`) and show the
   ack → note → history trail that the manual work order references. This is the
   "원인·조치·이력이 남는다" payoff ([first-contract](../wiki/gtm/first-contract.md)
   대표 고객 페인).

## 5. Verification checklist

Confirm before every customer demo. These keep the story honest and repeatable.

- [ ] Alarm can be raised, acknowledged (with operator attribution), and annotated
      with an action note.
- [ ] `GET /api/alarms/journal` returns the ack + note history for that alarm.
- [ ] Agent explains cause candidates + remediation from read-only context, and any
      risky write is blocked at pending approval (no auto-actuation).
- [ ] The Work Order step is narrated as a **manual handoff**, not as a PureIron
      feature that creates/stores/approves a work order.
- [ ] No PureIron screen is presented as a work-order module (the MES recipe/plan/task
      screens are not relabeled as Work Order).
- [ ] Any customer request for a real work-order feature is written down as a
      separate post-PoC scope item, not built during the demo.

## 6. When this can be promoted

If a backend Work Order API is ever added (out of v1 scope), promote the manual step
here into the AC-18 automated table in [testing](../wiki/sot/testing.md) §5 and add a
focused contract test alongside `test_work_order_demo_handoff_contract.py`. Until then,
Work Order stays a documented manual handoff.
