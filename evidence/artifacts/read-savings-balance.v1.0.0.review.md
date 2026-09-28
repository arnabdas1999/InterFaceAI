# Capability `read-savings-balance@1.0.0` - Read share savings balance

- **What it does:** Look up the member by member ID and read their current Share Savings balance. Open the Share Savings account detail and extract the Current Balance as money.
- **Status:** approved  |  **Content hash:** `sha256:12c98921de5affb0e32472961398f8c95d2aa27d408030c0c04b14693880e6e3`
- **Target:** SynthCore (legacy-web), app profile `synthcore@1.0.0`, product versions `>=4.2,<5.0`, entry `member_search` (`/members/search`)
- **Side effects:** none
- **Provenance:** run `disc-20260927T194320-2cb1b7`, model `gemini-3.8-flash`, prompt `sha256:5490e1fd0efc7dea`, 4 executed / 0 dropped actions

## Contract (what a calling agent supplies and gets back)

| Input | Type | Constraint | Sensitivity |
|---|---|---|---|
| `member_id` | string | ^\d{5}$ | pii_low |

| Output | Type | Sensitivity |
|---|---|---|
| `savings_balance` | money | financial |

Business outcomes (legitimate answers, not errors): `member_not_found`, `validation_rejected`

## Steps

| # | Action | Target (locator candidates, best first) | Value | Risk | Idempotent | Postcondition | Scoped states |
|---|---|---|---|---|---|---|---|
| s1 | type: Type input member_id into 'Member ID' | label_anchor(anchor_text=vocab:member_id, relation=same_row_following, control=input) [0.85] > attribute(tag=input, attributes={'name': 'mbr'}) [0.7] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(2) > td:nth-of-type(2) > input:nth-of-type(1)) [0.3] | input `member_id` | sensitive_reversible | yes | - | - |
| s2 | click: Click button 'Search' | role_name(role=button, name=Search, exact=True) [0.9] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(4) > td:nth-of-type(1) > input:nth-of-type(1)) [0.3] | - | sensitive_reversible | yes | URL matches `/members/:member_id`, member_id = input `member_id` | search_no_results->member_not_found, validation_banner->validation_rejected |
| s3 | click: Click button 'View Accounts' | role_name(role=button, name=vocab:view_accounts, exact=True) [0.9] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > input:nth-of-type(1)) [0.3] | - | reversible | yes | frame acctFrame at `/members/:member_id/accounts` | - |
| s4 | click: Click link 'Share Savings' | in frame `acctFrame`: role_name(role=link, name=vocab:share_savings, exact=True) [0.9] > structural(css=body > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(2) > td:nth-of-type(1) > a:nth-of-type(1)) [0.3] | - | reversible | yes | frame acctFrame at `/members/:member_id/accounts/savings` | - |

## Outputs are read by

- `savings_balance` (money) after `s4` from in frame `acctFrame`: label_anchor(anchor_text=vocab:current_balance, relation=same_row_following, control=cell) [0.8]

## Success condition (all must hold)

- URL matches `/members/:member_id`, member_id = input `member_id`
- frame acctFrame at `/members/:member_id/accounts/savings`
- value cell labelled 'Account Type' contains vocabulary `share_savings`
- value cell labelled 'Member ID' contains input `member_id`

## Policy (narrowing only)

- Actions: click, dialog_respond, extract, navigate, type, wait_for
- Routes: `/blank`, `/login`, `/main`, `/members/:member_id`, `/members/:member_id/accounts`, `/members/:member_id/accounts/savings`, `/members/lookup`, `/members/search`

## Needs reviewer attention

- nothing flagged

## Lifecycle

- 2026-09-27T19:45:48Z -> **draft** by cua-compiler/1.0: compiled from verified discovery run (runs disc-20260927T194320-2cb1b7)
- 2026-09-27T19:49:44Z -> **validated** by cua validate (evidence script): automatic validation replays passed (runs val-20260927T194939-74582c, val-20260927T194941-780999, val-20260927T194943-a41d9d)
- 2026-09-27T19:49:44Z -> **approved** by demo-reviewer: reviewed the review summary and validation runs
