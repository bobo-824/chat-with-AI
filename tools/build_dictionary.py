"""Compile a full ECDICT CSV into the compact runtime dictionary used by server.py.

ECDICT (https://github.com/skywind3000/ECDICT) is MIT licensed. The full CSV is
~66MB and is not committed to this repository; run this script after downloading
it to (re)generate data/dict-en-zh.json.

Usage:
    python tools/build_dictionary.py [--source data/ecdict.csv] [--limit 20000]
"""

import argparse
import csv
import datetime
import json
import re
from pathlib import Path

WORD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z]{0,23}$")
POS_PATTERN = re.compile(r"^([A-Za-z]{1,9}\.)\s*(.+)$")
INFLECTION_CODES = {"p", "d", "i", "s", "r", "t", "3"}
DROP_PATTERNS = (
    re.compile(r"^\["),
    re.compile(r"^\s*$"),
    re.compile(r"的过去式|的过去分词|的现在分词|的复数|的比较级|的最高级"),
)
MAX_SENSES = 4
MAX_SENSE_LENGTH = 90


def clean_senses(raw):
    senses = []
    seen = set()
    for line in re.split(r"(?:\\n|\n|\r)+", raw or ""):
        text = re.sub(r"\s+", " ", line).strip(" .;\u3002\uff1b")
        if not text or any(pattern.search(text) for pattern in DROP_PATTERNS):
            continue
        match = POS_PATTERN.match(text)
        pos, body = (match.group(1), match.group(2)) if match else ("", text)
        body = body[:MAX_SENSE_LENGTH].rstrip(" ,\uff0c")
        if not body:
            continue
        entry = (pos, body)
        if entry in seen:
            continue
        seen.add(entry)
        senses.append((pos + " " if pos else "") + body)
        if len(senses) >= MAX_SENSES:
            break
    return senses


def inflected_forms(raw):
    forms = []
    for part in (raw or "").split("/"):
        code, _, value = part.partition(":")
        value = value.strip().lower()
        if code.strip() in INFLECTION_CODES and WORD_PATTERN.match(value):
            forms.append(value)
    return forms


def frequency_rank(row):
    values = []
    for column in ("frq", "bnc"):
        try:
            number = int(str(row.get(column) or "0").strip() or 0)
        except ValueError:
            number = 0
        if number > 0:
            values.append(number)
    return min(values) if values else None


def load_rows(source):
    rows = []
    with source.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            word = (row.get("word") or "").strip()
            if not WORD_PATTERN.match(word):
                continue
            rank = frequency_rank(row)
            if rank is None:
                continue
            senses = clean_senses(row.get("translation"))
            if not senses:
                continue
            rows.append(
                {
                    "word": word.lower(),
                    "rank": rank,
                    "senses": senses,
                    "phonetic": re.sub(r"\s+", " ", (row.get("phonetic") or "").strip())[:48],
                    "forms": inflected_forms(row.get("exchange")),
                    "priority": 0 if (row.get("oxford") or "").strip() == "1" or (row.get("collins") or "").strip() in {"4", "5"} else 1,
                }
            )
    return rows


def build(rows, limit):
    rows.sort(key=lambda item: (item["priority"], item["rank"], item["word"]))
    selected = rows[:limit] if limit > 0 else rows
    words = {}
    phonetics = {}
    inflections = {}
    for item in selected:
        words[item["word"]] = item["senses"]
        if item["phonetic"]:
            phonetics[item["word"]] = item["phonetic"]
        for form in item["forms"]:
            if form != item["word"] and form not in inflections:
                inflections[form] = item["word"]
    payload = {
        "version": 1,
        "source": "ECDICT (https://github.com/skywind3000/ECDICT), MIT License",
        "generated_at": datetime.date.today().isoformat(),
        "note": "由 tools/build_dictionary.py 从完整 ECDICT CSV 生成的常用词子集，请勿手工编辑。",
        "words": dict(sorted(words.items())),
        "phonetics": dict(sorted(phonetics.items())),
        "inflections": dict(sorted(inflections.items())),
    }
    return payload


def dump(payload, target):
    lines = [
        "{",
        json.dumps("version", ensure_ascii=False) + ": " + json.dumps(payload["version"]) + ",",
        json.dumps("source", ensure_ascii=False) + ": " + json.dumps(payload["source"], ensure_ascii=False) + ",",
        json.dumps("generated_at", ensure_ascii=False) + ": " + json.dumps(payload["generated_at"]) + ",",
        json.dumps("note", ensure_ascii=False) + ": " + json.dumps(payload["note"], ensure_ascii=False) + ",",
        '"words": {',
    ]
    items = list(payload["words"].items())
    for index, (key, value) in enumerate(items):
        lines.append(json.dumps(key) + ": " + json.dumps(value, ensure_ascii=False) + ("," if index < len(items) - 1 else ""))
    for section in ("phonetics", "inflections"):
        lines.append("},")
        lines.append(json.dumps(section) + ": {")
        entries = list(payload[section].items())
        for index, (key, value) in enumerate(entries):
            lines.append(json.dumps(key) + ": " + json.dumps(value) + ("," if index < len(entries) - 1 else ""))
    lines.append("}")
    lines.append("}")
    text = "\n".join(lines) + "\n"
    json.loads(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Compile ECDICT CSV into data/dict-en-zh.json")
    parser.add_argument("--source", default=str(root / "data" / "ecdict.csv"))
    parser.add_argument("--target", default=str(root / "data" / "dict-en-zh.json"))
    parser.add_argument("--limit", type=int, default=20000, help="0 keeps every usable word")
    arguments = parser.parse_args()
    source = Path(arguments.source)
    if not source.exists():
        raise SystemExit("找不到 ECDICT CSV：%s\n请先下载 ecdict.csv 到 data/ 目录。" % source)
    rows = load_rows(source)
    target = Path(arguments.target)
    size = dump(build(rows, arguments.limit), target)
    print("可用词条 %d，写入 %s（%.1f KB）" % (len(rows), target, size / 1024))


if __name__ == "__main__":
    main()
