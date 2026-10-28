
import os
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

LISTINGS_URL = "https://xivpf.com/listings"
DUTY = os.getenv("XIVPF_DUTY", "Dancing Mad").casefold()
CENTRE = os.getenv("XIVPF_CENTRE", "Aether").casefold()
MIN_SLOTS = int(os.getenv("XIVPF_MIN_SLOTS", "6"))


def text(node):
    return " ".join(node.stripped_strings) if node else ""


def scrape():
    response = requests.get(
        LISTINGS_URL,
        headers={"User-Agent": "XIVPF-Discord-Feed/1.0"},
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select(".listing")

    # Never clear the feed if the site returns an unexpected page.
    if not cards:
        raise RuntimeError(
            "No listing cards found. Check the site response or HTML selectors."
        )

    results = []

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

        # Recruiting listings need at least one open slot.
        if not open_slots:
            continue

        # Explicit privacy indicators are excluded. XIVPF's public listings
        # page normally omits private listings in the first place.
        private = False
        for node in [card, *card.select(
            "[data-private], [data-is-private], [class], [title], [aria-label]"
        )]:
            classes = node.get("class", [])
            if any("private" in str(c).casefold() for c in classes):
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

        creator = text(card.select_one(".creator .text")) or "Unknown"
        world = text(card.select_one(".world .text")) or "Unknown"
        description = text(card.select_one(".description")) or "No description"
        min_ilvl = text(card.select_one(".middle .stat .value")) or "Unknown"
        updated = text(card.select_one(".updated .text")) or "Unknown"
        expires = text(card.select_one(".expires .text")) or "Unknown"

        # Use the source listing ID when available. The public page does not
        # necessarily provide an individual clickable URL for every listing.
        listing_id = card.get("data-id")
        url = LISTINGS_URL
        if listing_id:
            url = f"{LISTINGS_URL}#listing-{listing_id}"

        results.append({
            "duty": duty_name,
            "creator": creator,
            "world": world,
            "description": description,
            "total": len(slots),
            "open": len(open_slots),
            "min_ilvl": min_ilvl,
            "updated": updated,
            "expires": expires,
            "url": url,
        })

    return results


def discord_embed(listings):
    now = datetime.now(timezone.utc).isoformat()

    embed = {
        "title": "Dancing Mad — Aether Party Finder",
        "url": LISTINGS_URL,
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
            "at least 1 open slot."
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

        value = (
            f'**World:** {listing["world"]}\n'
            f'**Minimum item level:** {listing["min_ilvl"]}\n'
            f'**Description:** {listing["description"]}\n'
            f'**Updated:** {listing["updated"]} · '
            f'**Expires:** {listing["expires"]}\n'
            f'[View XIVPF listings]({listing["url"]})'
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
        # Edit the existing webhook message.
        url = webhook.rstrip("/") + "/messages/" + message_id
        response = requests.patch(
            url,
            json={"embeds": [embed], "content": ""},
            timeout=30,
        )
        response.raise_for_status()
        print(f"Updated message {message_id}; matches: {len(listings)}")
    else:
        # First run creates the message and prints its ID for setup.
        response = requests.post(
            webhook + "?wait=true",
            json={"embeds": [embed]},
            timeout=30,
        )
        response.raise_for_status()

        message = response.json()
        new_id = str(message.get("id", ""))
        if not new_id:
            raise RuntimeError("Discord did not return the created message ID.")

        print(f"CREATED_MESSAGE_ID={new_id}")
        print("Save this ID as the DISCORD_MESSAGE_ID repository variable.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
