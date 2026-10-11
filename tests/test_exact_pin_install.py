"""Opt-in installed-dependency contract for the isolated exact-pin CI job."""

import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "quant-platform-kit": "quant_platform_kit",
    "crypto-strategies": "crypto_strategies",
}


@unittest.skipUnless(os.environ.get("BINANCE_EXACT_PIN_TESTS") == "1", "isolated frozen-install job only")
class ExactPinInstallTests(unittest.TestCase):
    def test_imported_dependencies_are_the_noneditable_declared_locked_commits(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        for name, module_name in PACKAGES.items():
            with self.subTest(package=name):
                declarations = [
                    item for item in project["project"]["dependencies"]
                    if item.startswith(f"{name} @ git+")
                ]
                self.assertEqual(len(declarations), 1)
                url, pin = declarations[0].split(" @ git+", 1)[1].rsplit("@", 1)
                self.assertRegex(pin, re.compile(r"\A[0-9a-f]{40}\Z"))
                locked = [item for item in lock["package"] if item["name"] == name]
                self.assertEqual(len(locked), 1)
                self.assertEqual(locked[0]["source"], {"git": f"{url}?rev={pin}#{pin}"})

                dist = metadata.distribution(name)
                # Read the real installed PEP 610 metadata, never a mocked version.
                direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
                self.assertEqual(direct_url.get("url"), url)
                self.assertNotIn("dir_info", direct_url, "editable/path install is not an exact Git install")
                vcs = direct_url.get("vcs_info", {})
                self.assertEqual(vcs.get("vcs"), "git")
                self.assertEqual(vcs.get("requested_revision"), pin)
                self.assertEqual(vcs.get("commit_id"), pin)

                module = importlib.import_module(module_name)
                imported = Path(module.__file__).resolve()
                installed = {
                    Path(dist.locate_file(item)).resolve() for item in (dist.files or ())
                }
                self.assertIn(imported, installed, "import is shadowed by a sibling/source checkout")
