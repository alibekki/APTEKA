"""Fuzzy string matching for pharmacy product search.

Handles typos, missing letters, swapped characters in drug names.
Optimized for Cyrillic pharmacy names.
"""

import re
from difflib import SequenceMatcher


def _normalize(text: str) -> str:
    """Normalize text for comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_base_name(name: str) -> str:
    """Extract base drug name: remove dosage, form, count.

    'Парацетамол 500 мг таб. №20' → 'парацетамол'
    'НУРОФЕН ЭКСПРЕСС ФОРТЕ 400мг капс №20' → 'нурофен экспресс форте'
    """
    name = _normalize(name)
    # Remove from the first number+unit pattern onwards
    name = re.sub(
        r"\s*\d+[.,]?\d*\s*(мг|мл|г|мкг|ме|mg|ml|mcg|%)\b.*", "", name
    )
    # Remove dosage forms
    name = re.sub(
        r"\s*(табл?\.?|капс\.?|сироп|р-р|р/р|раствор|крем|мазь|гель|капли|свечи|суспензия|порошок|спрей|амп\.?|супп\.?|драже|пастилки|саше)\b.*",
        "", name, flags=re.IGNORECASE,
    )
    # Remove №XX count
    name = re.sub(r"\s*№\s*\d+.*", "", name)
    # Remove trailing numbers
    name = re.sub(r"\s+\d+\s*$", "", name)
    return name.strip()


def similarity_score(query: str, product_name: str) -> float:
    """Calculate similarity score between query and product name.

    Returns 0.0 to 2.0:
    - 0.0-0.4: poor match
    - 0.4-0.7: partial match
    - 0.7-1.0: good match
    - 1.0-2.0: excellent/exact match
    """
    query_norm = _normalize(query)
    name_norm = _normalize(product_name)

    if not query_norm or not name_norm:
        return 0.0

    # Exact substring match — highest score
    if query_norm in name_norm:
        # Boost if query matches the beginning (more relevant)
        if name_norm.startswith(query_norm):
            return 2.0
        return 1.5 + (len(query_norm) / len(name_norm)) * 0.5

    # Extract base name for comparison
    base_name = _extract_base_name(product_name)

    # Base name exact match
    if query_norm == base_name:
        return 1.8

    # Base name contains query
    if query_norm in base_name:
        return 1.3

    # Query words match
    query_words = query_norm.split()
    name_words = name_norm.split()

    if len(query_words) > 1:
        matched_words = sum(
            1 for qw in query_words
            if any(qw in nw or nw.startswith(qw) for nw in name_words)
        )
        if matched_words == len(query_words):
            return 1.2
        if matched_words > 0:
            return 0.6 + (matched_words / len(query_words)) * 0.4

    # Fuzzy match using SequenceMatcher
    # Compare against base name (more meaningful)
    ratio = SequenceMatcher(None, query_norm, base_name).ratio()

    # Also check first N chars match (catches typos in the middle)
    prefix_len = min(3, len(query_norm), len(base_name))
    if prefix_len >= 2 and query_norm[:prefix_len] == base_name[:prefix_len]:
        ratio += 0.15

    # Check if characters are similar (catches swapped/missing chars)
    if len(query_norm) >= 4 and len(base_name) >= 4:
        # Shared character ratio
        query_chars = set(query_norm.replace(" ", ""))
        name_chars = set(base_name.replace(" ", ""))
        if query_chars and name_chars:
            char_overlap = len(query_chars & name_chars) / max(len(query_chars), len(name_chars))
            if char_overlap > 0.7:
                ratio += 0.1

    return ratio


def fuzzy_search(query: str, products: list[dict], limit: int = 10,
                  threshold: float = 0.45) -> list[dict]:
    """Search products with fuzzy matching.

    Args:
        query: Search query
        products: List of product dicts with 'name' field
        limit: Max results to return
        threshold: Minimum similarity score

    Returns:
        Products sorted by relevance (score desc), then stock (desc), then price (asc)
    """
    query_norm = _normalize(query)
    if not query_norm:
        return []

    scored = []
    for product in products:
        name = product.get("name", "")
        score = similarity_score(query, name)
        if score >= threshold:
            scored.append((score, product))

    # Sort: score desc, then in-stock first, then price asc
    scored.sort(key=lambda x: (
        -x[0],
        0 if (x[1].get("rest_abs", 0) - x[1].get("rest_rezerv", 0)) > 0 else 1,
        x[1].get("price", 0),
    ))

    return [p for _, p in scored[:limit]]
