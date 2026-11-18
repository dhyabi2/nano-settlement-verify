"""Law: the distribution installs one package, nano_settlement_verify, and nothing beside it.

Up to 1.3.0 the wheel put 13 loose modules and the packages `node_set` and `links` at
the top of site-packages; `links` is a name a stranger's environment can already hold.
From 2.0.0 every module lives inside the package, and these tests keep it there.
"""

import importlib
import pathlib
import tomllib

import nano_settlement_verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

SUBMODULES = (
    "terms", "independence", "quorum", "payers", "claim", "five_fields", "two_node",
    "funding", "recount", "period_counts", "offline", "stated_payer_test",
    "node_set", "node_set.probe", "links", "links.public_check",
)


def test_version_is_the_distribution_version():
    assert nano_settlement_verify.__version__ == PYPROJECT["project"]["version"]


def test_the_wheel_ships_only_the_package_and_its_node_list():
    setuptools = PYPROJECT["tool"]["setuptools"]
    assert "py-modules" not in setuptools
    assert all(p == "nano_settlement_verify" or p.startswith("nano_settlement_verify.")
               for p in setuptools["packages"])
    assert setuptools["package-data"]["nano_settlement_verify.node_set"] == ["NODES.json"]
    assert sorted(p.name for p in ROOT.glob("nano_*.py")) == []


def test_every_submodule_imports_from_inside_the_package():
    for name in SUBMODULES:
        module = importlib.import_module("nano_settlement_verify." + name)
        assert pathlib.Path(module.__file__).resolve().is_relative_to(ROOT / "nano_settlement_verify")


def test_a_patched_package_name_is_seen_by_the_modules_that_call_it(monkeypatch):
    from nano_settlement_verify import quorum, terms

    def fake(rpc_url, payload):
        return {}

    monkeypatch.setattr(nano_settlement_verify, "post_json", fake)
    assert quorum.nano_settlement_verify.post_json is fake
    assert terms.nano_settlement_verify.post_json is fake
