import os
import asyncio

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


def block_to_lines(block, depth=0):
    t = block.get("type")
    data = block.get(t, {})
    indent = "  " * depth
    text = rich_to_text(data.get("rich_text"))
    lines = []

    if t == "paragraph":
        lines.append(indent + text)
    elif t == "heading_1":
        lines.append("")
        lines.append("# " + text)
    elif t == "heading_2":
        lines.append("## " + text)
    elif t == "heading_3":
        lines.append("### " + text)
    elif t == "bulleted_list_item":
        lines.append(indent + "- " + text)
    elif t == "numbered_list_item":
        lines.append(indent + "1. " + text)
    elif t == "to_do":
        box = "\u2611" if data.get("checked") else "\u2610"
        lines.append(indent + f"{box} " + text)
    elif t == "toggle":
        lines.append(indent + "\u25b8 " + text)
    elif t == "quote":
        lines.append("> " + text)
    elif t == "callout":
        emoji = (data.get("icon") or {}).get("emoji", "\U0001f4a1")
        lines.append(f"{emoji} " + text)
    elif t == "code":
        lang = data.get("language", "")
        lines.append(f"```{lang}\n{text}\n```")
    elif t == "divider":
        lines.append("\u2500" * 20)
    elif t == "image":
        img = data.get("file") or data.get("external") or {}
        cap = rich_to_text(data.get("caption"))
        lines.append(f"[image] {img.get('url', '')}" + (f" \u2014 {cap}" if cap else ""))
    elif t == "table":
        for row in fetch_all_children(block["id"]):
            cells = row.get("table_row", {}).get("cells", [])
            lines.append(indent + " | ".join(rich_to_text(c) for c in cells))
        return lines  # rows already consumed; don't recurse below
    else:
        if text:
            lines.append(indent + text)

    if block.get("has_children") and t != "table":
        for child in fetch_all_children(block["id"]):
            lines.extend(block_to_lines(child, depth + 1))
    return lines


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


def build_gameplan_text(page_id):
    page = notion.pages.retrieve(page_id)
    props = page.get("properties", {})
    name = rich_to_text(props.get("Name", {}).get("title")) or "Unknown villain"

    tag_line = " \u00b7 ".join(filter(None, [
        prop_value(props.get("Site")),
        prop_value(props.get("Stake")),
        prop_value(props.get("Game")),
        prop_value(props.get("Status")),
        prop_value(props.get("Player Profile")),
    ]))
    updated = page.get("last_edited_time", "")[:10]

    header = f"# {name}\n"
    if tag_line:
        header += tag_line + "\n"
    if updated:
        header += f"updated {updated}\n"

    leaks = prop_value(props.get("Top 5 Leaks")).strip()
    leaks_section = f"\n**Top 5 Leaks**\n{leaks}\n" if leaks else ""

    body_lines = []
    for block in fetch_all_children(page_id):
        body_lines.extend(block_to_lines(block))
    body = "\n".join(body_lines).strip()
    body_section = ("\n" + body) if body else "\n_No gameplan written in the page body yet._"

    return header + leaks_section + body_section


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


@tree.command(name="gameplan", description="Get the full gameplan for a villain")
@app_commands.describe(villain="Start typing the villain's name")
async def gameplan(interaction: discord.Interaction, villain: str):
    # ephemeral=True -> only the asker sees it (keeps channels clean).
    # Change both ephemeral values below to False to post the plan publicly.
    await interaction.response.defer(ephemeral=True)

    entry = INDEX.get(villain.lower())
    if not entry:
        matches = [v for k, v in INDEX.items() if villain.lower() in k]
        if len(matches) == 1:
            entry = matches[0]
        elif len(matches) > 1:
            names = ", ".join(m["name"] for m in matches[:10])
            await interaction.followup.send(f"Multiple matches: {names}. Be more specific.", ephemeral=True)
            return
    if not entry:
        await interaction.followup.send(f"No gameplan found for **{villain}**.", ephemeral=True)
        return

    try:
        text = await asyncio.to_thread(build_gameplan_text, entry["id"])
    except Exception as e:
        await interaction.followup.send(f"Error fetching gameplan: {e}", ephemeral=True)
        return

    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)


@gameplan.autocomplete("villain")
async def villain_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    names = sorted(v["name"] for k, v in INDEX.items() if cur in k)
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


client.run(DISCORD_TOKEN)
