# Fallback nodes for the verifier

`verify_cli.py` takes a comma-separated list in its RPC argument or in `$NANO_RPC_URL`. The next node is asked
only when the one before could not be reached or read (exit 4). Any answer is final.

Probed read-only on 2026-11-02 with `block_info` on block
`B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F` (send of 0.0005 XNO, height 4, from
`nano_3m8cz87...`): every node below returned the same `block_account`, `amount`, `balance`, `height` and
`local_timestamp` (1790150110).

| Node | Notes |
|---|---|
| `https://rpc.nano.to` | the default |
| `https://node.somenano.com/proxy` | answered identically |
| `https://nanoslo.0x.no/proxy` | answered identically |
| `https://rainstorm.city/api` | answered identically (adds `topo_height`) |

```sh
python3 skills/nano-settlement-verify/verify_cli.py BLOCK EXPECT_RAW ACCOUNT \
  "https://rpc.nano.to,https://node.somenano.com/proxy,https://nanoslo.0x.no/proxy"
```

Limits: failover is not agreement (use `nano_quorum` for that), public nodes come and go, and a "proxy" path may
sit in front of the same node as another entry. Send `Content-Type: application/json` and a User-Agent or some
of them answer an error that reads like a missing block. The default stays `https://rpc.nano.to`; nothing in the
code changed.
