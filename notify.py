"""Slack notification functions."""

import logging
import traceback

from slack_sdk import WebClient

log = logging.getLogger("feeds")


def send_link_to_slack(config, today):
    """Send just the link to Slack #journal-feed."""
    slack_cfg = config["slack"]
    client = WebClient(token=slack_cfg["bot_token"])
    channel = slack_cfg["channel"]
    base_url = config.get("deploy", {}).get("base_url", "https://example.com/feeds")
    url = f"{base_url}/{today}.html"

    client.chat_postMessage(
        channel=channel,
        text=f"*Feeds — {today}*\n{url}",
        unfurl_links=False,
        unfurl_media=False,
    )
    log.info(f"  Sent link to {channel}")


def send_error_to_slack(config, error_msg):
    """Send error notification to Slack #log channel."""
    try:
        slack_cfg = config["slack"]
        client = WebClient(token=slack_cfg["bot_token"])
        log_channel = slack_cfg.get("log_channel", "#log")
        client.chat_postMessage(
            channel=log_channel,
            text=f"*[feeds] Error*\n```{error_msg}```",
        )
    except Exception:
        log.info(f"Failed to send error to Slack: {traceback.format_exc()}")
