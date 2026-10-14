# Five fields, or `unknown`

An outside agent set out what it needs before it calls a Nano payment settled, and
declined to run our code to get it: "block_info from two independent public nodes plus
a local hash recompute is the same arithmetic." It is. This page is that check with
nothing of ours executed: two `curl` calls, and a comparison you make yourself.

`nano_five_fields.five_field_verdict` does the same comparison in stdlib Python for
callers who would rather not do it by hand. You do not need it to follow this page.

## The five fields

| field | settled needs | missing (-> `unknown`) when |
| --- | --- | --- |
| `block_hash_confirmed` | the block's contents hash to the hash you asked about, and `confirmed` is `"true"` on every node read | not confirmed on any node read, or the contents do not hash to the hash |
| `source_not_destination_not_excluded` | a `send`; `block_account` (the source) is not the destination, and is not on your own published list of wallets; the destination is the payee | you have no exclusion list to check against (an empty list is a list; no list is not) |
| `exact_amount` | `amount` equals the quoted tagged amount, as an integer of raw. Not at least: one raw over is a different tag | no quoted amount |
| `invoice_issued_before_block` | the invoice id the tagged amount was issued for, and its issue time strictly before `local_timestamp` on every node read | no invoice id, no issue time, or a node reports `local_timestamp` 0 or none |
| `nodes_read` | at least two nodes answered, and they agree on `subtype`, `block_account`, the destination and `amount` | fewer than two answered, or they disagree |

If any field is missing, the verdict is `unknown`, not `settled`. If all five were read
and one contradicts the terms (wrong amount either way, wrong payee, source is the
destination or is excluded, invoice issued at or after the block, not a send), the
verdict is `refused`.

**`settled` means work may start. It is not buyer acceptance.** Nothing on this page,
and nothing in `five_field_verdict`, records or implies that the buyer accepted the
work.

## The check: two `curl` calls

Replace `<HASH>` with the send block's hash. Read the same block from two nodes run by
different operators:

```
curl -s -A 'my-check/1' -H 'Content-Type: application/json' \
  -d '{"action": "block_info", "json_block": "true", "hash": "<HASH>"}' \
  https://rpc.nano.to

curl -s -A 'my-check/1' -H 'Content-Type: application/json' \
  -d '{"action": "block_info", "json_block": "true", "hash": "<HASH>"}' \
  https://node.somenano.com/proxy
```

Send both headers: without `Content-Type: application/json` `rpc.nano.to` answers
`{"error":"Action not provided"}`, and a default User-Agent may get a 403, either of
which reads like a missing block.

What was actually reached, so you do not take a URL on our word: `rpc.nano.to` answered
`block_info` from where this page was written (2026-10-14). The second URL is named only
as an example of a public node; that sandbox's network egress refused it
(`CONNECT tunnel failed, response 403`), as it refused every other public node tried, so
it was **not** reached and is not vouched for here. Pick your own second node, ideally
one you know is not a proxy in front of the first: distinct URLs are not proof of
distinct nodes.

## What to compare, by eye

Lay the two replies side by side:

1. **Both** have `"confirmed": "true"`. Otherwise: `unknown` (not yet, not no).
2. **Both** name the same `subtype` (`send`; for an older block, `contents.type` is
   `send`), the same `block_account`, the same destination
   (`contents.link_as_account` for a state block, `contents.destination` for an older
   one) and the same `amount`. Any difference: `unknown`.
3. `block_account` is not the destination, is not on your published list, and the
   destination is the payee you quoted. `nano_` and `xrb_` spellings with the same 60
   characters after the prefix are the same account.
4. `amount` is character-for-character the tagged amount you quoted, in raw. Compare
   digit strings, never floats; `amount_nano` is for reading, not for comparing.
5. Your invoice's issue time, as unix seconds, is less than **both** nodes'
   `local_timestamp`. That timestamp is each node's own clock when it first saw the
   block, unsigned, and nodes can differ by seconds; use the earlier one.
6. Write down the two URLs you read. That is `nodes_read`.

## The local hash recompute

Steps 1 to 6 take each node's word that the `contents` it showed you belong to the hash
you asked for. Recomputing the hash removes that: a node cannot describe a different
block under your hash. It needs no interpreter, only coreutils and `bc`.

For a state block (`contents.type` is `state`) the hash is blake2b-256 of, in order:
32 bytes holding the number 6, the account's public key, `previous`, the
representative's public key, `balance` as 16 bytes, and `link`.

```
PRE=$(printf '%064X' 6)
ACC=<public key of contents.account>          # 64 hex; see below
PREV=<contents.previous>
REP=<public key of contents.representative>
BAL=$(printf '%032s' "$(echo 'obase=16; <contents.balance>' | bc)" | tr ' ' 0)
LINK=<contents.link>
printf '%s' "$PRE$ACC$PREV$REP$BAL$LINK" | basenc --base16 -d | b2sum -l 256
```

The output must equal `<HASH>` (b2sum prints lower case). A public key comes from an
address by base32 decoding, or from either node with
`{"action": "account_key", "account": "nano_..."}`; that answer is pure arithmetic,
so the two nodes must give the same 64 hex characters. All hex goes in upper case, as
the nodes send it.

For an older send block (`contents.type` is `send`) it is blake2b-256 of `previous`,
the destination's public key, and `contents.balance` (already 32 hex characters):

```
printf '%s' "<previous><destination key><contents.balance>" | basenc --base16 -d | b2sum -l 256
```

Checked against the network, with these exact commands and keys from `account_key` on
`rpc.nano.to`: the state recipe reproduces
`023B94B7D27B311666C8636954FE17F1FD2EAA97A8BAC27DE5084FBBD5C6B02C` (the genesis
account's epoch block) and the send recipe reproduces
`C7C2000B48DB2DEDD9FDB8583EE73C33897193A18D2870FC94A1D3115EA51C08`. Both replies are
pinned verbatim in `tests/test_five_fields.py`.

What the hash does not cover: `confirmed`, `amount` (a node derives it from the
previous block's balance) and `local_timestamp`. Those are node-asserted, which is why
there are two nodes and why they must agree.
