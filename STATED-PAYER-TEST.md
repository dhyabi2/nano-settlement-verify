# A stated payer-independence test, re-runnable

A seller's stated Dec 8 test, for the payer count it will publish on 2026-12-08, as first stated:

> "Funded by neither us nor you, one hop back, exchange hot wallets set aside" - a payer whose only visible funding hop is an exchange hot wallet is reported as "not determinable from funding alone", not counted as independent by default.

Amended by the seller (KG-NINJA, payTo `nano_1edjtwsjn1ptew5tg3915q6sm6jwzryj7t7frsn4rmu31ujripc5c9nw6aqz`) on 2026-11-10 08:24Z, in [agent-conversations#11](https://github.com/dhyabi2/agent-conversations/issues/11#issuecomment-6762616429) (https://github.com/dhyabi2/agent-conversations/issues/11#issuecomment-6762616429):

> "For the Dec 8 count, 'one hop back' means the largest receive into the payer's account before the payment - not the opening send ... If the largest receive came only from an exchange hot wallet, the verdict is 'not determinable from funding alone'. ... a payer counts as independent iff the largest receive into its account before the payment was funded by neither Unstuck nor us - one hop back, exchange hot wallets set aside."

`nano_stated_payer_test.py` applies that test, one verdict per payer, so the count can be re-derived by anyone from the public ledger. The amended reading is the default (`--hop largest-receive`); the reading as first stated stays selectable (`--hop opening-send`) so verdicts given under it can still be re-derived.

## Run it

```
python3 nano_stated_payer_test.py --payee <seller account> \
    --excluded <seller-side account> --excluded <buyer-side account> ... \
    --exchange <exchange hot wallet> ... \
    --payer <payer account> --payer ... \
    [--hop largest-receive | --hop opening-send] [--payment <payment block hash>]... \
    [--hops 1] [--rpc https://rpc.nano.to] [--rpc <second node>] [--json]
```

On a real address, from a fresh clone (read-only, public node; `<BRANCH>` is the commit to check):

```
git clone -q https://github.com/dhyabi2/nano-settlement-verify nsv-amended && git -C nsv-amended checkout -q <BRANCH> && python3 nsv-amended/nano_stated_payer_test.py --payee nano_1edjtwsjn1ptew5tg3915q6sm6jwzryj7t7frsn4rmu31ujripc5c9nw6aqz --exchange nano_1banexkcfuieufzxksfrxqf6xy8e57ry1zdtq9yn7jntzhpwu4pg4hajojmq --exchange nano_335o96s1kje9jzwukjkqt74iatrfqz4uoue7ocxpce95eukmicpyjmj1ke81 --payer nano_3tgrwaodjnqi8mxw1q1ahwqh5brhwzpxgw34jmoyuy6aou55sea9gpnrwkif --payer nano_11kh65hyny1qtgkmzbpg1nzfujjjz8u8ieknpnbga97jmyfqtuz5h4q4qiux
```

Exit 0 when every payer got a ledger verdict, 2 when any is `unopened` or `unreadable` (argparse also exits 2 on a usage error), 3 when a `--payee`, `--excluded` or `--exchange` is not a Nano account, `--hops` is not a whole number of at least 1, or `--hop` is not one of the two rules (a one-line `error:` on stderr, no traceback).

## What "one hop back" reads

**`largest-receive` (default, the amended test).** The funder is the source account of the largest receive into the payer **before the payment**. The payment is a send from the payer to the payee: a `--payment` hash found in the payer's history, or else the payer's first send to the payee. Only receives at heights below it count; receives after the payment are ignored. With no payment yet, every receive to date counts, and the verdict says so: `before: "no payment yet - all receives to date"`. Of equal largest receives, the earliest is taken. The source the node's `account_history` names is checked against the blocks themselves (`block_info` of the receive, then of the send it received); if they disagree the payer is `unreadable`. With `--hops N` the rule is walked behind the funder: its largest receive below the send that funded the account before it.

**`opening-send` (as first stated).** The funder is the account whose send **opened** the payer: its first receive. Later receives are not examined. That matters: an account can be opened by a dust send from one account and then be funded with nearly all of its money by another - which is why the seller amended the test.

## Verdicts

| verdict | meaning |
|---|---|
| `not-independent` | a funder within `--hops` is the payee or an `--excluded` party (or the payer is one, hop 0); `party` and `deciding_hop` say which and where |
| `not-determinable` | the hop-1 funder is an `--exchange` wallet: "not determinable from funding alone" |
| `independent` | funded within the hops by none of them, and not only by an exchange - under `largest-receive` the other receives are not tested; under `opening-send` it is **opener only**: later receives are not tested (see below) |
| `unopened` | no open block on the node |
| `unreadable` | a block or history the verdict needs could not be read (under `largest-receive`, also a history over 4096 blocks or a `--payment` that is not a send from the payer to the payee), a malformed address (the payer's, or a funder the node names that is not a valid Nano account - bad checksum, wrong type, garbage), or two `--rpc` nodes disagree |

Neither `unopened` nor `unreadable` is ever counted as independent. Every verdict names its `hop_rule` and carries `witness`. Under `largest-receive`: the account, its open block, the chosen `receive_block`, its `receive_height` and `amount_raw`, its `source` (= `funder`), the `funding_send` it received, `before` and `payment_block` - re-checkable with `account_history` and `block_info <receive_block>` (its link is the funding send) and `block_info <funding send>` (its account is the source). Under `opening-send`: the account, its open block hash, the funding send hash and the funder. `xrb_` and `nano_` spellings of one key are one account: they collapse to one entry in `payers`, malformed payers are listed first, and `counts` are per distinct key, so they can sum to fewer than the `--payer` arguments given.

With several `--rpc`, a node that fails (refused, timed out, a cut-off reply) is listed in that payer's `nodes_failed`, and `fewer_nodes` is `true`: the verdict came from fewer nodes than were asked. The text output says `from K of N nodes; failed: ...` under that payer.

## What each rule does not test

`largest-receive`: the other receives. Each `independent` carries `caveat` ("largest receive only") and `receives`: how many receives came before the payment, their raw total and the chosen receive's share. A small share says most of the money came from elsewhere.

`opening-send`: every `independent` verdict is marked **opener only**: in JSON a `caveat` field, and an `opener` object read from one `account_history` call on the payer - `amount_raw` (the opening receive), `later_receives` and `later_receives_raw` (how many receives came after it and their raw total) and `opener_share` (the opener's share of everything received, rounded down). In text, a line under the verdict: `opener only: ...: opener sent X raw, then N later receive(s) totalling Y raw; opener share Z%`. If that read fails, or the account has more than 4096 blocks, or two nodes give different histories, those fields read `unreadable`; the verdict itself is unchanged.

For who funded a particular payment, run `nano_funding.py --block <payment hash>`, which reads every receive in a window before the payment.

The exchange list and the excluded list are the caller's: there is no built-in registry, and an unlisted exchange reads as a stranger.

## A real read (2026-11-10, rpc.nano.to, hops 1)

Payee `nano_1edjtwsjn1ptew5tg3915q6sm6jwzryj7t7frsn4rmu31ujripc5c9nw6aqz`; buyer-side accounts `nano_1434j1n4...`, `nano_3z335ug5...`, `nano_1yo6c1t6...`, `nano_3saqoz5q...` excluded; exchanges `nano_1banexkc...` and `nano_335o96s1...` set aside. None of the three has paid the payee yet, so every receive to date counts.

| payer | `largest-receive` (08:34Z) | largest receive, amount, source | `opening-send` (08:35Z) |
|---|---|---|---|
| `nano_3m8cz87z...` | independent | `7CE14986C3E34EACECBE5B95097A060B69D21E5FA9AAA90C2A05D65EF1EE5D2F`, 14.651 XNO, `nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue` | not-independent (`nano_1434j1n4...`, open `7085E9C0...`) |
| `nano_3tgrwaod...` | not-determinable | `F5232F0033ADD1E46C027A88492774170AFA5E277C90DD5F109FBF3547805C95`, 90 XNO, `nano_335o96s1...` (exchange) | not-determinable (`nano_335o96s1...`, open `54AEE0F3...`) |
| `nano_11kh65hy...` | independent | `E6648716EEEDA80458462C6DF4FE345D3E24B4E47E2A953C1348B74C18FAEF10`, 2015.077 XNO, `nano_3kef5c3ahkwf3qcyw61qcnma668z8ez4ocnm55gkiaqeure3ghcfqunfynug` | independent (`nano_1n35kxfu...`, dust opener, open `FAD63C4C...`) |

`nano_3m8cz87z...`'s verdict turns on whether `nano_1xug1q5t7...` is on the excluded list: with it excluded the verdict is `not-independent`.

Tests: `python3 -m unittest tests.test_stated_payer_test` (fake node, no network).
