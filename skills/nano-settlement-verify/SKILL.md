---
name: nano-settlement-verify
description: Check that a Nano (XNO) payment really settled before you serve a paid call. Give it the block hash, the amount you quoted and your account; it asks a public Nano node and returns one JSON verdict. Read-only - no seed, no wallet, no node of your own.
version: 1.0.0
metadata:
  openclaw:
    requires:
      bins:
        - python3
    envVars:
      - name: NANO_RPC_URL
        required: false
        description: Nano node RPC to ask, http or https with no credentials in it. Defaults to https://rpc.nano.to (an empty or blank value counts as unset)
---

# nano-settlement-verify

Use this when you SELL something for Nano (XNO) - a pay-per-call endpoint, an x402 route, a
paid answer - and a buyer hands you a block hash as proof of payment. It answers one question:
did that block really pay you the amount you quoted, and has the network confirmed it?

It only reads. It never holds a seed, signs, or sends. To hold or spend Nano, use a wallet
skill; this one is the seller's check.

## Run it

```
python3 verify_cli.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL] [--not-after WHEN]
```

- `BLOCK_HASH` - the 64-hex send block the buyer gave you.
- `EXPECT_RAW` - the amount you quoted, in raw, as an integer. 1 XNO is 10^30 raw
  (`1000000000000000000000000000000`); 0.001 XNO is `1000000000000000000000000000`.
  Never use a decimal or a float here: it loses the low digits.
- `ACCOUNT` - the `nano_` account you expected to be paid.
- `RPC_URL` - optional; any Nano node RPC. Defaults to `$NANO_RPC_URL`, else
  `https://rpc.nano.to`. The argument wins over the variable, so a per-call endpoint
  overrides the operator's default. An empty or blank `NANO_RPC_URL` counts as unset, and
  whitespace around the URL is ignored.
  It may be a comma-separated list of fallback nodes (`"https://rpc.nano.to,https://node2.example"`):
  the next is asked only when one could not be reached or read (exit 4), any answer is final,
  and the verdict then carries `"rpc_index"`, the 0-based position of the node that answered.
  In a list every entry must be `https`, except a node on this machine (`localhost`,
  `127.0.0.1`, `::1`), and a TLS failure stops the list: no other node is asked. A comma
  inside one URL (`?keys=a,b`) is not a list; the URL is split only when every piece starts
  with a scheme. A single URL whose query holds `,https://...` or ends in a comma reads as a
  list: write that comma as `%2C`. An http entry for this machine is refused whenever
  `http_proxy` is set, whatever `no_proxy` says.
  The URL must be `http` or `https` and must not contain an `@` anywhere: a URL with
  credentials (`http://user:password@host/`), with an `@` in its path or query, of any
  other scheme (`file://`, `ftp://`, none), with a port that is not a number from 0 to
  65535 (`http://host:key:443/`, `:99999`), or that cannot be parsed, is refused before
  anything is sent, as `node_unreachable` (exit 4) with a fixed detail that quotes no part
  of the URL. Credentials are never sent: put a node's key somewhere other than the URL's
  userinfo. An `@` that belongs in a path or query is written `%40`
  (`http://host/?key=a%40b`), and is sent as written; a `%40` or `%3A` in the host part
  (`http://user:pw%40host/`) is a credential and is refused the same way. So is a URL
  with no host (`http:///x`), with a control character anywhere, or whose host is not plain
  ASCII letters, digits, `-`, `.` and `_` or an IP address: no `%` in the host (a bracketed
  IPv6 zone id `[fe80::1%25eth0]` excepted), because urllib would decode it and a TLS error
  would print the decoded host; write an international name in its `xn--` form.
  When the node cannot be reached, the detail never quotes the URL: an HTTP error is
  `HTTP Error <code>` without the server's reason phrase; a connection, DNS or timeout
  failure is `[Errno N] strerror` (`<urlopen error [Errno 111] Connection refused>`); a proxy
  that refuses the tunnel is `<urlopen error Tunnel connection failed: 407>`, its status
  code without its reason phrase; a TLS
  failure is its reason and certificate message, which may name the host; a reply that is
  not a node's is the library's own text about it; anything else, `InvalidURL` included,
  is the error's class name only. A node you give a key to in its URL can still repeat it
  in its reply - as a mismatch's `got`, say - so give a key only to a node you trust.
- `--not-after WHEN` - optional; the order's expiry, as unix seconds or ISO-8601 with an
  offset (`2026-10-09T18:00:00Z`). Inclusive: a block seen exactly at `WHEN` is on time.

**What "seen" means.** A Nano block carries no sender-signed time. `seen_at` is the node's
`local_timestamp` - when THAT node first saw the block, on its own clock - and another node
can differ by seconds; very old blocks report `0`, which is `unknown_time`, never on time.
The expiry is checked against the node you chose: read it on two nodes if seconds matter.

## Read the verdict

One JSON object on stdout, and an exit code:

| exit | meaning | what to do |
| --- | --- | --- |
| 0 | `{"settled": true, ...}` - confirmed send of exactly that amount to that account | serve the call |
| 2 | `{"settled": false, ...}` - the node has the block but has not confirmed it | wait a second and ask again |
| 3 | `{"verdict": "mismatch"}`, `{"verdict": "no_such_block"}` or `{"verdict": "invalid_amount"}` - wrong amount, wrong payee, no such block, or an `EXPECT_RAW` that is not a whole number of raw | do not serve |
| 4 | `{"verdict": "node_unreachable"}` - nothing was checked, including a reply that is not a JSON object (a proxy's `"error"`, a bare number) | retry or try another RPC; do NOT treat this as a bad payment. A detail of *the RPC URL contains '@'*, *must be http or https*, *port must be a number from 0 to 65535*, *has no host*, *contains a control character*, *host must be ASCII letters, digits, ...* or *could not be parsed* is a configuration error that retrying will not fix: correct the URL |
| 5 | `{"verdict": "late", "seen_at": ..., "not_after": ...}` - right amount and account, seen after `--not-after` | do not serve the order; the money did arrive, so refund or re-quote |
| 6 | `{"verdict": "unknown_time", "seen_at": null, ...}` - settled, but the node reports no time for it | do not treat as on time; ask another node |
| 64 | usage error, or `{"verdict": "invalid_not_after"}` | fix the command |

Only a `send` block settles anything. A receive, open or change block is refused however well
its amount matches. The account compared is the account PAID (the block's destination), not
the chain the block sits on.

## One rule that protects you

A block hash is public the moment it is broadcast, so anyone can present someone else's
payment. Record each hash you have served and refuse it the second time, or quote a unique
amount per order so a block can match only one order.

## Source

One file, Python standard library only: https://github.com/dhyabi2/nano-settlement-verify (MIT).

## Keep the refusal in the path: probe_gate.py

`python3 probe_gate.py BLOCK_HASH EXPECT_RAW ACCOUNT [RPC_URL]` runs the same check twice: first with a
recipient the block was not sent to, which must come back `mismatch` (exit 3), then with the real one. It
prints `PROBE-REFUSED`, the real verdict and `APPROVE` (exit 0) only when both hold; a probe that was not
refused exits 7 and approves nothing, so a check that stopped refusing cannot pass a spend.
