# Check a Nano payment for free: no node, no account, no budget

For a seller or agent with no node and no outgoing budget. Reading a block costs nothing: several public
nodes answer `block_info` with no key (see `FALLBACK-NODES.md`; probed 2026-11-02).

## One command, nothing to install

```sh
curl -s -A 'Mozilla/5.0' -H 'Content-Type: application/json' \
  -d '{"action":"block_info","json_block":"true","hash":"B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"}' \
  https://rpc.nano.to
```

Read three fields: `confirmed` is `"true"`, `contents.link_as_account` is your address, `amount` is exactly the raw
amount you quoted (1 XNO = 10**30 raw; compare as integer strings).

## The same check as a tool (stdlib only)

```sh
git clone https://github.com/dhyabi2/nano-settlement-verify && cd nano-settlement-verify
python3 free_verify_fetch.py --quote quote.json     # quote.json: quote_id, pay_to, amount_raw, block_hash
```

Offline against a reply you already hold: `python3 free_verify.py --quote quote.json --block reply.json`.

| verdict | exit | meaning |
|---|---|---|
| `settled` | 0 | confirmed send to `pay_to` for exactly `amount_raw` |
| `unknown` | 2 | not found, not confirmed yet, node error, no block named |
| `mismatch` | 3 | confirmed, but wrong amount, wrong payee, or not a send |

There is no `unpaid` verdict and there cannot be one: a read that finds nothing means "not yet", and treating it as
unpaid is how a retry pays twice. If you need "unpaid", take it from your own quote expiry. A balance check cannot
answer "was this paid" and `check_from_balance` refuses to try.

Every verdict carries `cost_usd: 0.0`; `cost_report` computes "N settlement checks, 0.0 USD outgoing, no node run, no
account required" from the verdicts instead of asserting it.

## Host it yourself (optional)

`python3 free_verify_fetch.py --serve 8080` serves `GET /verify?pay_to=&amount_raw=&block=` on 127.0.0.1 with a
30-checks-a-minute limit per address and no key. Put your own TLS in front. No hosted instance is run by us.

## What this does not do

One node's word is still one node's word: this tool trusts the reply it fetches. To not trust it, recompute the
block hash (`TWO-NODE-CHECK.md`) and compare two nodes (`nano_two_node.py`). It reads only; it never sends, signs
or holds a key.
