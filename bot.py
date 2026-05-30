import os
import io
import asyncio

import aiohttp

from dotenv import load_dotenv
load_dotenv()  # reads a local .env file if present; ignored when deployed

import discord
from discord import app_commands
from discord.ext import tasks
from notion_client import Client

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GUILD_ID = os.environ.get("GUILD_ID")  # optional: makes the command appear instantly

DELETE_AFTER_SECONDS = 16 * 60 * 60   # gameplan replies auto-delete after 16 hours

notion = Client(auth=NOTION_TOKEN)

# notion-client 3.x removed databases.query; querying moved to data_sources.query.
# This resolves the right call once and caches the data source id for 3.x.
_DATA_SOURCE_ID = None


def _resolve_data_source_id():
    global _DATA_SOURCE_ID
    if _DATA_SOURCE_ID:
        return _DATA_SOURCE_ID
    db = notion.databases.retrieve(DATABASE_ID)
    sources = db.get("data_sources") or []
    if sources:
        _DATA_SOURCE_ID = sources[0]["id"]
    else:
        # fall back to the database id itself if no data_sources array is present
        _DATA_SOURCE_ID = DATABASE_ID
    return _DATA_SOURCE_ID


def query_database(**kwargs):
    """Works on notion-client 2.x (databases.query) and 3.x (data_sources.query)."""
    if hasattr(notion.databases, "query"):
        return notion.databases.query(database_id=DATABASE_ID, **kwargs)
    return notion.data_sources.query(data_source_id=_resolve_data_source_id(), **kwargs)

# lowercase name -> {"name": original, "id": page_id}
INDEX = {}


# ---------- Notion helpers (run in threads so they don't block the bot) ----------

def rich_to_text(rich):
    return "".join(r.get("plain_text", "") for r in (rich or []))


def fetch_all_children(block_id):
    blocks, cursor = [], None
    while True:
        resp = notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100)
        blocks.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return blocks


def _push_text(segments, line):
    """Append a text line, merging into the previous text segment if there is one."""
    if segments and segments[-1]["type"] == "text":
        segments[-1]["content"] += "\n" + line
    else:
        segments.append({"type": "text", "content": line})


def block_to_segments(block, segments, depth=0):
    """Walk a Notion block into ordered segments: text runs and image objects."""
    t = block.get("type")
    data = block.get(t, {})
    indent = "  " * depth
    text = rich_to_text(data.get("rich_text"))

    if t == "paragraph":
        _push_text(segments, indent + text)
    elif t == "heading_1":
        _push_text(segments, "")
        _push_text(segments, "# " + text)
    elif t == "heading_2":
        _push_text(segments, "## " + text)
    elif t == "heading_3":
        _push_text(segments, "### " + text)
    elif t == "bulleted_list_item":
        _push_text(segments, indent + "- " + text)
    elif t == "numbered_list_item":
        _push_text(segments, indent + "1. " + text)
    elif t == "to_do":
        box = "\u2611" if data.get("checked") else "\u2610"
        _push_text(segments, indent + f"{box} " + text)
    elif t == "toggle":
        _push_text(segments, indent + "\u25b8 " + text)
    elif t == "quote":
        _push_text(segments, "> " + text)
    elif t == "callout":
        emoji = (data.get("icon") or {}).get("emoji", "\U0001f4a1")
        _push_text(segments, f"{emoji} " + text)
    elif t == "code":
        lang = data.get("language", "")
        _push_text(segments, f"```{lang}\n{text}\n```")
    elif t == "divider":
        _push_text(segments, "\u2500" * 20)
    elif t == "image":
        img = data.get("file") or data.get("external") or {}
        url = img.get("url", "")
        cap = rich_to_text(data.get("caption"))
        if url:
            segments.append({"type": "image", "url": url, "caption": cap})
        elif cap:
            _push_text(segments, f"\U0001f4ce {cap}")
    elif t == "table":
        for row in fetch_all_children(block["id"]):
            cells = row.get("table_row", {}).get("cells", [])
            _push_text(segments, indent + " | ".join(rich_to_text(c) for c in cells))
        return  # rows already consumed; don't recurse below
    else:
        if text:
            _push_text(segments, indent + text)

    if block.get("has_children") and t != "table":
        for child in fetch_all_children(block["id"]):
            block_to_segments(child, segments, depth + 1)


def prop_value(prop):
    """Read any Notion property type to a plain string."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "select":
        sel = prop.get("select")
        return sel["name"] if sel else ""
    if t == "status":
        st = prop.get("status")
        return st["name"] if st else ""
    if t == "multi_select":
        return ", ".join(o["name"] for o in prop.get("multi_select", []))
    if t == "formula":
        f = prop.get("formula", {})
        ft = f.get("type")
        if ft == "string":
            return f.get("string") or ""
        if ft == "number":
            n = f.get("number")
            return str(n) if n is not None else ""
        if ft == "boolean":
            return "" if f.get("boolean") is None else str(f.get("boolean"))
        if ft == "date":
            d = f.get("date")
            return (d.get("start") or "") if d else ""
        return ""
    if t in ("rich_text", "title"):
        return rich_to_text(prop.get(t))
    if t == "date":
        d = prop.get("date")
        return (d.get("start") or "") if d else ""
    if t == "last_edited_time":
        return (prop.get("last_edited_time") or "")[:10]
    if t == "number":
        n = prop.get("number")
        return str(n) if n is not None else ""
    return ""


def build_gameplan_segments(page_id):
    page = notion.pages.retrieve(page_id)
    props = page.get("properties", {})
    name = rich_to_text(props.get("Name", {}).get("title")) or "Unknown villain"

    tag_line = " \u00b7 ".join(filter(None, [
        prop_value(props.get("Site")),
        prop_value(props.get("Stake")),
        prop_value(props.get("Game")),
        prop_value(props.get("Status")),
        prop_value(props.get("Player Profile")),
        prop_value(props.get("Latest Run")),
    ]))
    updated = page.get("last_edited_time", "")[:10]

    header = f"# {name}\n"
    if tag_line:
        header += tag_line + "\n"
    if updated:
        header += f"updated {updated}\n"

    leaks = prop_value(props.get("Top 5 Leaks")).strip()
    if leaks:
        header += f"\n**Top 5 Leaks**\n{leaks}\n"

    segments = [{"type": "text", "content": header}]

    body_start = len(segments)
    for block in fetch_all_children(page_id):
        block_to_segments(block, segments)
    if len(segments) == body_start:
        _push_text(segments, "\n_No gameplan written in the page body yet._")

    return segments


def refresh_index_sync():
    idx, cursor = {}, None
    while True:
        resp = query_database(start_cursor=cursor, page_size=100)
        for page in resp["results"]:
            name = rich_to_text(page.get("properties", {}).get("Name", {}).get("title"))
            if name:
                idx[name.lower()] = {"name": name, "id": page["id"]}
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return idx


# ---------- Discord ----------

def chunk_text(text, limit=1900):
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                     # hard-split very long lines
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def download_image(session, url):
    """Fetch an image from Notion's (temporary) URL into memory. Returns (bytes, filename) or None."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            if len(data) > 9_000_000:   # stay under Discord's upload limit
                return None
            # derive a clean filename from the path, ignoring the query string
            path = url.split("?")[0]
            fname = path.rsplit("/", 1)[-1] or "image.png"
            if "." not in fname:
                fname += ".png"
            return data, fname
    except Exception:
        return None


@tasks.loop(minutes=5)
async def refresh_index():
    global INDEX
    try:
        INDEX = await asyncio.to_thread(refresh_index_sync)
    except Exception as e:
        print("Index refresh failed:", e)


@client.event
async def on_ready():
    if not refresh_index.is_running():
        await refresh_index()          # populate cache before first command
        refresh_index.start()
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)   # instant for this one server
    else:
        await tree.sync()              # global; can take up to ~1h to show
    print(f"Logged in as {client.user} | {len(INDEX)} gameplans indexed")


@tree.command(name="gp", description="Get the full gameplan for a villain")
@app_commands.describe(villain="Start typing the villain's name")
async def gameplan(interaction: discord.Interaction, villain: str):
    # Public reply (everyone in the channel sees it) so images can be attached
    # and the message can auto-delete after DELETE_AFTER_SECONDS.
    await interaction.response.defer()

    entry = INDEX.get(villain.lower())
    if not entry:
        matches = [v for k, v in INDEX.items() if villain.lower() in k]
        if len(matches) == 1:
            entry = matches[0]
        elif len(matches) > 1:
            names = ", ".join(m["name"] for m in matches[:10])
            # errors stay private so they don't clutter the channel
            await interaction.followup.send(
                f"Multiple matches: {names}. Be more specific.", ephemeral=True)
            return
    if not entry:
        await interaction.followup.send(
            f"No gameplan found for **{villain}**.", ephemeral=True)
        return

    try:
        segments = await asyncio.to_thread(build_gameplan_segments, entry["id"])
    except Exception as e:
        await interaction.followup.send(f"Error fetching gameplan: {e}", ephemeral=True)
        return

    # Pre-download every image at once (parallel) so the wait happens all together,
    # not one-after-another. Results are keyed back to their segment by index.
    image_results = {}
    image_segs = [(i, s) for i, s in enumerate(segments) if s["type"] == "image"]
    if image_segs:
        async with aiohttp.ClientSession() as session:
            downloads = [download_image(session, s["url"]) for _, s in image_segs]
            fetched = await asyncio.gather(*downloads)
        for (i, _), result in zip(image_segs, fetched):
            image_results[i] = result

    sent = []  # collect every message we post so we can delete them later
    for idx, seg in enumerate(segments):
        if seg["type"] == "text":
            for chunk in chunk_text(seg["content"]):
                if chunk.strip():
                    msg = await interaction.followup.send(chunk, wait=True)
                    sent.append(msg)
        elif seg["type"] == "image":
            result = image_results.get(idx)
            caption = seg.get("caption") or ""
            if result:
                data, fname = result
                file = discord.File(io.BytesIO(data), filename=fname)
                msg = await interaction.followup.send(
                    content=(f"\U0001f4ce {caption}" if caption else None),
                    file=file,
                    wait=True,
                )
            else:
                # download failed (expired/too big) — note it instead of a broken link
                msg = await interaction.followup.send(
                    f"\U0001f4ce [image couldn't load{f' — {caption}' if caption else ''}]",
                    wait=True,
                )
            sent.append(msg)

    # Schedule all of this reply's messages to delete after DELETE_AFTER_SECONDS.
    if sent:
        asyncio.create_task(delete_messages_later(sent, DELETE_AFTER_SECONDS))


async def delete_messages_later(messages, delay):
    """Wait, then delete each message. Ignores ones already gone."""
    await asyncio.sleep(delay)
    for msg in messages:
        try:
            await msg.delete()
        except Exception:
            pass


@gameplan.autocomplete("villain")
async def villain_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    names = sorted(v["name"] for k, v in INDEX.items() if cur in k)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


client.run(DISCORD_TOKEN)
