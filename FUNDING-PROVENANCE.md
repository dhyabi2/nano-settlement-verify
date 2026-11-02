# Funding provenance: who funded the payer?

A payment that arrives is not necessarily demand. If the payer's money came from the payee, from a
sponsor, or from a dispenser that hands the same amount to many accounts, the payment is subsidised use.
This page is the check by hand; `nano_funding.py` reproduces it and is tested against the same
recipe. Reads only. One node is enough (two make it stronger).

## The recipe: two kinds of call

1. Read the payment block. If you were given the payee's **receive** block, its `contents.link` is the
   send it pockets: read that one. **Payment time is the payer's send block's `local_timestamp`, never
   the receive's.** A receive is stamped when the payee pocketed, which can be days later; reading
   payment times off receive blocks makes a batching payee look like a scripted payer.

```sh
curl -s -A 'Mozilla/5.0' -H 'Content-Type: application/json' -d '{"action":"block_info","json_block":"true","hash":"<SEND>"}' https://rpc.nano.to
```
   Take `block_account` (the payer), `contents.link_as_account` (the payee), `contents.previous`, `local_timestamp`.

2. Read the payer's history from the block before the send, and keep the `receive` entries inside your
   window (default 720 hours) before the payment. `account` in each entry is who funded it.

```sh
curl -s -A 'Mozilla/5.0' -H 'Content-Type: application/json' -d '{"action":"account_history","account":"<PAYER>","count":"100","head":"<PREVIOUS>"}' https://rpc.nano.to
```

3. For each distinct funder, read its own history. A funder that sent the **same amount to ten or more
   distinct accounts** is a dispenser. This is found from the ledger; no flag can switch it off.

## Labels

| label | when |
|---|---|
| `dispenser_funded` | any funder in the window is a dispenser (checked first, never suppressed) |
| `self_funded` | a funder is the payee, or an account you pass with `--self` |
| `sponsor_funded` | a funder is an account you pass with `--sponsor` (a label you chose; the output names the flag, drop it and re-run) |
| `unsubsidised` | receives were read for the whole window and none is flagged |
| `unknown` | no receive in the window, history not read back to the window start, nodes disagree, payment unconfirmed or not a send, payment time unreadable |

The label says whether a flagged or dispenser funder sent anything in the window, not how much. Each
funder row carries `received_raw` and `share_of_window_receives_ppm` so you can see a tiny starter
beside a large unflagged receive and judge for yourself. Dispenser detection reads at most the last 500
blocks of a funder; a funder with more history is judged on that.

## Worked example (public, read 2026-11-02 from three nodes that agreed)

Payment `B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F`, payer
`nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr`, payment time 1790150110.
The payer's receives before it:

| funder | received | share | label |
|---|---|---|---|
| `nano_1banexkcfuieufzxksfrxqf6xy8e57ry1zdtq9yn7jntzhpwu4pg4hajojmq` | 6.275603217158176 XNO | 999,998 ppm | unsubsidised (2 distinct recipients seen) |
| `nano_1434j1n4sin4cefs5njibag4tsmo596fmg3s6bdogtod3ndmdfez5yuebrh9` | 0.00001 XNO (`7085E9C0...`) | 1 ppm | dispenser (same 0.00001 to 12 distinct accounts) |

Verdict `dispenser_funded`, and the decisive receive is the 0.00001 XNO starter from
`nano_1434j1n4...`, which is the account Unstuck sends its first transactions from. So the payer of
the example block was seeded by us, with one part per million of what it later held. Both facts are in
the table; the label follows the rule, the share lets you weigh it. Run it:

```sh
python3 nano_funding.py --block B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F --json
```

Exit codes: 0 unsubsidised, 3 any subsidy label, 2 unknown.

## What this does not prove

A payer funded by a dispenser may still have bought something it wanted; a payer with no flagged funder
may still be the payee's friend. Funding is evidence about subsidy, not about identity or intent.
