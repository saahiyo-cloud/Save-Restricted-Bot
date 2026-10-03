import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from main import (
    send_start,
    help_handler,
    nav_callback_handler,
    build_start_text,
    build_guide_text,
    build_commands_text,
    owner_ids,
)


@pytest.mark.asyncio
async def test_send_start_authorized():
    owner_id = list(owner_ids)[0] if owner_ids else 12345
    with patch("main.owner_ids", {owner_id}):
        user_msg = MagicMock()
        user_msg.chat.id = 1001
        user_msg.id = 2001
        user_msg.from_user.id = owner_id
        user_msg.from_user.mention = "TestUser"

        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await send_start(MagicMock(), user_msg)

            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert "Save Restricted Bot" in mock_send.call_args[0][1]
            assert "Quick Formats:" in mock_send.call_args[0][1]
            assert call_kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_help_handler_authorized():
    owner_id = list(owner_ids)[0] if owner_ids else 12345
    with patch("main.owner_ids", {owner_id}):
        user_msg = MagicMock()
        user_msg.chat.id = 1001
        user_msg.id = 2001
        user_msg.from_user.id = owner_id

        with patch("main.bot.send_message", new_callable=AsyncMock) as mock_send:
            await help_handler(MagicMock(), user_msg)

            mock_send.assert_called_once()
            assert "Detailed Usage Guide" in mock_send.call_args[0][1]


@pytest.mark.asyncio
async def test_nav_callback_guide_and_back():
    owner_id = list(owner_ids)[0] if owner_ids else 12345
    with patch("main.owner_ids", {owner_id}):
        query = MagicMock()
        query.data = "nav_guide"
        query.from_user.id = owner_id
        query.from_user.mention = "TestUser"
        query.answer = AsyncMock()

        with patch("main.callback_query", create=True):
            query.edit_message_text = AsyncMock()
            await nav_callback_handler(MagicMock(), query)

            query.answer.assert_called_once()
            query.edit_message_text.assert_called_once()
            assert "Detailed Usage Guide" in query.edit_message_text.call_args[0][0]
