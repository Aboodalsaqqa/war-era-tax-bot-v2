import os
import asyncio
import json
import requests
import sqlite3
import discord
import time
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, date, timezone, timedelta


BOT_TOKEN = "PUt_YOUR_DISCORD_BOT_TOKEN_HERE"
WARERA_TOKEN = "Put_YOUR_WARERA_API_TOKEN_HERE"
COUNTRY_ID = "6813b6d546e731854c7ac845"
DB_FILE = "tax_bot.db"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= Queue + pending map (in-memory) =================
write_queue: asyncio.Queue = asyncio.Queue()
pending_links: dict = {}  # warera_user_id (str) -> discord_id (str)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    # WAL reduces write-lock contention
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS players (
        warera_user_id TEXT PRIMARY KEY,
        discord_id TEXT,
        warera_name TEXT,
        level INTEGER DEFAULT 0,
        factories INTEGER DEFAULT 0,
        ae_levels TEXT,
        weekly_tax REAL DEFAULT 0,
        amount_paid_today REAL DEFAULT 0,
        last_paid_date TEXT
    )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tax_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_level INTEGER,
            max_level INTEGER,
            base_tax REAL
        )
        """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS tax_settings (
            key TEXT PRIMARY KEY,
            value REAL
        )
        """)

    conn.commit()
    conn.close()

# ================= HELPERS =================
def find_key(obj, key):
    if not isinstance(obj, (dict, list)): return None
    if isinstance(obj, dict):
        if key in obj: return obj[key]
        for k, v in obj.items():
            item = find_key(v, key)
            if item is not None: return item
    elif isinstance(obj, list):
        for i in obj:
            item = find_key(i, key)
            if item is not None: return item
    return None

def api_get(endpoint, payload):
    url = f"https://api2.warera.io/trpc/{endpoint}"
    params = {"batch": 1, "input": json.dumps({"0": payload})}
    headers = {"authorization": WARERA_TOKEN}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        res = r.json()
        if isinstance(res, list) and len(res) > 0:
            data = res[0].get("result", {}).get("data", {})
            return data.get("json", data)
        return {}
    except Exception:
        return {}

def fetch_paid_today_api():
    url = "https://api2.warera.io/trpc/transaction.getPaginatedTransactions"
    headers = {"authorization": WARERA_TOKEN}
    params = {"batch": 1, "input": json.dumps({"0": {"limit": 100, "transactionType": "donation", "countryId": COUNTRY_ID}})}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            result_data = data[0].get("result", {}).get("data", {})
        else:
            result_data = data.get("result", {}).get("data", {}) if isinstance(data, dict) else {}

        items_container = result_data.get("json", result_data) if isinstance(result_data, dict) else {}
        items = items_container.get("items", []) if isinstance(items_container, dict) else []

        week_start = get_tax_week_start()
        paid_map = {}
        for tx in items:
            created_at_raw = tx.get("createdAt")
            try:
                if not created_at_raw:
                    continue
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except Exception:
                continue

            if created_at >= week_start:
                uid_raw = tx.get("buyerId") or tx.get("userId") or tx.get("buyer")
                if uid_raw is None:
                    continue
                uid = str(uid_raw)
                val_raw = tx.get("money") or tx.get("gold") or tx.get("amount") or 0
                try:
                    val = float(val_raw)
                except Exception:
                    val = 0.0
                paid_map[uid] = paid_map.get(uid, 0) + val
        return paid_map
    except Exception as e:
        print(f"❌ Error fetching payments: {e}")
        return {}

def get_tax_week_start():
    now = datetime.now(timezone.utc)
    days_since_friday = (now.weekday() - 4) % 7
    friday = now - timedelta(days=days_since_friday)
    week_start = friday.replace(hour=20, minute=0, second=0, microsecond=0)
    if now < week_start:
        week_start -= timedelta(days=7)
    return week_start

def find_player_in_db_by_name(warera_name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT warera_user_id, warera_name
        FROM players
        WHERE LOWER(warera_name) = LOWER(?)
    """, (warera_name.strip(),))
    row = c.fetchone()
    conn.close()
    return row

# ================= CORE LOGIC (sync + calc) =================
def seed_default_tax_rules():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM tax_rules")
    if c.fetchone()[0] == 0:
        rules = [
            (1, 4, 0),
            (5, 9, 5.25),
            (10, 15, 15.75),
            (16, 20, 29),
            (21, 25, 42),
            (26, 30, 63),
            (31, 39, 100),
            (40, 100, 150)
        ]
        c.executemany(
            "INSERT INTO tax_rules (min_level, max_level, base_tax) VALUES (?, ?, ?)",
            rules
        )

        c.execute(
            "INSERT OR REPLACE INTO tax_settings VALUES ('ae_multiplier', 0.5)"
        )

    conn.commit()
    conn.close()

def sync_country_players():
    print("🔍 Fetching Egypt players from WarEra (verified method)...")
    BASE_URL = "https://api2.warera.io/trpc"
    cursor = None
    total_added = 0
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    while True:
        input_data = {"countryId": COUNTRY_ID, "limit": 100}
        if cursor:
            input_data["cursor"] = cursor
        params = {"input": json.dumps(input_data)}
        try:
            response = requests.get(f"{BASE_URL}/user.getUsersByCountry", params=params, timeout=20)
            response.raise_for_status()
            data = response.json()["result"]["data"]
            users = data.get("items", [])
            cursor = data.get("nextCursor")
        except Exception as e:
            print("❌ Failed to fetch users:", e)
            break

        if not users:
            break

        for user in users:
            warera_user_id = user.get("_id") or user.get("userId")
            if not warera_user_id:
                continue

            try:
                r = requests.get(f"{BASE_URL}/user.getUserLite", params={"input": json.dumps({"userId": warera_user_id})}, timeout=10)
                if r.status_code != 200:
                    continue
                user_data = r.json()["result"]["data"]
                warera_name = user_data.get("username")
            except Exception:
                continue

            if not warera_name:
                continue

            c.execute("SELECT 1 FROM players WHERE warera_user_id=?", (warera_user_id,))
            if c.fetchone():
                continue

            c.execute("INSERT INTO players (warera_user_id, warera_name) VALUES (?, ?)", (warera_user_id, warera_name))
            total_added += 1
            time.sleep(0.1)

        if not cursor:
            break

    conn.commit()
    conn.close()
    print(f"✅ Country sync done. Added {total_added} players.")

def fetch_player_data(warera_uid):
    u_data = api_get("user.getUserLite", {"userId": warera_uid})
    level = find_key(u_data, "level") or 0

    c_data = api_get("company.getCompanies", {"userId": warera_uid, "perPage": 100})
    companies_raw = c_data.get("items", c_data) if isinstance(c_data, dict) else c_data
    if not isinstance(companies_raw, list): companies_raw = []

    ae_levels = []
    for comp in companies_raw:
        cid = comp if isinstance(comp, str) else comp.get("_id") if isinstance(comp, dict) else None
        if cid:
            up_res = api_get("upgrade.getUpgradeByTypeAndEntity", {"upgradeType": "automatedEngine", "companyId": cid})
            lvl = find_key(up_res, "level") or 0
            try:
                ae_levels.append(int(lvl))
            except Exception:
                ae_levels.append(0)
    return int(level), len(companies_raw), ae_levels

def calculate_tax_breakdown(level, factories, ae_levels):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        SELECT base_tax
        FROM tax_rules
        WHERE ? BETWEEN min_level AND max_level
        LIMIT 1
    """, (level,))
    row = c.fetchone()
    base_tax = row[0] if row else 0

    c.execute("""
        SELECT value FROM tax_settings WHERE key='ae_multiplier'
    """)
    ae_mult = c.fetchone()
    ae_mult = ae_mult[0] if ae_mult else 0.5

    conn.close()

    ae_tax = round(sum(lvl * ae_mult for lvl in ae_levels), 2)
    total = round(base_tax + ae_tax, 2)

    return total, base_tax, ae_tax



def sync_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT discord_id, warera_user_id FROM players")
    players = c.fetchall()
    paid_map = fetch_paid_today_api()
    today_str = date.today().isoformat()
    for d_id, w_uid in players:
        if not w_uid: continue
        try:
            lvl, fac, aes = fetch_player_data(w_uid)
            total_tax, lt, at = calculate_tax_breakdown(lvl, fac, aes)
            actual_paid = paid_map.get(str(w_uid), 0)
            c.execute("""
                UPDATE players SET 
                level=?, factories=?, ae_levels=?, weekly_tax=?, 
                amount_paid_today=?, last_paid_date=?
                WHERE warera_user_id=?
            """, (lvl, fac, json.dumps(aes), total_tax, actual_paid, today_str, w_uid))
        except Exception as e:
            print(f"❌ Error syncing {w_uid}: {e}")
    conn.commit()
    conn.close()
    print("🔄 تم التحديث بنجاح ومراجعة سجل التبرعات.")

# ================= Background writer (single writer pattern) =================
async def db_writer():
    while True:
        action, payload = await write_queue.get()

        def do_write():
            try:
                conn = sqlite3.connect(DB_FILE, timeout=30)
                c = conn.cursor()

                if action == "link_game":
                    c.execute("""
                        UPDATE players
                        SET discord_id = ?
                        WHERE warera_user_id = ?
                    """, (payload["discord_id"], payload["warera_user_id"]))

                elif action == "set_tax_rule":
                    c.execute("""
                        DELETE FROM tax_rules
                        WHERE min_level = ? AND max_level = ?
                    """, (payload["min_level"], payload["max_level"]))

                    c.execute("""
                        INSERT INTO tax_rules (min_level, max_level, base_tax)
                        VALUES (?, ?, ?)
                    """, (
                        payload["min_level"],
                        payload["max_level"],
                        payload["base_tax"]
                    ))

                elif action == "set_ae_tax":
                    c.execute("""
                        INSERT OR REPLACE INTO tax_settings (key, value)
                        VALUES ('ae_multiplier', ?)
                    """, (payload["amount"],))

                conn.commit()
                conn.close()
                return True
            except Exception as e:
                print("❌ DB write error:", e)
                return False

        # retry logic
        success = False
        for _ in range(5):
            success = await asyncio.to_thread(do_write)
            if success:
                break
            await asyncio.sleep(1)

        write_queue.task_done()


# ================= DISCORD COMMANDS =================
@bot.event
async def on_ready():
    seed_default_tax_rules()
    init_db()
    # start country sync in thread (blocking network calls)
    bot.loop.create_task(asyncio.to_thread(sync_country_players))
    # start background writer task
    bot.loop.create_task(db_writer())
    await bot.tree.sync()
    if not auto_sync.is_running():
        auto_sync.start()
    print(f"🚀 Tax Bot Online: {bot.user}")

@tasks.loop(minutes=30)
async def auto_sync():
    await asyncio.to_thread(sync_all)

@bot.tree.command(name="dashboard", description="Tax dashboard summary")
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    c = conn.cursor()
    # include warera_user_id so we can check pending_links
    c.execute("""
    SELECT warera_user_id, warera_name, discord_id, level, weekly_tax, amount_paid_today
    FROM players
    WHERE level >= 10
    ORDER BY level DESC, warera_name COLLATE NOCASE
    """)

    rows = c.fetchall()
    conn.close()

    lines = []
    total_players = 0
    count_paid = 0       # PAID + LEGEND
    count_partial = 0
    count_unpaid = 0
    count_legend = 0

    for warera_user_id, warera_name, discord_id_db, level, total, paid in rows:
        total_players += 1
        total = total or 0
        paid = paid or 0

        # override with pending_links if exists (shows immediate mention)
        discord_override = pending_links.get(str(warera_user_id))
        discord_id = discord_override or discord_id_db

        # display name: mention if linked, else game name bold
        if discord_id:
            # use mention (display will be Discord name)
            display_name = f"<@{discord_id}>"
        else:
            display_name = f"**{warera_name}**"

        # determine status
        if total == 0:
            status = "⚪ N/A"
            # show Due first (per request)
            line = f"{status} | {display_name} | Lv.{level} | Due: $0"
        elif paid > total:
            status = "🔵 LEGEND"
            count_legend += 1
            count_paid += 1  # legends count as paid in stats
            line = f"{status} | {display_name} | Lv.{level} | Due: ${total} | Paid: ${paid}"
        elif paid == total:
            status = "🟢 PAID"
            count_paid += 1
            line = f"{status} | {display_name} | Lv.{level} | Due: ${total}"
        elif paid > 0:
            status = "🟠 PARTIAL"
            count_partial += 1
            line = f"{status} | {display_name} | Lv.{level} | Due: ${total} | Paid: ${paid}"
        else:
            status = "🔴 UNPAID"
            count_unpaid += 1
            line = f"{status} | {display_name} | Lv.{level} | Due: ${total}"

        lines.append(line)

    if not lines:
        lines.append("No players found.")

    stats_line = (
        f"**Players:** {total_players} | "
        f"🟢 {count_paid} | "
        f"🟠 {count_partial} | "
        f"🔴 {count_unpaid} | "
        f"🔵 {count_legend}\n\n"
    )

    legend = (
        "**Categories:**\n"
        "🟢 PAID — Paid the required amount\n"
        "🟠 PARTIAL — Paid less than required\n"
        "🔴 UNPAID — No payment made\n"
        "🔵 LEGEND — Paid more than required\n"
        "⚪ N/A — No tax required"
    )

    # compose description (single embed field via description to avoid >25 fields limit)
    description = stats_line + "\n".join(lines) + "\n\n" + legend

    embed = discord.Embed(
        title="🇪🇬 وزارة المالية | مصر",
        description=description,
        color=discord.Color.dark_red(),
        timestamp=datetime.now()
    )

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="force_sync")
async def force_sync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # run sync_all in thread to avoid blocking
    await asyncio.to_thread(sync_all)
    await interaction.followup.send("✅ تم تحديث جميع البيانات.", ephemeral=True)

@bot.tree.command(name="link_game", description="Link your War Era name to your Discord account")
@app_commands.describe(warera_name="Your in-game name")
async def link_game(interaction: discord.Interaction, warera_name: str):
    # Fast read check (no defer) — respond immediately
    try:
        conn = sqlite3.connect(DB_FILE, timeout=3)
        c = conn.cursor()
        c.execute("""
            SELECT warera_user_id, warera_name
            FROM players
            WHERE LOWER(warera_name) = LOWER(?)
        """, (warera_name,))
        row = c.fetchone()
        conn.close()
    except sqlite3.OperationalError:
        await interaction.response.send_message(
            "⚠️ Database is busy (read). Please try again in a few seconds.",
            ephemeral=True
        )
        return

    if not row:
        await interaction.response.send_message(
            "❌ Name not found in Egypt players list.",
            ephemeral=True
        )
        return

    warera_user_id, real_name = row
    discord_id = str(interaction.user.id)

    # Put into pending map and queue for background writer
    pending_links[str(warera_user_id)] = discord_id
    # Queue the write (writer will handle retries)
    await write_queue.put((str(warera_user_id), discord_id))

    # Immediate confirmation
    await interaction.response.send_message(
        f"✅ Link queued — your Discord name will appear in the dashboard shortly.\n👤 In-game name: **{real_name}**",
        ephemeral=True
    )

@bot.tree.command(name="player", description="View detailed tax info for a player")
@app_commands.describe(name="Player in-game name")
async def player(interaction: discord.Interaction, name: str):
    # quick defer (we build embed)
    await interaction.response.defer(ephemeral=True)
    conn = sqlite3.connect(DB_FILE, timeout=5)
    c = conn.cursor()
    c.execute("""
        SELECT warera_user_id, warera_name, discord_id, level, factories, ae_levels,
               weekly_tax, amount_paid_today
        FROM players
        WHERE LOWER(warera_name) = LOWER(?)
    """, (name,))
    row = c.fetchone()
    conn.close()

    if not row:
        await interaction.followup.send("❌ Player not found in Egypt players database.", ephemeral=True)
        return

    warera_user_id, warera_name, discord_id_db, level, factories, ae_json, total, paid = row
    total = total or 0
    paid = paid or 0

    # use pending override if present
    discord_override = pending_links.get(str(warera_user_id))
    discord_id = discord_override or discord_id_db

    if discord_id:
        display_name = f"<@{discord_id}>"
    else:
        display_name = f"**{warera_name}**"

    ae_list = json.loads(ae_json) if ae_json else []
    ae_text = ", ".join(str(lv) for lv in ae_list) if ae_list else "None"

    if total == 0:
        status = "⚪ N/A"
        status_color = discord.Color.light_grey()
    elif paid > total:
        status = "🔵 LEGEND"
        status_color = discord.Color.blue()
    elif paid == total:
        status = "🟢 PAID"
        status_color = discord.Color.green()
    elif paid > 0:
        status = "🟠 PARTIAL"
        status_color = discord.Color.orange()
    else:
        status = "🔴 UNPAID"
        status_color = discord.Color.red()

    remaining = max(total - paid, 0)

    embed = discord.Embed(
        title="🇪🇬 Player Tax Profile",
        color=status_color,
        timestamp=datetime.now()
    )
    embed.add_field(name="👤 Player", value=display_name, inline=False)
    embed.add_field(name="📈 Level", value=f"Lv. {level}", inline=True)
    embed.add_field(name="🏭 Factories", value=str(factories), inline=True)
    embed.add_field(name="🤖 AE Levels", value=ae_text, inline=False)
    embed.add_field(name="💰 Weekly Tax (Due)", value=f"${total}", inline=True)
    embed.add_field(name="💵 Paid", value=f"${paid}", inline=True)
    embed.add_field(name="📉 Remaining", value=f"${remaining}", inline=True)
    embed.add_field(name="📌 Status", value=status, inline=False)
    embed.set_footer(text="War Era Tax System")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="set_tax_rule", description="(Admin) Set tax for a level range")
@app_commands.describe(
    min_level="Minimum level",
    max_level="Maximum level",
    base_tax="Base tax amount"
)
async def set_tax_rule(interaction: discord.Interaction, min_level: int, max_level: int, base_tax: float):

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return

    await write_queue.put((
        "set_tax_rule",
        {
            "min_level": min_level,
            "max_level": max_level,
            "base_tax": base_tax
        }
    ))

    await interaction.response.send_message(
        f"✅ Tax rule queued:\nLevels **{min_level}–{max_level}** → **${base_tax}**",
        ephemeral=True
    )


@bot.tree.command(name="set_ae_tax", description="(Admin) Set AE tax per level")
@app_commands.describe(amount="Tax per AE level")
async def set_ae_tax(interaction: discord.Interaction, amount: float):

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return

    await write_queue.put((
        "set_ae_tax",
        {
            "amount": amount
        }
    ))

    await interaction.response.send_message(
        f"✅ AE tax updated: **${amount} per level**",
        ephemeral=True
    )



# ================= START BOT =================
if __name__ == "__main__":
    init_db()
    bot.run(BOT_TOKEN)
