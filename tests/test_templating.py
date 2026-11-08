from joltbot.core.templating import placeholders_in, render


def test_known_placeholders_are_filled():
    assert render("Hi {user}, welcome to {server}!", user="@Bob", server="Jolt") == "Hi @Bob, welcome to Jolt!"


def test_unknown_placeholders_are_left_alone():
    assert render("Hi {nobody}", user="x") == "Hi {nobody}"


def test_old_guild_name_still_works():
    assert render("Welcome to {guild}", server="Jolt") == "Welcome to Jolt"


def test_stray_braces_never_crash():
    # The old str.format-based code raised an error on all of these.
    for text in ("{", "}", "{0}", "{user", "{a.b}", "{{user}}", "json: {\"a\": 1}"):
        render(text, user="x")
    assert render("{{user}}", user="x") == "{x}"


def test_placeholders_in():
    assert placeholders_in("{user} {guild} {user}") == {"user", "server"}
