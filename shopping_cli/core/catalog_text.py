"""Pure text normalization helpers used by catalog search."""

from __future__ import annotations

import re


def tokenize(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", value or "")]


def cjk_bigrams(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for sequence in re.findall(r"[\u4e00-\u9fff]+", value or ""):
        for index in range(0, max(len(sequence) - 1, 0)):
            term = sequence[index : index + 2]
            if term not in seen:
                terms.append(term)
                seen.add(term)
    return terms


def fts_search_document(value: str) -> str:
    """Preserve original text and add CJK singles/bigrams for unicode61 search."""
    original = str(value or "")
    bigrams = cjk_bigrams(original)
    singles: list[str] = []
    seen_singles: set[str] = set()
    for sequence in re.findall(r"[一-鿿]+", original):
        for character in sequence:
            if character not in seen_singles:
                singles.append(character)
                seen_singles.add(character)
    return " ".join([original, *singles, *bigrams]).strip()


def fts_query(query: str) -> str:
    """Build the FTS5 phrase-query string used by product/merchant search."""
    terms: list[str] = []
    seen: set[str] = set()
    for candidate in tokenize(query):
        if candidate and candidate not in seen:
            terms.append(candidate)
            seen.add(candidate)
    cj_bigrams = cjk_bigrams(query)
    for candidate in cj_bigrams:
        if candidate and candidate not in seen:
            terms.append(candidate)
            seen.add(candidate)
    if not cj_bigrams:
        for character in query:
            if "一" <= character <= "鿿" and character not in seen:
                terms.append(character)
                seen.add(character)
    return " OR ".join(f'"{token.replace(chr(34), chr(34) + chr(34))}"' for token in terms)
