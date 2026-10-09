"""Law: the README's test count is the number this suite actually collects.

The page said "274 tests" while the suite ran 286. Nothing checked the two
against each other, so every pull request that added a test left the number one
step further behind - it had drifted twelve tests by the time it was measured.

On this repository that is not cosmetic. The whole claim of the page is that a
stranger can check a settlement without taking our word for anything, and the
first number they can check is this one. A reader who counts 286 and reads 274
cannot tell a stale page from twelve tests that no longer run, which is the same
doubt the *Check it without running our code* section exists to remove.

`--collect-only` is used rather than a real run so the law stays cheap and
cannot recurse into itself.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# The page states the count once, as "N tests:" at the head of the sentence that
# then enumerates what they cover.
CLAIM = re.compile(r"\b(\d{1,4}) tests:")
COLLECTED = re.compile(r"\b(\d+) tests? collected")


def test_readme_test_count_is_the_count_pytest_collects():
    claim = CLAIM.search(README.read_text(encoding="utf-8"))
    assert claim, "the README no longer says how many tests this suite has"
    claimed = int(claim.group(1))

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    got = COLLECTED.search(proc.stdout)
    assert got, (
        "could not read a collection count from pytest:\n"
        f"{proc.stdout[-2000:]}{proc.stderr[-2000:]}"
    )
    collected = int(got.group(1))

    assert claimed == collected, (
        f"the README says {claimed} tests, pytest collects {collected} - "
        "update the count in README.md beside the enumeration of what they cover"
    )
