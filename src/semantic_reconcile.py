"""语义多重集对账器。

两地交换同一张明细表（一侧 CSV、一侧 JSONL），原始字节哈希不同但行数与总金额
相同并不代表数据一致。本模块按显式字段契约把每一行规范成 *有类型、长度前缀* 的
规范字节，以规范字节的多重集（保留合法重复的重数）做精确对账。

设计要点：
- 金额 Decimal 按固定 scale 精确规范，超精度拒绝，绝不自动舍入；全程不使用
  二进制 float（JSON 的 number 用 parse_float=Decimal 接收）。
- 时间必须带时区，统一换算为 UTC 的精确时刻（整数秒 + 精确十进制小秒）。
- 字符串按契约选择 NFC 规范化；空串与 NULL 是两种状态，缺列是第三种状态，
  三者互不等价。
- 行顺序无意义：先按规范行 SHA256 分桶做粗筛（桶计数 + 无序多重集摘要），
  再对每个桶做外部排序后的逐字节精确核对。哈希相同不作为相等证明。
- 内存超预算即溢写临时文件，重复行保留重数，绝不放进去重 set。
- 每条规范行都带输入文件与记录位置血缘（逻辑记录号、物理行号、字节区间）。
- 概率摘要模式可预检，但结果另标“未精确认证”；同路径不作为内容一致的捷径。
"""

from __future__ import annotations

import argparse
import calendar
import codecs
import csv
import hashlib
import heapq
import io
import itertools
import json
import os
import re
import tempfile
import unicodedata
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Callable, Iterator, Optional

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ReconcileError(Exception):
    """对账器基础异常。"""


class ContractViolation(ReconcileError):
    """字段契约被违反（类型错误、缺列策略冲突等）。"""


class PrecisionViolation(ContractViolation):
    """Decimal 输入超过契约 scale，拒绝而非舍入。"""


class SpillCorruptionError(ReconcileError):
    """溢写临时文件校验失败（CRC/摘要/截断）。"""


# ---------------------------------------------------------------------------
# 无符号 varint 与长度前缀帧编解码
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint 不能为负数")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise SpillCorruptionError("varint 越界，帧被截断")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise SpillCorruptionError("varint 过长")


# 字段三态：缺列 / NULL / 有值，互不等价。
STATE_MISSING = 0x00
STATE_NULL = 0x01
STATE_VALUE = 0x02

# 有值字段的类型标签。
TYPE_STRING = 0x11
TYPE_DECIMAL = 0x12
TYPE_TIMESTAMP = 0x13

_TYPE_CODES = {
    "string": TYPE_STRING,
    "nullable_string": TYPE_STRING,
    "decimal": TYPE_DECIMAL,
    "timestamp_with_timezone": TYPE_TIMESTAMP,
}

_RUN_MAGIC = b"SRFR1\n"
_JOURNAL_NAME = "journal.json"


# ---------------------------------------------------------------------------
# 字段契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    field_id: int
    name: str
    type_name: str
    scale: Optional[int] = None

    @property
    def nullable(self) -> bool:
        return self.type_name == "nullable_string"

    @property
    def type_code(self) -> int:
        return _TYPE_CODES[self.type_name]


@dataclass(frozen=True)
class Contract:
    encoding: str
    fields: tuple[FieldSpec, ...]
    csv_null_token: str
    unicode_normalization: str  # "none" | "nfc"
    missing_column: str  # 当前仅支持 "distinct_from_null"
    hash_name: str
    buffer_max_bytes: int
    bucket_count: int

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Contract":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {
            "encoding",
            "fields",
            "csv_null_token",
            "unicode_normalization",
            "missing_column",
            "hash",
            "buffer_max_bytes",
            "bucket_count",
        }
        missing = required - data.keys()
        if missing:
            raise ContractViolation(f"契约缺少键: {sorted(missing)}")
        if data["hash"] != "sha256":
            raise ContractViolation("仅支持 sha256 哈希")
        if data["missing_column"] != "distinct_from_null":
            raise ContractViolation("missing_column 策略仅支持 distinct_from_null")
        if data["unicode_normalization"] not in ("none", "nfc"):
            raise ContractViolation("unicode_normalization 仅支持 none / nfc")
        if int(data["bucket_count"]) < 1:
            raise ContractViolation("bucket_count 必须 >= 1")
        if int(data["buffer_max_bytes"]) < 1:
            raise ContractViolation("buffer_max_bytes 必须 >= 1")

        fields = []
        seen_ids: set[int] = set()
        for raw in data["fields"]:
            spec = FieldSpec(
                field_id=int(raw["field_id"]),
                name=str(raw["name"]),
                type_name=str(raw["type"]),
                scale=(int(raw["scale"]) if "scale" in raw else None),
            )
            if spec.field_id in seen_ids:
                raise ContractViolation(f"field_id 重复: {spec.field_id}")
            seen_ids.add(spec.field_id)
            if spec.type_name not in _TYPE_CODES:
                raise ContractViolation(f"不支持的字段类型: {spec.type_name}")
            if spec.type_name == "decimal" and spec.scale is None:
                raise ContractViolation("decimal 字段必须声明 scale")
            fields.append(spec)
        if not fields:
            raise ContractViolation("契约至少需要一个字段")
        return cls(
            encoding=str(data["encoding"]),
            fields=tuple(fields),
            csv_null_token=str(data["csv_null_token"]),
            unicode_normalization=str(data["unicode_normalization"]),
            missing_column=str(data["missing_column"]),
            hash_name=str(data["hash"]),
            buffer_max_bytes=int(data["buffer_max_bytes"]),
            bucket_count=int(data["bucket_count"]),
        )


# ---------------------------------------------------------------------------
# 标量规范化
# ---------------------------------------------------------------------------

_ZERO = Decimal(0)
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|[+-]\d{2}(?::?\d{2})?)$"
)


def canonical_decimal(value: object, scale: int, where: str) -> bytes:
    """把金额规范为固定 scale 的 ASCII 字节；超精度拒绝，不做舍入。"""
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int) and not isinstance(value, bool):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise ContractViolation(f"{where} 金额无法解析为 Decimal: {value!r}") from exc
    else:
        raise ContractViolation(f"{where} 金额类型不被契约接受: {type(value).__name__}")
    if not dec.is_finite():
        raise ContractViolation(f"{where} 金额必须有限: {value!r}")
    if dec.as_tuple().exponent < -scale:
        raise PrecisionViolation(
            f"{where} 金额 {value!r} 超过 scale={scale}，拒绝自动舍入"
        )
    unit = _ZERO.scaleb(-scale)
    quantized = dec.quantize(unit) + _ZERO  # 加 0 消去负零
    return str(quantized).encode("ascii")


def canonical_timestamp(value: object, where: str) -> bytes:
    """时间必须带时区；规范为 UTC 精确时刻：整数秒[.精确小秒] 的 ASCII。"""
    if not isinstance(value, str):
        raise ContractViolation(f"{where} 时间必须是 ISO8601 字符串")
    match = _ISO_RE.match(value)
    if not match:
        raise ContractViolation(f"{where} 时间无法解析或缺少时区: {value!r}")
    year, month, day, hour, minute, second, frac_digits, zone = match.groups()
    if zone == "Z":
        offset_minutes = 0
    else:
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        off_hour = int(digits[:2])
        off_minute = int(digits[2:]) if len(digits) > 2 else 0
        offset_minutes = sign * (off_hour * 60 + off_minute)
    try:
        dt = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise ContractViolation(f"{where} 时间日历分量非法: {value!r}") from exc
    # 固定偏移的算术换算，不依赖系统时区，不经过 float。
    int_utc_seconds = calendar.timegm(dt.timetuple()) - offset_minutes * 60

    payload = str(int_utc_seconds).encode("ascii")
    if frac_digits:
        with localcontext() as ctx:
            ctx.prec = max(28, len(frac_digits) + 2)
            frac = Decimal(int(frac_digits)) / Decimal(10 ** len(frac_digits))
            frac = frac.normalize()
        if frac:
            payload += b"." + str(frac).split(".", 1)[1].encode("ascii")
    return payload


def canonical_string(value: object, normalize_nfc: bool, where: str) -> bytes:
    if not isinstance(value, str):
        raise ContractViolation(f"{where} 字符串字段收到类型: {type(value).__name__}")
    if normalize_nfc:
        value = unicodedata.normalize("NFC", value)
    return value.encode("utf-8")


# ---------------------------------------------------------------------------
# 行血缘与规范行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageRef:
    """原始记录位置血缘；不保存原始行对象副本，只保存定位坐标。"""

    source: str
    record_no: int
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int

    def to_jsonable(self) -> dict:
        return {
            "source": self.source,
            "record_no": self.record_no,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
        }


@dataclass(frozen=True)
class Frame:
    """一条规范行：规范字节 + 血缘。"""

    canonical: bytes
    ref: LineageRef

    def __lt__(self, other: "Frame") -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return self.canonical < other.canonical


_MISSING_SENTINEL = object()


def encode_field(spec: FieldSpec, state: int, payload: bytes = b"") -> bytes:
    out = bytearray((state,))
    if state == STATE_VALUE:
        out.append(spec.type_code)
    out.extend(encode_varint(len(payload)))
    out.extend(payload)
    return bytes(out)


def canonicalize_row(
    raw_getter: Callable[[str], object],
    contract: Contract,
    ref: LineageRef,
) -> tuple[bytes, Decimal]:
    """把一行原始数据规范为长度前缀的类型化字节，并返回金额合计贡献。"""
    where = f"{ref.source}:记录{ref.record_no}"
    row = bytearray(encode_varint(len(contract.fields)))
    amount_sum = Decimal(0)
    for spec in contract.fields:
        raw = raw_getter(spec.name)
        if raw is _MISSING_SENTINEL:
            # 缺列：独立状态，绝不与 NULL 混淆。
            row.extend(encode_field(spec, STATE_MISSING))
            continue
        if raw is None:
            if not spec.nullable:
                raise ContractViolation(f"{where} 字段 {spec.name!r} 不允许 NULL")
            row.extend(encode_field(spec, STATE_NULL))
            continue
        if spec.type_name in ("string", "nullable_string"):
            payload = canonical_string(
                raw, contract.unicode_normalization == "nfc", where
            )
            row.extend(encode_field(spec, STATE_VALUE, payload))
        elif spec.type_name == "decimal":
            payload = canonical_decimal(raw, spec.scale, where)
            row.extend(encode_field(spec, STATE_VALUE, payload))
            amount_sum += Decimal(payload.decode("ascii"))
        elif spec.type_name == "timestamp_with_timezone":
            payload = canonical_timestamp(raw, where)
            row.extend(encode_field(spec, STATE_VALUE, payload))
    return bytes(row), amount_sum


def decode_canonical(row: bytes) -> list[dict]:
    """把规范字节解回可读预览（用于差异输出，不参与相等判定）。"""
    pos = 0
    count, pos = decode_varint(row, pos)
    out = []
    state_names = {STATE_MISSING: "missing", STATE_NULL: "null", STATE_VALUE: "value"}
    type_names = {v: k for k, v in _TYPE_CODES.items()}
    for _ in range(count):
        state = row[pos]
        pos += 1
        entry: dict = {"state": state_names[state]}
        tag: Optional[int] = None
        if state == STATE_VALUE:
            tag = row[pos]
            pos += 1
            entry["type"] = type_names[tag]
        length, pos = decode_varint(row, pos)
        payload = row[pos : pos + length]
        pos += length
        if state == STATE_VALUE and tag is not None:
            entry["value"] = (
                payload.decode("utf-8") if tag == TYPE_STRING else payload.decode("ascii")
            )
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 流式读取器：CSV / JSONL，带物理位置血缘，不缓存全部原始对象
# ---------------------------------------------------------------------------


def _physical_line_stream(binary_fh, encoding: str):
    """按声明编码增量解码并产出物理行，同时给出每行行首字节偏移。

    不使用 TextIOWrapper：其内部预读块会让底层 tell() 跳到块边界，无法给出
    逐行字节偏移。这里用 IncrementalDecoder 配合小块读取自行切行，
    utf-16 等多字节编码也能正确按解码后的 \\r/\\n 分行。字节游标按已产出
    文本重新编码自维护，不依赖调用时刻的文件指针位置。
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    codec = codecs.lookup(encoding)
    # 字节计数用无 BOM 的变体编码器：utf-16/utf-32 的 BOM 单独按行首偏移处理，
    # 行片段本身按本机端序的无 BOM 编码计长。
    stateless_name = {
        "utf-16": "utf-16-le",
        "utf-32": "utf-32-le",
    }.get(encoding, encoding)
    byte_encoder = codecs.getencoder(stateless_name)
    # 带 BOM 的编解码器首字节包含 BOM；首行起点须跳过它，无 BOM 时长度为 0。
    line_start = len(codec.incrementalencoder(encoding).encode("", final=False))
    pending = ""
    line_no = 0

    def byte_len(text: str) -> int:
        return len(byte_encoder(text)[0])

    while True:
        chunk = binary_fh.read(io.DEFAULT_BUFFER_SIZE)
        final = not chunk
        text = pending + decoder.decode(chunk, final=final)
        if final:
            if text:
                line_no += 1
                yield line_no, line_start, line_start + byte_len(text), text, \
                    text.rstrip("\r\n") == ""
            return
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            pending = lines.pop()
        else:
            pending = ""
        for line in lines:
            line_no += 1
            next_start = line_start + byte_len(line)
            yield line_no, line_start, next_start, line, line.rstrip("\r\n") == ""
            line_start = next_start
        # 块边界处：文件已读位置 - 尚未解码的原始字节 = 已进入解码器的字节，
        # 再减去滞留半行字节，即下一个物理行的真实起点；无需在 yield 点 tell()。
        decoded_so_far = binary_fh.tell() - len(decoder.buffer)
        line_start = decoded_so_far - byte_len(pending)


def iter_csv(path: str | os.PathLike, contract: Contract) -> Iterator[tuple[dict, LineageRef]]:
    source = str(path)
    with open(path, "rb") as fb:
        starts: deque[tuple[int, int, int, bool]] = deque()

        def gen() -> Iterator[str]:
            for line_no, start, end, text, is_blank in _physical_line_stream(
                fb, contract.encoding
            ):
                starts.append((line_no, start, end, is_blank))
                yield text

        reader = csv.reader(gen())
        try:
            header = next(reader)
        except StopIteration:
            return
        starts.clear()
        index_by_name: dict[str, int] = {}
        for idx, col in enumerate(header):
            index_by_name.setdefault(col, idx)  # 重名列保留首个，契约字段为准

        record_no = 0
        for values in reader:
            record_no += 1
            while starts and starts[0][3]:  # 跳过 csv 模块吞掉的空物理行
                starts.popleft()
            if not starts:
                raise ReconcileError(f"{source} CSV 记录物理位置丢失: 记录{record_no}")
            start_line, start_byte, _end, _blank = starts[0]
            end_line = starts[-1][0]
            end_byte = starts[-1][2]
            starts.clear()

            def getter(name: str, _values=values, _idx=index_by_name) -> object:
                col = _idx.get(name)
                if col is None or col >= len(_values):
                    return _MISSING_SENTINEL  # 缺列
                text = _values[col]
                if text == contract.csv_null_token:
                    return None  # NULL 协议
                return text

            ref = LineageRef(source, record_no, start_line, end_line, start_byte, end_byte)
            yield {"_get": getter}, ref


def _reject_constant(token: str) -> None:
    raise ContractViolation(f"JSON 不允许 {token} 常量（精度/语义未定义）")


def iter_jsonl(path: str | os.PathLike, contract: Contract) -> Iterator[tuple[dict, LineageRef]]:
    source = str(path)
    with open(path, "rb") as fb:
        record_no = 0
        for line_no, start_byte, end_byte, text, is_blank in _physical_line_stream(
            fb, contract.encoding
        ):
            if is_blank:
                continue
            record_no += 1
            try:
                obj = json.loads(
                    text,
                    parse_float=Decimal,  # 数字不先转二进制 float
                    parse_constant=_reject_constant,
                )
            except json.JSONDecodeError:
                raise  # 保持标准库异常类型，与既有读取器契约一致
            if not isinstance(obj, dict):
                raise ContractViolation(f"{source}:物理行{line_no} 不是 JSON 对象")

            def getter(name: str, _obj=obj) -> object:
                if name not in _obj:
                    return _MISSING_SENTINEL  # 缺键，区别于 null
                value = _obj[name]
                if isinstance(value, float):  # 防御：parse_float 已接管，正常不会触发
                    raise ContractViolation(f"{source}:记录{record_no} 出现二进制 float")
                return value

            ref = LineageRef(source, record_no, line_no, line_no, start_byte, end_byte)
            yield {"_get": getter}, ref


def iter_source(path: str | os.PathLike, contract: Contract):
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return iter_csv(path, contract)
    if suffix == ".jsonl":
        return iter_jsonl(path, contract)
    raise ReconcileError(f"不支持的输入格式: {path}")


# ---------------------------------------------------------------------------
# 溢写运行文件：长度前缀帧 + CRC32，清单/journal 支持恢复
# ---------------------------------------------------------------------------


def _run_path(work_dir: Path, side: str, run_id: int) -> Path:
    return work_dir / f"{side}_run_{run_id:05d}.bin"


class RunWriter:
    def __init__(self, path: Path, source: str):
        self.path = path
        self.source = source
        self.frames = 0
        self._hasher = hashlib.sha256()
        self._fh = path.open("wb")
        self._fh.write(_RUN_MAGIC)
        src_prefix = encode_varint(len(source.encode("utf-8"))) + source.encode("utf-8")
        self._fh.write(src_prefix)
        self._hasher.update(_RUN_MAGIC)
        self._hasher.update(src_prefix)

    def add(self, frame: Frame, bucket_idx: int) -> None:
        ref_payload = json.dumps(
            {
                "bucket": bucket_idx,
                "record_no": frame.ref.record_no,
                "start_line": frame.ref.start_line,
                "end_line": frame.ref.end_line,
                "start_byte": frame.ref.start_byte,
                "end_byte": frame.ref.end_byte,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        body = (
            encode_varint(len(frame.canonical))
            + frame.canonical
            + encode_varint(len(ref_payload))
            + ref_payload
        )
        wrapper = encode_varint(len(body)) + zlib.crc32(body).to_bytes(4, "little") + body
        self._fh.write(wrapper)
        self._hasher.update(wrapper)
        self.frames += 1

    def close(self) -> dict:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        return {
            "name": self.path.name,
            "frames": self.frames,
            "size_bytes": self.path.stat().st_size,
            "sha256": self._hasher.hexdigest(),
            "source": self.source,
        }


def _read_exact(fh, size: int, run_name: str) -> bytes:
    chunk = fh.read(size)
    if len(chunk) != size:
        raise SpillCorruptionError(f"{run_name} 帧截断")
    return chunk


def _read_varint(fh, run_name: str) -> int:
    result = 0
    shift = 0
    while True:
        byte = fh.read(1)
        if not byte:
            raise SpillCorruptionError(f"{run_name} varint 截断")
        value = byte[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
        shift += 7
        if shift > 63:
            raise SpillCorruptionError(f"{run_name} varint 过长")


class RunReader:
    """流式读取一个运行文件，逐帧 CRC32 校验 + 整体 sha256 校验。"""

    def __init__(self, path: Path, expected: Optional[dict] = None):
        self.path = path
        self.expected = expected
        self._hasher = hashlib.sha256()
        self.frames_read = 0
        self.source = ""

    def __iter__(self) -> Iterator[Frame]:
        run_name = self.path.name
        with self.path.open("rb") as fh:
            magic = fh.read(len(_RUN_MAGIC))
            if magic != _RUN_MAGIC:
                raise SpillCorruptionError(f"{run_name} 魔数不匹配")
            self._hasher.update(magic)
            src_len = _read_varint(fh, run_name)
            src_header = encode_varint(src_len) + _read_exact(fh, src_len, run_name)
            self._hasher.update(src_header)
            self.source = src_header[len(encode_varint(src_len)) :].decode("utf-8")

            while True:
                size_byte = fh.read(1)
                if not size_byte:
                    break
                wrap_len = (
                    size_byte[0]
                    if not size_byte[0] & 0x80
                    else _finish_varint(fh, size_byte[0], run_name)
                )
                prefix = encode_varint(wrap_len)
                crc_bytes = _read_exact(fh, 4, run_name)
                body = _read_exact(fh, wrap_len, run_name)
                if int.from_bytes(crc_bytes, "little") != zlib.crc32(body):
                    raise SpillCorruptionError(f"{run_name} CRC32 校验失败")
                self._hasher.update(prefix + crc_bytes + body)
                self.frames_read += 1
                bucket_idx, frame = self._parse_body(body, run_name)
                yield bucket_idx, frame

        if self.expected is not None:
            if self.frames_read != self.expected["frames"]:
                raise SpillCorruptionError(
                    f"{run_name} 帧数 {self.frames_read} != 清单 {self.expected['frames']}"
                )
            if self._hasher.hexdigest() != self.expected["sha256"]:
                raise SpillCorruptionError(f"{run_name} 整体 sha256 不匹配")

    def frames(self) -> Iterator[Frame]:
        for _bucket, frame in self:
            yield frame

    def _parse_body(self, body: bytes, run_name: str) -> tuple[int, Frame]:
        c_len, bpos = decode_varint(body, 0)
        canonical = body[bpos : bpos + c_len]
        if len(canonical) != c_len:
            raise SpillCorruptionError(f"{run_name} 规范字节截断")
        bpos += c_len
        r_len, bpos = decode_varint(body, bpos)
        ref_json = body[bpos : bpos + r_len]
        if len(ref_json) != r_len:
            raise SpillCorruptionError(f"{run_name} 血缘截断")
        ref_data = json.loads(ref_json.decode("utf-8"))
        bucket_idx = int(ref_data.pop("bucket"))
        ref = LineageRef(source=self.source, **ref_data)
        return bucket_idx, Frame(canonical, ref)


def _finish_varint(fh, first_byte: int, run_name: str) -> int:
    result = first_byte & 0x7F
    shift = 7
    while True:
        byte = fh.read(1)
        if not byte:
            raise SpillCorruptionError(f"{run_name} varint 截断")
        value = byte[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
        shift += 7
        if shift > 63:
            raise SpillCorruptionError(f"{run_name} varint 过长")


# ---------------------------------------------------------------------------
# 单侧桶存储：内存预算 + 外部排序溢写
# ---------------------------------------------------------------------------


@dataclass
class SideStore:
    side: str
    work_dir: Path
    source: str
    budget: int
    bucket_count: int
    hash_fn: Callable[[bytes], bytes]
    buckets: list[list[Frame]] = field(default_factory=list)
    bucket_bytes: list[int] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    total_frames: int = 0
    total_amount: Decimal = Decimal(0)
    _run_seq: int = 0
    sealed: bool = False

    def __post_init__(self):
        if not self.buckets:
            self.buckets = [[] for _ in range(self.bucket_count)]
            self.bucket_bytes = [0] * self.bucket_count

    def bucket_index(self, canonical: bytes) -> int:
        digest = self.hash_fn(canonical)
        return int.from_bytes(digest[:8], "little") % self.bucket_count

    def add(self, frame: Frame, amount: Decimal) -> int:
        idx = self.bucket_index(frame.canonical)
        self.buckets[idx].append(frame)
        self.bucket_bytes[idx] += len(frame.canonical) + 64
        self.total_frames += 1
        self.total_amount += amount
        if self.bucket_bytes[idx] > self.budget:
            self._flush_bucket(idx)
        return idx

    def _flush_bucket(self, idx: int) -> None:
        items = self.buckets[idx]
        if not items:
            return
        items.sort(key=lambda f: f.canonical)  # 运行内有序，供 k 路归并
        self._run_seq += 1
        path = _run_path(self.work_dir, self.side, self._run_seq)
        writer = RunWriter(path, self.source)
        for frame in items:  # 重复行逐条写入，重数保留，绝不去重
            writer.add(frame, idx)
        self.runs.append(writer.close())
        self.buckets[idx] = []
        self.bucket_bytes[idx] = 0
        self._write_journal()

    def _write_journal(self) -> None:
        journal = {
            "side": self.side,
            "source": self.source,
            "bucket_count": self.bucket_count,
            "total_frames": self.total_frames,
            "runs": self.runs,
        }
        tmp = self.work_dir / f"{self.side}.{_JOURNAL_NAME}.tmp"
        tmp.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.work_dir / f"{self.side}_{_JOURNAL_NAME}")

    def seal(self) -> dict:
        """收尾：仍有内存帧的桶补写运行；保证所有帧都在有序运行中。"""
        for idx in range(self.bucket_count):
            if self.buckets[idx]:
                self._flush_bucket(idx)
        self.sealed = True
        self._write_journal()
        return {
            "side": self.side,
            "source": self.source,
            "frames": self.total_frames,
            "run_files": len(self.runs),
            "spill_bytes": sum(r["size_bytes"] for r in self.runs),
            "journal": str(self.work_dir / f"{self.side}_{_JOURNAL_NAME}"),
        }

    def sorted_frames(self, bucket_idx: int) -> Iterator[Frame]:
        """对一个桶做 k 路外部归并，输出按规范字节有序的帧流。"""
        streams = []
        for run in self.runs:
            reader = RunReader(self.work_dir / run["name"], expected=run)
            streams.append(
                frame for stored_bucket, frame in reader if stored_bucket == bucket_idx
            )
        if self.buckets[bucket_idx]:  # seal 后通常为空，保留给未 seal 的场景
            ordered = sorted(self.buckets[bucket_idx], key=lambda f: f.canonical)
            streams.append(iter(ordered))
        if not streams:
            return iter(())
        if len(streams) == 1:
            return streams[0]
        return heapq.merge(*streams, key=lambda f: f.canonical)

    def all_frames(self) -> Iterator[Frame]:
        for run in self.runs:
            yield from RunReader(self.work_dir / run["name"], expected=run).frames()
        for items in self.buckets:
            yield from items


def recover_side(work_dir: str | os.PathLike, side: str, hash_fn: Callable[[bytes], bytes]):
    """从工作目录恢复单侧存储。

    - journal 已登记的运行：逐帧 CRC + 整体 sha256 校验，损坏即抛错。
    - 未登记的孤儿运行（崩溃前写完但 journal 未更新）：完整可解析则收养，
      截断/损坏则丢弃并在 dropped_runs 中报告。
    """
    work_dir = Path(work_dir)
    journal_path = work_dir / f"{side}_{_JOURNAL_NAME}"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    registered = {r["name"]: r for r in journal["runs"]}
    orphan_files = sorted(
        p for p in work_dir.glob(f"{side}_run_*.bin") if p.name not in registered
    )
    adopted, dropped = [], []
    for orphan in orphan_files:
        reader = RunReader(orphan, expected=None)
        try:
            frames = list(reader.frames())
        except SpillCorruptionError:
            orphan.unlink()
            dropped.append(orphan.name)
            continue
        adopted.append(
            {
                "name": orphan.name,
                "frames": len(frames),
                "size_bytes": orphan.stat().st_size,
                "sha256": reader._hasher.hexdigest(),
                "source": reader.source,
            }
        )
    runs = journal["runs"] + adopted
    # total_frames 以运行文件实际帧数为准；journal 的快照只用于交叉核对。
    total_frames = sum(r["frames"] for r in runs)
    if journal["total_frames"] > total_frames:
        raise SpillCorruptionError(
            f"{side} journal 记录 {journal['total_frames']} 帧，"
            f"但运行文件仅恢复 {total_frames} 帧"
        )
    store = SideStore(
        side=side,
        work_dir=work_dir,
        source=journal["source"],
        budget=1,
        bucket_count=int(journal["bucket_count"]),
        hash_fn=hash_fn,
        runs=runs,
        total_frames=total_frames,
        sealed=True,
    )
    # 强制读取一次登记运行，CRC/sha 不符立即抛 SpillCorruptionError。
    for run in journal["runs"]:
        for _ in RunReader(work_dir / run["name"], expected=run).frames():
            pass
    return store, {"adopted_runs": adopted, "dropped_runs": dropped}


# ---------------------------------------------------------------------------
# 粗筛：分桶计数 + 无序多重集摘要（哈希只用于分流，从不裁决相等）
# ---------------------------------------------------------------------------


def coarse_summaries(
    left: SideStore, right: SideStore, hash_fn: Callable[[bytes], bytes]
) -> list[dict]:
    summaries = []
    for idx in range(left.bucket_count):
        left_hashes: list[bytes] = []
        right_hashes: list[bytes] = []
        left_fps: list[int] = []
        right_fps: list[int] = []
        left_count = right_count = 0
        for frame in left.sorted_frames(idx):
            digest = hash_fn(frame.canonical)
            left_hashes.append(digest)
            left_fps.append(int.from_bytes(digest[:8], "little"))
            left_count += 1
        for frame in right.sorted_frames(idx):
            digest = hash_fn(frame.canonical)
            right_hashes.append(digest)
            right_fps.append(int.from_bytes(digest[:8], "little"))
            right_count += 1
        left_digest = hashlib.sha256(b"".join(sorted(left_hashes))).hexdigest()
        right_digest = hashlib.sha256(b"".join(sorted(right_hashes))).hexdigest()
        left_fp_digest = hashlib.sha256(
            b"".join(x.to_bytes(8, "little") for x in sorted(left_fps))
        ).hexdigest()
        right_fp_digest = hashlib.sha256(
            b"".join(x.to_bytes(8, "little") for x in sorted(right_fps))
        ).hexdigest()
        summaries.append(
            {
                "bucket": idx,
                "left_frames": left_count,
                "right_frames": right_count,
                "counts_equal": left_count == right_count,
                "sha256_multiset_digest_equal": left_digest == right_digest,
                "fp64_multiset_digest_equal": left_fp_digest == right_fp_digest,
                "coarse_equal": left_count == right_count
                and left_digest == right_digest,
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# 精确认证：有序规范字节流的多重集归并核对
# ---------------------------------------------------------------------------


def _groups(stream: Iterator[Frame]):
    for canonical, group in itertools.groupby(stream, key=lambda f: f.canonical):
        frames = list(group)
        yield canonical, frames


def exact_verify(left: SideStore, right: SideStore) -> list[dict]:
    differences: list[dict] = []
    for idx in range(left.bucket_count):
        left_groups = _groups(left.sorted_frames(idx))
        right_groups = _groups(right.sorted_frames(idx))
        l_item = next(left_groups, None)
        r_item = next(right_groups, None)
        while l_item is not None or r_item is not None:
            if r_item is None or (l_item is not None and l_item[0] < r_item[0]):
                canonical, l_frames = l_item
                differences.append(
                    _diff_entry("left_only", canonical, l_frames, [], idx)
                )
                l_item = next(left_groups, None)
            elif l_item is None or r_item[0] < l_item[0]:
                canonical, r_frames = r_item
                differences.append(
                    _diff_entry("right_only", canonical, [], r_frames, idx)
                )
                r_item = next(right_groups, None)
            else:
                canonical, l_frames = l_item
                _, r_frames = r_item
                if len(l_frames) != len(r_frames):
                    differences.append(
                        _diff_entry(
                            "multiplicity_diff",
                            canonical,
                            l_frames,
                            r_frames,
                            idx,
                        )
                    )
                l_item = next(left_groups, None)
                r_item = next(right_groups, None)
    return differences


def _diff_entry(kind: str, canonical: bytes, left_frames, right_frames, bucket: int) -> dict:
    return {
        "kind": kind,
        "bucket": bucket,
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "canonical_hex": canonical.hex(),
        "canonical_preview": decode_canonical(canonical),
        "left_count": len(left_frames),
        "right_count": len(right_frames),
        "multiplicity_delta": len(left_frames) - len(right_frames),
        "left_refs": [f.ref.to_jsonable() for f in left_frames],
        "right_refs": [f.ref.to_jsonable() for f in right_frames],
    }


def probabilistic_verify(summaries: list[dict]) -> bool:
    """仅基于 64 位指纹多重集的概率预检，调用方必须标注未精确认证。"""
    return all(s["counts_equal"] and s["fp64_multiset_digest_equal"] for s in summaries)


# ---------------------------------------------------------------------------
# 门面：端到端对账
# ---------------------------------------------------------------------------


def _ingest(side: str, source, contract: Contract, work_dir: Path, hash_fn) -> SideStore:
    store = SideStore(
        side=side,
        work_dir=work_dir,
        source=str(source),
        budget=contract.buffer_max_bytes,
        bucket_count=contract.bucket_count,
        hash_fn=hash_fn,
    )
    for raw_bag, ref in iter_source(source, contract):
        canonical, amount = canonicalize_row(raw_bag["_get"], contract, ref)
        store.add(Frame(canonical, ref), amount)
    store.seal()
    return store


def reconcile(
    left_path: str | os.PathLike,
    right_path: str | os.PathLike,
    contract_path: str | os.PathLike,
    *,
    mode: str = "exact",
    hash_fn: Optional[Callable[[bytes], bytes]] = None,
    work_dir: Optional[str | os.PathLike] = None,
    keep_work_dir: bool = False,
) -> dict:
    """对账主入口。

    mode:
      - "exact"（默认）：粗筛之后逐桶做规范字节 + 重数的精确核对，
        exact_verified=True，equal 才可信。
      - "probabilistic"：只做桶计数与 64 位指纹多重集预检，不做逐字节核对，
        结果显式标注 exact_verified=False。
    hash_fn: 可注入摘要函数（默认 hashlib.sha256），用于强制碰撞测试。
    即使左右路径相同也完整跑流程，绝不把路径相同当作内容一致。
    """
    if mode not in ("exact", "probabilistic"):
        raise ValueError("mode 仅支持 exact / probabilistic")
    contract = Contract.load(contract_path)
    digest_fn = hash_fn or (lambda blob: hashlib.sha256(blob).digest())

    created_temp = False
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="semrec_"))
        created_temp = True
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 注意：不比较路径、不短路，两侧各自独立规范与溢写。
        left = _ingest("left", left_path, contract, work_dir, digest_fn)
        right = _ingest("right", right_path, contract, work_dir, digest_fn)
        summaries = coarse_summaries(left, right, digest_fn)

        if mode == "exact":
            differences = exact_verify(left, right)
            exact_verified = True
            equal = not differences
            status = "equal_exact" if equal else "different_exact"
        else:
            differences = []
            exact_verified = False
            equal = probabilistic_verify(summaries)
            status = (
                "equal_unverified_probabilistic"
                if equal
                else "different_probabilistic_precheck"
            )

        return {
            "status": status,
            "equal": equal,
            "exact_verified": exact_verified,
            "mode": mode,
            "summary": {
                "left_rows": left.total_frames,
                "right_rows": right.total_frames,
                "row_counts_equal": left.total_frames == right.total_frames,
                "left_total_amount": str(left.total_amount),
                "right_total_amount": str(right.total_amount),
                "total_amounts_equal": left.total_amount == right.total_amount,
            },
            "coarse_buckets": summaries,
            "coarse_authoritative": False,
            "differences": differences,
            "spill": {"left": _spill_info(left), "right": _spill_info(right)},
            "work_dir": str(work_dir),
        }
    finally:
        if created_temp and not keep_work_dir:
            for child in work_dir.glob("*"):
                child.unlink()
            work_dir.rmdir()


def _spill_info(store: SideStore) -> dict:
    return {
        "side": store.side,
        "source": store.source,
        "frames": store.total_frames,
        "run_files": len(store.runs),
        "spill_bytes": sum(r["size_bytes"] for r in store.runs),
        "journal": str(store.work_dir / f"{store.side}_{_JOURNAL_NAME}"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CSV/JSONL 语义多重集对账器")
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--mode", choices=["exact", "probabilistic"], default="exact")
    parser.add_argument("--work-dir", default=None, help="保留溢写文件的工作目录")
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--output", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    result = reconcile(
        args.left,
        args.right,
        args.contract,
        mode=args.mode,
        work_dir=args.work_dir,
        keep_work_dir=args.keep_work_dir,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
