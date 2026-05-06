import os
import json
import anthropic
from parser import PHONE_TO_NAME

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def format_messages_for_prompt(messages):
    lines = []
    for msg in messages:
        rowid, handle_id, text, date, has_attachment = msg
        name = PHONE_TO_NAME.get(handle_id, handle_id or "Unknown")
        parts = []
        if has_attachment:
            parts.append("[photo]")
        if text:
            parts.append(text)
        if parts:
            lines.append(f"{name}: {' '.join(parts)}")
    return "\n".join(lines)

def parse_ambiguous(flagged_messages, reason, context_before=None, context_after=None):
    """
    Send ambiguous messages to Claude with surrounding context to extract drink logs.
    Returns list of {person, drink_number, details} or empty list.
    """
    before_text = format_messages_for_prompt(context_before or [])
    flagged_text = format_messages_for_prompt(flagged_messages)
    after_text = format_messages_for_prompt(context_after or [])

    context_block = ""
    if before_text:
        context_block += f"--- Context before ---\n{before_text}\n\n"
    context_block += f"--- Flagged messages ({reason}) ---\n{flagged_text}\n"
    if after_text:
        context_block += f"\n--- Context after ---\n{after_text}"

    prompt = f"""You are parsing messages from a group iMessage chat called "One beer at a time" where 12 friends are counting drinks toward a goal of 10,000. Each drink is logged with a photo and a number.

Members: Hunter, Lucas, Liam, Joseph, Kacper, Miggy, Marek, Owen, Maxim, Jacob, Avi, Cole.

{context_block}

Rules:
- The person who sent the PHOTO is the one who drank, regardless of who typed the number
- If no photo, the person who typed the number drank
- Numbers like "5588 5587" mean two drinks by the same person (list each separately)
- Ranges like "5588-5585" mean drinks 5588, 5587, 5586, 5585 by the photo sender (list each separately)
- "9 drinks someone do the math" means use the context before/after to find the last known drink number, then assign the next 9 sequential numbers to the photo sender
- Corrections: if someone sends "5583" then "5593*", use 5593 only
- details should only be drink type (e.g. "modelo", "IPA"), never an explanation
- Ignore reactions, normal conversation, jokes
- If genuinely impossible to determine, return []

Return ONLY a JSON array, no explanation, no markdown:
[{{"person": "Name", "drink_number": 1234, "details": null}}]

If nothing can be determined: []"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        results = json.loads(raw)

        valid = []
        for r in results:
            if isinstance(r.get("drink_number"), int) and isinstance(r.get("person"), str):
                valid.append(r)

        return valid

    except Exception as e:
        print(f"  AI parser error: {e}")
        return []
