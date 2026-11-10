# A stated payer-independence test, re-runnable

A seller's stated Dec 8 test, for the payer count it will publish on 2026-12-08:

> "Funded by neither us nor you, one hop back, exchange hot wallets set aside" - a payer whose only visible funding hop is an exchange hot wallet is reported as "not determinable from funding alone", not counted as independent by default.

`nano_stated_payer_test.py` applies exactly that test, one verdict per payer, so the count can be re-derived by anyone from the public ledger.

## Run it

```
python3 nano_stated_payer_test.py --payee <seller account> \
    --excluded <seller-side account> --excluded <buyer-side account> ... \
    --exchange <exchange hot wallet> ... \
    --payer <payer account> --payer ... \
    [--hops 1] [--rpc https://rpc.nano.to] [--rpc <second node>] [--json]
```

Exit 0 when every payer got a ledger verdict, 2 when any is `unopened` or `unreadable`.

## Verdicts

| verdict | meaning |
|---|---|
| `not-independent` | a funder within `--hops` is the payee or an `--excluded` party (or the payer is one, hop 0); `party` and `deciding_hop` say which and where |
| `not-determinable` | the hop-1 funder is an `--exchange` wallet: "not determinable from funding alone" |
| `independent` | funded within the hops by none of them, and not only by an exchange |
| `unopened` | no open block on the node |
| `unreadable` | a block the verdict needs could not be read, a malformed address, or two `--rpc` nodes disagree |

Neither `unopened` nor `unreadable` is ever counted as independent. Every verdict carries `witness`: the account, its open block hash, the funding send hash and the funder, so it can be re-checked on any node with `block_info <open block>` (its link is the funding send) and `block_info <funding send>` (its account is the funder). `xrb_` and `nano_` spellings of one key are one account.

## What "one hop back" reads, and what it does not

The funder is the account whose send **opened** the payer: its first receive. Later receives are not examined, because the stated test does not examine them. That matters: an account can be opened by a dust send from one account and then be funded with nearly all of its money by another. On the 2026-11-10 read below, `nano_11kh65...` was opened by 0.000123 XNO from `nano_1n35kxfu...` and then received 391 XNO from `nano_1p3uejeji...`; under the stated test it reads `independent`. For who funded a particular payment, run `nano_funding.py --block <payment hash>`, which reads every receive in a window before the payment.

The exchange list and the excluded list are the caller's: there is no built-in registry, and an unlisted exchange reads as a stranger.

## A real read (2026-11-10, rpc.nano.to, hops 1)

Payee `nano_1edjtwsjn1ptew5tg3915q6sm6jwzryj7t7frsn4rmu31ujripc5c9nw6aqz`; five buyer-side accounts excluded; exchanges `nano_1banexkc...` and `nano_335o96s1...` set aside.

| payer | verdict | funder | open block |
|---|---|---|---|
| `nano_3m8cz87z...` | not-independent (excluded, hop 1) | `nano_1434j1n4...` | `7085E9C0719609BE3CBB590CF77EFE58084B3FDF7BECAFA77C53F60B84E79B0E` |
| `nano_3tgrwaod...` | not-determinable | `nano_335o96s1...` (exchange) | `54AEE0F3F5E9722471702566CD99E1F35B3060EFE0746313B6EF6646B54B17BF` |
| `nano_11kh65hy...` | independent | `nano_1n35kxfu6out49sed1jbg7poxyx1737qdnikitp1a1ennof1ysbcr6uzpeky` | `FAD63C4C49AA7FDF07AC9D38ABDAF32DF579EC6B56ACA65C859824E3D55F7774` |

Tests: `python3 -m unittest tests.test_stated_payer_test` (fake node, no network).
