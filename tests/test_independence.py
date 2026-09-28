"""Laws for nano_independence: how many INDEPENDENT payers a list of payer accounts holds.

No test touches the network: the node is a dict of stubbed replies, and the
no_sockets fixture makes any real urlopen call fail the test that made it.
"""

import pytest

import nano_independence
import nano_settlement_verify
from nano_independence import funding_chain, independence

URL = "http://127.0.0.1:7076"
SELLER = "nano_1seller"
EXCHANGE = "nano_1exchange"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a socket")

    monkeypatch.setattr(nano_settlement_verify.urllib.request, "urlopen", refuse)


def ledger(funded_by: dict, legacy: frozenset = frozenset()):
    """A fake node: `funded_by[account]` is the account whose send opened it.

    Accounts absent from `funded_by` are unopened. Each account's open block is
    OPEN_<account>, and the send that funded it is SEND_<account>, sitting on the
    funder's chain. Accounts in `legacy` get a pre-state open block (contents.source).
    """

    def post_json(rpc_url, payload):
        assert rpc_url == URL
        action = payload["action"]
        if action == "account_info":
            account = payload["account"]
            if account not in funded_by:
                return {"error": "Account not found"}
            return {"open_block": f"OPEN_{account}", "frontier": f"OPEN_{account}", "balance": "1"}
        if action == "block_info":
            block = payload["hash"]
            assert payload.get("json_block") == "true"
            kind, _, account = block.partition("_")
            if kind == "OPEN" and account in funded_by:
                if account in legacy:
                    return {"block_account": account, "contents": {"type": "open", "source": f"SEND_{account}"}}
                return {
                    "block_account": account,
                    "subtype": "open",
                    "contents": {"type": "state", "account": account, "link": f"SEND_{account}"},
                }
            if kind == "SEND" and account in funded_by:
                funder = funded_by[account]
                return {
                    "block_account": funder,
                    "subtype": "send",
                    "contents": {"type": "state", "account": funder, "link_as_account": account},
                }
            return {"error": "Block not found"}
        raise AssertionError(f"unexpected action {action}")

    return post_json


@pytest.fixture
def node(monkeypatch):
    def install(funded_by, legacy=frozenset()):
        monkeypatch.setattr(nano_settlement_verify, "post_json", ledger(funded_by, legacy))

    return install


def test_chain_names_the_direct_funder(node):
    node({"nano_1a": "nano_1op", "nano_1op": EXCHANGE, EXCHANGE: "nano_1genesis"})
    assert funding_chain("nano_1a", URL, hops=1) == ["nano_1op"]
    assert funding_chain("nano_1a", URL, hops=2) == ["nano_1op", EXCHANGE]


def test_chain_reads_a_legacy_open_block(node):
    node({"xrb_1old": "nano_1op"}, legacy=frozenset({"xrb_1old"}))
    assert funding_chain("xrb_1old", URL, hops=1) == ["nano_1op"]


def test_chain_stops_at_an_account_that_opened_itself_or_loops(node):
    node({"nano_1a": "nano_1b", "nano_1b": "nano_1a"})
    assert funding_chain("nano_1a", URL, hops=5) == ["nano_1b"]


def test_unopened_payer_has_no_chain(node):
    node({})
    assert funding_chain("nano_1ghost", URL, hops=2) is None


def test_hops_must_be_a_positive_int(node):
    node({})
    for bad in (0, -1, 1.5, True):
        with pytest.raises((TypeError, ValueError)):
            funding_chain("nano_1a", URL, hops=bad)


def test_three_keys_one_operator_is_one_payer(node):
    """The case the need names: counting keys is not counting buyers."""
    node({"nano_1k1": "nano_1op", "nano_1k2": "nano_1op", "nano_1k3": "nano_1op", "nano_1real": "nano_1other"})
    report = independence(["nano_1k1", "nano_1k2", "nano_1k3", "nano_1real"], URL)
    assert report.keys == 4
    assert report.independent == 2
    assert sorted(map(sorted, report.groups)) == [["nano_1k1", "nano_1k2", "nano_1k3"], ["nano_1real"]]


def test_a_payer_that_funded_another_payer_is_the_same_payer(node):
    node({"nano_1a": "nano_1x", "nano_1b": "nano_1a"})
    report = independence(["nano_1a", "nano_1b"], URL)
    assert report.independent == 1


def test_seller_funded_payers_are_money_moving_in_a_circle(node):
    node({"nano_1a": SELLER, "nano_1b": "nano_1y", "nano_1c": "nano_1z"})
    report = independence(["nano_1a", "nano_1b", "nano_1c"], URL, seller=SELLER)
    assert report.funded_by_seller == ["nano_1a"]
    assert report.independent == 2
    assert all("nano_1a" not in group for group in report.groups)


def test_seller_is_recognised_in_either_spelling(node):
    node({"nano_1a": "nano_1seller"})
    report = independence(["nano_1a"], URL, seller="xrb_1seller")
    assert report.funded_by_seller == ["nano_1a"]
    assert report.independent == 0


def test_seller_two_hops_back_is_still_the_seller(node):
    node({"nano_1a": "nano_1mid", "nano_1mid": SELLER})
    assert independence(["nano_1a"], URL, seller=SELLER, hops=1).funded_by_seller == []
    assert independence(["nano_1a"], URL, seller=SELLER, hops=2).funded_by_seller == ["nano_1a"]


def test_a_shared_exchange_does_not_merge_strangers_when_ignored(node):
    """Two people withdrawing from one exchange are two buyers, if the caller says it is a hub."""
    node({"nano_1a": EXCHANGE, "nano_1b": EXCHANGE})
    assert independence(["nano_1a", "nano_1b"], URL).independent == 1
    report = independence(["nano_1a", "nano_1b"], URL, ignore=[EXCHANGE])
    assert report.independent == 2


def test_ignore_matches_either_spelling(node):
    node({"nano_1a": "nano_1exchange", "nano_1b": "nano_1exchange"})
    assert independence(["nano_1a", "nano_1b"], URL, ignore=["xrb_1exchange"]).independent == 2


def test_unopened_payers_are_reported_not_counted(node):
    node({"nano_1a": "nano_1x"})
    report = independence(["nano_1a", "nano_1ghost"], URL)
    assert report.unopened == ["nano_1ghost"]
    assert report.independent == 1


def test_duplicate_and_respelled_payers_are_one_key(node):
    node({"nano_1a": "nano_1x"})
    report = independence(["nano_1a", "xrb_1a", "nano_1a"], URL)
    assert report.keys == 1
    assert report.independent == 1


def test_report_is_json_with_every_field(node):
    import json

    node({"nano_1a": "nano_1x", "nano_1b": "nano_1x"})
    data = json.loads(independence(["nano_1a", "nano_1b"], URL).to_json())
    assert data == {
        "keys": 2,
        "independent": 1,
        "groups": [["nano_1a", "nano_1b"]],
        "funded_by_seller": [],
        "unopened": [],
        "hops": 1,
        "chains": {"nano_1a": ["nano_1x"], "nano_1b": ["nano_1x"]},
    }


def test_only_reads(monkeypatch):
    """No action that writes, signs or sends is ever asked of the node."""
    seen = []
    inner = ledger({"nano_1a": "nano_1x", "nano_1x": EXCHANGE})

    def spy(rpc_url, payload):
        seen.append(payload["action"])
        return inner(rpc_url, payload)

    monkeypatch.setattr(nano_settlement_verify, "post_json", spy)
    independence(["nano_1a"], URL, hops=3)
    assert set(seen) <= {"account_info", "block_info"}


def test_module_names_its_limit():
    """A lower bound on common control, never a proof of independence: the docstring says so."""
    doc = nano_independence.__doc__.lower()
    assert "lower bound" in doc and "not proof" in doc
