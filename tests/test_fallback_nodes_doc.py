"""Law: the fallback list the docs print is a list the CLI itself accepts.

A documented command that the tool refuses (plain http, a duplicate, a bad port) would send a stranger
to exit 4 on the one line meant to save them from exit 4.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("verify_cli_doc_under_test",
                                               ROOT / "skills" / "nano-settlement-verify" / "verify_cli.py")
verify_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_cli)


def test_documented_list_is_accepted_by_the_cli():
    text = (ROOT / "FALLBACK-NODES.md").read_text()
    m = re.search(r'"(https://[^"]+(?:,https://[^"]+)+)"', text)
    assert m, "FALLBACK-NODES.md prints no quoted comma-separated list"
    urls = [u.strip() for u in m.group(1).split(",")]
    assert len(urls) >= 3
    assert verify_cli._refuse_rpc_urls(urls) is None


def test_readme_names_the_same_nodes():
    readme = (ROOT / "README.md").read_text()
    for url in ("https://node.somenano.com/proxy", "https://nanoslo.0x.no/proxy"):
        assert url in readme
