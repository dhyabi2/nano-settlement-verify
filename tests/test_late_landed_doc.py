"""Law: the README says what happens to the money when `verify` raises `Late`.

`Late` is raised for a block that is confirmed, for the exact amount, to the
seller's account. The payer cannot pull a confirmed Nano send back, so the page
that explains the raise must also say the seller now holds the money and must
decide, and that `receipt` carries what it needs to serve, credit or refund.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
HEADING = "**A late payment has still landed.**"


def test_readme_says_a_late_payment_has_landed_and_names_the_receipt():
    text = README.read_text(encoding="utf-8")
    assert HEADING in text, "the README no longer says what a seller does after `Late`"
    section = text.split(HEADING, 1)[1].split("**Changes from the previous release.**", 1)[0]
    for needed in ("exc.receipt", "amount_raw", "account", "UnknownTime", "not_after=None"):
        assert needed in section, f"the late-payment paragraph no longer mentions {needed}"
    # A confirmed send is receivable until the payee publishes its receive block.
    assert "XNO is in your account" not in section
    assert "receivable" in section


def test_serve_anyway_is_the_default_and_the_deadline_belongs_before_broadcast():
    """A seller's review (2026-11-04): serving needs a used-hash record like any
    payment, credit needs state,
    sending back costs a signed block, and a deadline only binds before the payer
    broadcasts. The paragraph must rank them that way and say so."""
    text = README.read_text(encoding="utf-8")
    section = text.split(HEADING, 1)[1].split("**Changes from the previous release.**", 1)[0]
    serve = section.find("Serve anyway")
    credit = section.find("credit against that block hash")
    back = section.find("Send it back")
    assert -1 not in (serve, credit, back), "the three choices are no longer named"
    assert serve < credit < back, "serve anyway must come first and sending back last"
    assert "the default" in section[serve:credit]
    # verify keeps no record of accepted hashes: serving still needs the
    # seller to refuse a hash it has already used, or an old send replays.
    serve_text = " ".join(section[serve:credit].split())
    assert "It needs nothing more" not in serve_text
    assert "record the block hash once and refuse a hash already used" in serve_text
    assert "signed send block" in section[back:]
    assert "before the\npayer broadcasts" in section or "before the payer broadcasts" in section
    # A fresh payTo per quote attributes a payment to its quote; it does not
    # bind the payer, and a late send to it is still that quote's money.
    flat = " ".join(section.split())
    assert "Nor does a fresh `payTo` for every quote bind the payer or stop a late send" in flat
    assert "still that quote's money" in flat
    # maxTimeoutSeconds is not an expiry for a Nano send; on EVM exact it is signed.
    assert "For a Nano send, x402's `maxTimeoutSeconds` is not such an expiry" in flat
    assert "`validBefore`" in flat
    # Third-party descriptions are marked as theirs.
    assert "pursekeeper reports" in flat and "not something we have measured" in flat
