"""Who has actually paid a seller — from the seller's own published terms and the ledger.

`verify` answers "did THIS payment settle?". This answers the question a buyer
asks one step earlier, before it has paid anything at all: *is anybody paying
this seller, and is the account it advertises the account that gets paid?*

Both halves come from somewhere the seller does not control the reading of:

- the payee comes out of the seller's **own** x402 document, which it serves and
  signs its name to, not out of a directory entry or a README;
- the payers come off the **public ledger**, by reading the payee's chain.

So a stranger holding nothing but a URL can re-derive every number here, and a
seller that claims traffic it does not have is contradicted by the ledger rather
than argued with. That is the only reason this module exists: an address and a
count that only we can produce are worth nothing to the agent deciding whether
to pay us.

    import json, urllib.request
    from nano_payers import payees_from_manifest, payers

    doc = json.load(urllib.request.urlopen("https://seller.example/.well-known/x402"))
    for payee in payees_from_manifest(doc):
        report = payers(payee.account, ["https://rpc.nano.to"])
        print(payee.account, report.distinct_payers, report.received_raw)
        print(report.repeat_payers, report.repeat_rate)  # payers with >= 2 payments
        print(report.to_json())     # per payer: payments, raw, first and last seen

What it is not:

- **It does not prove a single payment final.** A payer count is read from one
  node's view of a chain (`payers`) or from several that must agree on the whole
  payer set (`payers_corroborated`). To decide that one specific payment arrived,
  and to hold the receipt that says so, use `verify` / `verify_corroborated`.
- **It cannot tell a customer from the seller funding itself.** The ledger
  records that an account sent XNO, never why. `outside_payers` exists so that
  judgement is made explicitly, by whoever knows which accounts are their own,
  and is visible in the output instead of baked into the count.

Raw amounts are integers throughout: 1 XNO is `10**30` raw and a float loses the
low digits. No signing, no sending, no key: it only reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import nano_quorum
import nano_settlement_verify
from nano_settlement_verify import is_valid_account, public_key_from_address

__all__ = [
    "Payee",
    "Payer",
    "PayerReport",
    "ManifestRefused",
    "HistoryIncomplete",
    "HISTORY_PAGE",
    "HISTORY_PAGES_MAX",
    "payees_from_manifest",
    "payers",
    "payers_corroborated",
    "outside_payers",
]

# How many blocks to ask for per `account_history` call, and how many calls one
# chain may take. The cap is a bound on a stranger's URL, not a tuning knob: an
# account with a very long chain is reported as incomplete (see
# `HistoryIncomplete`) rather than quietly truncated into a smaller count.
HISTORY_PAGE = 1000
HISTORY_PAGES_MAX = 50

# The Nano network identifiers an x402 entry may carry for mainnet. Both
# spellings are in shipped code: `nano:mainnet` is the literal in
# `x402-nano-exact` and `@x402nano/exact`, while `nano-mainnet` is accepted by
# x402's v1 network schema and refused by its v2 one. A reader that knows only
# one of them reads a payable seller as having no Nano rail.
_MAINNET = frozenset({"nano:mainnet", "nano-mainnet"})
# The family token that makes an identifier Nano's at all, so that a USDC or
# EVM entry beside ours is skipped as another rail rather than reported as a
# fault in this seller.
_NANO_FAMILY = frozenset({"nano", "xno"})

# What counts as "this endpoint told us nothing usable", the same family
# nano_quorum fails over on. OSError covers a refused connection, DNS and the
# timeout; ValueError covers a body that is not JSON and `NotANodeReply`, which
# here includes a history row whose amount is not an integer - a node whose
# numbers cannot be summed has told us nothing about this seller, and dropping
# that row instead would make a total that looks complete and is short by real
# money. KeyError and TypeError cover a reply whose fields are the wrong shape.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError)


class ManifestRefused(Exception):
    """An x402 document that does not say where to pay, or says it unpayably.

    Carries `reason` (a stable code) and `detail`.
    """

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class HistoryIncomplete(Exception):
    """The node did not hand back the whole chain, so a count would understate.

    Reported rather than returned, because a partial payer count is the one
    answer here that is wrong in a way the caller cannot see: it looks like a
    smaller business, not like a shorter read.
    """

    def __init__(self, account: str, read: int, oldest_height: int):
        self.account = account
        self.read = read
        self.oldest_height = oldest_height
        super().__init__(
            f"read {read} block(s) of {account} back to height {oldest_height}, "
            "not to the open block at height 1, so the payer count would be "
            "short by whatever is older; raise HISTORY_PAGES_MAX or pass "
            "allow_partial=True to take the short view knowingly"
        )


@dataclass(frozen=True)
class Payee:
    """An account a seller's own x402 document says to pay, and at what price."""

    account: str
    network: str
    # Every distinct price in raw advertised against this account, ascending. A
    # catalogue prices its resources differently, and the cheapest is what a
    # single call costs - which is what makes a payment of exactly that amount
    # on the ledger recognisable as a call rather than as funding.
    prices_raw: tuple[int, ...]
    resources: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "account": self.account,
                "network": self.network,
                "prices_raw": [str(price) for price in self.prices_raw],
                "resources": list(self.resources),
            }
        )


@dataclass(frozen=True)
class Payer:
    """One account that has paid the payee, as the ledger records it."""

    account: str
    payments: int
    received_raw: int
    # The earliest and latest `local_timestamp` of this payer's receive blocks:
    # when the node saw the payee COLLECT the money, not when the payer sent it,
    # and 0 when the node does not know. They bound the relationship.
    first_timestamp: int
    last_timestamp: int


@dataclass(frozen=True)
class PayerReport:
    """Every account that has paid this payee, counted off its own chain."""

    account: str
    distinct_payers: int
    payments: int
    received_raw: int
    payers: tuple[Payer, ...]
    # Receives the payee made from itself. Not payers: an account paying itself
    # is moving its own money, and counting it inflates the only number here
    # anyone cares about.
    self_payments: int
    # Receives a node reported as not yet confirmed. Never counted, because an
    # unconfirmed block can still be rolled back and a count that includes one
    # claims a customer that may never have existed. Reported so that "nothing
    # arrived" and "something arrived and is still settling" stay different
    # answers.
    unconfirmed_payments: int
    blocks_read: int
    complete: bool
    asked: tuple[str, ...] = field(default=())

    # Derived from `payers` rather than stored, so a report rebuilt from a
    # subset of payers - `outside_payers` removing our own accounts - cannot
    # carry a repeat rate that belongs to the payer set before the removal.
    @property
    def repeat_payers(self) -> int:
        """Payers with two or more confirmed payments.

        `distinct_payers` cannot tell an account that paid once from one that
        pays every week; this can. It is still a count of keys, not of buyers.
        """
        return sum(1 for payer in self.payers if payer.payments >= 2)

    @property
    def repeat_rate(self) -> float:
        """`repeat_payers / distinct_payers`, to four places; 0 when nobody has paid."""
        if not self.payers:
            return 0.0
        return round(self.repeat_payers / len(self.payers), 4)

    def to_json(self) -> str:
        return json.dumps(
            {
                "account": self.account,
                "distinct_payers": self.distinct_payers,
                "repeat_payers": self.repeat_payers,
                "repeat_rate": self.repeat_rate,
                "payments": self.payments,
                "received_raw": str(self.received_raw),
                "self_payments": self.self_payments,
                "unconfirmed_payments": self.unconfirmed_payments,
                "blocks_read": self.blocks_read,
                "complete": self.complete,
                "asked": list(self.asked),
                "payers": [
                    {
                        "account": payer.account,
                        "payments": payer.payments,
                        "received_raw": str(payer.received_raw),
                        "first_timestamp": payer.first_timestamp,
                        "last_timestamp": payer.last_timestamp,
                    }
                    for payer in self.payers
                ],
            }
        )


def _network_of(entry: dict) -> str:
    network = entry.get("network")
    return network.strip().lower() if isinstance(network, str) else ""


def _is_nano_network(network: str) -> bool:
    """Whether this identifier names Nano at all, mainnet or not.

    Split on the first separator so that `nano:mainnet`, `nano-mainnet`,
    `nano:testnet` and a bare `nano` are all recognised as this family, while
    `base-sepolia` or `eip155:8453` are another rail and none of our business.
    """
    head = network.replace(":", "-").split("-", 1)[0]
    return head in _NANO_FAMILY


def _price_raw(entry: dict, where: str = "") -> int | None:
    """The entry's price in raw, under either field name x402 has used.

    `amount` is the field in x402 2.x; `maxAmountRequired` is the older
    spelling, still served by live sellers. A reader that knows one of them
    reports a priced resource as free.

    Refused rather than coerced: raw is an integer string, so `1e26`, `0.0001`
    and a JSON number are all rejected. A JSON number cannot carry 30 digits
    exactly - `1e26` round-trips through a float and comes back short - and the
    whole point of raw is that the low digits are real money.
    """
    for name in ("amount", "maxAmountRequired"):
        if name not in entry:
            continue
        value = entry[name]
        if not isinstance(value, str) or not value.isdigit():
            raise ManifestRefused(
                "amount_not_raw",
                f"{name}={value!r} is not a raw integer string; a price in raw "
                "must be digits only, with no decimal point and no exponent"
                + (f" (on {where})" if where else ""),
            )
        return int(value)
    return None


def payees_from_manifest(doc: object) -> tuple[Payee, ...]:
    """The mainnet Nano accounts this x402 document says to pay.

    Takes the already-parsed document, so this touches no network and can be
    run against a file, a fixture or a response body.

    Two shapes are read, because they are different documents and a reader that
    knows only one of them is blind to half the sellers:

    - a **catalogue** (`resources: [{accepts: [...]}, ...]`), which is what a
      seller serves at `/.well-known/x402` to advertise everything it sells;
    - a single **challenge** (`accepts: [...]`), which is what one resource
      answers a 402 with.

    Entries on other rails are skipped in silence - a seller taking USDC beside
    XNO is doing nothing wrong. A Nano entry that cannot be paid is a different
    matter and is refused:

    - `payee_checksum`: a `payTo` that fails its own blake2b checksum. XNO sent
      there is gone, so it must never be reported as an address to pay.
    - `network_unspecified`: a bare `nano` or `xno`, which names the family and
      not the network. Nano's test networks share the `nano_` address prefix, so
      reading it as mainnet would be a guess about where money goes.
    - `not_mainnet`: `nano:testnet` and the like - a real answer, and not this.

    Payees are deduplicated by account identity, not by spelling: the `nano_`
    and `xrb_` forms of one account are one payee, and counting them twice would
    double a seller's apparent reach.
    """
    if not isinstance(doc, dict):
        raise ManifestRefused(
            "not_an_x402_document",
            f"expected a JSON object, got {type(doc).__name__}",
        )
    resources = doc.get("resources")
    if isinstance(resources, list):
        pairs = [
            (resource, resource.get("accepts"))
            for resource in resources
            if isinstance(resource, dict)
        ]
    elif "accepts" in doc:
        pairs = [(doc, doc.get("accepts"))]
    else:
        raise ManifestRefused(
            "not_an_x402_document",
            "no `resources` array and no `accepts` array, so this document "
            f"advertises nothing payable; keys present: {sorted(doc)}",
        )

    # Keyed by public key so that two spellings of one account collapse.
    found: dict[bytes, dict] = {}
    nano_entries = 0
    for resource, accepts in pairs:
        if not isinstance(accepts, list):
            continue
        name = resource.get("url") or resource.get("resource") or resource.get("name") or ""
        for entry in accepts:
            if not isinstance(entry, dict):
                continue
            network = _network_of(entry)
            if not _is_nano_network(network):
                continue
            nano_entries += 1
            if network in _NANO_FAMILY:
                raise ManifestRefused(
                    "network_unspecified",
                    f"network={network!r} names the Nano family but not the "
                    "network, and Nano's test networks share the nano_ address "
                    "prefix; nano:mainnet is the identifier for mainnet",
                )
            if network not in _MAINNET:
                raise ManifestRefused(
                    "not_mainnet",
                    f"network={network!r} is a Nano network and is not mainnet",
                )
            pay_to = entry.get("payTo")
            if not is_valid_account(pay_to):
                raise ManifestRefused(
                    "payee_checksum",
                    f"payTo={pay_to!r} is not an address XNO can be sent to: "
                    "its checksum does not match the key it carries",
                )
            price = _price_raw(entry, str(name))
            key = public_key_from_address(pay_to)
            slot = found.setdefault(
                key, {"account": str(pay_to), "network": network, "prices": set(), "resources": []}
            )
            if price is not None:
                slot["prices"].add(price)
            if name and name not in slot["resources"]:
                slot["resources"].append(str(name))

    if not found:
        raise ManifestRefused(
            "no_nano_payee",
            "this document advertises no Nano mainnet entry"
            + (f" ({nano_entries} Nano entry/entries were read and none held a payee)" if nano_entries else ""),
        )
    return tuple(
        Payee(
            account=slot["account"],
            network=slot["network"],
            prices_raw=tuple(sorted(slot["prices"])),
            resources=tuple(slot["resources"]),
        )
        for slot in found.values()
    )


def _history_page(account: str, rpc_url: str, head: str | None):
    payload = {
        "action": "account_history",
        "account": account,
        "count": str(HISTORY_PAGE),
    }
    if head is not None:
        payload["head"] = head
    # Looked up per call so that a stub takes effect, as nano_quorum does.
    reply = nano_settlement_verify.post_json(rpc_url, payload)
    if not isinstance(reply, dict):
        raise nano_settlement_verify.NotANodeReply(
            f"the endpoint's reply is {type(reply).__name__}, not a node's "
            "account_history reply"
        )
    history = reply.get("history")
    # A node answers an account it has never seen with history: "" rather than
    # an empty list. That is "no blocks", not a malformed reply, and an unopened
    # account genuinely has no payers - so it must not be refused as garbage.
    if history == "" or history is None:
        if "error" in reply:
            raise nano_settlement_verify.NotANodeReply(
                f"the endpoint refused the account_history call: {reply['error']!r}"
            )
        return []
    if not isinstance(history, list):
        raise nano_settlement_verify.NotANodeReply(
            f"account_history answered history={type(history).__name__}, "
            "which is not a list of blocks"
        )
    return history


def _int_field(block: dict, name: str, account: str) -> int:
    """An integer field of a history row, refused rather than coerced.

    `amount` is raw. A node sends it as a digit string; anything else - a float,
    a number in scientific notation, a missing field - means this reader does
    not understand the reply, and a row dropped from a sum makes a total that
    looks complete and is short by real money.
    """
    value = block.get(name)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise nano_settlement_verify.NotANodeReply(
            f"a history row of {account} carries {name}={value!r}, which is not "
            "an integer; the amounts in this chain cannot be summed safely"
        )
    text = str(value)
    if not text.isdigit():
        raise nano_settlement_verify.NotANodeReply(
            f"a history row of {account} carries {name}={value!r}, which is not "
            "a non-negative integer string"
        )
    return int(text)


def _confirmed(block: dict) -> bool:
    """Whether a node reported this block as confirmed.

    Nodes answer the string "true"; some proxies answer the JSON boolean. Only
    those two are confirmed - anything else, an absent field included, is read
    as not yet confirmed, which is the direction that cannot overstate what a
    seller has been paid.
    """
    value = block.get("confirmed")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _tally_chain(account: str, rpc_url: str, allow_partial: bool) -> PayerReport:
    """Walk one endpoint's view of `account`'s chain and tally who paid it.

    Everything that makes this endpoint's answer unusable - a reply that is not
    a node's, an amount that is not an integer, a source address whose checksum
    fails - is raised from here, so that `payers` can treat it as "ask the next
    node" rather than as a verdict. A node serving a broken or truncated ledger
    is a node to walk away from, not an answer about a seller.
    """
    identity = public_key_from_address(account)
    tally: dict[bytes, dict] = {}
    payments = 0
    received = 0
    self_payments = 0
    unconfirmed = 0
    seen_hashes: set[str] = set()
    oldest_height: int | None = None
    head: str | None = None

    for _ in range(HISTORY_PAGES_MAX):
        page = _history_page(account, rpc_url, head)
        fresh = 0
        for block in page:
            if not isinstance(block, dict):
                raise nano_settlement_verify.NotANodeReply(
                    f"a history row of {account} is {type(block).__name__}, not a block"
                )
            block_hash = str(block.get("hash") or "")
            # Paging by `head` re-serves the block it starts from, so the same
            # block arrives twice and would be counted twice. Identity is the
            # block hash, which is the one field that cannot repeat on a chain.
            if block_hash and block_hash in seen_hashes:
                continue
            if block_hash:
                seen_hashes.add(block_hash)
            fresh += 1
            height = _int_field(block, "height", account)
            if oldest_height is None or height < oldest_height:
                oldest_height = height
            if block.get("type") != "receive":
                continue
            if not _confirmed(block):
                unconfirmed += 1
                continue
            sender = block.get("account")
            amount = _int_field(block, "amount", account)
            sender_key = public_key_from_address(sender)
            if sender_key is None:
                raise nano_settlement_verify.NotANodeReply(
                    f"a receive on {account} names account={sender!r} as its "
                    "source, which is not an address whose checksum matches"
                )
            if sender_key == identity:
                self_payments += 1
                continue
            payments += 1
            received += amount
            when = _int_field(block, "local_timestamp", account)
            slot = tally.setdefault(
                sender_key,
                {"account": str(sender), "payments": 0, "raw": 0, "first": when, "last": when},
            )
            slot["payments"] += 1
            slot["raw"] += amount
            # A node reports local_timestamp as when IT saw the block, and 0
            # when it does not know, so these bound the relationship rather
            # than date it. A 0 must not become the "first" payment of an
            # account that has real timestamps.
            if when:
                slot["first"] = min(slot["first"], when) if slot["first"] else when
                slot["last"] = max(slot["last"], when)
        if oldest_height == 1 or fresh == 0 or len(page) < HISTORY_PAGE:
            break
        head = str(page[-1].get("hash") or "")
        if not head:
            break

    # Completeness is "we reached the open block", not "we read block_count
    # rows": account_history leaves out change and epoch blocks, so a complete
    # read of a chain that has any is legitimately shorter than its height.
    complete = oldest_height is None or oldest_height == 1
    if not complete and not allow_partial:
        raise HistoryIncomplete(account, len(seen_hashes), oldest_height or 0)

    ordered = sorted(tally.values(), key=lambda slot: (-slot["raw"], slot["account"]))
    return PayerReport(
        account=account,
        distinct_payers=len(ordered),
        payments=payments,
        received_raw=received,
        payers=tuple(
            Payer(
                account=slot["account"],
                payments=slot["payments"],
                received_raw=slot["raw"],
                first_timestamp=slot["first"],
                last_timestamp=slot["last"],
            )
            for slot in ordered
        ),
        self_payments=self_payments,
        unconfirmed_payments=unconfirmed,
        blocks_read=len(seen_hashes),
        complete=complete,
        asked=(rpc_url,),
    )


def payers(account: str, rpc_urls, *, allow_partial: bool = False) -> PayerReport:
    """Every account that has paid `account`, read off its own chain.

    `rpc_urls` is a sequence of node RPC endpoints, tried in order until one
    gives a **usable** answer - not merely an HTTP 200. An endpoint that serves
    a reply this cannot read, an amount that is not an integer, or a chain it
    cannot walk to the open block is one we move on from, because each of those
    produces a smaller seller rather than an error the caller can see. The
    report names the endpoint it came from, and it is still ONE node's view: for
    an audit a stranger is meant to trust, use `payers_corroborated`.

    Only a **receive** counts. A send on this chain is money leaving, and
    counting the whole chain would report the payee's own spending as income.
    Only a **confirmed** receive counts, and a receive from the payee itself is
    recorded as a self-payment rather than as a customer.

    Raises `HistoryIncomplete` when every endpoint answered and none reached the
    open block, unless `allow_partial=True`; `nano_quorum.NotCorroborated` when
    no endpoint gave a usable answer at all, which is "we could not look" and
    not "nobody has paid".
    """
    if not is_valid_account(account):
        raise ValueError(
            f"account={account!r} is not a Nano address whose checksum matches; "
            "nothing was asked of any node"
        )
    if not isinstance(allow_partial, bool):
        raise TypeError(
            f"allow_partial must be a bool, got {type(allow_partial).__name__}"
        )
    if isinstance(rpc_urls, str):
        raise TypeError("rpc_urls must be a sequence of URLs, not one URL string")
    endpoints = tuple(dict.fromkeys(rpc_urls))
    if not endpoints:
        raise ValueError("no endpoints given")

    failures: dict[str, str] = {}
    incomplete: HistoryIncomplete | None = None
    for rpc_url in endpoints:
        try:
            return _tally_chain(account, rpc_url, allow_partial)
        except HistoryIncomplete as error:
            # Kept rather than folded in with the unreadable ones: a node that
            # answered and could not reach the open block is a different
            # diagnosis from one that said nothing, and the caller can act on
            # it (raise the page cap, or take the short view knowingly).
            incomplete = incomplete or error
            failures[rpc_url] = f"{type(error).__name__}: {error}"
        except _UNREADABLE as error:
            failures[rpc_url] = f"{type(error).__name__}: {error}"
    if incomplete is not None:
        raise incomplete
    raise nano_quorum.NotCorroborated(answered=0, agree=1, failures=failures)
def payers_corroborated(
    account: str,
    rpc_urls,
    agree: int = nano_quorum.AGREE_DEFAULT,
    *,
    allow_partial: bool = False,
) -> PayerReport:
    """The payer set, only if `agree` independent endpoints report the same one.

    A payer count is a claim about someone else's business, so one node's word
    is the wrong instrument for it: a node serving a stale or partial ledger
    reports a smaller seller, and nothing in the answer looks wrong. Here each
    endpoint is asked on its own and the **sets of paying accounts** must match.

    Amounts and timestamps are deliberately NOT compared: `local_timestamp` is
    when each node saw a block and legitimately differs between them, so
    comparing it would turn ordinary disagreement about clocks into a refusal.

    Raises `nano_quorum.Disagreement` when two endpoints report different payer
    sets - that is a contradiction to look into, never something a third vote
    settles - and `nano_quorum.NotCorroborated` when too few could be reached,
    which is "we could not look".
    """
    endpoints = nano_quorum.distinct_endpoints(rpc_urls, agree)
    reports: list[PayerReport] = []
    failures: dict[str, str] = {}
    for endpoint in endpoints:
        try:
            reports.append(payers(account, [endpoint], allow_partial=allow_partial))
        except (HistoryIncomplete, nano_quorum.NotCorroborated) as error:
            failures[endpoint] = f"{type(error).__name__}: {error}"
        except (OSError, ValueError, KeyError, TypeError) as error:
            failures[endpoint] = f"{type(error).__name__}: {error}"
    if len(reports) < agree:
        raise nano_quorum.NotCorroborated(
            answered=len(reports), agree=agree, failures=failures
        )
    def _where(report: PayerReport) -> str:
        return report.asked[0] if report.asked else "?"

    first, *rest = reports
    expected = {public_key_from_address(payer.account) for payer in first.payers}
    for other in rest:
        got = {public_key_from_address(payer.account) for payer in other.payers}
        if got != expected:
            # Reported as who each endpoint named, not as a count: two endpoints
            # can agree on how many have paid and name different accounts, and a
            # count would hide exactly the disagreement worth looking into.
            raise nano_quorum.Disagreement(
                {
                    _where(report): sorted(payer.account for payer in report.payers)
                    for report in (first, other)
                }
            )
    return PayerReport(
        account=first.account,
        distinct_payers=first.distinct_payers,
        payments=first.payments,
        received_raw=first.received_raw,
        payers=first.payers,
        self_payments=first.self_payments,
        unconfirmed_payments=first.unconfirmed_payments,
        blocks_read=first.blocks_read,
        complete=first.complete,
        asked=tuple(url for report in reports for url in report.asked),
    )


def outside_payers(report: PayerReport, our_accounts) -> PayerReport:
    """The same report with our own accounts removed, so the count means something.

    The ledger records that an account sent XNO, never why, and a seller
    funding its own payee leaves blocks that look exactly like a customer's.
    This subtracts the accounts the caller declares its own - compared as
    accounts, so the `nano_` and `xrb_` spellings of one of them cannot slip
    through as a stranger - and leaves a count of people who are not us.

    Nothing here guesses which accounts are ours. An empty `our_accounts`
    returns the report unchanged, and says so by doing nothing, rather than
    inferring ownership from round amounts.
    """
    if isinstance(our_accounts, str):
        raise TypeError("our_accounts must be a sequence of addresses, not one address string")
    ours = set()
    for address in our_accounts:
        key = public_key_from_address(address)
        if key is None:
            raise ValueError(
                f"our_accounts carries {address!r}, which is not an address whose "
                "checksum matches; an account we cannot name is not one we can exclude"
            )
        ours.add(key)
    kept = tuple(
        payer for payer in report.payers if public_key_from_address(payer.account) not in ours
    )
    return PayerReport(
        account=report.account,
        distinct_payers=len(kept),
        payments=sum(payer.payments for payer in kept),
        received_raw=sum(payer.received_raw for payer in kept),
        payers=kept,
        self_payments=report.self_payments,
        unconfirmed_payments=report.unconfirmed_payments,
        blocks_read=report.blocks_read,
        complete=report.complete,
        asked=report.asked,
    )


def _main(argv: list[str]) -> int:
    """`python3 nano_payers.py <x402-url> [rpc-url ...]` - audit a seller.

    Fetching lives here and not in the functions above so that they stay
    offline and testable: everything this prints was derived from the seller's
    own document and the ledger, and a reader can repeat it with curl.
    """
    import sys
    import urllib.request

    if not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(
            "usage: python3 nano_payers.py <x402-document-url> [rpc-url ...]",
            file=sys.stderr,
        )
        return 64
    url, *rpc = argv
    rpc = rpc or ["https://rpc.nano.to"]
    request = urllib.request.Request(
        url, headers={"User-Agent": nano_settlement_verify.USER_AGENT}
    )
    with urllib.request.urlopen(
        request, timeout=nano_settlement_verify.RPC_TIMEOUT_S
    ) as response:
        doc = json.loads(response.read().decode("utf-8"))
    try:
        payees = payees_from_manifest(doc)
    except ManifestRefused as error:
        print(json.dumps({"verdict": error.reason, "detail": error.detail}))
        return 3
    out = []
    for payee in payees:
        try:
            report = payers(payee.account, rpc)
        except HistoryIncomplete as error:
            out.append({"payee": json.loads(payee.to_json()), "verdict": "history_incomplete",
                        "detail": str(error)})
            continue
        except nano_quorum.NotCorroborated as error:
            out.append({"payee": json.loads(payee.to_json()), "verdict": "node_unreachable",
                        "detail": str(error)})
            continue
        out.append({"payee": json.loads(payee.to_json()), "payers": json.loads(report.to_json())})
    print(json.dumps({"source": url, "payees": out}, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
