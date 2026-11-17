"""Resilient fact-check scraper for the project dataset.

This version improves ordinary public-page fetching and extraction without
bypassing CAPTCHAs, authentication, robots restrictions, or access controls.
Run from the project root: python scraper.py
"""
from __future__ import annotations
import argparse, csv, json, re, time, socket, hashlib
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# A normal desktop-browser UA is more compatible with publisher sites than a
# custom placeholder research UA. It does not impersonate a logged-in user.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ur;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
SELECTED = {"yes", "true", "1", "y"}
MIN_HTML_BYTES = 700
MIN_TEXT_CHARS = 120
REQUEST_TIMEOUT = (10, 35)
MAX_ATTEMPTS = 5
REQUEST_DELAY = 2.0
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def now():
    return datetime.now(timezone.utc).isoformat()


def norm_url(url):
    return (url or "").strip().split("#", 1)[0].rstrip("/")


def read_jsonl(path):
    records = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[WARN] Invalid JSONL line {line_no} in {path}; ignoring it.")
    return records


def write_jsonl_atomic(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_selected_urls(csv_path):
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = []
        for row in csv.DictReader(f):
            if (row.get("selected") or "").strip().lower() in SELECTED:
                row = {k: (v or "").strip() for k, v in row.items()}
                if row.get("article_id") and row.get("url"):
                    rows.append(row)
    # Prevent accidental repeated IDs in the input CSV.
    unique = {}
    for row in rows:
        unique.setdefault(row["article_id"], row)
    return list(unique.values())


def walk_jsonld(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_jsonld(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_jsonld(value)


def structured_metadata(soup):
    meta = {"title": "", "date": "", "body": "", "claim": "", "verdict": ""}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        val = (tag.get("content") or "").strip()
        if not val:
            continue
        if key in {"og:title", "twitter:title", "headline"} and not meta["title"]:
            meta["title"] = val
        elif key in {"article:published_time", "datepublished", "pubdate"} and not meta["date"]:
            meta["date"] = val
        elif key in {"og:description", "description", "twitter:description"} and not meta["body"]:
            meta["body"] = val
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for item in walk_jsonld(data):
            typ = item.get("@type", [])
            types = [typ] if isinstance(typ, str) else typ
            meta["title"] = meta["title"] or item.get("headline") or item.get("name") or ""
            meta["date"] = meta["date"] or item.get("datePublished") or item.get("dateCreated") or ""
            meta["body"] = meta["body"] or item.get("articleBody") or ""
            if "ClaimReview" in types:
                meta["claim"] = item.get("claimReviewed") or meta["claim"]
                rating = item.get("reviewRating") or {}
                if isinstance(rating, dict):
                    meta["verdict"] = rating.get("alternateName") or rating.get("ratingExplanation") or meta["verdict"]
    # Some CMS platforms place article JSON inside hydration scripts.
    for script in soup.find_all("script", id="__NEXT_DATA__"):
        raw = script.string or script.get_text(" ", strip=True)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        chunks = []
        def collect(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in {"articlebody", "body", "content", "description"} and isinstance(v, str):
                        if len(v) > 100:
                            chunks.append(v)
                    else:
                        collect(v)
            elif isinstance(obj, list):
                for v in obj:
                    collect(v)
        collect(data)
        if chunks and len(max(chunks, key=len)) > len(meta["body"]):
            meta["body"] = max(chunks, key=len)
    return meta


def clean_node(node):
    # Remove only clear non-editorial elements; preserve lists, quotes and headings.
    node = BeautifulSoup(str(node), "html.parser")
    for tag in node.select("script, style, noscript, iframe, nav, footer, header, aside, form, button, svg, figure, picture"):
        tag.decompose()
    for tag in node.select(
        '[class*="share"],[class*="social"],[class*="comment"],[class*="related"],'
        '[class*="newsletter"],[class*="advert"],[class*="breadcrumb"],[class*="author-bio"],'
        '[class*="most-read"],[class*="recommended"]'
    ):
        tag.decompose()
    pieces, seen = [], set()
    for el in node.find_all(["p", "h2", "h3", "blockquote", "li"]):
        value = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if len(value) < 25:
            continue
        low = value.casefold()
        if any(x in low for x in (
            "subscribe to our", "follow us on", "share this article", "related articles",
            "all rights reserved", "click here to subscribe", "download our app",
            "read more:", "you may also like"
        )):
            continue
        key = re.sub(r"\W+", " ", low).strip()
        if key and key not in seen:
            pieces.append(value)
            seen.add(key)
    return "\n\n".join(pieces)


def extract_article(html, source=""):
    soup = BeautifulSoup(html, "html.parser")
    meta = structured_metadata(soup)
    h1 = soup.find("h1") or soup.select_one(".article-title, .story-title, .story__title, [itemprop='headline']")
    title = (h1.get_text(" ", strip=True) if h1 else "") or meta["title"]
    time_node = soup.find("time")
    date = ((time_node.get("datetime") or time_node.get_text(" ", strip=True)) if time_node else "") or meta["date"]

    # Includes known common CMS patterns plus broad semantic containers.
    selectors = [
        "article .content", "article .article-content", "article .article__content",
        "article .story-content", "article .story__content", "article .entry-content",
        ".article-detail", ".article__body", ".article-body", ".article-content",
        ".story-detail", ".story__content", ".story-content", ".story-body",
        ".field--name-body", ".field-body", ".entry-content", ".post-content",
        "[itemprop='articleBody']", "[class*='article_body']", "[class*='article-body']",
        "[class*='article-content']", "[class*='story-content']", "[class*='story__content']",
        "[id*='article-body']", "main article", "article", "main"
    ]
    candidates = []
    for selector in selectors:
        for node in soup.select(selector)[:8]:
            text = clean_node(node)
            if len(text) >= MIN_TEXT_CHARS:
                candidates.append(text)
    # Global paragraph fallback is useful for sites whose CMS markup changes.
    if not candidates:
        candidates.append(clean_node(soup))
    text = max(candidates, key=lambda x: (len(x), len(x.split()))) if candidates else ""

    # Prefer real page prose over short metadata descriptions; use JSON-LD body
    # when it contains more text than the DOM extraction.
    structured_body = re.sub(r"<[^>]+>", " ", str(meta["body"] or ""))
    structured_body = BeautifulSoup(structured_body, "html.parser").get_text(" ", strip=True)
    if len(structured_body) > len(text):
        text = structured_body

    paras, seen = [], set()
    for p in re.split(r"\n\s*\n|(?<=[.!?؟۔])\s+(?=[A-Zآ-ی0-9])", text):
        p = re.sub(r"\s+", " ", p).strip()
        key = re.sub(r"\W+", " ", p.casefold()).strip()
        if len(p) >= 25 and key and key not in seen:
            paras.append(p)
            seen.add(key)
    text = "\n\n".join(paras)
    return title, date, text, meta["claim"], meta["verdict"]


def classify_exception(exc):
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, requests.exceptions.ConnectionError) and any(x in low for x in ("name or service not known", "failed to resolve", "getaddrinfo", "name resolution")):
        return "DNS resolution failure: check internet/DNS; retry later"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS/SSL failure: check system clock, certificates, or network"
    if isinstance(exc, requests.exceptions.Timeout):
        return "Connection/read timeout"
    return f"{type(exc).__name__}: {msg[:500]}"


def fetch_html(session, url):
    last_reason, last_status = "Unknown error", None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            last_status = response.status_code
            if response.status_code == 200:
                content = response.content
                if len(content) < MIN_HTML_BYTES:
                    return None, f"HTTP 200 but response too small ({len(content)} bytes); possible block/interstitial", 200
                ctype = response.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype and "application/xhtml" not in ctype:
                    return None, f"Unexpected content type: {ctype or 'missing'}", 200
                return response.text, "", 200
            if response.status_code in (401, 403):
                return None, f"HTTP {response.status_code}: publisher denied this request; not retried", response.status_code
            if response.status_code in (404, 410):
                return None, f"HTTP {response.status_code}: URL may be removed or incorrect", response.status_code
            last_reason = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                return None, last_reason, response.status_code
            retry_after = response.headers.get("Retry-After", "")
            delay = min(90, int(retry_after)) if retry_after.isdigit() else min(30, 2 ** attempt)
        except requests.RequestException as exc:
            last_reason = classify_exception(exc)
            delay = min(30, 2 ** attempt)
        if attempt < MAX_ATTEMPTS:
            print(f"  retry {attempt}/{MAX_ATTEMPTS - 1} after {delay}s: {last_reason}")
            time.sleep(delay)
    return None, last_reason, last_status


def main(csv_path="article_urls.csv", output_dir="data", diagnose=False):
    root = Path(".")
    output = root / output_dir
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manual_dir = output / "manual_html"
    manual_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output / "articles_raw.jsonl"
    skipped_path = output / "skipped.json"
    status_path = output / "scrape_status.csv"
    recovery_path = output / "recovery_queue.csv"
    rows = load_selected_urls(root / csv_path)

    existing = read_jsonl(raw_path)
    by_id = {}
    for rec in existing:
        aid = rec.get("article_id")
        if aid and len((rec.get("text") or "").strip()) >= MIN_TEXT_CHARS:
            by_id[aid] = rec

    session = requests.Session()
    session.headers.update(HEADERS)
    failures, statuses = [], []
    added = recovered = 0
    old_ids = set(by_id)

    for i, row in enumerate(rows, 1):
        aid, url = row["article_id"], norm_url(row["url"])
        if aid in by_id:
            statuses.append({"article_id": aid, "url": url, "source": row.get("source", ""), "status": "already_extracted", "http_status": "", "reason": ""})
            continue

        cache = raw_dir / f"{aid}.html"
        manual_cache = manual_dir / f"{aid}.html"
        html_content = None
        source_used = "network"
        for candidate, label in ((manual_cache, "manual_html"), (cache, "cache")):
            if candidate.exists() and candidate.stat().st_size >= MIN_HTML_BYTES:
                html_content = candidate.read_text(encoding="utf-8", errors="replace")
                source_used = label
                print(f"[{i}/{len(rows)}] {label}: {aid}")
                break
        if html_content is None:
            print(f"[{i}/{len(rows)}] fetch: {aid}")
            html_content, reason, status_code = fetch_html(session, url)
            time.sleep(REQUEST_DELAY)
            if html_content:
                cache.write_text(html_content, encoding="utf-8")
            else:
                failures.append({"article_id": aid, "url": url, "source": row.get("source", ""), "topic": row.get("topic", ""), "claim_idea": row.get("claim_idea", ""), "reason": reason, "http_status": status_code, "checked_at": now()})
                statuses.append({"article_id": aid, "url": url, "source": row.get("source", ""), "status": "needs_recovery", "http_status": status_code or "", "reason": reason})
                continue

        try:
            title, date, article_text, claim, verdict = extract_article(html_content, row.get("source", ""))
            reason = ""
        except Exception as exc:
            title = date = article_text = claim = verdict = ""
            reason = f"Extraction error: {type(exc).__name__}: {exc}"

        if len(article_text.strip()) < MIN_TEXT_CHARS:
            # Do not replace a good cache with a failed/empty response.
            failures.append({"article_id": aid, "url": url, "source": row.get("source", ""), "topic": row.get("topic", ""), "claim_idea": row.get("claim_idea", ""), "reason": reason or f"Article body too short ({len(article_text)} chars)", "http_status": 200, "cache_file": str(manual_cache if source_used == "manual_html" else cache), "checked_at": now()})
            statuses.append({"article_id": aid, "url": url, "source": row.get("source", ""), "status": "needs_recovery", "http_status": 200, "reason": reason or f"Article body too short ({len(article_text)} chars)"})
            continue

        record = {
            "article_id": aid, "title": title or row.get("title", ""), "text": article_text,
            "language": row.get("language", ""), "url": url, "date": date,
            "source": row.get("source", ""), "topic": row.get("topic", ""),
            "claim_idea": row.get("claim_idea", "") or claim, "notes": row.get("notes", ""),
            "verdict": verdict, "extraction_method": f"multi_strategy_{source_used}", "scraped_at": now()
        }
        by_id[aid] = record
        if aid in old_ids:
            recovered += 1
        else:
            added += 1
        statuses.append({"article_id": aid, "url": url, "source": row.get("source", ""), "status": "extracted", "http_status": 200, "reason": ""})

    # Preserve every previously valid record and update only successful recoveries.
    write_jsonl_atomic(raw_path, list(by_id.values()))
    with skipped_path.open("w", encoding="utf-8") as f:
        json.dump(failures, f, ensure_ascii=False, indent=2)
    with status_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["article_id", "url", "source", "status", "http_status", "reason"])
        writer.writeheader()
        writer.writerows(statuses)

    # Keep the recovery queue synchronized with the current selected URL list.
    # Old AFP/Geo/404 rows marked selected=no will no longer reappear here.
    with recovery_path.open("w", newline="", encoding="utf-8-sig") as f:
        queue_fields = ["article_id", "source", "topic", "url", "http_status", "reason", "claim_idea", "checked_at"]
        writer = csv.DictWriter(f, fieldnames=queue_fields)
        writer.writeheader()
        for item in failures:
            writer.writerow({key: item.get(key, "") for key in queue_fields})

    counts = {}
    for row in rows:
        counts[row.get("source", "unknown")] = counts.get(row.get("source", "unknown"), 0) + 1
    summary = {
        "generated_at": now(), "selected_urls": len(rows), "valid_records": len(by_id),
        "new_records": added, "recovered_records": recovered, "still_needing_recovery": len(failures),
        "selected_by_source": counts,
        "failed_by_reason": {}
    }
    for item in failures:
        key = item.get("reason", "unknown").split(":", 1)[0]
        summary["failed_by_reason"][key] = summary["failed_by_reason"].get(key, 0) + 1
    (output / "scrape_run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Run summary ===")
    print(f"Selected URLs: {len(rows)}")
    print(f"Valid records now stored: {len(by_id)}")
    print(f"New records: {added}; recovered: {recovered}; still needing recovery: {len(failures)}")
    print(f"Reports: {skipped_path}, {status_path}, {recovery_path}, {output / 'scrape_run_summary.json'}")
    print("Note: HTTP 401/403 and CAPTCHA/access-denied pages require publisher-authorized access or an alternative licensed copy.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape selected public fact-check articles")
    parser.add_argument("--csv", default="article_urls.csv", help="Input URL CSV")
    parser.add_argument("--output", default="data", help="Output directory")
    args = parser.parse_args()
    main(args.csv, args.output)
