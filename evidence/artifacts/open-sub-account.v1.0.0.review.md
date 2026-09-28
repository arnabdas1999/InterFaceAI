# Capability `open-sub-account@1.0.0` - Open a sub-account (review, then approved commit)

- **What it does:** Look up the member by member ID, start opening a new sub-account with the given product and nickname, and stop at the review screen. Do NOT open the account.
- **Status:** draft  |  **Content hash:** `sha256:1f6ee71fda531bde2f0bfaf4632ef0ecef1969dc66dd8e30063420647c620b63`
- **Target:** SynthCore (legacy-web), app profile `synthcore@1.0.0`, product versions `>=4.2,<5.0`, entry `member_search` (`/members/search`)
- **Side effects:** none
- **Provenance:** run `disc-20260927T194811-b16f52`, model `gemini-3.7-flash`, prompt `sha256:5490e1fd0efc7dea`, 6 executed / 0 dropped actions

## Contract (what a calling agent supplies and gets back)

| Input | Type | Constraint | Sensitivity |
|---|---|---|---|
| `member_id` | string | ^\d{5}$ | pii_low |
| `nickname` | string | ^[A-Za-z0-9 ]{1,24}$ | pii_low |
| `product` | string | ['share_savings', 'money_market', 'share_certificate'] | public |

| Output | Type | Sensitivity |
|---|---|---|

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

## Outputs are read by


## Success condition (all must hold)

- URL matches `/members/:member_id/subaccounts/review`, member_id = input `member_id`
- value cell labelled 'Member ID' contains input `member_id`
- value cell labelled 'Nickname' contains input `nickname`

## Policy (narrowing only)

- Actions: click, dialog_respond, extract, navigate, select, type, wait_for
- Routes: `/blank`, `/login`, `/main`, `/members/:member_id`, `/members/:member_id/subaccounts/new`, `/members/:member_id/subaccounts/review`, `/members/lookup`, `/members/search`

## Needs reviewer attention

- nothing flagged

## Lifecycle

- 2026-09-27T19:49:39Z -> **draft** by cua-compiler/1.0: compiled from verified discovery run (runs disc-20260927T194811-b16f52)
