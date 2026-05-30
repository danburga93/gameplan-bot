# /gameplan bot — setup

The bot does one thing: a player types `/gameplan`, picks a villain from the
autocomplete list (pulled live from your Notion DB), and gets the full gameplan
posted back in Discord. No AI. Lookup is instant.

## 1. Notion integration (gives the bot read access)

1. Go to https://www.notion.so/profile/integrations → **New integration** (internal).
2. Copy the **Internal Integration Secret** → this is `NOTION_TOKEN`.
3. Open your **Gameplans** database → top-right **•••** → **Connections** →
   add the integration you just made. (Without this the bot sees nothing.)
4. Get `NOTION_DATABASE_ID`: open the DB as a full page, copy the URL. The ID is
   the 32-character string after the workspace slug and before `?v=`.
   Example: `notion.so/Gameplans-`**`8a1b...e9f0`**`?v=...`

## 2. Discord application

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy → this is `DISCORD_TOKEN`. (No privileged
   intents needed — leave them off.)
3. **Installation** (or OAuth2 → URL Generator) → scopes: **bot** +
   **applications.commands**. Bot permission: **Send Messages**. Use the generated
   URL to invite the bot to your server.

## 3. (Optional) GUILD_ID — makes the command show up instantly

In Discord: User Settings → Advanced → enable **Developer Mode**. Right-click your
server icon → **Copy Server ID** → this is `GUILD_ID`. Skip it and the command
still works, but global commands can take up to ~1 hour to appear.

## 4. Environment variables

```
DISCORD_TOKEN=...
NOTION_TOKEN=...
NOTION_DATABASE_ID=...
GUILD_ID=...        # optional
```

## 5. Run locally (to test)

```
pip install -r requirements.txt
# set the env vars above, then:
python bot.py
```

In Discord, type `/gameplan` and start typing a villain's name.

## 6. Deploy on Railway (always-on, ~$5/mo)

1. Push these files to a GitHub repo (`bot.py`, `requirements.txt`, `Procfile`).
2. railway.app → **New Project** → **Deploy from GitHub repo**.
3. **Variables** tab → add the four env vars from step 4.
4. Railway detects the `Procfile` and runs the worker. Done.

## Knobs you might flip

- **Public vs private replies:** the plan is sent only to the asker (`ephemeral=True`).
  To post it in the channel for everyone, change both `ephemeral=True` values in
  `bot.py` to `False`.
- **Refresh speed:** the villain list re-syncs from Notion every 5 minutes
  (`@tasks.loop(minutes=5)`). New villains appear in autocomplete within that window.
- **Long plans** are automatically split into multiple messages to stay under
  Discord's character limit. Notion images come through as links (Discord can't
  render Notion blocks); tables come through as plain ` | ` rows.
