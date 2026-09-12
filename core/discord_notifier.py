import logging
import requests
import time
from core import config

logger = logging.getLogger("DiscordNotifier")

# Deduplication and cooldown variables
last_message = None
last_message_time = None
COOLDOWN_SECONDS = 30

def send_discord_webhook(payload: dict) -> bool:
    """
    Sends a JSON payload to the configured DISCORD_WEBHOOK_URL.
    Implements deduplication and cooldown to prevent duplicate notifications.
    """
    global last_message, last_message_time
    
    # Skip if the same message was sent within the cooldown period
    current_time = time.time()
    if last_message == payload and last_message_time and (current_time - last_message_time) < COOLDOWN_SECONDS:
        logger.info("Skipping duplicate Discord notification due to cooldown.")
        return False
    
    webhook_url = config.DISCORD_WEBHOOK_URL
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL is not set. Skipping Discord notification.")
        return False
        
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code in (200, 204):
            logger.info("Successfully sent Discord webhook.")
            last_message = payload
            last_message_time = current_time
            return True
        else:
            logger.error(f"Failed to send Discord webhook. Status code: {response.status_code}, Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error sending Discord webhook: {e}")
        return False

def send_discord_message(content: str) -> bool:
    """Sends a simple text message to Discord."""
    return send_discord_webhook({"content": content})

def send_discord_embed(embed: dict) -> bool:
    """Sends a rich embed message to Discord."""
    return send_discord_webhook({"embeds": [embed]})

def send_discord_embeds(embeds: list[dict]) -> bool:
    """Sends multiple rich embed messages to Discord."""
    return send_discord_webhook({"embeds": embeds})


def send_ticker_roster_summary(roster: dict) -> bool:
    """Send a Discord summary of a ticker roster (promote/relegate/watch).

    ``roster`` is the dict from ``tools.ticker_promotion_relegation.build_roster``
    (keys: tiers, to_add, to_remove, stats). Builds a compact message.
    """
    tiers = roster.get("tiers", {})
    lines = ["📊 **Ticker Roster**"]
    to_add = roster.get("to_add", [])
    to_remove = roster.get("to_remove", [])
    watch = tiers.get("WATCH", [])

    if to_add:
        lines.append(f"🔺 **Promoted:** {', '.join(to_add)}")
    if to_remove:
        lines.append(f"🔻 **Relegated:** {', '.join(to_remove)}")
    if watch:
        lines.append(f"⚠️ **Watch:** {', '.join(watch)}")
    if not (to_add or to_remove or watch):
        lines.append("No roster changes.")

    return send_discord_message("\n".join(lines))
