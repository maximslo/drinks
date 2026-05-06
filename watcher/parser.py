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
    Returns list of (number, details, starred) tuples, or [] if no drink numbers found.

    Handles:
      "5593"                    → [(5593, None, False)]
      "5593*"                   → [(5593, None, True)]
      "5593, modelo"            → [(5593, "modelo", False)]
      "5649-5647"               → [(5649,None,F), (5648,None,F), (5647,None,F)]
      "5649 + 5648"             → [(5649,None,F), (5648,None,F)]
      "5649, 5648, On our way"  → [(5649,None,F), (5648,"On our way",F)]
      "5649 5648 Shooters"      → [(5649,None,F), (5648,"Shooters",F)]
    """
    if not text:
        return []
    text = text.strip()

    # Range: 5649-5647 (digits, dash, digits — nothing else)
    m = re.match(r'^(\d+)\s*-\s*(\d+)\s*$', text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        hi, lo = max(a, b), min(a, b)
        if hi >= MIN_DRINK_NUMBER:
            return [(n, None, False) for n in range(hi, lo - 1, -1)]
        return []

    # Plus notation: 5649 + 5648 + 5647 (only digits and +)
    if re.match(r'^\d+(\s*\+\s*\d+)+\s*$', text):
        nums = [int(n) for n in re.findall(r'\d+', text)]
        if max(nums) >= MIN_DRINK_NUMBER:
            return [(n, None, False) for n in sorted(nums, reverse=True)]
        return []

    # General: tokenize on comma/whitespace, collect leading numbers then details
    tokens = [t for t in re.split(r'[\s,]+', text) if t]
    numbers = []
    detail_tokens = []
    in_details = False

    for token in tokens:
        if not in_details:
            m = re.match(r'^(\d+)(\*?)$', token)
            if m and int(m.group(1)) >= MIN_DRINK_NUMBER:
                numbers.append((int(m.group(1)), m.group(2) == '*'))
            else:
                in_details = True
                detail_tokens.append(token)
        else:
            detail_tokens.append(token)

    if not numbers:
        return []

    details = ' '.join(detail_tokens).strip() or None
    result = []
    for i, (num, starred) in enumerate(numbers):
        d = details if i == len(numbers) - 1 else None
        result.append((num, d, starred))
    return result
