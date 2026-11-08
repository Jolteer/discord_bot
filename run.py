"""Entry point: python run.py"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord

from bot.client import TicketBot
from bot.config import ConfigError, load_config


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler("logs/bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    log = logging.getLogger("run")
    try:
        cfg = load_config()
    except ConfigError as e:
        log.critical("%s", e)
        sys.exit(1)

    bot = TicketBot(cfg)
    try:
        bot.run(cfg.token, log_handler=None)
    except discord.LoginFailure:
        log.critical("Discord rejected the token. Reset it in the Developer Portal (Bot → Reset Token) and update .env.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "Privileged intents are not enabled. Developer Portal → your app → Bot → Privileged Gateway Intents: "
            "turn ON 'Server Members Intent' and 'Message Content Intent', save, then restart."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
