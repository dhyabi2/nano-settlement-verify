"""Believe a Nano payment only when more than one node says the same thing.

`nano_settlement_verify.verify` asks one node. That is the right default - it is
one call on a seller's request path - but it means the receipt is exactly as
good as that one endpoint. Public Nano RPC is thin: `rpc.nano.to` wants a key
and rate-limits a burst, Nanswap throttles, and a proxy in front of any of them
can answer 200 with something that is not a node reply at all. An agent with no
node of its own has no second opinion to fall back on, and "the node said so"
is the whole evidence that money arrived.

This module is that second opinion. It asks several endpoints and hands back a
receipt only for what they agree on.

Three things it deliberately does NOT do:

- **It does not break ties.** Two nodes that both answer and contradict each
  other about a confirmed block mean one of them is wrong, and nothing here can
  tell you which. A third vote would only make a guess look like a quorum. A
  contradiction raises.
- **It does not retry.** A failed endpoint is skipped, not re-asked. Waiting is
  the caller's to decide, as it is for `verify`.
- **It does not send, sign, broadcast or hold a key.** `process` is not here.
  This reads, like the rest of the package.

Amounts stay integers of raw throughout, as everywhere else in this package:
1 XNO is 10**30 raw and a float loses the low digits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import nano_settlement_verify
from nano_settlement_verify import Mismatch, NotFound, Receipt

__all__ = [
    "AGREE_DEFAULT",
    "Corroboration",
    "Disagreement",
    "NotCorroborated",
    "balance_corroborated",
    "distinct_endpoints",
    "post_json_failover",
    "verify_corroborated",
]

# How many endpoints must say the same thing before it is believed. Two is the
# smallest number that is more than one witness; it is a default, not a law.
AGREE_DEFAULT = 2


class NotCorroborated(Exception):
    """Too few endpoints answered to decide anything. NOTHING was checked.

    This is the multi-endpoint form of `verify`'s transport errors, and it means
    the same thing: not "the payment is bad" but "we could not look". A seller
    that refuses a call on this has refused a call the buyer may well have paid
    for, and XNO does not come back. Hold the call and ask again.
    """

    def __init__(self, answered: int, agree: int, failures: dict) -> None:
        detail = "; ".join(f"{url}: {why}" for url, why in failures.items())
        super().__init__(
            f"{answered} of {answered + len(failures)} endpoints answered, "
            f"{agree} needed to agree ({detail})"
        )
        self.answered = answered
        self.agree = agree
        self.failures = dict(failures)


class Disagreement(Exception):
    """Endpoints that both answered gave answers that cannot both be true.

    A Nano block's contents are fixed by its hash, so two honest nodes that have
    both confirmed it report the same amount, height and payee. Different
    answers mean one endpoint is broken or lying. Which one is not knowable from
    here, so nothing is served.
    """

    def __init__(self, answers: dict) -> None:
        detail = "; ".join(f"{url}: {answer}" for url, answer in answers.items())
        super().__init__(f"endpoints disagree: {detail}")
        self.answers = dict(answers)


@dataclass(frozen=True)
class Corroboration:
    """A receipt, and which endpoints stood behind it.

    `receipt.settled` is True only when `agree` endpoints independently reported
    the same confirmed send. An unsettled receipt here means the endpoints that
    answered did not get there - the block is not confirmed yet, or not enough
    of them have seen it - and, exactly as in `verify`, that is "not yet", not
    "no".
    """

    receipt: Receipt
    agreed: tuple[str, ...]
    answered: tuple[str, ...]
    failed: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "settled": self.receipt.settled,
                "amount_raw": self.receipt.amount_raw,
                "height": self.receipt.height,
                "account": self.receipt.account,
                "agreed": list(self.agreed),
                "answered": list(self.answered),
                "failed": list(self.failed),
            }
        )


def _endpoint_key(rpc_url: object) -> str:
    """How two endpoint URLs are told apart as witnesses.

    Only the obvious spelling differences are folded - case and a trailing
    slash. Nothing here resolves DNS or follows redirects, so two different
    names for one node still count as two; that is the caller's to know, and
    `distinct_endpoints` says so.
    """
    if not isinstance(rpc_url, str):
        raise TypeError(f"an endpoint must be a URL string, got {type(rpc_url).__name__}")
    return rpc_url.strip().rstrip("/").lower()


def distinct_endpoints(rpc_urls, agree: int = AGREE_DEFAULT) -> tuple[str, ...]:
    """The endpoints to ask, refusing a list that cannot support `agree`.

    One endpoint written twice is one witness, not two. Silently folding the
    duplicate would leave the caller believing a payment was corroborated by two
    nodes when one node said it twice, so a list with too few distinct entries
    is refused here, before any call is made.

    Being distinct URLs does not make endpoints independent: several public
    Nano RPCs are proxies in front of the same node, and this cannot see that.
    Agreement is only worth what the endpoint list is worth.
    """
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    if isinstance(agree, bool) or not isinstance(agree, int):
        raise TypeError(f"agree must be an int, got {type(agree).__name__}")
    if agree < 2:
        raise ValueError(f"agree must be at least 2; for one endpoint use verify(), got {agree}")
    ordered: list[str] = []
    seen: set[str] = set()
    for rpc_url in rpc_urls:
        key = _endpoint_key(rpc_url)
        if key not in seen:
            seen.add(key)
            ordered.append(rpc_url)
    if len(ordered) < agree:
        raise ValueError(
            f"{len(ordered)} distinct endpoint(s) given, {agree} needed to agree"
        )
    return tuple(ordered)


# What counts as "this endpoint did not answer", as opposed to "it answered and
# the answer was no". OSError covers a refused connection, DNS and the timeout;
# ValueError covers a body that is not JSON. KeyError and TypeError cover a body
# that IS JSON but is not a node reply - a proxy's status page, an error
# envelope with no `confirmed` - which `verify` raises on by design rather than
# read as settled or unsettled. All of them mean the same thing here: this
# endpoint told us nothing, ask the next one.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError)


def post_json_failover(rpc_urls, payload: dict):
    """POST `payload` to each endpoint in turn and return the first real answer.

    Returns `(reply, rpc_url)` - the reply, and which endpoint gave it.

    **This is failover, not corroboration.** It returns ONE endpoint's word,
    whichever answered first, and it is for calls where that is enough: reading
    a representative, listing receivables to decide what to pocket, checking
    whether an account is opened. Do not decide that money arrived with it -
    `verify_corroborated` is that function, and it is a different thing.

    Raises `NotCorroborated` when no endpoint answered readably, carrying what
    each one did instead; that is "we could not look", not "the answer is no".
    """
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    # Duplicates are harmless here - this wants one answer, not two witnesses -
    # but asking the same dead endpoint twice is pure latency on a request path.
    endpoints = tuple(dict.fromkeys(rpc_urls))
    if not endpoints:
        raise ValueError("no endpoints given")
    post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
    failures: dict[str, str] = {}
    for rpc_url in endpoints:
        try:
            return post(rpc_url, payload), rpc_url
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
    raise NotCorroborated(answered=0, agree=1, failures=failures)


def _answer(block_hash: str, expect_raw: int, account: str, rpc_url: str):
    """One endpoint's answer, as something two answers can be compared by.

    A settled receipt becomes the facts it asserts; everything else becomes a
    tag. `Mismatch` is carried rather than raised here so the caller can decide
    what one node's refusal is worth.
    """
    verify = nano_settlement_verify.verify  # looked up per call so a stub takes effect
    try:
        receipt = verify(block_hash, expect_raw, account, rpc_url)
    except NotFound:
        return ("not-seen",), None
    except Mismatch as error:
        return ("mismatch", repr(error.got), repr(error.expected)), error
    if not receipt.settled:
        return ("not-yet",), None
    return ("settled", receipt.amount_raw, receipt.height, receipt.account), receipt


def verify_corroborated(
    block_hash: str,
    expect_raw: int,
    account: str,
    rpc_urls,
    agree: int = AGREE_DEFAULT,
) -> Corroboration:
    """Prove a send settled, believing it only when `agree` endpoints concur.

    Asks the endpoints in the order given and stops as soon as `agree` of them
    have reported the SAME confirmed send - so an endpoint listed after the
    quorum is not called at all, and the list should be ordered by how much the
    caller trusts it. Endpoints that cannot be read are skipped, not retried.

    Returns a `Corroboration`. Its receipt is settled only on agreement.

    Raises:
      `Mismatch` - an endpoint has the block confirmed and says it paid a
        different amount, a different account, or was not a send at all. One
        such answer is enough to refuse: a confirmed block's contents are fixed,
        so this is not something a later endpoint can overturn.
      `Disagreement` - two endpoints each reported a confirmed send, and their
        receipts differ. Impossible between honest nodes; nothing is served.
      `NotCorroborated` - fewer than `agree` endpoints answered at all. Nothing
        was checked; hold the call and ask again rather than refuse it.
      `TypeError` - `expect_raw` is not an int, the same refusal `verify` makes,
        made here before any endpoint is called.
    """
    if isinstance(expect_raw, bool) or not isinstance(expect_raw, int):
        # Checked before the first call, not inside the loop: a float
        # expectation is the caller's bug, and no number of nodes repairs it.
        raise TypeError(f"expect_raw must be an int of raw, got {type(expect_raw).__name__}")
    endpoints = distinct_endpoints(rpc_urls, agree=agree)

    failures: dict[str, str] = {}
    answered: list[str] = []
    settled_by: dict[tuple, list[str]] = {}
    settled_receipt: dict[tuple, Receipt] = {}

    for rpc_url in endpoints:
        try:
            answer, payload = _answer(block_hash, expect_raw, account, rpc_url)
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
            continue
        answered.append(rpc_url)
        if answer[0] == "mismatch":
            # Fail closed, immediately. This endpoint has the block CONFIRMED
            # and reports it paid someone else or a different amount. Blocks do
            # not change after confirmation, so asking another node cannot make
            # this answer go away - it can only find a node that has not caught
            # up yet and read its silence as agreement.
            raise payload
        if answer[0] != "settled":
            continue
        if settled_by and answer not in settled_by:
            other = next(iter(settled_by))
            raise Disagreement(
                {settled_by[other][0]: str(other), rpc_url: str(answer)}
            )
        settled_by.setdefault(answer, []).append(rpc_url)
        settled_receipt[answer] = payload
        if len(settled_by[answer]) >= agree:
            return Corroboration(
                receipt=settled_receipt[answer],
                agreed=tuple(settled_by[answer]),
                answered=tuple(answered),
                failed=tuple(failures),
            )

    if len(answered) < agree:
        # Not enough witnesses even spoke. This is NOT an unsettled receipt:
        # "nobody could tell us" and "they told us it is not confirmed yet" are
        # different facts, and only the second one is safe to act on by waiting
        # a little and asking again.
        raise NotCorroborated(answered=len(answered), agree=agree, failures=failures)

    # Enough endpoints answered, but fewer than `agree` of them reported the
    # send confirmed - the usual reason being that confirmation has not reached
    # all of them yet. As in `verify`, that is "not yet", not "no".
    return Corroboration(
        receipt=Receipt(settled=False, amount_raw=0, height=0, account=""),
        agreed=(),
        answered=tuple(answered),
        failed=tuple(failures),
    )


def balance_corroborated(account: str, rpc_urls, agree: int = AGREE_DEFAULT):
    """The confirmed balance and receivable of an account, agreed by `agree` nodes.

    Returns `(balance_raw, receivable_raw)` as integers of raw.

    Only confirmed figures are asked for and compared, because an unconfirmed
    balance is the thing that differs between nodes by design. An endpoint that
    answers without them - a proxy that drops `include_confirmed`, say - counts
    as not having answered rather than being read for its unconfirmed `balance`.
    An account no endpoint has seen opened reads as `(0, 0)`: that is a real
    answer, and the agreement rule applies to it like any other.

    Confirmed figures still move, so two honest nodes a block apart disagree
    here. That is the intended outcome and the remedy is to ask again, not to
    take the larger number.

    Raises `Disagreement` when two endpoints report different confirmed figures,
    and `NotCorroborated` when fewer than `agree` endpoints answer.
    """
    endpoints = distinct_endpoints(rpc_urls, agree=agree)
    post = nano_settlement_verify.post_json  # looked up per call so a stub takes effect
    payload = {
        "action": "account_info",
        "account": account,
        "receivable": "true",
        "include_confirmed": "true",
    }
    failures: dict[str, str] = {}
    figures: dict[tuple, list[str]] = {}
    for rpc_url in endpoints:
        try:
            reply = post(rpc_url, payload)
            if "error" in reply:
                # An unopened account is not a failure to read; it is a balance
                # of nothing, and two nodes can agree on that. The check has to
                # come first: a node answering "Account not found" sends a
                # `balance` of "0" alongside the error, so reading the figure
                # before the error would turn an unknown account into a real
                # zero without saying so.
                figure = (0, 0)
            else:
                # Only the CONFIRMED figures, and no falling back to `balance`.
                # A node that ignores `include_confirmed` answers with `balance`
                # alone - the unconfirmed figure, which is precisely the number
                # that differs between nodes and must not be trusted. Falling
                # back to it would answer a question nobody asked while looking
                # like agreement, so its absence is read as this endpoint not
                # having answered: KeyError here is caught just below.
                figure = (
                    int(reply["confirmed_balance"]),
                    int(reply["confirmed_receivable"]),
                )
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
            continue
        if figures and figure not in figures:
            other = next(iter(figures))
            raise Disagreement({figures[other][0]: str(other), rpc_url: str(figure)})
        figures.setdefault(figure, []).append(rpc_url)
        if len(figures[figure]) >= agree:
            return figure
    raise NotCorroborated(
        answered=sum(len(urls) for urls in figures.values()), agree=agree, failures=failures
    )
