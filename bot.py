import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime

# ─────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
GUILD_ID         = int(os.environ.get("GUILD_ID", "0"))
MEMBER_ROLE_ID   = int(os.environ.get("MEMBER_ROLE_ID", "0"))
OFFICER_ROLE_ID  = int(os.environ.get("OFFICER_ROLE_ID", "0"))
LOG_CHANNEL_ID   = int(os.environ.get("LOG_CHANNEL_ID", "0"))
APPLY_CHANNEL_ID = int(os.environ.get("APPLY_CHANNEL_ID", "0"))  # канал где появляются заявки
MAX_MEMBERS      = 250
# ─────────────────────────────────────────

DATA_FILE = "players.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def active_count(data):
    return sum(1 for v in data.values() if not v.get("banned") and not v.get("left") and v.get("approved"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────
#  КНОПКИ ОДОБРЕНИЯ / ОТКЛОНЕНИЯ
# ─────────────────────────────────────────
class ApproveView(discord.ui.View):
    def __init__(self, applicant_id: int, game_id: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.game_id = game_id

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message("❌ Только офицеры могут одобрять заявки.", ephemeral=True)
            return

        data = load_data()
        uid = str(self.applicant_id)

        if active_count(data) >= MAX_MEMBERS:
            await interaction.response.send_message(
                f"❌ Нет свободных мест ({MAX_MEMBERS}/{MAX_MEMBERS}). Освободи место перед одобрением.",
                ephemeral=True
            )
            return

        data[uid] = {
            "game_id": self.game_id,
            "discord_tag": data.get(uid, {}).get("discord_tag", ""),
            "joined": datetime.utcnow().isoformat(),
            "warnings": 0,
            "notes": [],
            "approved": True,
            "banned": False,
            "left": False
        }
        save_data(data)

        guild = interaction.guild
        member = guild.get_member(self.applicant_id)
        role = guild.get_role(MEMBER_ROLE_ID)
        if member and role:
            await member.add_roles(role)

        # уведомляем игрока
        if member:
            try:
                await member.send(
                    f"✅ **Твоя заявка в Blood Pact одобрена!**\n"
                    f"Game ID: `{self.game_id}`\n"
                    f"Добро пожаловать в лигу! ⚔️"
                )
            except discord.Forbidden:
                pass

        # лог
        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="✅ Заявка одобрена", color=0x57F287)
            embed.add_field(name="Игрок", value=f"<@{self.applicant_id}>", inline=True)
            embed.add_field(name="Game ID", value=f"`{self.game_id}`", inline=True)
            embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
            embed.add_field(name="Мест занято", value=f"{active_count(data)}/{MAX_MEMBERS}", inline=True)
            await log_ch.send(embed=embed)

        # обновляем сообщение с заявкой
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = 0x57F287
        embed.set_footer(text=f"Одобрено: {interaction.user} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(f"✅ Заявка <@{self.applicant_id}> одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message("❌ Только офицеры могут отклонять заявки.", ephemeral=True)
            return

        data = load_data()
        uid = str(self.applicant_id)
        # удаляем pending заявку если есть
        if uid in data and not data[uid].get("approved"):
            del data[uid]
            save_data(data)

        guild = interaction.guild
        member = guild.get_member(self.applicant_id)
        if member:
            try:
                await member.send(
                    f"❌ **Твоя заявка в Blood Pact отклонена.**\n"
                    f"Если считаешь это ошибкой — обратись к офицеру."
                )
            except discord.Forbidden:
                pass

        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="❌ Заявка отклонена", color=0xED4245)
            embed.add_field(name="Игрок", value=f"<@{self.applicant_id}>", inline=True)
            embed.add_field(name="Game ID", value=f"`{self.game_id}`", inline=True)
            embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
            await log_ch.send(embed=embed)

        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = 0xED4245
        embed.set_footer(text=f"Отклонено: {interaction.user} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(f"❌ Заявка <@{self.applicant_id}> отклонена.", ephemeral=True)


# ─────────────────────────────────────────
#  /apply — подача заявки игроком
# ─────────────────────────────────────────
@tree.command(name="apply", description="Подать заявку на вступление в Blood Pact", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(game_id="Твой игровой ID в Hero Siege")
async def apply(interaction: discord.Interaction, game_id: str):
    data = load_data()
    discord_id = str(interaction.user.id)

    # уже активный участник
    if discord_id in data and data[discord_id].get("approved") and not data[discord_id].get("left") and not data[discord_id].get("banned"):
        await interaction.response.send_message(
            "❌ Ты уже являешься участником Blood Pact.", ephemeral=True
        )
        return

    # забанен
    if discord_id in data and data[discord_id].get("banned"):
        await interaction.response.send_message(
            "❌ Ты забанен и не можешь подать заявку.", ephemeral=True
        )
        return

    # уже есть pending заявка
    if discord_id in data and not data[discord_id].get("approved"):
        await interaction.response.send_message(
            "⏳ Твоя заявка уже на рассмотрении. Ожидай ответа офицера.", ephemeral=True
        )
        return

    # проверяем дублирующийся game_id
    for uid, info in data.items():
        if info.get("game_id") == game_id and uid != discord_id and info.get("approved") and not info.get("left"):
            await interaction.response.send_message(
                f"❌ Game ID `{game_id}` уже используется другим участником.\n"
                f"Если это ошибка — обратись к офицеру.",
                ephemeral=True
            )
            return

    # сохраняем pending заявку
    data[discord_id] = {
        "game_id": game_id,
        "discord_tag": str(interaction.user),
        "applied": datetime.utcnow().isoformat(),
        "approved": False,
        "banned": False,
        "left": False,
        "warnings": 0,
        "notes": []
    }
    save_data(data)

    # отправляем заявку в канал офицеров
    apply_ch = bot.get_channel(APPLY_CHANNEL_ID)
    if apply_ch:
        embed = discord.Embed(
            title="📨 Новая заявка в Blood Pact",
            color=0x5865F2
        )
        embed.add_field(name="Игрок", value=interaction.user.mention, inline=True)
        embed.add_field(name="Discord", value=f"`{interaction.user}`", inline=True)
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Мест занято", value=f"{active_count(data)}/{MAX_MEMBERS}", inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.timestamp = datetime.utcnow()

        view = ApproveView(applicant_id=interaction.user.id, game_id=game_id)
        await apply_ch.send(embed=embed, view=view)

    await interaction.response.send_message(
        f"📨 Заявка отправлена!\n"
        f"Game ID: `{game_id}`\n"
        f"Офицер рассмотрит её и ты получишь уведомление в личку.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /leave — игрок покидает Blood Pact
# ─────────────────────────────────────────
@tree.command(name="leave", description="Покинуть Blood Pact и освободить место", guild=discord.Object(id=GUILD_ID))
async def leave(interaction: discord.Interaction):
    data = load_data()
    discord_id = str(interaction.user.id)

    if discord_id not in data or not data[discord_id].get("approved") or data[discord_id].get("left") or data[discord_id].get("banned"):
        await interaction.response.send_message(
            "❌ Ты не числишься активным участником Blood Pact.", ephemeral=True
        )
        return

    game_id = data[discord_id].get("game_id", "?")
    data[discord_id]["left"] = True
    data[discord_id]["left_date"] = datetime.utcnow().isoformat()
    save_data(data)

    guild = interaction.guild
    role = guild.get_role(MEMBER_ROLE_ID)
    if role and role in interaction.user.roles:
        await interaction.user.remove_roles(role)

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="👋 Игрок покинул Blood Pact", color=0xFEE75C)
        embed.add_field(name="Игрок", value=interaction.user.mention, inline=True)
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Мест свободно", value=f"{MAX_MEMBERS - active_count(data)}/{MAX_MEMBERS}", inline=True)
        await log_ch.send(embed=embed)

    await interaction.response.send_message(
        "👋 Ты покинул **Blood Pact**. Место освобождено.\n"
        "Если захочешь вернуться — подай заявку через `/apply`.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /lookup — карточка игрока (офицеры)
# ─────────────────────────────────────────
@tree.command(name="lookup", description="Найти игрока по Discord или Game ID [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Discord пользователь", game_id="Игровой ID")
async def lookup(interaction: discord.Interaction, member: discord.Member = None, game_id: str = None):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    found = None

    if member:
        found_id = str(member.id)
        found = data.get(found_id)
        target_name = str(member)
    elif game_id:
        for uid, info in data.items():
            if info.get("game_id") == game_id:
                found = info
                found_id = uid
                target_name = info.get("discord_tag", uid)
                break
    else:
        await interaction.response.send_message("Укажи @пользователя или game_id.", ephemeral=True)
        return

    if not found:
        await interaction.response.send_message("❌ Игрок не найден в базе Blood Pact.", ephemeral=True)
        return

    warnings = found.get("warnings", 0)
    notes = found.get("notes", [])
    notes_text = "\n".join([f"• {n}" for n in notes[-5:]]) if notes else "нет"

    if found.get("banned"):
        status, color = "🔨 Забанен", 0xED4245
    elif found.get("left"):
        status, color = "👋 Покинул лигу", 0xFEE75C
    elif not found.get("approved"):
        status, color = "⏳ Заявка на рассмотрении", 0x5865F2
    else:
        status, color = "✅ Активен", 0x57F287

    embed = discord.Embed(title=f"🔍 {target_name}", color=color)
    embed.add_field(name="Game ID", value=f"`{found.get('game_id', 'н/д')}`", inline=True)
    embed.add_field(name="Статус", value=status, inline=True)
    embed.add_field(name="Предупреждения", value=f"{'⚠️' * warnings} {warnings}", inline=True)
    embed.add_field(name="В лиге с", value=found.get("joined", "н/д")[:10], inline=True)
    embed.add_field(name="История (последние 5)", value=notes_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
#  /warn — предупреждение (офицеры)
# ─────────────────────────────────────────
@tree.command(name="warn", description="Выдать предупреждение игроку [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Игрок", reason="Причина предупреждения")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)

    if uid not in data or not data[uid].get("approved") or data[uid].get("banned") or data[uid].get("left"):
        await interaction.response.send_message(
            f"❌ {member.mention} не является активным участником Blood Pact.", ephemeral=True
        )
        return

    data[uid]["warnings"] = data[uid].get("warnings", 0) + 1
    note = f"[{datetime.utcnow().strftime('%Y-%m-%d')}] ⚠️ Предупреждение от {interaction.user}: {reason}"
    data[uid].setdefault("notes", []).append(note)
    warnings = data[uid]["warnings"]
    save_data(data)

    try:
        await member.send(
            f"⚠️ **Blood Pact — Предупреждение**\n"
            f"Причина: **{reason}**\n\n"
            f"Следующее нарушение — **бан из Blood Pact**."
        )
    except discord.Forbidden:
        pass

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="⚠️ Предупреждение выдано", color=0xFFA500)
        embed.add_field(name="Игрок", value=member.mention, inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Предупреждений всего", value=str(warnings), inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        await log_ch.send(embed=embed)

    await interaction.response.send_message(
        f"✅ Предупреждение выдано {member.mention}. Всего: **{warnings}**.", ephemeral=True
    )


# ─────────────────────────────────────────
#  /bp_ban — бан из Blood Pact (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_ban", description="Забанить игрока из Blood Pact [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Игрок", reason="Причина бана")
async def bp_ban(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)

    guild = interaction.guild
    role = guild.get_role(MEMBER_ROLE_ID)
    if role and role in member.roles:
        await member.remove_roles(role)

    if uid not in data:
        data[uid] = {"discord_tag": str(member), "notes": []}
    note = f"[{datetime.utcnow().strftime('%Y-%m-%d')}] 🔨 БАН от {interaction.user}: {reason}"
    data[uid].setdefault("notes", []).append(note)
    data[uid]["banned"] = True
    data[uid]["approved"] = False
    data[uid]["left"] = False
    save_data(data)

    try:
        await member.send(
            f"🔨 **Blood Pact — Бан**\n"
            f"Ты исключён из Blood Pact.\n"
            f"Причина: **{reason}**"
        )
    except discord.Forbidden:
        pass

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🔨 Бан из Blood Pact", color=0xED4245)
        embed.add_field(name="Игрок", value=member.mention, inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        await log_ch.send(embed=embed)

    await interaction.response.send_message(f"🔨 {member.mention} исключён из Blood Pact.", ephemeral=True)


# ─────────────────────────────────────────
#  /bp_list — список участников (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_list", description="Список участников Blood Pact [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_list(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    active  = [(uid, i) for uid, i in data.items() if i.get("approved") and not i.get("banned") and not i.get("left")]
    pending = [(uid, i) for uid, i in data.items() if not i.get("approved") and not i.get("banned")]
    left    = [(uid, i) for uid, i in data.items() if i.get("left")]
    banned  = [(uid, i) for uid, i in data.items() if i.get("banned")]

    embed = discord.Embed(
        title="⚔️ Blood Pact — участники",
        description=f"Мест занято: **{len(active)}/{MAX_MEMBERS}** | Свободно: **{MAX_MEMBERS - len(active)}**",
        color=0x5865F2
    )
    if active:
        lines = []
        for uid, info in active[:25]:
            w = info.get("warnings", 0)
            lines.append(f"`{info.get('game_id','?')}` <@{uid}>{'  ⚠️' * w}")
        if len(active) > 25:
            lines.append(f"... и ещё {len(active) - 25}")
        embed.add_field(name=f"✅ Активные ({len(active)})", value="\n".join(lines), inline=False)
    if pending:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>" for uid, i in pending[:10]]
        embed.add_field(name=f"⏳ Ожидают одобрения ({len(pending)})", value="\n".join(lines), inline=False)
    if left:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>" for uid, i in left[:10]]
        embed.add_field(name=f"👋 Покинули ({len(left)})", value="\n".join(lines), inline=False)
    if banned:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>" for uid, i in banned[:10]]
        embed.add_field(name=f"🔨 Забаненные ({len(banned)})", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
#  СТАРТ
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Blood Pact Bot запущен как {bot.user}")
    print(f"   Участников: {active_count(load_data())}/{MAX_MEMBERS}")

bot.run(BOT_TOKEN)
