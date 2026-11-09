"""Tests for invite permission bits and Manage Server checks."""

from urllib.parse import parse_qs, urlparse

from joltbot.core.permissions import (
    ADMINISTRATOR,
    BOT_PERMISSIONS_VALUE,
    MANAGE_GUILD,
    bot_invite_url,
    can_manage_guild,
)


def test_owner_can_manage_even_without_bits():
    assert can_manage_guild("0", owner=True)


def test_administrator_can_manage():
    assert can_manage_guild(str(ADMINISTRATOR))


def test_manage_server_can_manage():
    assert can_manage_guild(str(MANAGE_GUILD | 1 << 10))


def test_regular_member_cannot_manage():
    # View Channels + Send Messages + Kick Members: still not enough
    assert not can_manage_guild(str(1 << 10 | 1 << 11 | 1 << 1))


def test_garbage_permissions_are_denied():
    assert not can_manage_guild(None)
    assert not can_manage_guild("")
    assert not can_manage_guild("not-a-number")


def test_invite_permissions_match_the_old_bot():
    assert BOT_PERMISSIONS_VALUE == 2251800082377746
    assert not BOT_PERMISSIONS_VALUE & ADMINISTRATOR


def test_invite_url_preselects_guild():
    url = bot_invite_url("123", guild_id=456)
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["123"]
    assert query["guild_id"] == ["456"]
    assert query["disable_guild_select"] == ["true"]
    assert query["scope"] == ["bot applications.commands"]
    assert query["permissions"] == [str(BOT_PERMISSIONS_VALUE)]
