"""
Conservative bilingual cleaning and normalization for fact-check retrieval.
Raw article text is always retained in text_raw; cleaned text is stored separately.
The cleaner deliberately avoids aggressive stemming, transliteration, or stopword removal.
"""
import json, re, html, unicodedata
from pathlib import Path
from collections import Counter

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
CHAR_MAP = str.maketrans({"ي":"ی", "ى":"ی", "ك":"ک", "\u200c":" ", "\u200d":"", "\ufeff":""})
PUNCT_MAP = str.maketrans({"۔":".", "،":",", "؛":";", "؟":"?", "“":'"', "”":'"', "‘":"'", "’":"'", "—":" - ", "–":" - ", "…":"..."})

# Remove only standalone boilerplate lines. Do not use broad patterns that can
# consume the remainder of a paragraph/article.
BOILERPLATE = [
    re.compile(r"^(share (this )?(article|story|post)|follow us|subscribe( to)?|read also|also read|related articles?)\b.*$", re.I),
    re.compile(r"^(facebook|twitter|whatsapp|linkedin)\s*(share|follow)?$", re.I),
    re.compile(r"^(مزید پڑھیں|یہ بھی پڑھیں|ہمیں فالو کریں|فیس بک پر شیئر کریں|ٹوئٹر پر شیئر کریں|واٹس ایپ پر شیئر کریں)\s*:?.*$"),
    re.compile(r"^(photo|image)\s+credits?\s*:?.*$", re.I),
    re.compile(r"^all rights reserved\.?$", re.I),
]
URL_EMAIL = re.compile(r"(https?://\S+|www\.\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b)")

def normalize_digits(text):
    return text.translate(ARABIC_DIGITS)

def normalize_urdu_characters(text):
    return text.translate(CHAR_MAP)

def remove_diacritics(text):
    # Keep Quranic/Arabic combining marks out of the normalized retrieval text,
    # while preserving the original in text_raw.
    return re.sub(r"[\u064B-\u065F\u0670]", "", text)

def normalize_text(text, language="en"):
    if not text:
        return ""
    text = html.unescape(str(text))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize_digits(text)
    text = normalize_urdu_characters(text)
    text = text.translate(PUNCT_MAP)
    if language == "ur" or re.search(r"[\u0600-\u06FF]", text):
        text = remove_diacritics(text)

    cleaned = []
    for raw_line in re.split(r"[\r\n]+", text):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if any(pattern.match(line) for pattern in BOILERPLATE):
            continue
        line = URL_EMAIL.sub(" ", line)
        line = re.sub(r"\s+", " ", line).strip(" \t|")
        if len(line) >= 12:
            cleaned.append(line)

    # Deduplicate exact repeated lines, preserving article order.
    seen, result = set(), []
    for line in cleaned:
        key = line.casefold()
        if key not in seen:
            result.append(line); seen.add(key)
    return "\n\n".join(result)

def detect_language(record):
    declared = (record.get("language") or "").strip().lower()
    text = record.get("text") or record.get("text_raw") or ""
    urdu_chars = len(re.findall(r"[\u0600-\u06FF]", text))
    latin_chars = len(re.findall(r"[A-Za-z]", text))
    if urdu_chars and (declared == "ur" or urdu_chars >= max(10, latin_chars * 0.08)):
        return "ur"
    return "en" if declared not in ("ur", "urd") else "ur"

def clean_article(record):
    raw = record.get("text_raw") or record.get("text") or ""
    language = detect_language(record)
    cleaned = normalize_text(raw, language)
    raw_words, clean_words = len(raw.split()), len(cleaned.split())
    return {
        "article_id": record.get("article_id", ""),
        "title": normalize_text(record.get("title", ""), language).replace("\n\n", " "),
        "language": language,
        "topic": (record.get("topic") or "").strip().lower(),
        "source": (record.get("source") or "").strip(),
        "url": (record.get("url") or "").strip(),
        "date": (record.get("date") or "").strip(),
        "claim_idea": normalize_text(record.get("claim_idea", ""), language).replace("\n\n", " "),
        "notes": normalize_text(record.get("notes", ""), language).replace("\n\n", " "),
        "verdict": normalize_text(record.get("verdict", ""), language).replace("\n\n", " "),
        "text_raw": raw,
        "text_cleaned": cleaned,
        "quality": {
            "has_title": bool(record.get("title")),
            "has_text": bool(cleaned),
            "raw_char_count": len(raw),
            "clean_char_count": len(cleaned),
            "raw_word_count": raw_words,
            "clean_word_count": clean_words,
            "cleaning_retention_pct": round(100 * len(cleaned) / max(1, len(raw)), 2)
        }
    }

def load_jsonl(path):
    records = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip(): continue
            try: records.append(json.loads(line))
            except json.JSONDecodeError: print(f"[WARN] Invalid JSON on line {line_no}; skipped.")
    return records

def process_jsonl(input_path="data/articles_raw.jsonl", output_path="data/articles_clean.jsonl"):
    source, target = Path(input_path), Path(output_path)
    if not source.exists():
        raise FileNotFoundError(f"Input file not found: {source}")
    records = load_jsonl(source)
    # Prefer the record with the most usable text when duplicate IDs occur.
    chosen = {}
    for rec in records:
        aid = rec.get("article_id") or f"missing_id_{len(chosen)}"
        previous = chosen.get(aid)
        if previous is None or len(rec.get("text", "")) > len(previous.get("text", "")):
            chosen[aid] = rec
    cleaned = [clean_article(r) for r in chosen.values()]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in cleaned:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(target)

    langs = Counter(r["language"] for r in cleaned)
    empty = [r["article_id"] for r in cleaned if not r["text_cleaned"]]
    print("=== Cleaning complete ===")
    print(f"Input records: {len(records)} | Unique records: {len(cleaned)}")
    print(f"Language distribution: {dict(langs)}")
    print(f"Records with empty cleaned text: {len(empty)}")
    print(f"Saved: {target}")
    return cleaned

if __name__ == "__main__":
    process_jsonl()
