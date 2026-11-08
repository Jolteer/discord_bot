"""Code shared by the bot and the web dashboard.

Nothing in this package imports discord.py or FastAPI, so both processes can
use it. It does read the module list (modules/registry.py) to know which
per-server settings exist; those settings files follow the same rule.
"""
