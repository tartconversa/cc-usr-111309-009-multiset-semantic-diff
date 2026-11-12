import argparse
import csv
import json
from pathlib import Path


def load(path):
    source = Path(path)
    if source.suffix == ".csv":
        with source.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    if source.suffix == ".jsonl":
        with source.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    if source.suffix == ".json":
        return json.loads(source.read_text(encoding="utf-8"))
    raise ValueError("不支持的输入文件格式")


def main():
    parser = argparse.ArgumentParser(description="读取合成科学资料并显示类型与记录数")
    parser.add_argument("path")
    args = parser.parse_args()
    value = load(args.path)
    print(json.dumps({"path": args.path, "items": len(value), "input_type": type(value).__name__}, ensure_ascii=False))


if __name__ == "__main__":
    main()
