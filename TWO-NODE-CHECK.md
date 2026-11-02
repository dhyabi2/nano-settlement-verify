# Two nodes, five fields, nothing of ours executed

You do not have to run our code to check that a Nano payment settled. This page is the whole check:
two `curl` calls, one hash recompute with the Python standard library, and a decision table you apply
yourself. `nano_two_node.py` in this repository does the same comparison and is tested to send the
same request as the first call below, but you do not need it. It adds nothing.

## 1. The two calls

Send both headers: without `Content-Type: application/json`, `rpc.nano.to` answers
`{"error":"Action not provided"}`, and a default User-Agent can be answered 403. Either reads like a
missing block when it is only a refused request.

```sh
curl -s -A 'Mozilla/5.0' -H 'Content-Type: application/json' -d '{"action":"block_info","json_block":"true","hash":"<BLOCK>"}' https://rpc.nano.to
curl -s -A 'Mozilla/5.0' -H 'Content-Type: application/json' -d '{"action":"block_info","json_block":"true","hash":"<BLOCK>"}' https://node.somenano.com/proxy
```

Two URLs are not proof of two operators. Choose a second node you know is not a proxy in front of the
first; a third (`https://nanoslo.0x.no/proxy` answered the example block identically on 2026-11-02)
costs one more line.

## 2. What to read out of each reply

| # | Field | JSON path in the reply |
|---|---|---|
| 1 | block is final | `confirmed` must be `"true"` (see "confirmed and cemented" below) |
| 2 | who paid whom | `block_account` (source), `contents.link_as_account` (destination), `subtype` must be `send` |
| 3 | amount | `amount`, a string of integer raw |
| 4 | when | `local_timestamp` (unix seconds, this node's own clock) |
| 5 | agreement | the same four values from both nodes |

## 3. The local recompute

A node's `contents` are only worth reading if they hash to the hash you asked about. Save one reply
as `reply.json` and run this (standard library only; it decodes the account and prints the hash the
contents actually have):

```python
import hashlib, json
d = json.load(open("reply.json"))["contents"]
A = "13456789abcdefghijkmnopqrstuwxyz"
def pk(addr):
    n = int("".join(format(A.index(c), "05b") for c in addr.split("_")[1][:52]), 2)
    return (n & ((1 << 256) - 1)).to_bytes(32, "big")
h = hashlib.blake2b(digest_size=32)
for part in (b"\x00" * 31 + b"\x06", pk(d["account"]), bytes.fromhex(d["previous"]),
             pk(d["representative"]), int(d["balance"]).to_bytes(16, "big"), bytes.fromhex(d["link"])):
    h.update(part)
print(h.hexdigest().upper())
```

The printed value must equal `<BLOCK>`. It is a blake2b-256 over the state-block preamble, account,
previous, representative, balance (16 bytes) and link; it is not a sha256.

## 4. The decision table

| Field | `settled` needs | Otherwise |
|---|---|---|
| 1 confirmed, hash recomputed | `confirmed` is `"true"` on every node read and the recompute matches | `unknown` |
| 2 who paid whom | `subtype` is `send`; destination is the payee you meant; source is not the destination; source is not on your own list of wallets | wrong payee, self-send, listed source: `refused`. No list supplied: `unknown` |
| 3 amount | `amount` equals the tagged amount you quoted, as integers. Not "at least": one raw over is a different tag | any difference: `refused` |
| 4 order | your invoice's issue time is strictly before `local_timestamp` on every node | equal or later: `refused`; a `local_timestamp` of `0` or none: `unknown` |
| 5 nodes | at least two distinct nodes answered and agree on field 2 and 3 | one node, or disagreement: `unknown`. Never resolved by majority, never by preferring one node |

`settled` is the only verdict that starts work. A missing field is ignorance, not a refusal, which is
why `unknown` and `refused` are different words and different exit codes (2 and 4) in the tool.

## 5. The two ways it says `unknown`, and what `settled` is not

`unknown` happens when something was not read (fewer than two nodes, a node that errored, a missing
invoice time, no list of your own wallets) or when the nodes disagree. **`settled` means: I can start
work. It is NOT buyer acceptance.** Nothing here records that the buyer accepted the output.

### confirmed and cemented

A Nano node's `block_info` reports `confirmed: "true"` for a block at or below its account's
confirmation height; that is the node's word for cemented, and there is no separate `cemented` key in
the reply. A block that is merely published and not yet confirmed reports `"false"` and is `unknown`
here. This is the objection `atlastr_oz` raised (a finality delay can be abused before cementing); the
answer is that this check refuses to say `settled` until every node you read says confirmed.

## 6. Worked example (public block, checkable against a third party)

Block `B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F`, a real 0.0005 XNO send.
Read on 2026-11-02 from `rpc.nano.to`, `node.somenano.com/proxy` and `nanoslo.0x.no/proxy`, identical:

- `confirmed` `"true"`, `subtype` `send`, `local_timestamp` `1790150110`
- source `nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr`
- destination `nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7`
- `amount` `500000000000000000000000000` (0.0005 XNO; `bazhao` published the same figure independently)
- the recompute in section 3 prints the block hash above

Suppose you quoted exactly `500000000000000000000000000` raw, issued the invoice at unix `1790150000`
(before the block) and the destination is your payee: every row passes, so the verdict is `settled`.
With the tool, same inputs:

```sh
python3 nano_two_node.py --block B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F \
  --amount-raw 500000000000000000000000000 --invoice-id example --invoice-issued-at 1790150000 \
  --payee nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7 --json
```

Change `--amount-raw` by one raw and it exits 4 (`refused`); give `--node` only once and it exits 2.

## 7. What this does not prove

Not that the work was done; not that the buyer authorised or accepted anything; not that the same
buyer was not charged twice; not that the operator consents to your use of the money. `local_timestamp`
is the answering node's own clock when it first saw the block, not ledger truth, and a node can be
wrong or dishonest, which is the reason for two. A payer address is a key, not an identity.
