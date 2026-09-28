import pytest

from interface_cua.domain.routes import canonicalize, generalize_path, match_route, route_allowed
from interface_cua.domain.types import NormalizationError, OutputType, normalize

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "amount"),
    [
        ("$1,234.56", "1234.56"),
        ("$(12.00)", "-12.00"),
        ("($12.00)", "-12.00"),
        ("-$5", "-5.00"),
        ("USD 7.1", "7.10"),
        (" $15,020.00 ", "15020.00"),
    ],
)
def test_money_normalizes(raw: str, amount: str) -> None:
    assert normalize(OutputType.MONEY, raw) == {"amount": amount, "currency": "USD"}


@pytest.mark.parametrize("raw", ["12,34", "abc", "$", "1.234", "$1,23.00"])
def test_money_rejects_garbage(raw: str) -> None:
    with pytest.raises(NormalizationError):
        normalize(OutputType.MONEY, raw)


def test_other_types() -> None:
    assert normalize(OutputType.DATE, "09/25/2026") == "2026-09-25"
    assert normalize(OutputType.INTEGER, "1,024") == 1024
    assert normalize(OutputType.BOOLEAN, "Active") is True
    assert (
        normalize(OutputType.ENUM, "Money Market", enum_values=["money_market"], enum_labels={"money_market": "Money Market"})
        == "money_market"
    )
    assert normalize(OutputType.STRING, " CNF-004821 ", pattern=r"^CNF-\d{6}$") == "CNF-004821"
    with pytest.raises(NormalizationError):
        normalize(OutputType.STRING, "X", pattern=r"^\d$")


def test_route_patterns() -> None:
    assert match_route("/members/:member_id", "/members/12345") == {"member_id": "12345"}
    assert match_route("/members/:member_id", "/members/12345/accounts") is None
    assert match_route("/members/**", "/members/12345/accounts/savings") == {}
    assert match_route("/members/*/subaccounts/commit", "/members/1/subaccounts/commit") == {}
    assert route_allowed("/__admin/faults", ["/__admin/**"])
    assert not route_allowed("/__admin/faults", ["/members/**", "/login"])


def test_canonicalize_strips_session_tokens() -> None:
    cu = canonicalize("http://127.0.0.1:8765/main;jsessionid=abc?jsessionid=zz&x=1")
    assert str(cu) == "http://127.0.0.1:8765/main"
    assert canonicalize("http://h/p?x=1&y=2", keep_params=frozenset({"y"})).query == "y=2"


def test_generalize_path_removes_concrete_ids() -> None:
    assert generalize_path("/members/12345/accounts", {"member_id": "12345"}) == "/members/:member_id/accounts"
    assert generalize_path("/items/777", {}) == "/items/:id"
