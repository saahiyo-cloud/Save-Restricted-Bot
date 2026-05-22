# Save Restricted Bot

*A Telegram Bot, Which can send you restricted content by it's post link*

Based on [Save-Restricted-Bot](https://github.com/bipinkrish/Save-Restricted-Bot).

---

## Variables

- `HASH` Your API Hash from my.telegram.org
- `ID` Your API ID from my.telegram.org
- `TOKEN` Your bot token from @BotFather
- `STRING` Your session string, you can get it at [gist](https://gist.github.com/bipinkrish/0940b30ed66a5537ae1b5aaaee716897#file-main-py) and run it locally
- `OWNER_ID` Telegram user IDs allowed to use the bot

---

## Deployment

### Configure the bot

Set the required values in `config.json` before starting the bot:

```json
{
    "TOKEN": "your_bot_token",
    "ID": "your_api_id",
    "HASH": "your_api_hash",
    "STRING": "your_pyrogram_session_string",
    "OWNER_ID": [
        123456789,
        987654321
    ]
}
```

`STRING` can be `null` if you only need public chats. Private chats, bot chats, invite-link joining, and restricted-content fallback require a valid session string.

### Run directly with Python

```bash
pip install -r requirements.txt
python main.py
```

### Run with Docker Compose

Build and start the bot:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f bot
```

Stop the bot:

```bash
docker compose down
```

The compose file builds the local `Dockerfile` and mounts `./config.json` into the container as `/app/config.json`.

---

# Usage

Send a Telegram message link and the bot will send the content back to you.

## Public channels / groups

Send a normal post link:

```text
https://t.me/channelname/123
```

## Private channels / groups / restricted content

If the user session has not joined the target chat yet, send the invite link first:

```text
https://t.me/+invite_code
```

Then send the post link:

```text
https://t.me/c/123456789/123
```

These links require a valid `STRING`.

## Bot chat messages

Use the `/b/` format:

```text
https://t.me/b/botusername/4321
```

These links also require a valid `STRING`.

## Multiple messages

Use `start_id-end_id` in the message ID position:

```text
https://t.me/channelname/1001-1010

https://t.me/c/123456789/101-120
```

The bot processes up to 100 messages per request. Albums / media groups are sent as a group when possible.
