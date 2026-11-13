"""语义多重集对账器测试。

覆盖：行数/总额相同陷阱、跨行 CSV、Unicode NFC、缺列三态、空串与 NULL 区分、
超精度拒绝、强制哈希碰撞（粗筛误判但精确认证仍发现差异）、外部排序溢写、
临时文件恢复（孤儿收养/损坏丢弃/CRC 失败）、概率模式标注、JSON 数字精度、
重复行重数、同路径不短路等。
"""

import json
import shutil
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path

from src import semantic_reconcile as sr
from src.reconcile_reader import load

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def _write_contract(directory: Path, **overrides) -> Path:
    data = {
        "encoding": "utf-8",
        "fields": [
            {"field_id": 1, "name": "code", "type": "string"},
            {"field_id": 2, "name": "amount", "type": "decimal", "scale": 2},
            {"field_id": 3, "name": "time", "type": "timestamp_with_timezone"},
            {"field_id": 4, "name": "note", "type": "nullable_string"},
        ],
        "csv_null_token": "",
        "unicode_normalization": "none",
        "missing_column": "distinct_from_null",
        "hash": "sha256",
        "buffer_max_bytes": 4096,
        "bucket_count": 4,
    }
    data.update(overrides)
    path = directory / "contract.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _csv_row(code="x", amount="1.00", time="2026-01-01T00:00:00Z", note="n") -> str:
    return f"{code},{amount},{time},{note}"


HEADER = "code,amount,time,note\n"
TS_A = "2026-01-01T08:00:00+08:00"
TS_Z = "2026-01-01T00:00:00Z"


class FixtureReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="semrec_test_"))
        self.work = self.tmp / "spill"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reconcile(self, left=FIXTURES / "left.csv", right=FIXTURES / "right.jsonl",
                   contract=FIXTURES / "contract.json", **kwargs):
        return sr.reconcile(left, right, contract, work_dir=self.work, **kwargs)

    def test_row_count_and_total_amount_trap_still_detected(self):
        """[a,a,b] vs [a,b,b]：行数与总金额都相同，对账必须判定不一致。"""
        result = self._reconcile()
        self.assertTrue(result["exact_verified"])
        self.assertFalse(result["equal"])
        self.assertEqual(result["status"], "different_exact")
        self.assertTrue(result["summary"]["row_counts_equal"])
        self.assertTrue(result["summary"]["total_amounts_equal"])
        self.assertEqual(result["summary"]["left_total_amount"], "3.00")
        # a 左多 1、b 右多 1
        deltas = {
            d["canonical_preview"][0]["value"]: d["multiplicity_delta"]
            for d in result["differences"]
        }
        self.assertEqual(deltas, {"a": 1, "b": -1})
        for diff in result["differences"]:
            self.assertIn("canonical_hex", diff)
            self.assertTrue(diff["left_refs"] or diff["right_refs"])

    def test_cross_line_csv_lineage(self):
        result = self._reconcile()
        a_diff = next(d for d in result["differences"]
                      if d["canonical_preview"][0]["value"] == "a")
        first_ref = a_diff["left_refs"][0]
        self.assertEqual((first_ref["start_line"], first_ref["end_line"]), (2, 3))
        second_ref = a_diff["left_refs"][1]
        self.assertEqual((second_ref["start_line"], second_ref["end_line"]), (4, 5))
        # 第一条记录的字节区间正好覆盖带引号的字段内换行内容
        raw = (FIXTURES / "left.csv").read_bytes()
        segment = raw[first_ref["start_byte"]:first_ref["end_byte"]]
        self.assertIn(b"line1\nline2", segment)
        # JSONL 血缘给物理行号与字节区间
        self.assertEqual(a_diff["right_refs"][0]["start_line"], 1)

    def test_timezone_and_decimal_canonical_equivalence(self):
        """08:00+08:00 与 00:00Z 规范相等；1.00 与 1 在 scale=2 下相等。"""
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + _csv_row("k", "1.00", TS_A, "n") + "\n", encoding="utf-8")
        right.write_text(
            json.dumps({"code": "k", "amount": "1", "time": TS_Z, "note": "n"},
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right)
        self.assertTrue(result["equal"], result["differences"])

    def test_identical_multisets_in_different_formats_are_equal(self):
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        rows = [
            ("a", "1.00", TS_Z, "x"),
            ("a", "2.50", TS_A, "y"),
            ("b", "0.01", TS_Z, "z"),
        ]
        left.write_text(HEADER + "".join(_csv_row(*r) + "\n" for r in rows), encoding="utf-8")
        right.write_text(
            "".join(
                json.dumps(
                    {"code": c, "amount": a, "time": t, "note": n}, ensure_ascii=False
                )
                + "\n"
                for c, a, t, n in reversed(rows)  # 故意打乱顺序
            ),
            encoding="utf-8",
        )
        result = self._reconcile(left, right)
        self.assertTrue(result["equal"])
        self.assertEqual(result["differences"], [])
        self.assertTrue(all(b["coarse_equal"] for b in result["coarse_buckets"]))

    def test_duplicate_rows_preserve_multiplicity(self):
        """合法重复行必须逐条保留重数，不能被去重 set 吞掉。"""
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + (_csv_row("d", "1.00", TS_Z, "n") + "\n") * 5, encoding="utf-8")
        right.write_text(
            (json.dumps({"code": "d", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n") * 3,
            encoding="utf-8",
        )
        result = self._reconcile(left, right)
        self.assertFalse(result["equal"])
        diff = result["differences"][0]
        self.assertEqual(diff["kind"], "multiplicity_diff")
        self.assertEqual((diff["left_count"], diff["right_count"]), (5, 3))
        self.assertEqual(diff["multiplicity_delta"], 2)

    def test_empty_string_differs_from_null(self):
        contract = _write_contract(self.tmp, csv_null_token="\\N")
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + 'e,1.00,2026-01-01T00:00:00Z,""\n', encoding="utf-8")
        right.write_text(
            json.dumps({"code": "e", "amount": "1.00",
                        "time": "2026-01-01T00:00:00Z", "note": None}) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right, contract)
        self.assertFalse(result["equal"])
        note_states = {d["canonical_preview"][3]["state"] for d in result["differences"]}
        self.assertEqual(note_states, {"value", "null"})
        empty_row = next(
            d for d in result["differences"]
            if d["canonical_preview"][3].get("value") == ""
        )
        self.assertEqual(empty_row["canonical_preview"][3]["state"], "value")

    def test_missing_column_distinct_from_null_and_value(self):
        """缺列是第三种状态：缺列 != NULL != 空串。"""
        contract = _write_contract(self.tmp)
        left = self.tmp / "l.jsonl"
        right = self.tmp / "r.jsonl"
        left.write_text(
            json.dumps({"code": "m", "amount": "1.00", "time": TS_Z}) + "\n",
            encoding="utf-8",
        )  # 缺 note 键
        right = self.tmp / "r.jsonl"
        right.write_text(
            json.dumps({"code": "m", "amount": "1.00", "time": TS_Z, "note": None}) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right, contract)
        self.assertFalse(result["equal"])
        states = {d["canonical_preview"][3]["state"] for d in result["differences"]}
        self.assertEqual(states, {"missing", "null"})

    def test_missing_csv_header_column(self):
        contract = _write_contract(self.tmp)
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text("code,amount,time\nm,1.00,2026-01-01T00:00:00Z\n", encoding="utf-8")
        right.write_text(
            json.dumps({"code": "m", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right, contract)
        self.assertFalse(result["equal"])
        missing = next(
            d for d in result["differences"]
            if d["canonical_preview"][3]["state"] == "missing"
        )
        self.assertTrue(missing["left_refs"])

    def test_same_key_different_value_is_not_key_only_diff(self):
        """同主键不同值必须输出两条完整规范行，而不是只报主键。"""
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + _csv_row("k", "1.00", TS_Z, "note-left") + "\n",
                        encoding="utf-8")
        right.write_text(
            json.dumps({"code": "k", "amount": "1.00", "time": TS_Z,
                        "note": "note-right"}) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right)
        self.assertFalse(result["equal"])
        kinds = sorted(d["kind"] for d in result["differences"])
        self.assertEqual(kinds, ["left_only", "right_only"])
        for diff in result["differences"]:
            self.assertTrue(diff["canonical_hex"])  # 完整规范字节都在
            self.assertEqual(diff["multiplicity_delta"],
                             diff["left_count"] - diff["right_count"])

    def test_over_precision_decimal_rejected_not_rounded(self):
        bad = self.tmp / "bad.jsonl"
        bad.write_text(
            json.dumps({"code": "p", "amount": "1.005", "time": TS_Z, "note": "n"}) + "\n",
            encoding="utf-8",
        )
        good = self.tmp / "good.csv"
        good.write_text(HEADER + _csv_row("p", "1.00", TS_Z, "n") + "\n", encoding="utf-8")
        with self.assertRaises(sr.PrecisionViolation):
            self._reconcile(good, bad)
        # CSV 侧超精度同样拒绝
        bad_csv = self.tmp / "bad.csv"
        bad_csv.write_text(HEADER + _csv_row("p", "12.3456", TS_Z, "n") + "\n",
                          encoding="utf-8")
        with self.assertRaises(sr.PrecisionViolation):
            self._reconcile(bad_csv, good)

    def test_naive_timestamp_rejected(self):
        naive = self.tmp / "naive.jsonl"
        naive.write_text(
            json.dumps({"code": "p", "amount": "1.00",
                        "time": "2026-01-01T00:00:00", "note": "n"}) + "\n",
            encoding="utf-8",
        )
        good = self.tmp / "good.csv"
        good.write_text(HEADER + _csv_row("p", "1.00", TS_Z, "n") + "\n", encoding="utf-8")
        with self.assertRaises(sr.ContractViolation):
            self._reconcile(good, naive)

    def test_forced_hash_collision_exact_verify_still_distinguishes(self):
        """强制哈希碰撞：粗筛桶计数相等无法发现差异，逐字节精确认证必须发现。"""
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + _csv_row("k", "1.00", TS_Z, "alpha") + "\n",
                        encoding="utf-8")
        right.write_text(
            json.dumps({"code": "k", "amount": "1.00", "time": TS_Z, "note": "beta"}) + "\n",
            encoding="utf-8",
        )

        def collision_hash(_blob: bytes) -> bytes:
            return b"\x00" * 32  # 所有规范行、所有桶共享同一摘要

        result = self._reconcile(left, right, hash_fn=collision_hash)
        self.assertTrue(result["exact_verified"])
        self.assertFalse(result["equal"])
        # 粗筛被碰撞欺骗（同一桶、同一摘要、计数相同）
        bucket = result["coarse_buckets"][0]
        self.assertTrue(bucket["counts_equal"])
        self.assertTrue(bucket["sha256_multiset_digest_equal"])
        # 但精确差异仍被逐字节核对抓到
        self.assertEqual(len(result["differences"]), 2)
        notes = {
            d["canonical_preview"][3].get("value") for d in result["differences"]
        }
        self.assertEqual(notes, {"alpha", "beta"})

    def test_probabilistic_mode_marked_unverified_and_collision_fools_it(self):
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(HEADER + _csv_row("k", "1.00", TS_Z, "alpha") + "\n",
                        encoding="utf-8")
        right.write_text(
            json.dumps({"code": "k", "amount": "1.00", "time": TS_Z, "note": "beta"}) + "\n",
            encoding="utf-8",
        )

        def collision_hash(_blob: bytes) -> bytes:
            return b"\x00" * 32

        result = self._reconcile(
            left, right, mode="probabilistic", hash_fn=collision_hash
        )
        self.assertFalse(result["exact_verified"])
        self.assertEqual(result["status"], "equal_unverified_probabilistic")
        self.assertTrue(result["equal"])  # 概率预检的结论，已显式标注未认证
        self.assertEqual(result["differences"], [])

        # 真实 sha256 下概率预检能抓到非碰撞差异，但同样标注未认证
        honest = self._reconcile(left, right, mode="probabilistic")
        self.assertFalse(honest["exact_verified"])
        self.assertFalse(honest["equal"])

    def test_spill_external_sort_under_tiny_budget(self):
        """极小内存预算强制溢写多运行，外部归并仍给出正确多重集结果。"""
        contract = _write_contract(self.tmp, buffer_max_bytes=1, bucket_count=3)
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left_rows = [("a", "1.00", TS_Z, "n"), ("a", "2.00", TS_A, "n"),
                     ("c", "3.00", TS_Z, "n")]
        right_rows = [("a", "1.00", TS_Z, "n"), ("b", "2.00", TS_Z, "n"),
                      ("c", "3.00", TS_Z, "n")]
        left.write_text(HEADER + "".join(_csv_row(*r) + "\n" for r in left_rows),
                        encoding="utf-8")
        right.write_text(
            "".join(json.dumps({"code": c, "amount": a, "time": t, "note": n}) + "\n"
                    for c, a, t, n in right_rows),
            encoding="utf-8",
        )
        result = self._reconcile(left, right, contract)
        self.assertFalse(result["equal"])
        # 每条记录都应触发一次溢写
        self.assertGreaterEqual(result["spill"]["left"]["run_files"], 3)
        self.assertGreaterEqual(result["spill"]["right"]["run_files"], 3)
        codes = sorted(
            d["canonical_preview"][0]["value"] for d in result["differences"]
        )
        self.assertEqual(codes, ["a", "b"])

    def test_spill_recovery_adopts_orphan_and_drops_corrupt(self):
        contract = _write_contract(self.tmp, buffer_max_bytes=1, bucket_count=2)
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        left.write_text(
            HEADER
            + _csv_row("a", "1.00", TS_Z, "n") + "\n"
            + _csv_row("b", "1.00", TS_Z, "n") + "\n",
            encoding="utf-8",
        )
        right.write_text(
            json.dumps({"code": "a", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n"
            + json.dumps({"code": "b", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n",
            encoding="utf-8",
        )
        result = sr.reconcile(left, right, contract, work_dir=self.work)
        self.assertTrue(result["equal"])

        # 复制一个已登记运行文件，伪装成崩溃后未登记的孤儿：应被收养。
        journal = json.loads((self.work / "left_journal.json").read_text(encoding="utf-8"))
        registered = self.work / journal["runs"][0]["name"]
        orphan = self.work / "left_run_99999.bin"
        shutil.copyfile(registered, orphan)
        store, report = sr.recover_side(self.work, "left",
                                        lambda b: __import__("hashlib").sha256(b).digest())
        self.assertEqual(len(report["adopted_runs"]), 1)
        self.assertEqual(report["dropped_runs"], [])
        # 孤儿是已有运行的完整副本，恢复后帧数以运行文件实际内容为准
        self.assertEqual(store.total_frames,
                         sum(r["frames"] for r in store.runs))

        # 截断孤儿文件：恢复时必须识别损坏并丢弃。
        corrupt = self.work / "left_run_99998.bin"
        shutil.copyfile(registered, corrupt)
        truncate_at = corrupt.stat().st_size - 5
        with corrupt.open("r+b") as fh:
            fh.truncate(truncate_at)
        store2, report2 = sr.recover_side(self.work, "left",
                                          lambda b: __import__("hashlib").sha256(b).digest())
        self.assertIn("left_run_99998.bin", report2["dropped_runs"])
        self.assertFalse((self.work / "left_run_99998.bin").exists())

    def test_registered_run_crc_corruption_is_rejected(self):
        contract = _write_contract(self.tmp, buffer_max_bytes=1, bucket_count=2)
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        line = _csv_row("a", "1.00", TS_Z, "n") + "\n"
        left.write_text(HEADER + line, encoding="utf-8")
        right.write_text(
            json.dumps({"code": "a", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n",
            encoding="utf-8",
        )
        sr.reconcile(left, right, contract, work_dir=self.work)
        journal = json.loads((self.work / "left_journal.json").read_text(encoding="utf-8"))
        target = self.work / journal["runs"][0]["name"]
        data = bytearray(target.read_bytes())
        data[-1] ^= 0xFF  # 翻转最后一帧载荷字节，CRC32/整体 sha 必失败
        target.write_bytes(bytes(data))
        with self.assertRaises(sr.SpillCorruptionError):
            sr.recover_side(self.work, "left",
                            lambda b: __import__("hashlib").sha256(b).digest())

    def test_same_input_path_is_not_short_circuited(self):
        """同路径不能当作内容一致的捷径；跑完整流程后应得到相等。"""
        source = self.tmp / "same.jsonl"
        source.write_text(
            json.dumps({"code": "s", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n"
            + json.dumps({"code": "s", "amount": "1.00", "time": TS_Z, "note": "n"}) + "\n",
            encoding="utf-8",
        )
        contract = _write_contract(self.tmp)
        with unittest.mock.patch.object(sr, "exact_verify",
                                        wraps=sr.exact_verify) as spy:
            result = self._reconcile(source, source, contract)
            spy.assert_called()  # 证明没有短路
        self.assertTrue(result["equal"])
        self.assertTrue(result["exact_verified"])

    def test_unicode_nfc_normalization(self):
        contract = _write_contract(self.tmp, unicode_normalization="nfc")
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        nfd = "é"  # e + 组合尖音符
        nfc = "é"        # 单码位
        left.write_text(HEADER + _csv_row("u", "1.00", TS_Z, nfd) + "\n",
                        encoding="utf-8")
        right.write_text(
            json.dumps({"code": "u", "amount": "1.00", "time": TS_Z, "note": nfc}) + "\n",
            encoding="utf-8",
        )
        result = self._reconcile(left, right, contract)
        self.assertTrue(result["equal"], result["differences"])

        # none 模式下字节不同，应判定差异
        raw_contract = _write_contract(self.tmp, unicode_normalization="none")
        result_raw = self._reconcile(left, right, raw_contract)
        self.assertFalse(result_raw["equal"])

    def test_json_numbers_never_pass_through_float(self):
        """JSON number 以 Decimal 接收，高精度金额被拒绝而非静默丢精度。"""
        right = self.tmp / "r.jsonl"
        right.write_text(
            '{"code": "j", "amount": 0.005, "time": "' + TS_Z + '", "note": "n"}\n',
            encoding="utf-8",
        )
        left = self.tmp / "l.csv"
        left.write_text(HEADER + _csv_row("j", "0.01", TS_Z, "n") + "\n", encoding="utf-8")
        contract = _write_contract(self.tmp)
        with self.assertRaises(sr.PrecisionViolation):
            self._reconcile(left, right, contract)

        # 整数 JSON number 与字符串金额在 scale 下规范相等
        right_ok = self.tmp / "r2.jsonl"
        right_ok.write_text(
            '{"code": "j", "amount": 1, "time": "' + TS_Z + '", "note": "n"}\n',
            encoding="utf-8",
        )
        left_ok = self.tmp / "l2.csv"
        left_ok.write_text(HEADER + _csv_row("j", "1.00", TS_Z, "n") + "\n",
                          encoding="utf-8")
        self.assertTrue(self._reconcile(left_ok, right_ok, contract)["equal"])

    def test_json_nan_infinity_rejected(self):
        bad = self.tmp / "bad.jsonl"
        bad.write_text('{"code": "x", "amount": NaN, "time": "' + TS_Z + '", "note": "n"}\n',
                       encoding="utf-8")
        good = self.tmp / "good.csv"
        good.write_text(HEADER + _csv_row("x", "1.00", TS_Z, "n") + "\n", encoding="utf-8")
        with self.assertRaises(sr.ContractViolation):
            self._reconcile(good, bad)

    def test_declared_encoding_is_honored(self):
        contract = _write_contract(self.tmp, encoding="utf-16")
        left = self.tmp / "l.csv"
        right = self.tmp / "r.jsonl"
        row_csv = HEADER + _csv_row("编", "1.00", TS_Z, "注") + "\n"
        left.write_text(row_csv, encoding="utf-16")
        right.write_text(
            json.dumps({"code": "编", "amount": "1.00", "time": TS_Z, "note": "注"},
                       ensure_ascii=False) + "\n",
            encoding="utf-16",
        )
        result = self._reconcile(left, right, contract)
        self.assertTrue(result["equal"], result["differences"])


class CanonicalPrimitiveTests(unittest.TestCase):
    def test_varint_roundtrip(self):
        for value in (0, 1, 127, 128, 16383, 16384, 2**32, 2**63):
            encoded = sr.encode_varint(value)
            decoded, pos = sr.decode_varint(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(pos, len(encoded))

    def test_decimal_negative_zero_normalized(self):
        self.assertEqual(
            sr.canonical_decimal(Decimal("-0.00"), 2, "t"), b"0.00"
        )

    def test_decimal_no_float_path(self):
        with self.assertRaises(sr.ContractViolation):
            sr.canonical_decimal(1.5, 2, "t")  # 二进制 float 直接拒绝

    def test_timestamp_utc_exact_moment_and_fraction(self):
        self.assertEqual(
            sr.canonical_timestamp("2026-01-01T08:00:00+08:00", "t"),
            sr.canonical_timestamp("2026-01-01T00:00:00Z", "t"),
        )
        self.assertEqual(
            sr.canonical_timestamp("2026-01-01T00:00:00.50Z", "t"),
            b"1767225600.5",
        )
        self.assertEqual(
            sr.canonical_timestamp("2026-01-01T00:00:00.123456789Z", "t"),
            b"1767225600.123456789",  # 小秒精确保留，不经过 float
        )
        with self.assertRaises(sr.ContractViolation):
            sr.canonical_timestamp("not-a-timestamp", "t")

    def test_three_field_states_encode_distinctly(self):
        spec = sr.FieldSpec(4, "note", "nullable_string")
        self.assertNotEqual(
            sr.encode_field(spec, sr.STATE_MISSING),
            sr.encode_field(spec, sr.STATE_NULL),
        )
        self.assertNotEqual(
            sr.encode_field(spec, sr.STATE_NULL),
            sr.encode_field(spec, sr.STATE_VALUE, b""),
        )
        # 长度前缀使不同内容不会产生歧义边界
        decoded = sr.decode_canonical(
            sr.encode_varint(1) + sr.encode_field(spec, sr.STATE_VALUE, "a:b".encode())
        )
        self.assertEqual(decoded[0]["value"], "a:b")


class ReaderIntegrationTests(unittest.TestCase):
    """新对账器与既有读取器在 fixture 上保持可读契约。"""

    def test_existing_reader_still_loads_fixtures(self):
        self.assertEqual(len(load(FIXTURES / "left.csv")), 3)
        self.assertEqual(len(load(FIXTURES / "right.jsonl")), 3)

    def test_lineage_byte_offsets_roundtrip_jsonl(self):
        tmp = Path(tempfile.mkdtemp(prefix="semrec_off_"))
        try:
            contract = _write_contract(tmp)
            source = tmp / "r.jsonl"
            payload = {"code": "o", "amount": "1.00", "time": TS_Z, "note": "n"}
            source.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            other = tmp / "l.csv"
            other.write_text(HEADER + _csv_row("o", "1.00", TS_Z, "n") + "\n",
                             encoding="utf-8")
            result = sr.reconcile(other, source, contract, work_dir=tmp / "w")
            self.assertTrue(result["equal"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
