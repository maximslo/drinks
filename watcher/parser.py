import re
import os


# ----------------------------
# Load name mappings from ENV
# ----------------------------

def load_phone_to_name():
    """
    Loads PHONE_NAMES from env:
    +1234567890:Name,+1234567891:Name2
    """
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
    """
    Optional: supports EMAIL_NAMES env:
    email:name,email2:name2
    """
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


# ----------------------------
# Resolver
# ----------------------------

def resolve_name(handle_id):
    return PHONE_TO_NAME.get(handle_id, handle_id)


# ----------------------------
# Drink parser
# ----------------------------

def parse_drink_text(text):
    """
    Parses drink number and optional details from message text.
    Examples:
      "5140"         -> (5140, None)
      "5030, modelo" -> (5030, "modelo")
      "5593*"        -> (5593, None)
    """
    if not text:
        return None, None

    text = text.strip()

    match = re.match(r'^(\d+)\*?(?:[,\s]+(.+))?$', text)
    if match:
        number = int(match.group(1))
        details = match.group(2).strip() if match.group(2) else None
        return number, details

    return None, None