import logging
import requests
from core import config

logger = logging.getLogger("DiscordNotifier")

def send_discord_webhook(payload: dict) -> bool:
    """
    Sends a JSON payload to the configured DISCORD_WEBHOOK_URL.
    """
    webhook_url = config.DISCORD_WEBHOOK_URL
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL is not set. Skipping Discord notification.")
        return False
        
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code in (200, 204):
            logger.info("Successfully sent Discord webhook.")
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
