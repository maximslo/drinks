import re
import os


# ─── Name resolution ──────────────────────────────────────────────────────────

def load_phone_to_name():
    raw = os.getenv("PHONE_NAMES", "")
    mapping = {}
    if raw:
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            phone, name = pair.split(":", 1)
            mapping[phone.strip()] = name.strip()
    return mapping

def load_email_to_name():
    raw = os.getenv("EMAIL_NAMES", "")
    mapping = {}
    if raw:
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            email, name = pair.split(":", 1)
            mapping[email.strip()] = name.strip()
    return mapping

PHONE_TO_NAME = {
    **load_phone_to_name(),
    **load_email_to_name(),
}

def resolve_name(handle_id):
    return PHONE_TO_NAME.get(handle_id, handle_id)


# ─── Number parser ────────────────────────────────────────────────────────────

MIN_DRINK_NUMBER = 1000

def parse_numbers(text):
    """
    Parse drink number(s) and optional details from message text.
    Numbers can appear anywhere in the text. Everything else becomes details.
    Returns list of (number, details, starred) tuples, or [] if none found.

    Handles:
      "5593"                       → [(5593, None, False)]
      "5593*"                      → [(5593, None, True)]
      "5593, modelo"               → [(5593, "modelo", False)]
      "nothin man just snackin 9998" → [(9998, "nothin man just snackin", False)]
      "5649-5647"                  → [(5649,None,F), (5648,None,F), (5647,None,F)]
      "cheers 5649-5647 woo"       → [(5649,None,F), (5648,None,F), (5647,"cheers woo",F)]
      "5649 + 5648"                → [(5649,None,F), (5648,None,F)]
      "5649, 5648, On our way"     → [(5649,None,F), (5648,"On our way",F)]
      "5649 5648 Shooters"         → [(5649,None,F), (5648,"Shooters",F)]
    """
    if not text:
        return []
    text = text.strip()

    # Range anywhere in text: 5649-5647
    m = re.search(r'(?<!\d)(\d+)\s*-\s*(\d+)(?!\d)', text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        hi, lo = max(a, b), min(a, b)
        if hi >= MIN_DRINK_NUMBER and lo >= MIN_DRINK_NUMBER and hi - lo <= 20:
            leftover = _leftover(text, m.start(), m.end())
            nums = list(range(hi, lo - 1, -1))
            return [(n, leftover if i == len(nums) - 1 else None, False) for i, n in enumerate(nums)]

    # Plus notation anywhere: 5649 + 5648 + 5647
    m = re.search(r'(?<!\d)\d+(\s*\+\s*\d+)+(?!\d)', text)
    if m:
        nums = sorted([int(n) for n in re.findall(r'\d+', m.group(0))], reverse=True)
        if nums[0] >= MIN_DRINK_NUMBER:
            leftover = _leftover(text, m.start(), m.end())
            return [(n, leftover if i == len(nums) - 1 else None, False) for i, n in enumerate(nums)]

    # General: find all drink numbers anywhere in text
    matches = [
        (int(m.group(1)), bool(m.group(2)), m.start(), m.end())
        for m in re.finditer(r'(?<!\d)(\d+)(\*?)(?!\d)', text)
        if int(m.group(1)) >= MIN_DRINK_NUMBER
    ]
    if not matches:
        return []

    # Details = everything left after removing the matched number spans
    leftover = text
    for _, _, start, end in reversed(matches):
        leftover = leftover[:start] + leftover[end:]
    leftover = re.sub(r'[\s,]+', ' ', leftover).strip() or None

    result = []
    for i, (num, starred, _, _) in enumerate(matches):
        d = leftover if i == len(matches) - 1 else None
        result.append((num, d, starred))
    return result


def _leftover(text, start, end):
    """Return text with the span [start, end] removed, cleaned up."""
    s = (text[:start] + text[end:]).strip(' ,')
    return re.sub(r'[\s,]+', ' ', s).strip() or None
