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

MAX_MESSAGES = 4
MAX_LISTINGS_PER_MESSAGE = 25
MAX_TOTAL_LISTINGS = 100
MAX_EMBED_CHARS = 5500

JOB_CODES = {
    "GLA", "PGL", "MRD", "LNC", "ARC", "CNJ", "THM", "ACN",
    "PLD", "MNK", "WAR", "DRG", "BRD", "WHM", "BLM", "SMN",
    "SCH", "ROG", "NIN", "MCH", "DRK", "AST", "SAM", "RDM",
    "BLU", "GNB", "DNC", "RPR", "SGE", "VPR", "PCT",
}

job_roles = {
    "PLD": "🟦", "WAR": "🟦", "DRK": "🟦", "GNB": "🟦",
    "WHM": "🟩", "SCH": "🟩", "AST": "🟩", "SGE": "🟩",
    "MNK": "🟥", "DRG": "🟥", "NIN": "🟥", "SAM": "🟥",
    "RPR": "🟥", "VPR": "🟥", "BRD": "🟥", "MCH": "🟥",
    "DNC": "🟥", "BLM": "🟥", "SMN": "🟥", "RDM": "🟥",
    "PCT": "🟥", "BLU": "🟥",
}

role_order = {
    "PLD": 0, "WAR": 0, "DRK": 0, "GNB": 0,
    "WHM": 1, "SCH": 1, "AST": 1, "SGE": 1,
    "MNK": 2, "DRG": 2, "NIN": 2, "SAM": 2,
    "RPR": 2, "VPR": 2, "BRD": 2, "MCH": 2,
    "DNC": 2, "BLM": 2, "SMN": 2, "RDM": 2,
    "PCT": 2, "BLU": 2,
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
    """Discord's relative timestamp is localized for each viewer."""
    if timestamp is None:
        return "Unknown"

    return f"<t:{int(timestamp)}:R>"


def get_filled_jobs(slots):
    """Read job abbreviations from occupied party slots only."""
    jobs = []

    for slot in slots:
        if "filled" not in slot.get("class", []):
            continue

        job = (slot.get("title") or "").strip().upper()

        # Fallback to the icon identifier if the title is unavailable.
        if not job:
            icon = slot.select_one("use[href], use[xlink\\:href]")
            if icon:
                href = icon.get("href") or icon.get("xlink:href") or ""
                job = href.rsplit("#", 1)[-1].upper()

        # Keep recognized job codes only.
        for code in job.split():
            if code in JOB_CODES:
                jobs.append(code)
                break

    return jobs


def is_private_listing(card):
    """Exclude listings explicitly marked private in their markup."""
    nodes = [
        card,
        *card.select(
            "[data-private], [data-is-private], [class], [title], [aria-label]"
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

        # XIVPF provides relative "updated" and "expires" values.
        # It does not expose an actual creation timestamp in the sample markup.
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
            "total": len(slots),
            "open": len(open_slots),
            "updated_timestamp": updated_timestamp,
            "expires_timestamp": expires_timestamp,
        })

    return results, int(scrape_time)


def format_listing_field(listing):
    """Format one listing as a Discord embed field."""
    if listing["total"] == 8:
        name = f'{listing["creator"]} — {listing["open"]} open'
    else:
        name = (
            f'{listing["creator"]} — '
            f'{listing["open"]} open / {listing["total"]} slots'
        )

    name = name[:256]

    sorted_jobs = sorted(
        listing["party_jobs"],
        key=lambda j: role_order.get(j, 3)
    )

    jobs = " ".join(
        f'{job_roles.get(j, "⬜")} {j}'
        for j in sorted_jobs
    ) or "None listed"

    updated = discord_relative_timestamp(listing["updated_timestamp"])
    expires = discord_relative_timestamp(listing["expires_timestamp"])

    value = (
        f"**Party jobs:** {jobs}\n"
        f"**Description:** {listing['description']}\n"
        f"**Updated:** {updated} | **Expires:** {expires}"
    )[:1024]

    return {
        "name": name,
        "value": value,
        "inline": False,
    }


def split_listings(listings):
    """
    Split listings across messages, respecting both Discord's
    25-field limit and the combined embed character budget.
    """
    chunks = []
    current_chunk = []
    current_chars = 500  # Reserve space for title, description and metadata.

    for listing in listings:
        field = format_listing_field(listing)
        field_chars = len(field["name"]) + len(field["value"])

        if current_chunk and (
            len(current_chunk) >= MAX_LISTINGS_PER_MESSAGE
            or current_chars + field_chars > MAX_EMBED_CHARS
        ):
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 500

        current_chunk.append(field)
        current_chars += field_chars

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def discord_embed(fields, total_count, start_number, overflow_count=0,
                  no_matches=False):
    """Create a listing embed without a timestamp."""
    title = "Dancing Mad — Aether Party Finder"

    if no_matches:
        description = (
            "No matching public, actively recruiting listings right now.\n\n"
            f"Filters: {CENTRE.title()} • {DUTY.title()} • "
            f"at least {MIN_SLOTS} total slots • at least 1 open slot • "
            f"maximum iLvl {MAX_ILVL} (missing values allowed)."
        )
    elif not fields:
        description = "No further listings."
    else:
        end_number = start_number + len(fields) - 1
        description = (
            f"Currently matching: **{total_count}** listing(s).\n"
            f"Showing listings **{start_number}–{end_number}**."
        )

    if overflow_count:
        description += (
            f"\n\n**Warning:** {overflow_count} additional listings "
            "could not fit within the four-message display limit."
        )

    return {
        "title": title,
        "color": 0x8B0000,
        "description": description,
        "fields": fields,
        "footer": {
            "text": "XIVPF feed"
        },
    }


def discord_timestamp_embed(last_updated):
    """Create a separate embed for the dynamically updating timestamp."""
    return {
        "color": 0x8B0000,
        "description": (
            f"Last updated: <t:{int(last_updated)}:R>"
        ),
    }


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    # Use the new multi-message variable.
    message_ids_text = os.environ.get(
        "DISCORD_MESSAGE_IDS", ""
    ).strip()

    # Migrate automatically from the old single-message variable.
    if not message_ids_text:
        message_ids_text = os.environ.get(
            "DISCORD_MESSAGE_ID", ""
        ).strip()

    message_ids = [
        value.strip()
        for value in message_ids_text.split(",")
        if value.strip()
    ]

    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing.")

    if len(message_ids) > MAX_MESSAGES:
        raise RuntimeError(
            f"Expected at most {MAX_MESSAGES} message IDs, "
            f"received {len(message_ids)}."
        )

    listings, last_updated = scrape()
    total_count = len(listings)

    # Limit the feed to 100 listings.
    displayed = listings[:MAX_TOTAL_LISTINGS]

    # Split according to Discord's field and character limits.
    chunks = split_listings(displayed)

    # Only four messages can be maintained.
    visible_chunks = chunks[:MAX_MESSAGES]

    displayed_count = sum(len(chunk) for chunk in visible_chunks)
    overflow_count = total_count - displayed_count

    # Bottom-justify the listings.
    # Unused messages above the listings will say "No further listings".
    slots = [None] * (MAX_MESSAGES - len(visible_chunks))
    slots.extend(visible_chunks)

    for index, fields in enumerate(slots):

        is_bottom_message = index == MAX_MESSAGES - 1
        no_matches = total_count == 0 and is_bottom_message

        if fields is None:
            fields = []

        # Calculate the starting listing number for this message.
        start_number = 1 + sum(
            len(chunk)
            for chunk in slots[:index]
            if chunk is not None
        )

        message_overflow = (
            overflow_count if is_bottom_message else 0
        )

        embed = discord_embed(
            fields=fields,
            total_count=total_count,
            start_number=start_number,
            overflow_count=message_overflow,
            no_matches=no_matches,
        )

        # Only the bottom message receives the timestamp embed.
        embeds = [embed]

        if is_bottom_message:
            embeds.append(discord_timestamp_embed(last_updated))

        if index < len(message_ids):
            # Edit an existing message.
            message_id = message_ids[index]

            url = (
                webhook.rstrip("/")
                + "/messages/"
                + message_id
            )

            response = requests.patch(
                url,
                json={
                    "embeds": embeds,
                    "content": "",
                },
                timeout=30,
            )

            response.raise_for_status()

            print(
                f"Updated message {index + 1}: {message_id}"
            )

        else:
            # Create a new message.
            response = requests.post(
                webhook + "?wait=true",
                json={"embeds": embeds},
                timeout=30,
            )

            response.raise_for_status()

            message = response.json()
            new_id = str(message.get("id", ""))

            if not new_id:
                raise RuntimeError(
                    "Discord did not return the created message ID."
                )

            message_ids.append(new_id)

            print(
                f"Created message {index + 1}: {new_id}"
            )

            # Print the updated IDs immediately in case a later
            # request fails before all messages have been processed.
            print(
                "CURRENT_DISCORD_MESSAGE_IDS="
                + ",".join(message_ids)
            )

    print(f"Total matching listings: {total_count}")
    print(f"Displayed listings: {displayed_count}")
    print(f"Messages maintained: {len(message_ids)}")

    if overflow_count:
        print(
            f"WARNING: {overflow_count} listings were not displayed "
            "because the four-message capacity was reached."
        )

    print(
        "DISCORD_MESSAGE_IDS=" + ",".join(message_ids)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
