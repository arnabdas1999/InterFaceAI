"""Per-tenant branding/vocabulary for the synthetic vendor product.

Two tenants run the same "SynthCore" product with different labels, which is the
stand-in for many institutions running one vendor app configured differently.
"""

from __future__ import annotations

BRANDING: dict[str, dict[str, str]] = {
    "tenant-a": {
        "institution": "Harborview Federal Credit Union",
        "member": "Member",
        "member_id": "Member ID",
        "member_inquiry": "Member Inquiry",
        "share_savings": "Share Savings",
        "share_draft": "Share Draft Checking",
        "money_market": "Money Market",
        "share_certificate": "Share Certificate",
        "open_sub_account": "Open Sub-Account",
        "view_accounts": "View Accounts",
        "current_balance": "Current Balance",
        "search_label": "Search",
        "search_style": "input",
    },
    "tenant-b": {
        "institution": "Prairie Plains Community Bank",
        "member": "Customer",
        "member_id": "Customer No.",
        "member_inquiry": "Customer Lookup",
        "share_savings": "Savings",
        "share_draft": "Checking",
        "money_market": "Money Market",
        "share_certificate": "Certificate of Deposit",
        "open_sub_account": "Add Account",
        "view_accounts": "View Accounts",
        "current_balance": "Current Balance",
        # A different configuration of the same product: the search control is a <button> captioned
        # "Find". Vocabulary cannot absorb this; a reviewed per-tenant override patch does.
        "search_label": "Find",
        "search_style": "button",
    },
}

# Same product and version as tenant-a, deployed with SynthCore's classic frameset UI (banner / nav /
# content frames). The application pages live inside the "content" frame.
BRANDING["tenant-c"] = {
    **BRANDING["tenant-a"],
    "institution": "Lakeshore Teachers Credit Union (classic UI)",
    "ui_mode": "classic",
}

PRODUCTS = ("share_savings", "money_market", "share_certificate")
