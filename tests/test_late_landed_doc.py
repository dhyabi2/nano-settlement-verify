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
    section = text.split(HEADING, 1)[1].split("\n\n", 1)[0]
    for needed in ("exc.receipt", "amount_raw", "account", "UnknownTime", "not_after=None"):
        assert needed in section, f"the late-payment paragraph no longer mentions {needed}"
