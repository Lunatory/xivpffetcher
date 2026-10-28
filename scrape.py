
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

LISTINGS_URL = "https://xivpf.com/listings"
DUTY = os.getenv("XIVPF_DUTY", "Dancing Mad").casefold()
CENTRE = os.getenv("XIVPF_CENTRE", "Aether").casefold()
MIN_SLOTS = int(os.getenv("XIVPF_MIN_SLOTS", "6"))
MAX_ILVL = 790

# Job categories and party-state colors.
TANKS = {"PLD", "WAR", "DRK", "GNB"}
HEALERS = {"WHM", "SCH", "AST", "SGE"}
MELEE = {"MNK", "DRG", "NIN", "SAM", "RPR", "VPR"}
RANGED = {"BRD", "MCH", "DNC"}
CASTERS = {"BLM", "SMN", "RDM", "PCT", "BLU"}

JOB_CODES = (
    TANKS | HEALERS | MELEE | RANGED | CASTERS
    | {
        "GLA", "PGL", "MRD", "LNC", "ARC", "CNJ",
        "THM", "ACN", "ROG",
    }
)

ROLE_ORDER = {
    **{job: 0 for job in TANKS},
    **{job: 1 for job in HEALERS},
    **{job: 2 for job in MELEE},
    **{job: 3 for job in CASTERS},
    **{job: 4 for job in RANGED},
}

# Circle = filled slot; square = open slot.
ROLE_SYMBOLS = {
    "tank": ("🟦", "🟦"),
    "healer": ("🟩", "🟩"),
    "melee": ("🟥", "🟥"),
    "caster": ("🟪", "🟪"),
    "ranged": ("🟠", "🟠"),
}

# The requested symbols are visually identical in color for filled
# and open slots, so use the symbol shape to distinguish them.
FILLED_SYMBOLS = {
    "tank": "🔵",
    "healer": "🟢",
    "melee": "🔴",
    "caster": "🟣",
    "ranged": "🟠",
}

OPEN_SYMBOLS = {
    "tank": "🟦",
    "healer": "🟩",
    "melee": "🟥",
    "caster": "🟪",
    "ranged": "🟧",
}


def text(node):
    return " ".join(node.stripped_strings) if node else ""


def parse_relative_timestamp(value, now=None):
    """Convert XIVPF relative-time text to an approximate Unix timestamp."""
    if now is None:
        now = time.time()

    value = (value or "").strip().casefold()

    if not value:
        return None

    if value in {"now", "just now", "a moment ago"}:
        return int(now)

    match = re.search(
        r"\b(\d+|a|an)\s+"
        r"(seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b",
        value,
    )

    if not match:
        return None

    amount_text, unit_text = match.groups()
    amount = 1 if amount_text in {"a", "an"} else int(amount_text)

    unit = unit_text.rstrip("s")
    seconds_per_unit = {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 86400,
        "week": 604800,
        "month": 2592000,
        "year": 31536000,
    }

    delta = amount * seconds_per_unit[unit]

    if re.search(r"\bin\b", value):
        return int(now + delta)

    return int(now - delta)


def discord_relative_timestamp(timestamp):
    """Discord localizes relative timestamps for each viewer."""
    if timestamp is None:
        return "Unknown"

    return f"<t:{int(timestamp)}:R>"


def job_category(job):
    """Return the display category for a job abbreviation."""
    if job in TANKS:
        return "tank"
    if job in HEALERS:
        return "healer"
    if job in MELEE:
        return "melee"
    if job in CASTERS:
        return "caster"
    if job in RANGED:
        return "ranged"
    return None


def extract_job_codes(value):
    """Extract recognized job codes from titles or icon identifiers."""
    value = (value or "").upper()
    candidates = re.findall(r"[A-Z]{3}", value)
    return list(dict.fromkeys(
        code for code in candidates if code in JOB_CODES
    ))


def slot_job_codes(slot):
    """Find job codes exposed by a slot's title or SVG icon."""
    values = [
        slot.get("title", ""),
        slot.get("aria-label", ""),
        slot.get("data-job", ""),
        slot.get("data-jobs", ""),
    ]

    for icon in slot.select("use[href], use[xlink\\:href]"):
        values.append(icon.get("href", ""))
        values.append(icon.get("xlink:href", ""))

    jobs = []
    for value in values:
        jobs.extend(extract_job_codes(value))

    return list(dict.fromkeys(jobs))


def get_filled_jobs(slots):
    """Read job abbreviations from occupied slots only."""
    jobs = []

    for slot in slots:
        if "filled" not in slot.get("class", []):
            continue

        codes = slot_job_codes(slot)
        if codes:
            # Occupied slots should represent one current job.
            jobs.append(codes[0])

    # Tanks, healers, melee, casters, then physical ranged.
    return sorted(
        jobs,
        key=lambda job: (ROLE_ORDER.get(job, 99), job),
    )


def get_party_state(slots):
    """
    Render one symbol per party slot in XIVPF's slot order.

    Filled slots use colored circles. Open slots use colored squares.
    If an open slot can accept jobs from multiple color categories,
    show a black square instead.
    """
    state = []

    for slot in slots:
        codes = slot_job_codes(slot)
        filled = "filled" in slot.get("class", [])

        if filled:
            job = next(
                (code for code in codes if job_category(code)),
                None,
            )
            category = job_category(job) if job else None

            if category:
                state.append(FILLED_SYMBOLS[category])
            else:
                # A filled slot whose job cannot be identified.
                state.append("⚫")

            continue

        # Open slot: infer which role categories it accepts.
        categories = {
            job_category(code)
            for code in codes
            if job_category(code)
        }

        if len(categories) == 1:
            category = next(iter(categories))
            state.append(OPEN_SYMBOLS[category])
        elif len(categories) > 1:
            # Multiple possible colors/categories.
            state.append("⬛")
        else:
            # No readable job options in the source markup.
            state.append("⬛")

    return " ".join(state) if state else "Unknown"


def is_private_listing(card):
    """Exclude listings explicitly marked private in their markup."""
    nodes = [
        card,
        *card.select(
            "[data-private], [data-is-private], [class], [title], "
            "[aria-label]"
        ),
    ]

    for node in nodes:
        classes = node.get("class", [])

        if any("private" in str(c).casefold() for c in classes):
            return True

        for attribute in ("data-private", "data-is-private"):
            if str(node.get(attribute, "")).casefold() in {
                "true", "1", "yes"
            }:
                return True

        if "private" in str(node.get("title", "")).casefold():
            return True

        if "private" in str(node.get("aria-label", "")).casefold():
            return True

    return False


def scrape():
    response = requests.get(
        LISTINGS_URL,
        headers={"User-Agent": "XIVPF-Discord-Feed/1.0"},
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(".listing")

    # Do not overwrite the feed if XIVPF returns unexpected HTML.
    if not cards:
        raise RuntimeError(
            "No listing cards found. Check the site response or HTML selectors."
        )

    results = []
    scrape_time = time.time()

    for card in cards:
        if card.get("data-centre", "").casefold() != CENTRE:
            continue

        duty_name = text(card.select_one(".duty"))
        if DUTY not in duty_name.casefold():
            continue

        slots = card.select(".party .slot")
        if len(slots) < MIN_SLOTS:
            continue

        open_slots = [
            slot for slot in slots
            if "filled" not in slot.get("class", [])
        ]

        # Only actively recruiting parties.
        if not open_slots:
            continue

        if is_private_listing(card):
            continue

        # Filter minimum item level when set. Missing values are allowed.
        min_ilvl = text(card.select_one(".middle .stat .value"))
        ilvl_digits = "".join(c for c in min_ilvl if c.isdigit())

        if ilvl_digits and int(ilvl_digits) > MAX_ILVL:
            continue

        creator = text(card.select_one(".creator .text")) or "Unknown"
        description = (
            text(card.select_one(".description")) or "No description"
        )

        # XIVPF exposes relative update/expiry text, not creation timestamps.
        updated_text = text(card.select_one(".updated .text"))
        expires_text = text(card.select_one(".expires .text"))

        updated_timestamp = parse_relative_timestamp(
            updated_text, now=scrape_time
        )
        expires_timestamp = parse_relative_timestamp(
            expires_text, now=scrape_time
        )

        results.append({
            "creator": creator,
            "description": description,
            "party_jobs": get_filled_jobs(slots),
            "party_state": get_party_state(slots),
            "total": len(slots),
            "open": len(open_slots),
            "updated_timestamp": updated_timestamp,
            "expires_timestamp": expires_timestamp,
        })

    return results


def discord_embed(listings):
    embed = {
        "title": "Dancing Mad — Aether Party Finder",
        "color": 0x8B0000,
        "footer": {
            "text": "XIVPF feed • refreshed every 5 minutes"
        },
    }

    if not listings:
        embed["description"] = (
            "No matching public, actively recruiting listings right now.\n\n"
            "Filters: Aether • Dancing Mad • at least 6 total slots • "
            "at least 1 open slot • minimum iLvl 790 or lower, if set."
        )
        return embed

    embed["description"] = (
        f"Currently matching: **{len(listings)}** listing(s)."
    )

    fields = []
    total_chars = len(embed["title"]) + len(embed["description"]) + 100
    omitted = 0

    for listing in listings:
        name = (
            f'{listing["creator"]} — '
            f'{listing["open"]} open / {listing["total"]} slots'
        )[:256]

        jobs = ", ".join(listing["party_jobs"]) or "None listed"
        party_state = listing["party_state"]
        updated = discord_relative_timestamp(listing["updated_timestamp"])
        expires = discord_relative_timestamp(listing["expires_timestamp"])

        value = (
            f"**Party jobs:** {jobs}\n"
            f"**Party state:** {party_state}\n"
            f"**Description:** {listing['description']}\n"
            f"**Updated:** {updated}\n"
            f"**Expires:** {expires}"
        )[:1024]

        cost = len(name) + len(value)

        if len(fields) >= 25 or total_chars + cost > 5700:
            omitted += 1
            continue

        fields.append({
            "name": name,
            "value": value,
            "inline": False,
        })
        total_chars += cost

    embed["fields"] = fields

    if omitted:
        embed["description"] += (
            f"\n\n**Warning:** {omitted} additional matches could not fit "
            "in this embed."
        )

    return embed


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    message_id = os.environ.get("DISCORD_MESSAGE_ID", "").strip()

    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing.")

    listings = scrape()
    embed = discord_embed(listings)

    if message_id:
        # Edit the existing message sent by this webhook.
        url = webhook.rstrip("/") + "/messages/" + message_id

        response = requests.patch(
            url,
            json={"embeds": [embed], "content": ""},
            timeout=30,
        )
        response.raise_for_status()

        print(f"Updated message {message_id}; matches: {len(listings)}")
    else:
        # Create the first message and print its ID for configuration.
        response = requests.post(
            webhook + "?wait=true",
            json={"embeds": [embed]},
            timeout=30,
        )
        response.raise_for_status()

        message = response.json()
        new_id = str(message.get("id", ""))

        if not new_id:
            raise RuntimeError(
                "Discord did not return the created message ID."
            )

        print(f"CREATED_MESSAGE_ID={new_id}")
        print(
            "Save this ID as the DISCORD_MESSAGE_ID repository variable."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
