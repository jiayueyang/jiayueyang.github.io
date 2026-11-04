#!/usr/bin/env python3
"""Refresh the bilingual static homepage's INSPIRE metrics.

Source: https://inspirehep.net/api/authors/2679717
No third-party credentials or Python packages required.
The published author profile is used to resolve an exact BAI before searching.
On failure, the existing dated snapshot is left unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

BASE = "https://inspirehep.net/api"
AUTHOR_ID = "2679717"
PAGE = Path(__file__).resolve().parents[1] / "index.html"


def api_get(url: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "jiayueyang.github.io INSPIRE metrics updater (+https://github.com/jiayueyang/jiayueyang.github.io)",
        },
    )
    with urlopen(request, timeout=35) as response:
        return json.load(response)


def get_author_papers() -> tuple[int, list[dict]]:
    author = api_get(f"{BASE}/authors/{AUTHOR_ID}")
    meta = author.get("metadata") or {}
    identifiers = meta.get("ids") or []
    bai = next(
        (item.get("value") for item in identifiers if "INSPIRE BAI" in item.get("schema", "").upper()),
        None,
    )
    bai = bai or meta.get("bai")
    if not isinstance(bai, str) or not bai.strip():
        raise ValueError("No INSPIRE BAI found on the supplied author record")

    fields = "titles,dois,arxiv_eprints,citation_count,document_type"
    url = f"{BASE}/literature?size=250&page=1&fields={quote(fields)}&q={quote('a ' + bai)}"
    response = api_get(url)
    result = response.get("hits") or {}
    total = result.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    hits = result.get("hits") or []
    if not isinstance(total, int) or not 10 <= total <= 250 or len(hits) != total:
        raise ValueError(f"Unexpected paper result count: total={total!r}, hits={len(hits)}")
    return total, hits


def replace_with_count(html: str, field: str, value: int) -> str:
    pattern = rf'(<(?:div|span)\b[^>]*data-inspire-stat="{field}"[^>]*>)[0-9]+(</(?:div|span)>)'
    changed, count = re.subn(pattern, lambda m: f"{m.group(1)}{value}{m.group(2)}", html)
    if count != 4:
        raise ValueError(f"Expected four {field} displays, found {count}")
    return changed


def refresh() -> None:
    total, hits = get_author_papers()
    cites = sum(int(p.get("metadata", {}).get("citation_count") or 0) for p in hits)
    date = datetime.now(timezone.utc).date().isoformat()

    html = PAGE.read_bytes().decode("utf-8")
    modified = replace_with_count(html, "records", total)
    modified = replace_with_count(modified, "citations", cites)

    date_index = 0

    def rewrite_date(match: re.Match[str]) -> str:
        nonlocal date_index
        date_index += 1
        message = f"（INSPIRE 更新于 {date}）" if date_index == 1 else f"(INSPIRE updated {date})"
        return f"{match.group(1)}{message}{match.group(2)}"

    modified = re.sub(
        r'(<span class="metric-asof" data-inspire-date>).*?(</span>)',
        rewrite_date,
        modified,
        flags=re.DOTALL,
    )
    if date_index != 2:
        raise ValueError("Expected two language-specific update date labels")

    by_doi: dict[str, dict] = {}
    by_arxiv: dict[str, dict] = {}
    by_record: dict[str, dict] = {}
    for paper in hits:
        meta = paper.get("metadata") or {}
        for item in meta.get("dois") or []:
            if item.get("value"):
                by_doi[item["value"].lower()] = paper
        for item in meta.get("arxiv_eprints") or []:
            if item.get("value"):
                by_arxiv[item["value"]] = paper
        if paper.get("id"):
            by_record[str(paper["id"])] = paper

    matched = 0

    def rewrite_card(match: re.Match[str]) -> str:
        nonlocal matched
        card = match.group(0)
        doi = re.search(r'https://doi\.org/([^"]+)', card)
        arxiv = re.search(r'https://arxiv\.org/abs/(\d{4}\.\d{4,5})', card)
        record = re.search(r'https://inspirehep\.net/literature/(\d+)', card)
        paper = (
            (by_doi.get(unquote(doi.group(1)).lower()) if doi else None)
            or (by_arxiv.get(arxiv.group(1)) if arxiv else None)
            or (by_record.get(record.group(1)) if record else None)
        )
        if paper is None:
            return card
        matched += 1
        number = int((paper.get("metadata") or {}).get("citation_count") or 0)
        chinese = "INSPIRE 引用:" in card
        label = f"INSPIRE 引用: {number} · 更新于 {date}" if chinese else f"INSPIRE citations: {number} · Updated {date}"
        changed, n = re.subn(
            r'(<p class="pub-metrics">)INSPIRE (?:引用|citations):.*?(</p>)',
            lambda m: f"{m.group(1)}{label}{m.group(2)}",
            card,
            count=1,
            flags=re.DOTALL,
        )
        if n != 1:
            raise ValueError("Matched paper lacks a citation-count paragraph")
        return changed

    modified, cards = re.subn(
        r'<div class="publication">.*?</div>', rewrite_card, modified, flags=re.DOTALL
    )
    if cards != 26 or matched < 16:
        raise ValueError(f"Unexpected publication matching: cards={cards}, matched={matched}")
    if modified != html:
        PAGE.write_bytes(modified.encode("utf-8"))
    print(f"INSPIRE {date}: {total} indexed records, {cites} total citations; {matched}/{cards} cards refreshed.")


if __name__ == "__main__":
    try:
        refresh()
    except Exception as exc:
        print(f"INSPIRE sync failed without altering the homepage: {exc}", file=sys.stderr)
        sys.exit(1)
