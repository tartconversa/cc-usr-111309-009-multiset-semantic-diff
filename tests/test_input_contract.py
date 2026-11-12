import json
import tempfile
import unittest
import hashlib
from pathlib import Path
from src.reconcile_reader import load

class InputContractTests(unittest.TestCase):
    def test_primary_material_readable(self):
        self.assertGreater(len(load(Path(__file__).resolve().parents[1] / 'fixtures/left.csv')), 0)
    def test_inventory_matches_input_bytes(self):
        root=Path(__file__).resolve().parents[1]
        for item in load(root / "material-index.json"):
            with self.subTest(path=item["path"]):
                self.assertEqual(hashlib.sha256((root/item["path"]).read_bytes()).hexdigest(),item["sha256"])
                self.assertIsInstance(load(root/item["path"]),(list,dict))
    def test_blank_differs_from_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"values.csv"
            p.write_text("id,value\na,\nb,0\n",encoding="utf-8")
            self.assertEqual([x["value"] for x in load(p)],["","0"])
    def test_malformed_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"bad.jsonl"
            p.write_text('{"key":1}\n{bad}\n',encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):load(p)
    def test_absent_not_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):load(Path(directory)/"absent.csv")
