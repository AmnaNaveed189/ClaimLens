"""Audit selected URLs against extracted JSONL records and write a quality report."""
import csv, json
from pathlib import Path
from collections import Counter

def main():
    with open("article_urls.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    selected = [r for r in rows if (r.get("selected") or "").strip().lower() in {"yes","true","1","y"}]
    records = []
    p = Path("data/articles_raw.jsonl")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                if line.strip(): records.append(json.loads(line))
            except json.JSONDecodeError: pass
    by_id = {}
    for r in records:
        aid = r.get("article_id")
        if aid and len((r.get("text") or "").strip()) >= 120:
            by_id[aid] = r
    missing = [r for r in selected if r.get("article_id") not in by_id]
    report = {
        "selected_urls": len(selected),
        "valid_extracted_records": len(by_id),
        "missing_or_insufficient_records": len(missing),
        "duplicate_article_ids_in_raw_file": len(records) - len({r.get("article_id") for r in records}),
        "by_source": {},
        "missing_article_ids": [r.get("article_id") for r in missing],
        "empty_text_records": [r.get("article_id") for r in records if not (r.get("text") or "").strip()]
    }
    srcs = sorted({r.get("source","") for r in selected})
    for src in srcs:
        group = [r for r in selected if r.get("source","")==src]
        ok = sum(r.get("article_id") in by_id for r in group)
        report["by_source"][src] = {"selected":len(group),"extracted":ok,"missing":len(group)-ok}
    Path("data").mkdir(exist_ok=True)
    Path("data/quality_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__ == "__main__": main()
