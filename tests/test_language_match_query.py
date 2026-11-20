"""Replies follow the language of the user's message, not the interface language."""
import language_policy


def test_default_directive_matches_user_message_language():
    text = language_policy.apply_to_system("Be helpful.")
    assert "SAME language as the user's latest message" in text
    assert "Tajik" in text and "Russian" in text and "English" in text
    assert language_policy.MARKER in text


def test_explicit_language_still_pins_when_asked():
    text = language_policy.apply_to_system("Be helpful.", "en")
    assert "English" in text and "SAME language" not in text


def test_directive_is_refreshed_not_duplicated():
    once = language_policy.apply_to_system("Be helpful.")
    twice = language_policy.apply_to_system(once, "ru")
    assert twice.count(language_policy.MARKER) == 1
    assert "Russian" in twice
