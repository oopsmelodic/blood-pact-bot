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
APPLY_CHANNEL_ID = int(os.environ.get("APPLY_CHANNEL_ID", "0"))
MAX_MEMBERS      = 250
REGISTRATION_OPEN = True  # управляется командами /bp_open и /bp_close
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
#  PERSISTENT VIEW для кнопок заявок
# ─────────────────────────────────────────
class ApproveView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _get_applicant_id_and_game_id(self, message: discord.Message):
        if not message.embeds:
            return None, None
        embed = message.embeds[0]
        applicant_id = None
        game_id = None
        for field in embed.fields:
            if field.name == "Игрок":
                raw = field.value.strip("<@>")
                try:
                    applicant_id = int(raw)
                except ValueError:
                    pass
            if field.name == "Game ID":
                game_id = field.value.strip("`")
        return applicant_id, game_id

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="bp_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message("❌ Только офицеры могут одобрять заявки.", ephemeral=True)
            return

        applicant_id, game_id = await self._get_applicant_id_and_game_id(interaction.message)
        if not applicant_id or not game_id:
            await interaction.response.send_message("❌ Не удалось прочитать данные заявки.", ephemeral=True)
            return

        data = load_data()
        uid = str(applicant_id)

        if data.get(uid, {}).get("approved"):
            await interaction.response.send_message("⚠️ Этот игрок уже одобрен.", ephemeral=True)
            return

        if active_count(data) >= MAX_MEMBERS:
            await interaction.response.send_message(f"❌ Нет свободных мест ({MAX_MEMBERS}/{MAX_MEMBERS}).", ephemeral=True)
            return

        data[uid] = {
            "game_id": game_id,
            "discord_tag": data.get(uid, {}).get("discord_tag", ""),
            "joined": datetime.utcnow().isoformat(),
            "warnings": data.get(uid, {}).get("warnings", 0),
            "notes": data.get(uid, {}).get("notes", []),
            "approved": True,
            "banned": False,
            "left": False
        }
        save_data(data)

        guild = interaction.guild
        member = guild.get_member(applicant_id)
        role = guild.get_role(MEMBER_ROLE_ID)
        if member and role:
            await member.add_roles(role)

        try:
            if member:
                await member.send(
                    f"✅ **Твоя заявка в Blood Pact одобрена!**\n"
                    f"Game ID: `{game_id}`\n"
                    f"Добро пожаловать в лигу! ⚔️"
                )
        except discord.Forbidden:
            pass

        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="✅ Заявка одобрена", color=0x57F287)
            embed.add_field(name="Игрок", value=f"<@{applicant_id}>", inline=True)
            embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
            embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
            embed.add_field(name="Мест занято", value=f"{active_count(data)}/{MAX_MEMBERS}", inline=True)
            await log_ch.send(embed=embed)

        new_embed = discord.Embed(title="📨 Заявка в Blood Pact — ОДОБРЕНА", color=0x57F287)
        for field in interaction.message.embeds[0].fields:
            new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
        new_embed.set_footer(text=f"Одобрено: {interaction.user} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.response.send_message(f"✅ Заявка <@{applicant_id}> одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="bp_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message("❌ Только офицеры могут отклонять заявки.", ephemeral=True)
            return

        applicant_id, game_id = await self._get_applicant_id_and_game_id(interaction.message)
        if not applicant_id or not game_id:
            await interaction.response.send_message("❌ Не удалось прочитать данные заявки.", ephemeral=True)
            return

        data = load_data()
        uid = str(applicant_id)

        if data.get(uid, {}).get("approved"):
            await interaction.response.send_message("⚠️ Этот игрок уже был одобрен ранее.", ephemeral=True)
            return

        if uid in data:
            del data[uid]
            save_data(data)

        guild = interaction.guild
        member = guild.get_member(applicant_id)
        try:
            if member:
                await member.send(
                    f"❌ **Твоя заявка в Blood Pact отклонена.**\n"
                    f"Если считаешь это ошибкой — обратись к офицеру."
                )
        except discord.Forbidden:
            pass

        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(title="❌ Заявка отклонена", color=0xED4245)
            embed.add_field(name="Игрок", value=f"<@{applicant_id}>", inline=True)
            embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
            embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
            await log_ch.send(embed=embed)

        new_embed = discord.Embed(title="📨 Заявка в Blood Pact — ОТКЛОНЕНА", color=0xED4245)
        for field in interaction.message.embeds[0].fields:
            new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
        new_embed.set_footer(text=f"Отклонено: {interaction.user} • {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.response.send_message(f"❌ Заявка <@{applicant_id}> отклонена.", ephemeral=True)


# ─────────────────────────────────────────
#  CONFIRM VIEW для опасных операций
# ─────────────────────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, action: str, officer_id: int):
        super().__init__(timeout=30)
        self.action = action
        self.officer_id = officer_id
        self.confirmed = False

    @discord.ui.button(label="✅ Подтвердить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.officer_id:
            await interaction.response.send_message("❌ Только ты можешь подтвердить это действие.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.send_message("Отменено.", ephemeral=True)


# ─────────────────────────────────────────
#  /apply — подача заявки
# ─────────────────────────────────────────
@tree.command(name="apply", description="Подать заявку на вступление в Blood Pact", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(game_id="Твой игровой ID в Hero Siege", comment="Комментарий к заявке (необязательно, макс. 100 символов)")
async def apply(interaction: discord.Interaction, game_id: str, comment: str = None):
    data = load_data()
    discord_id = str(interaction.user.id)


    if not REGISTRATION_OPEN:
        await interaction.response.send_message("❌ Регистрация в Blood Pact сейчас закрыта. Следи за объявлениями.", ephemeral=True)
        return
    if comment and len(comment) > 100:
        await interaction.response.send_message("❌ Комментарий слишком длинный — максимум 100 символов.", ephemeral=True)
        return

    if discord_id in data and data[discord_id].get("banned"):
        await interaction.response.send_message("❌ Ты забанен и не можешешь подать заявку.", ephemeral=True)
        return
    if discord_id in data and data[discord_id].get("approved") and not data[discord_id].get("left"):
        await interaction.response.send_message("❌ Ты уже являешься участником Blood Pact.", ephemeral=True)
        return
    if discord_id in data and not data[discord_id].get("approved"):
        await interaction.response.send_message("⏳ Твоя заявка уже на рассмотрении.", ephemeral=True)
        return
    for uid, info in data.items():
        if info.get("game_id") == game_id and uid != discord_id and info.get("approved") and not info.get("left"):
            await interaction.response.send_message(f"❌ Game ID `{game_id}` уже используется другим участником.", ephemeral=True)
            return

    data[discord_id] = {
        "game_id": game_id,
        "discord_tag": str(interaction.user),
        "applied": datetime.utcnow().isoformat(),
        "approved": False,
        "banned": False,
        "left": False,
        "warnings": 0,
        "comment": comment or "",
        "notes": []
    }
    save_data(data)

    apply_ch = bot.get_channel(APPLY_CHANNEL_ID)
    if apply_ch:
        embed = discord.Embed(title="📨 Новая заявка в Blood Pact", color=0x5865F2)
        embed.add_field(name="Игрок", value=f"<@{interaction.user.id}>", inline=True)
        embed.add_field(name="Discord", value=f"`{interaction.user}`", inline=True)
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Мест занято", value=f"{active_count(data)}/{MAX_MEMBERS}", inline=True)
        if comment:
            embed.add_field(name="Комментарий", value=comment, inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.timestamp = datetime.utcnow()
        await apply_ch.send(embed=embed, view=ApproveView())

    await interaction.response.send_message(
        f"📨 Заявка отправлена! Game ID: `{game_id}`\nОфицер рассмотрит её — ты получишь уведомление в личку.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /leave — игрок покидает сам
# ─────────────────────────────────────────
@tree.command(name="leave", description="Покинуть Blood Pact и освободить место", guild=discord.Object(id=GUILD_ID))
async def leave(interaction: discord.Interaction):
    data = load_data()
    discord_id = str(interaction.user.id)

    if discord_id not in data or not data[discord_id].get("approved") or data[discord_id].get("left") or data[discord_id].get("banned"):
        await interaction.response.send_message("❌ Ты не числишься активным участником Blood Pact.", ephemeral=True)
        return

    game_id = data[discord_id].get("game_id", "?")
    data[discord_id]["left"] = True
    data[discord_id]["left_date"] = datetime.utcnow().isoformat()
    save_data(data)

    role = interaction.guild.get_role(MEMBER_ROLE_ID)
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
        "👋 Ты покинул **Blood Pact**. Место освобождено.\nЕсли захочешь вернуться — подай заявку через `/apply`.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /bp_remove — убрать игрока вручную (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_remove", description="Убрать игрока из Blood Pact вручную [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Игрок", reason="Причина удаления")
async def bp_remove(interaction: discord.Interaction, member: discord.Member, reason: str = "Удалён офицером"):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)

    if uid not in data or not data[uid].get("approved") or data[uid].get("left") or data[uid].get("banned"):
        await interaction.response.send_message(f"❌ {member.mention} не является активным участником Blood Pact.", ephemeral=True)
        return

    game_id = data[uid].get("game_id", "?")

    # помечаем как удалён (не бан, не ушёл сам — отдельный статус)
    note = f"[{datetime.utcnow().strftime('%Y-%m-%d')}] 🗑 Удалён офицером {interaction.user}: {reason}"
    data[uid].setdefault("notes", []).append(note)
    data[uid]["left"] = True
    data[uid]["left_date"] = datetime.utcnow().isoformat()
    data[uid]["removed_by_officer"] = True
    save_data(data)

    # снимаем роль
    role = interaction.guild.get_role(MEMBER_ROLE_ID)
    if role and role in member.roles:
        await member.remove_roles(role)

    # уведомляем игрока
    try:
        await member.send(
            f"🗑 **Blood Pact — Удаление**\n"
            f"Офицер убрал тебя из Blood Pact.\n"
            f"Причина: **{reason}**\n\n"
            f"Если хочешь вернуться — подай заявку через `/apply`."
        )
    except discord.Forbidden:
        pass

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🗑 Игрок удалён из Blood Pact", color=0xFFA500)
        embed.add_field(name="Игрок", value=member.mention, inline=True)
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Мест свободно", value=f"{MAX_MEMBERS - active_count(data)}/{MAX_MEMBERS}", inline=True)
        await log_ch.send(embed=embed)

    await interaction.response.send_message(
        f"🗑 {member.mention} убран из Blood Pact. Роль снята. Место освобождено.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /bp_clear_left — очистить ушедших (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_clear_left", description="Удалить из базы всех кто покинул лигу [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_clear_left(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    left_players = [(uid, info) for uid, info in data.items() if info.get("left") and not info.get("banned")]

    if not left_players:
        await interaction.response.send_message("✅ Нет игроков которых нужно очистить — все активны.", ephemeral=True)
        return

    # показываем список и просим подтвердить
    names = "\n".join([f"• `{i.get('game_id','?')}` <@{uid}>" for uid, i in left_players[:20]])
    if len(left_players) > 20:
        names += f"\n... и ещё {len(left_players) - 20}"

    view = ConfirmView(action="clear_left", officer_id=interaction.user.id)
    await interaction.response.send_message(
        f"🗑 Будут удалены из базы **{len(left_players)}** игроков, покинувших лигу:\n{names}\n\n"
        f"Подтверди действие:",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    for uid, _ in left_players:
        del data[uid]
    save_data(data)

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🗑 Очистка базы — ушедшие игроки", color=0xFFA500)
        embed.add_field(name="Удалено записей", value=str(len(left_players)), inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        await log_ch.send(embed=embed)

    await interaction.followup.send(
        f"✅ Удалено **{len(left_players)}** записей из базы. Места освобождены.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /bp_reset — полный сброс лиги (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_reset", description="Полный сброс Blood Pact — новый сезон [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_reset(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    total = len(data)

    view = ConfirmView(action="reset", officer_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **ПОЛНЫЙ СБРОС BLOOD PACT**\n\n"
        f"Это удалит **всех {total} игроков** из базы и снимет роль со всех участников на сервере.\n"
        f"Действие необратимо. Используй только в начале нового сезона.\n\n"
        f"Подтверди:",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    # снимаем роль со всех участников на сервере
    guild = interaction.guild
    role = guild.get_role(MEMBER_ROLE_ID)
    removed_roles = 0
    if role:
        for member in role.members:
            try:
                await member.remove_roles(role)
                removed_roles += 1
            except discord.Forbidden:
                pass

    # сохраняем архив перед сбросом
    archive_file = f"players_archive_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.json"
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            archive_data = f.read()
        with open(archive_file, "w", encoding="utf-8") as f:
            f.write(archive_data)

    # очищаем базу
    save_data({})

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🔄 Blood Pact — Полный сброс (новый сезон)", color=0xED4245)
        embed.add_field(name="Удалено игроков", value=str(total), inline=True)
        embed.add_field(name="Снято ролей", value=str(removed_roles), inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Архив", value=f"`{archive_file}`", inline=False)
        await log_ch.send(embed=embed)

    await interaction.followup.send(
        f"✅ Blood Pact сброшен. Удалено **{total}** записей, снято ролей: **{removed_roles}**.\n"
        f"Архив сохранён в `{archive_file}`.",
        ephemeral=True
    )


# ─────────────────────────────────────────
#  /lookup
# ─────────────────────────────────────────
@tree.command(name="lookup", description="Найти игрока [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Discord пользователь", game_id="Игровой ID")
async def lookup(interaction: discord.Interaction, member: discord.Member = None, game_id: str = None):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    found = None
    found_id = None

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
        await interaction.response.send_message("❌ Игрок не найден.", ephemeral=True)
        return

    warnings = found.get("warnings", 0)
    notes_text = "\n".join([f"• {n}" for n in found.get("notes", [])[-5:]]) or "нет"

    if found.get("banned"):
        status, color = "🔨 Забанен", 0xED4245
    elif found.get("removed_by_officer") and found.get("left"):
        status, color = "🗑 Удалён офицером", 0xFFA500
    elif found.get("left"):
        status, color = "👋 Покинул сам", 0xFEE75C
    elif not found.get("approved"):
        status, color = "⏳ Ожидает одобрения", 0x5865F2
    else:
        status, color = "✅ Активен", 0x57F287

    embed = discord.Embed(title=f"🔍 {target_name}", color=color)
    embed.add_field(name="Game ID", value=f"`{found.get('game_id', 'н/д')}`", inline=True)
    embed.add_field(name="Статус", value=status, inline=True)
    embed.add_field(name="Предупреждения", value=f"{'⚠️' * warnings} {warnings}", inline=True)
    embed.add_field(name="В лиге с", value=found.get("joined", "н/д")[:10], inline=True)
    embed.add_field(name="История", value=notes_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
#  /warn
# ─────────────────────────────────────────
@tree.command(name="warn", description="Выдать предупреждение [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Игрок", reason="Причина")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)

    if uid not in data or not data[uid].get("approved") or data[uid].get("banned") or data[uid].get("left"):
        await interaction.response.send_message(f"❌ {member.mention} не активный участник.", ephemeral=True)
        return

    data[uid]["warnings"] = data[uid].get("warnings", 0) + 1
    note = f"[{datetime.utcnow().strftime('%Y-%m-%d')}] ⚠️ от {interaction.user}: {reason}"
    data[uid].setdefault("notes", []).append(note)
    warnings = data[uid]["warnings"]
    save_data(data)

    try:
        await member.send(f"⚠️ **Blood Pact — Предупреждение**\nПричина: **{reason}**\n\nСледующее нарушение — **бан**.")
    except discord.Forbidden:
        pass

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="⚠️ Предупреждение", color=0xFFA500)
        embed.add_field(name="Игрок", value=member.mention, inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Всего", value=str(warnings), inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        await log_ch.send(embed=embed)

    await interaction.response.send_message(f"✅ Предупреждение выдано {member.mention}. Всего: **{warnings}**.", ephemeral=True)


# ─────────────────────────────────────────
#  /bp_ban
# ─────────────────────────────────────────
@tree.command(name="bp_ban", description="Забанить игрока [офицеры]", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Игрок", reason="Причина")
async def bp_ban(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    uid = str(member.id)

    role = interaction.guild.get_role(MEMBER_ROLE_ID)
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
        await member.send(f"🔨 **Blood Pact — Бан**\nТы исключён из Blood Pact.\nПричина: **{reason}**")
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
#  /bp_list
# ─────────────────────────────────────────
@tree.command(name="bp_list", description="Список участников [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_list(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    active  = [(uid, i) for uid, i in data.items() if i.get("approved") and not i.get("banned") and not i.get("left")]
    pending = [(uid, i) for uid, i in data.items() if not i.get("approved") and not i.get("banned")]
    left    = [(uid, i) for uid, i in data.items() if i.get("left") and not i.get("banned")]
    banned  = [(uid, i) for uid, i in data.items() if i.get("banned")]

    embed = discord.Embed(
        title="⚔️ Blood Pact — участники",
        description=f"Мест занято: **{len(active)}/{MAX_MEMBERS}** | Свободно: **{MAX_MEMBERS - len(active)}**",
        color=0x5865F2
    )
    if active:
        lines = []
        for uid, i in active[:25]:
            w = i.get('warnings', 0)
            comment = i.get('comment', '')
            comment_str = f" — *{comment}*" if comment else ""
            lines.append(f"`{i.get('game_id','?')}` <@{uid}>{'  ⚠️' * w}{comment_str}")
        if len(active) > 25:
            lines.append(f"... и ещё {len(active) - 25}")
        embed.add_field(name=f"✅ Активные ({len(active)})", value="\n".join(lines), inline=False)
    if pending:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>" for uid, i in pending[:10]]
        embed.add_field(name=f"⏳ Ожидают ({len(pending)})", value="\n".join(lines), inline=False)
    if left:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>{'  🗑' if i.get('removed_by_officer') else ''}" for uid, i in left[:10]]
        embed.add_field(name=f"👋 Покинули ({len(left)})", value="\n".join(lines), inline=False)
    if banned:
        lines = [f"`{i.get('game_id','?')}` <@{uid}>" for uid, i in banned[:10]]
        embed.add_field(name=f"🔨 Забаненные ({len(banned)})", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)



# ─────────────────────────────────────────
#  /bp_open — открыть регистрацию (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_open", description="Открыть регистрацию в Blood Pact [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_open(interaction: discord.Interaction):
    global REGISTRATION_OPEN
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return
    if REGISTRATION_OPEN:
        await interaction.response.send_message("⚠️ Регистрация уже открыта.", ephemeral=True)
        return
    REGISTRATION_OPEN = True
    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="✅ Регистрация открыта", color=0x57F287)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Мест свободно", value=f"{MAX_MEMBERS - active_count(load_data())}/{MAX_MEMBERS}", inline=True)
        await log_ch.send(embed=embed)
    await interaction.response.send_message("✅ Регистрация в Blood Pact **открыта**. Игроки могут подавать заявки через `/apply`.", ephemeral=True)


# ─────────────────────────────────────────
#  /bp_close — закрыть регистрацию (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_close", description="Закрыть регистрацию в Blood Pact [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_close(interaction: discord.Interaction):
    global REGISTRATION_OPEN
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return
    if not REGISTRATION_OPEN:
        await interaction.response.send_message("⚠️ Регистрация уже закрыта.", ephemeral=True)
        return
    REGISTRATION_OPEN = False
    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🔒 Регистрация закрыта", color=0xED4245)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        await log_ch.send(embed=embed)
    await interaction.response.send_message("🔒 Регистрация в Blood Pact **закрыта**. Новые заявки не принимаются.", ephemeral=True)


# ─────────────────────────────────────────
#  СТАРТ
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(ApproveView())
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Blood Pact Bot запущен как {bot.user}")
    print(f"   Участников: {active_count(load_data())}/{MAX_MEMBERS}")

bot.run(BOT_TOKEN)

