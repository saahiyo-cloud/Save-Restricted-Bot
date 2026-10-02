import pytest
from unittest.mock import MagicMock
from main import parse_tme_link, parse_message_range, get_message_type


def test_parse_tme_link_invite():
    res1 = parse_tme_link("https://t.me/+AbCdEfGh123")
    assert res1 == {"type": "invite", "link": "https://t.me/+AbCdEfGh123"}

    res2 = parse_tme_link("https://t.me/joinchat/AbCdEfGh123/")
    assert res2 == {"type": "invite", "link": "https://t.me/joinchat/AbCdEfGh123/"}


def test_parse_tme_link_private_channel():
    # Without topic
    res = parse_tme_link("https://t.me/c/1234567890/100")
    assert res == {"type": "private", "chatid": -1001234567890, "from_id": 100, "to_id": 100}

    # With range
    res_range = parse_tme_link("https://t.me/c/1234567890/100-110")
    assert res_range == {"type": "private", "chatid": -1001234567890, "from_id": 100, "to_id": 110}

    # With topic
    res_topic = parse_tme_link("https://t.me/c/1234567890/45/100")
    assert res_topic == {"type": "private", "chatid": -1001234567890, "from_id": 100, "to_id": 100}

    # With ?single
    res_single = parse_tme_link("https://t.me/c/1234567890/100?single")
    assert res_single == {"type": "private", "chatid": -1001234567890, "from_id": 100, "to_id": 100}


def test_parse_tme_link_bot_chat():
    res = parse_tme_link("https://t.me/b/sample_bot/4321")
    assert res == {"type": "bot", "chatid": "sample_bot", "from_id": 4321, "to_id": 4321}

    res_range = parse_tme_link("https://t.me/b/sample_bot/10-15")
    assert res_range == {"type": "bot", "chatid": "sample_bot", "from_id": 10, "to_id": 15}


def test_parse_tme_link_public_channel():
    # Without topic
    res = parse_tme_link("https://t.me/telegram/123")
    assert res == {"type": "public", "chatid": "telegram", "from_id": 123, "to_id": 123}

    # With topic
    res_topic = parse_tme_link("https://t.me/telegram/12/345")
    assert res_topic == {"type": "public", "chatid": "telegram", "from_id": 345, "to_id": 345}

    # With range
    res_range = parse_tme_link("https://t.me/telegram/100-105")
    assert res_range == {"type": "public", "chatid": "telegram", "from_id": 100, "to_id": 105}


def test_parse_tme_link_invalid():
    assert parse_tme_link("not a link") is None
    assert parse_tme_link("https://google.com") is None
    assert parse_tme_link("https://t.me/") is None


def test_parse_message_range():
    assert parse_message_range("10") == (10, 10)
    assert parse_message_range("10-20") == (10, 20)
    assert parse_message_range("invalid") is None


def test_get_message_type():
    msg = MagicMock()
    msg.document = None
    msg.video = None
    msg.animation = None
    msg.sticker = None
    msg.voice = None
    msg.audio = None
    msg.photo = None
    msg.text = None

    assert get_message_type(msg) is None

    msg.document = MagicMock()
    assert get_message_type(msg) == "Document"
    msg.document = None

    msg.video = MagicMock()
    assert get_message_type(msg) == "Video"
    msg.video = None

    msg.animation = MagicMock()
    assert get_message_type(msg) == "Animation"
    msg.animation = None

    msg.sticker = MagicMock()
    assert get_message_type(msg) == "Sticker"
    msg.sticker = None

    msg.voice = MagicMock()
    assert get_message_type(msg) == "Voice"
    msg.voice = None

    msg.audio = MagicMock()
    assert get_message_type(msg) == "Audio"
    msg.audio = None

    msg.photo = MagicMock()
    assert get_message_type(msg) == "Photo"
    msg.photo = None

    msg.text = "Hello"
    assert get_message_type(msg) == "Text"
