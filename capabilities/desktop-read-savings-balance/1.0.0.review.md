# Capability `desktop-read-savings-balance@1.0.0` - Read share savings balance (desktop client)

- **What it does:** In the SynthCore Desktop client, look up the member by member ID and read their Share Savings Balance as money.
- **Status:** approved  |  **Content hash:** `sha256:0f04f7d8b6099b2d0fa845916065c1c1c41ceac43a41f59db41424b5ba76418e`
- **Target:** SynthCore Desktop (desktop), app profile `synthcore-desktop@1.0.0`, product versions `>=4.2,<5.0`, entry `member_inquiry` (`/desktop/member-inquiry`)
- **Side effects:** none
- **Provenance:** run `disc-20260927T204319-164236`, model `gemini-3.6-flash`, prompt `sha256:5490e1fd0efc7dea`, 2 executed / 0 dropped actions

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
| s1 | type: Type input member_id into 'Member ID' | label_anchor(anchor_text=vocab:member_id, relation=same_row_following, control=input) [0.85] > attribute(tag=EditControl, attributes={'automation_id': 'txtMbr'}) [0.7] | input `member_id` | reversible | yes | - | - |
| s2 | click: Click button 'Search' | role_name(role=button, name=Search, exact=True) [0.9] > attribute(tag=ButtonControl, attributes={'automation_id': 'btnSearch'}) [0.7] | - | reversible | yes | value cell labelled 'Share Savings Balance:' contains /\S/ | search_no_results->member_not_found, validation_banner->validation_rejected |

## Outputs are read by

- `savings_balance` (money) after `s2` from label_anchor(anchor_text=vocab:share_savings_balance, relation=same_row_following, control=cell) [0.8]

## Success condition (all must hold)

- URL matches `/desktop/member-inquiry`
- value cell labelled 'Member:' contains input `member_id`

## Policy (narrowing only)

- Actions: click, dialog_respond, extract, navigate, type, wait_for
- Routes: `/desktop/member-inquiry`

## Needs reviewer attention

- nothing flagged

## Lifecycle

- 2026-09-27T20:43:53Z -> **draft** by cua-compiler/1.0: compiled from verified discovery run (runs disc-20260927T204319-164236)
- 2026-09-27T20:45:41Z -> **validated** by cua validate (evidence script): automatic validation replays passed (runs val-20260927T204527-c6e543, val-20260927T204532-598c95, val-20260927T204536-4b8b2d)
- 2026-09-27T20:45:41Z -> **approved** by demo-reviewer: reviewed the review summary and validation runs
