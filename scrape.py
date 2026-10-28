
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

LISTINGS_URL = "https://xivpf.com/listings"
DUTY = os.getenv("XIVPF_DUTY", "Dancing Mad").casefold()
CENTRE = os.getenv("XIVPF_CENTRE", "Aether").casefold()
MIN_SLOTS = int(os.getenv("XIVPF_MIN_SLOTS", "6"))
MAX_ILVL = 790


def text(node):
    return " ".join(node.stripped_strings) if node else ""


def parse_relative_timestamp(value, now=None, default_to_now=False):
    """Convert XIVPF relative time text into an approximate Unix timestamp."""
    if now is None:
        now = time.time()

    value = (value or "").strip().casefold()

    if not value:
        return int(now) if default_to_now else None

    if value == "now":
        return int(now)

    if value in {"just now", "a moment ago"}:
        return int(now)

    match = re.search(
        r"(?:in\s+)?(\d+)\s+"
        r"(second|minute|hour|day|week|month|year)s?",
        value,
    )

    if not match:
        # Handle phrases such as "about a minute ago".
        match = re.search(
            r"(?:about\s+)?(?:in\s+)?a\s+"
            r"(second|minute|hour|day|week|month|year)",
            value,
        )
        if not match:
            return int(now) if default_to_now else None

        amount = 1
        unit = match.group(1)
    else:
        amount = int(match.group(1))
        unit = match.group(2)

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

    if value.startswith("in "):
        return int(now + delta)

    # "12 seconds ago" or similar.
    return int(now - delta)


def discord_timestamp(timestamp):
    """Discord renders this timestamp in each viewer's local time zone."""
    if timestamp is None:
        return "Unknown"

    return f"<t:{int(timestamp)}:f>"


def scrape():
    response = requests.get(
        LISTINGS_URL,
        headers={"User-Agent": "XIVPF-Discord-Feed/1.0"},
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(".listing")

    # Fail safely if the site returns unexpected HTML.
    if not cards:
        raise RuntimeError(
            "No listing cards found. Check the site response or HTML selectors."
        )

    results = []
    scrape_time = time.time()

    for card in cards:
        # Data centre filter
        if card.get("data-centre", "").casefold() != CENTRE:
            continue

        # Duty filter
        duty_name = text(card.select_one(".duty"))
        if DUTY not in duty_name.casefold():
            continue

        # Require at least MIN_SLOTS total party slots
        slots = card.select(".party .slot")
        if len(slots) < MIN_SLOTS:
            continue

        # A slot without the "filled" class is an open slot.
        open_slots = [
            slot
            for slot in slots
            if "filled" not in slot.get("class", [])
        ]

        # Only actively recruiting listings
        if not open_slots:
            continue

        # Exclude listings explicitly marked as private.
        # The public XIVPF listings page normally omits private listings.
        private = False
        for node in [
            card,
            *card.select(
                "[data-private], [data-is-private], [class], [title], [aria-label]"
            ),
        ]:
            classes = node.get("class", [])

            if any(
                "private" in str(class_name).casefold()
                for class_name in classes
            ):
                private = True

            if str(node.get("data-private", "")).casefold() in {
                "true", "1", "yes"
            }:
                private = True

            if str(node.get("data-is-private", "")).casefold() in {
                "true", "1", "yes"
            }:
                private = True

            if "private" in str(node.get("title", "")).casefold():
                private = True

            if "private" in str(node.get("aria-label", "")).casefold():
                private = True

        if private:
            continue

        # Filter by minimum item level.
        # Missing or unset item levels are allowed.
        min_ilvl = text(card.select_one(".middle .stat .value"))
        ilvl_digits = "".join(c for c in min_ilvl if c.isdigit())

        if ilvl_digits and int(ilvl_digits) > MAX_ILVL:
            continue

        # Only filled slots have a specific job abbreviation in their title.
        # Empty slots list possible jobs, not a job currently in the party.
        party_jobs = []

        for slot in slots:
            if "filled" not in slot.get("class", []):
                continue

            job = (slot.get("title") or "").strip().upper()

            # Fallback to the SVG icon reference, if available.
            if not job:
                icon = slot.select_one("use[href], use[xlink:href]")
                if icon:
                    href = (
                        icon.get("href")
                        or icon.get("xlink:href")
                        or ""
                    )
                    job = href.rsplit("#", 1)[-1].upper()

            if job:
                party_jobs.append(job)

        creator = text(card.select_one(".creator .text")) or "Unknown"
        description = (
            text(card.select_one(".description")) or "No description"
        )

        updated_text = text(card.select_one(".updated .text"))
        expires_text = text(card.select_one(".expires .text"))

        updated_timestamp = parse_relative_timestamp(
            updated_text,
            now=scrape_time,
            default_to_now=True,
        )
        expires_timestamp = parse_relative_timestamp(
            expires_text,
            now=scrape_time,
            default_to_now=False,
        )

        results.append({
            "duty": duty_name,
            "creator": creator,
            "description": description,
            "party_jobs": party_jobs,
            "total": len(slots),
            "open": len(open_slots),
            "updated_timestamp": updated_timestamp,
            "expires_timestamp": expires_timestamp,
        })

    return results


def discord_embed(listings):
    now = datetime.now(timezone.utc).isoformat()

    embed = {
        "title": "Dancing Mad — Aether Party Finder",
        "color": 0x8B0000,
        "timestamp": now,
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

        updated = discord_timestamp(listing["updated_timestamp"])
        expires = discord_timestamp(listing["expires_timestamp"])

        value = (
            f'**Party jobs:** {jobs}\n'
            f'**Description:** {listing["description"]}\n'
            f'**Updated:** {updated}\n'
            f'**Expires:** {expires}'
        )[:1024]

        cost = len(name) + len(value)

        # Discord allows up to 25 fields and 6000 combined embed characters.
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
        # Edit the existing webhook message.
        url = webhook.rstrip("/") + "/messages/" + message_id

        response = requests.patch(
            url,
            json={
                "embeds": [embed],
                "content": "",
            },
            timeout=30,
        )
        response.raise_for_status()

        print(f"Updated message {message_id}; matches: {len(listings)}")

    else:
        # First run: create the message and print its ID for setup.
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
