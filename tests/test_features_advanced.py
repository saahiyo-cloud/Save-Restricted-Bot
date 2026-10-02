import os
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from main import (
    get_user_thumb,
    set_user_thumb,
    del_user_thumb,
    get_user_caption_template,
    set_user_caption_template,
    del_user_caption_template,
    apply_caption_template,
    check_file_size_limit,
    generate_video_thumbnail,
    start_web_server,
    setthumb_handler,
    delthumb_handler,
    showthumb_handler,
    setcaption_handler,
    delcaption_handler,
    showcaption_handler,
    owner_ids,
    MAX_BOT_FILE_SIZE,
)


def test_thumbnail_management_lifecycle(tmp_path):
    user_id = 998877
    sample_img = tmp_path / "sample.jpg"
    sample_img.write_text("fake image data")

    # Set thumbnail
    set_user_thumb(user_id, str(sample_img))
    retrieved = get_user_thumb(user_id)
    assert retrieved is not None
    assert os.path.exists(retrieved)

    # Delete thumbnail
    deleted = del_user_thumb(user_id)
    assert deleted is True
    assert get_user_thumb(user_id) is None

    # Deleting non-existent returns False
    assert del_user_thumb(user_id) is False


def test_caption_template_lifecycle():
    user_id = 112233
    template = "📁 File: {filename}\n\n📝 Details:\n{caption}\n\n🤖 By Bot"

    set_user_caption_template(user_id, template)
    assert get_user_caption_template(user_id) == template

    # Formatting with variables
    formatted = apply_caption_template(user_id, "Sample caption text", "document.pdf")
    assert "📁 File: document.pdf" in formatted
    assert "Sample caption text" in formatted
    assert "🤖 By Bot" in formatted

    # Deleting caption template
    assert del_user_caption_template(user_id) is True
    assert get_user_caption_template(user_id) is None

    # Fallback to original caption when no template is set
    original = "Just normal caption"
    assert apply_caption_template(user_id, original, "test.mp4") == original


def test_file_size_limit():
    msg_normal = MagicMock()
    msg_normal.document.file_size = 500 * 1024 * 1024  # 500 MB
    msg_normal.video = None
    msg_normal.audio = None

    is_over, size = check_file_size_limit(msg_normal)
    assert not is_over
    assert size == 500 * 1024 * 1024

    msg_oversized = MagicMock()
    msg_oversized.document.file_size = 2500 * 1024 * 1024  # 2.5 GB
    msg_oversized.video = None
    msg_oversized.audio = None

    is_over, size = check_file_size_limit(msg_oversized)
    assert is_over
    assert size == 2500 * 1024 * 1024


@pytest.mark.asyncio
async def test_cloud_web_health_server():
    import urllib.request

    port = 18999
    server = await start_web_server(port)
    assert server is not None

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()

        response = await reader.read(1024)
        writer.close()
        await writer.wait_closed()

        res_str = response.decode("utf-8")
        assert "200 OK" in res_str
        assert '"status": "ok"' in res_str
        assert '"uptime"' in res_str
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_setthumb_and_showthumb_commands(tmp_path):
    owner_id = list(owner_ids)[0] if owner_ids else 12345
    fake_img = tmp_path / "mock_thumb.jpg"
    fake_img.write_text("photo binary data")

    with patch("main.owner_ids", {owner_id}):
        # 1. /setthumb without photo
        msg_no_photo = MagicMock()
        msg_no_photo.chat.id = 100
        msg_no_photo.id = 200
        msg_no_photo.from_user.id = owner_id
        msg_no_photo.photo = None
        msg_no_photo.reply_to_message = None

        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await setthumb_handler(MagicMock(), msg_no_photo)
            assert "reply to a photo" in mock_send.call_args[0][1]

        # 2. /setthumb with photo attached
        msg_with_photo = MagicMock()
        msg_with_photo.chat.id = 100
        msg_with_photo.id = 201
        msg_with_photo.from_user.id = owner_id
        msg_with_photo.photo = MagicMock()
        msg_with_photo.reply_to_message = None

        with patch("main.bot.download_media", new_callable=AsyncMock, return_value=str(fake_img)), \
             patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:

            await setthumb_handler(MagicMock(), msg_with_photo)
            assert "saved successfully" in mock_send.call_args[0][1]

        # 3. /showthumb shows the photo
        with patch("main.bot.send_photo", new_callable=AsyncMock) as mock_send_photo:
            await showthumb_handler(MagicMock(), msg_with_photo)
            mock_send_photo.assert_called_once()
            assert "Your Current Custom Thumbnail" in mock_send_photo.call_args[1]["caption"]

        # 4. /delthumb removes it
        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await delthumb_handler(MagicMock(), msg_with_photo)
            assert "deleted" in mock_send.call_args[0][1]


@pytest.mark.asyncio
async def test_caption_commands():
    owner_id = list(owner_ids)[0] if owner_ids else 12345
    with patch("main.owner_ids", {owner_id}):
        msg = MagicMock()
        msg.chat.id = 500
        msg.id = 600
        msg.from_user.id = owner_id

        # 1. /setcaption without arguments
        msg.text = "/setcaption"
        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await setcaption_handler(MagicMock(), msg)
            assert "Usage:" in mock_send.call_args[0][1]

        # 2. /setcaption with template
        msg.text = "/setcaption File: {filename} - {caption}"
        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await setcaption_handler(MagicMock(), msg)
            assert "updated" in mock_send.call_args[0][1]

        # 3. /showcaption
        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await showcaption_handler(MagicMock(), msg)
            assert "Active Caption Template" in mock_send.call_args[0][1]

        # 4. /delcaption
        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await delcaption_handler(MagicMock(), msg)
            assert "deleted" in mock_send.call_args[0][1]
