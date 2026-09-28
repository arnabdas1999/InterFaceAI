# Capability `open-sub-account@2.0.0` - Open a sub-account (review, then approved commit)

- **What it does:** Look up the member by member ID, start opening a new sub-account with the given product and nickname, and stop at the review screen. Do NOT open the account.
- **Status:** approved  |  **Content hash:** `sha256:434c404f29fc850a348c07b97b575d2a656d2c2035803541544e2f0c0f312e0a`
- **Target:** SynthCore (legacy-web), app profile `synthcore@1.0.0`, product versions `>=4.2,<5.0`, entry `member_search` (`/members/search`)
- **Side effects:** irreversible
- **Provenance:** run `disc-20260927T194811-b16f52`, model `gemini-3.7-flash`, prompt `sha256:5490e1fd0efc7dea`, 6 executed / 0 dropped actions, parent `open-sub-account@1.0.0`, patches ['synthcore/open-sub-account.commit@1']

## Contract (what a calling agent supplies and gets back)

| Input | Type | Constraint | Sensitivity |
|---|---|---|---|
| `member_id` | string | ^\d{5}$ | pii_low |
| `nickname` | string | ^[A-Za-z0-9 ]{1,24}$ | pii_low |
| `product` | string | ['share_savings', 'money_market', 'share_certificate'] | public |

| Output | Type | Sensitivity |
|---|---|---|
| `confirmation_number` | string | public |
| `new_account_last4` | string | pii_low |

Business outcomes (legitimate answers, not errors): `member_not_found`, `validation_rejected`

## Steps

| # | Action | Target (locator candidates, best first) | Value | Risk | Idempotent | Postcondition | Scoped states |
|---|---|---|---|---|---|---|---|
| s1 | type: Type input member_id into 'Member ID' | label_anchor(anchor_text=vocab:member_id, relation=same_row_following, control=input) [0.85] > attribute(tag=input, attributes={'name': 'mbr'}) [0.7] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(2) > td:nth-of-type(2) > input:nth-of-type(1)) [0.3] | input `member_id` | sensitive_reversible | yes | - | - |
| s2 | click: Click button 'Search' | role_name(role=button, name=Search, exact=True) [0.9] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(4) > td:nth-of-type(1) > input:nth-of-type(1)) [0.3] | - | sensitive_reversible | yes | URL matches `/members/:member_id`, member_id = input `member_id` | search_no_results->member_not_found, validation_banner->validation_rejected |
| s3 | click: Click link 'Open Sub-Account' | role_name(role=link, name=vocab:open_sub_account, exact=True) [0.9] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(2) > a:nth-of-type(1)) [0.3] | - | reversible | yes | URL matches `/members/:member_id/subaccounts/new`, member_id = input `member_id` | - |
| s4 | select: Select input product in 'Product' | label_anchor(anchor_text=Product, relation=same_row_following, control=select) [0.85] > attribute(tag=select, attributes={'name': 'prd'}) [0.7] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(2) > td:nth-of-type(2) > select:nth-of-type(1)) [0.3] | input `product` | reversible | yes | - | - |
| s5 | type: Type input nickname into 'Nickname' | label_anchor(anchor_text=Nickname, relation=same_row_following, control=input) [0.85] > attribute(tag=input, attributes={'name': 'nck'}) [0.7] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(3) > td:nth-of-type(2) > input:nth-of-type(1)) [0.3] | input `nickname` | reversible | yes | - | - |
| s6 | click: Click button 'Continue' | role_name(role=button, name=Continue, exact=True) [0.9] > structural(css=body > table:nth-of-type(2) > tbody:nth-of-type(1) > tr:nth-of-type(1) > td:nth-of-type(1) > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(4) > td:nth-of-type(1) > input:nth-of-type(1)) [0.3] | - | reversible | yes | URL matches `/members/:member_id/subaccounts/review`, member_id = input `member_id` | validation_banner->validation_rejected |
| c1 **(review)** | click: Click 'Open Account' to commit the new sub-account | role_name(role=button, name=Open Account, exact=True) [0.9] | - | irreversible | NO | page contains /Sub-Account Opened/ | - |

## Outputs are read by

- `confirmation_number` (string) after `c1` from label_anchor(anchor_text=Confirmation Number, relation=same_row_following, control=cell) [0.8]
- `new_account_last4` (string) after `c1` from label_anchor(anchor_text=New Account, relation=same_row_following, control=cell) [0.8]

## Success condition (all must hold)

- URL matches `/members/:member_id/subaccounts/commit`, member_id = input `member_id`
- page contains /Sub-Account Opened/
- value cell labelled 'Member ID' contains input `member_id`

## Policy (narrowing only)

- Actions: click, dialog_respond, extract, navigate, select, type, wait_for
- Routes: `/blank`, `/login`, `/main`, `/members/:member_id`, `/members/:member_id/subaccounts/commit`, `/members/:member_id/subaccounts/new`, `/members/:member_id/subaccounts/review`, `/members/lookup`, `/members/search`

## Needs reviewer attention

- c1: Click 'Open Account' to commit the new sub-account - authored (synthcore/open-sub-account.commit@1); risk irreversible

## Lifecycle

- 2026-09-27T19:49:39Z -> **draft** by cua-compiler/1.0: open-sub-account@1.0.0 + authored patch synthcore/open-sub-account.commit@1
- 2026-09-27T19:49:48Z -> **validated** by cua validate (evidence script): automatic validation replays passed (runs val-20260927T194944-247132, val-20260927T194946-96aad5, val-20260927T194947-fcbee9)
- 2026-09-27T19:49:48Z -> **approved** by demo-reviewer: reviewed the review summary and validation runs
