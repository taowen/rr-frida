"""Tests for probe and probe-set declaration validation.

    python3 -m unittest rrfrida.tests.test_probes
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from rrfrida.probes import (
    DeclarationError,
    load_catalog,
    load_probe_set,
    load_probes,
)


def write(directory: Path, name: str, value) -> Path:
    path = directory / name
    path.write_text(json.dumps(value))
    return path


VALID_PROBE = {
    "add_ints": {
        "kind": 100,
        "type": "function",
        "rva": "0x1120",
        "expected": "000800b9",
        "snapshots": [{"phase": "enter", "source": "arg0", "size": 8}],
    }
}


class TestProbes(unittest.TestCase):
    def test_valid_probe_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), "p.json", VALID_PROBE)
            self.assertEqual(load_probes(path), VALID_PROBE)

    def test_register_snapshot_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = {"f": {"kind": 1, "type": "function", "rva": "0x10",
                           "expected": "00000000",
                           "snapshots": [{"phase": "enter", "register": "s0"}]}}
            load_probes(write(Path(tmp), "p.json", probe))

    def test_register_snapshot_rejects_source_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = {"f": {"kind": 1, "type": "function", "rva": "0x10",
                           "expected": "00000000",
                           "snapshots": [{"phase": "enter", "register": "s0",
                                          "size": 4}]}}
            with self.assertRaises(DeclarationError):
                load_probes(write(Path(tmp), "p.json", probe))

    def test_short_expected_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = {"f": {"kind": 1, "type": "function", "rva": "0x10",
                           "expected": "00"}}
            with self.assertRaises(DeclarationError):
                load_probes(write(Path(tmp), "p.json", probe))

    def test_bad_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = {"f": {"kind": 1, "type": "wat", "rva": "0x10",
                           "expected": "00000000"}}
            with self.assertRaises(DeclarationError):
                load_probes(write(Path(tmp), "p.json", probe))

    def test_probe_set_requires_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), "s.json", {"module": "x.so", "required": []})
            with self.assertRaises(DeclarationError):
                load_probe_set(path)

    def test_probe_set_name_defaults_to_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), "myset.json",
                         {"module": "x.so", "required": [], "sha256": "a" * 64})
            self.assertEqual(load_probe_set(path)["name"], "myset")

    def test_catalog_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "p.json", VALID_PROBE)
            with self.assertRaises(DeclarationError):
                load_catalog(Path(tmp), ["absent"])


if __name__ == "__main__":
    unittest.main()
