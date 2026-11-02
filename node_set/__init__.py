"""node_set: the pinned, measured list of public Nano RPC nodes. Never touches the network."""
from __future__ import annotations

import json
import logging
import os

_LOG = logging.getLogger("node_set")
NODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NODES.json")
_warned = set()


def _warn(msg):
    if msg not in _warned:
        _warned.add(msg)
        _LOG.warning(msg)


def fallback_urls(exclude=None, path=None):
    """NODES.json "usable", in file order. Missing, empty or malformed file: [] (degrades to single node)."""
    path = path or NODES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        usable = data["usable"]
        if not isinstance(usable, list):
            raise ValueError("usable is not a list")
    except FileNotFoundError:
        _warn(f"node set file missing: {path}")
        return []
    except (ValueError, KeyError, TypeError, OSError) as e:
        _warn(f"node set file unreadable ({path}): {e}")
        return []
    out = []
    for u in usable:
        if not isinstance(u, str) or not u.startswith("https://"):
            _warn(f"node set entry dropped, not https: {u!r}")
            continue
        if u != exclude and u not in out:
            out.append(u)
    if not out:
        _warn("node set has no usable entries; single-node only")
    return out
