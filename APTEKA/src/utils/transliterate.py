"""Bidirectional transliteration: Latin ↔ Cyrillic for pharmacy product search."""

# Latin → Cyrillic mapping (pharmacy-oriented)
_LAT_TO_CYR = {
    "shch": "щ", "sch": "щ",
    "zh": "ж", "ch": "ч", "sh": "ш",
    "ts": "ц", "tz": "ц",
    "ya": "я", "ja": "я",
    "yu": "ю", "ju": "ю",
    "yo": "ё", "jo": "ё",
    "ye": "е",
    "ph": "ф", "th": "т",
    "kh": "х",
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д",
    "e": "е", "f": "ф", "h": "х", "i": "и",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "r": "р", "s": "с", "t": "т", "u": "у",
    "w": "в", "x": "кс", "y": "и", "z": "з",
    "c": "с", "j": "дж", "q": "к",
}

# Cyrillic → Latin mapping
_CYR_TO_LAT = {
    "щ": "shch", "ш": "sh", "ч": "ch", "ж": "zh",
    "ц": "ts", "я": "ya", "ю": "yu", "ё": "yo",
    "э": "e", "ъ": "", "ь": "",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ы": "y",
}

# Common pharmacy brand transliterations (brand name → how it might be typed)
_PHARMACY_ALIASES = {
    # Latin brand names commonly searched in cyrillic
    "нурофен": "nurofen",
    "парацетамол": "paracetamol",
    "ибупрофен": "ibuprofen",
    "амоксициллин": "amoxicillin",
    "омепразол": "omeprazole",
    "лоратадин": "loratadine",
    "цетиризин": "cetirizine",
    "метформин": "metformin",
    "аспирин": "aspirin",
    "диклофенак": "diclofenac",
    "кетопрофен": "ketoprofen",
    "напроксен": "naproxen",
    "дротаверин": "drotaverine",
    "лоперамид": "loperamide",
    "мезим": "mezim",
    "смекта": "smecta",
    "линекс": "linex",
    "супрастин": "suprastin",
    "кларитин": "claritin",
    "зиртек": "zyrtec",
    "ренни": "rennie",
    "гастал": "gastal",
    "фестал": "festal",
    "валидол": "validol",
    "корвалол": "corvalol",
    "анальгин": "analgin",
    "цитрамон": "citramon",
    "темпалгин": "tempalgin",
    "пенталгин": "pentalgin",
}

# Build reverse alias map
_PHARMACY_ALIASES_REVERSE = {v: k for k, v in _PHARMACY_ALIASES.items()}


def _is_latin(text: str) -> bool:
    """Check if text is predominantly Latin characters."""
    latin = sum(1 for c in text if 'a' <= c.lower() <= 'z')
    return latin > len(text) * 0.5


def _is_cyrillic(text: str) -> bool:
    """Check if text is predominantly Cyrillic characters."""
    cyrillic = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    return cyrillic > len(text) * 0.5


def lat_to_cyr(text: str) -> str:
    """Transliterate Latin text to Cyrillic."""
    text_lower = text.lower()

    # Check pharmacy aliases first
    if text_lower in _PHARMACY_ALIASES_REVERSE:
        return _PHARMACY_ALIASES_REVERSE[text_lower]

    result = []
    i = 0
    while i < len(text_lower):
        matched = False
        # Try longest match first (4, 3, 2, 1 chars)
        for length in (4, 3, 2, 1):
            chunk = text_lower[i:i + length]
            if chunk in _LAT_TO_CYR:
                result.append(_LAT_TO_CYR[chunk])
                i += length
                matched = True
                break
        if not matched:
            result.append(text_lower[i])
            i += 1

    return "".join(result)


def cyr_to_lat(text: str) -> str:
    """Transliterate Cyrillic text to Latin."""
    text_lower = text.lower()

    # Check pharmacy aliases first
    if text_lower in _PHARMACY_ALIASES:
        return _PHARMACY_ALIASES[text_lower]

    result = []
    for char in text_lower:
        if char in _CYR_TO_LAT:
            result.append(_CYR_TO_LAT[char])
        else:
            result.append(char)

    return "".join(result)


def generate_search_variants(query: str) -> list[str]:
    """Generate all possible search variants for a query.

    Returns a list of search strings to try:
    - Original query
    - Transliterated version
    - Known pharmacy aliases
    """
    query = query.strip()
    if not query:
        return []

    variants = [query.lower()]

    if _is_latin(query):
        # Latin input → try Cyrillic transliteration
        cyr = lat_to_cyr(query)
        if cyr != query.lower():
            variants.append(cyr)

        # Check alias
        if query.lower() in _PHARMACY_ALIASES_REVERSE:
            variants.append(_PHARMACY_ALIASES_REVERSE[query.lower()])

    elif _is_cyrillic(query):
        # Cyrillic input → try Latin transliteration
        lat = cyr_to_lat(query)
        if lat != query.lower():
            variants.append(lat)

        # Check alias
        if query.lower() in _PHARMACY_ALIASES:
            variants.append(_PHARMACY_ALIASES[query.lower()])

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)

    return unique
