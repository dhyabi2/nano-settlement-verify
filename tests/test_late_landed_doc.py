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
    """A seller's review (2026-11-04): serving needs nothing, credit needs state,
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
    assert "signed send block" in section[back:]
    assert "before the\npayer broadcasts" in section or "before the payer broadcasts" in section
