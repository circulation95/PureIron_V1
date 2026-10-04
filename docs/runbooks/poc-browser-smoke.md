# PoC browser smoke runbook

## 1. Purpose

This runbook fixes the **manual browser smoke path** that must be executed before a customer-site read-only PoC review or final demo. It is not a Playwright/e2e automation spec. The goal is to leave customer-safe evidence that the operator-facing path works:

1. login
2. dashboard and alarm visibility
3. alarm acknowledge
4. action note entry
5. alarm history review
6. trend/history review artifact
7. read-only write/control block

Run this after the stack is deployed with the [on-prem read-only PoC runbook](on-prem-read-only-poc.md). Keep `PUREIRON_POC_READ_ONLY_MODE=true` for customer-site PoCs.

## 2. Non-goals

Do not use this smoke to expand v1 scope:

- no write/control enablement
- no automatic action execution
- no backend Work Order feature validation
- no customer PLC write test
- no secret/API key capture in screenshots
- no claim that this replaces DB/driver field validation

If the customer asks for automatic work order creation, PLC write/control, TLS automation, or offline bundle packaging, record it as separate post-PoC scope.

## 3. Preconditions

- The PoC stack is running and reachable from the operator browser.
- `/api/health` reports `poc_read_only_mode: true`.
- The operator has a valid login.
- At least one alarm scenario can be observed. This may come from a real read-only device route, approved simulator/demo seed, or a pre-existing active alarm in the PoC dataset.
- If trend export is checked, at least one history-enabled tag has collected data in the selected review window.

Record before starting:

| Item | Value |
|---|---|
| Date/time |  |
| Host/UI URL |  |
| Browser/version |  |
| Operator account/role |  |
| Data source | real read-only device / simulator / seeded data |
| `poc_read_only_mode` | true / false |

Stop if `poc_read_only_mode` is not true for a customer-site read-only PoC.

## 4. Evidence directory

Create one local evidence directory outside the repository, for example:

```bash
mkdir -p ~/pureiron-poc-evidence/$(date +%Y%m%d)-browser-smoke
```

Recommended artifact names:

| Artifact | Filename |
|---|---|
| Health JSON | `00-health.json` |
| Login/dashboard screenshot | `01-dashboard.png` |
| Active alarm before ack | `02-active-alarm-before-ack.png` |
| Acked alarm state | `03-active-alarm-acked.png` |
| Action note saved | `04-action-note-saved.png` |
| Alarm history row | `05-alarm-history.png` |
| Trend/history screen | `06-trend-history.png` |
| Trend CSV export | `07-trend-export.csv` |
| Read-only block evidence | `08-read-only-block.png` |
| Smoke notes | `smoke-notes.md` |

Do not commit customer evidence. Keep artifacts in the customer's approved storage or a local handoff folder.

## 5. Sanitization rules

Before sharing screenshots or CSV files outside the customer-approved channel, redact:

- passwords, JWTs, API keys, LLM provider keys, connection strings
- public IPs, VPN names, firewall details, usernames if the customer requires anonymity
- PLC credentials or proprietary tag names if not approved for sharing
- browser autofill suggestions and OS notifications
- customer production quantities if the PoC agreement treats them as confidential

It is acceptable to keep tag names and alarm names unredacted only when the customer/SI explicitly approves them as PoC evidence.

## 6. Smoke checklist

### 6.1 Health and read-only mode

From the PoC host or operator workstation:

```bash
curl -f http://<poc-host>:<port>/api/health > 00-health.json
```

Pass criteria:

- request succeeds
- response contains `poc_read_only_mode: true`
- frontend is reachable from the operator LAN

### 6.2 Login and dashboard visibility

1. Open `http://<poc-host>:<port>`.
2. Log in as the intended operator/reviewer role.
3. Confirm the main dashboard loads without a blocking error.
4. Capture `01-dashboard.png`.

Pass criteria:

- authenticated UI loads
- dashboard/operator navigation is visible
- no secret or token is visible in the screenshot

### 6.3 Active alarm before ack

1. Navigate to the alarm screen.
2. Confirm at least one active alarm is visible.
3. Capture `02-active-alarm-before-ack.png`.

Pass criteria:

- alarm name, status, severity, and tag/path context are understandable
- the operator can identify what needs attention

If there is no active alarm, record `blocked: no active alarm source` in `smoke-notes.md`. Do not fabricate an alarm on a customer PLC.

### 6.4 Acknowledge alarm

1. Acknowledge the selected alarm through the UI.
2. Confirm the alarm state changes from unacked to acked or otherwise clearly shows acknowledgment.
3. Capture `03-active-alarm-acked.png`.

Pass criteria:

- ack action succeeds for an authorized operator
- the UI reflects the acked state without refresh-dependent confusion
- the alarm remains active if the triggering condition is still active

### 6.5 Add action note

1. Add a short operator action note, for example `PoC smoke: checked by operator, no write action taken`.
2. Save the note.
3. Capture `04-action-note-saved.png`.

Pass criteria:

- note save succeeds
- note text is visible or success state is clear
- note content does not include customer secrets

### 6.6 Alarm history review

1. Open the alarm history/journal view.
2. Find the same alarm.
3. Confirm operator and note fields are visible when available.
4. Capture `05-alarm-history.png`.

Pass criteria:

- the acknowledged alarm is traceable in history/journal
- the operator/action note trail is visible enough for PoC review
- if DB is unavailable, record the 503/no-persistence limitation honestly in `smoke-notes.md`

### 6.7 Trend/history review artifact

1. Open the Trend Management screen.
2. Select one or more history-enabled tags that have data for the selected window.
3. Confirm the chart and Pen Statistics table load.
4. Capture `06-trend-history.png`.
5. If the Trend CSV export feature is available, download the CSV and save it as `07-trend-export.csv`.

Pass criteria:

- selected tags render on the chart
- min/max/average/count are visible for selected pens
- exported CSV opens locally and does not contain unintended secrets

If the environment has no historian data, record `blocked: no historian data` rather than inventing a CSV.

### 6.8 Read-only write/control block

Verify one safe write/control path is blocked. Prefer a UI path that does not target a real PLC write. If only backend verification is available, use an approved non-production tag/value path.

Pass criteria:

- write/control attempt is rejected or held by approval/read-only policy
- no customer PLC output is changed
- capture `08-read-only-block.png` or record the exact HTTP response in `smoke-notes.md`

Do not bypass `PUREIRON_POC_READ_ONLY_MODE` to make this check pass.

## 7. Result template

Create `smoke-notes.md` with this template:

```markdown
# PureIron PoC browser smoke result

- Date/time:
- Host/UI URL:
- Browser/version:
- Operator role:
- Data source:
- `poc_read_only_mode`: true / false

## Checklist

- [ ] Health endpoint confirms read-only mode
- [ ] Login/dashboard screenshot captured
- [ ] Active alarm before ack captured
- [ ] Acked alarm state captured
- [ ] Action note saved and captured
- [ ] Alarm history/journal captured
- [ ] Trend/history screen captured
- [ ] Trend CSV export saved or limitation recorded
- [ ] Read-only write/control block captured

## Blockers or limitations

- 

## Artifact list

- `00-health.json`
- `01-dashboard.png`
- `02-active-alarm-before-ack.png`
- `03-active-alarm-acked.png`
- `04-action-note-saved.png`
- `05-alarm-history.png`
- `06-trend-history.png`
- `07-trend-export.csv`
- `08-read-only-block.png`
```

## 8. Pass/fail rule

The smoke passes only if:

- read-only mode is confirmed
- the operator can see the dashboard and alarm path
- an alarm ack and action note are visible either in active state or history
- any missing historian/trend evidence is recorded as an explicit environment limitation
- no evidence artifact exposes secrets

A failure does not automatically block the paid PoC if it is caused by missing customer data or historian data, but it must be recorded before customer review. A failure caused by write/control not being blocked is a hard blocker for read-only PoC.
