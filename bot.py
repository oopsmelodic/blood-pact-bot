import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import io
from datetime import datetime
from storage import (
    create_archive,
    get_setting,
    init_storage,
    load_data,
    save_data,
    set_setting,
    storage_description,
)

# ─────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
GUILD_ID         = int(os.environ.get("GUILD_ID", "0"))
MEMBER_ROLE_ID   = int(os.environ.get("MEMBER_ROLE_ID", "0"))
OFFICER_ROLE_ID  = int(os.environ.get("OFFICER_ROLE_ID", "0"))
LOG_CHANNEL_ID   = int(os.environ.get("LOG_CHANNEL_ID", "0"))
APPLY_CHANNEL_ID  = int(os.environ.get("APPLY_CHANNEL_ID", "0"))
STATUS_CHANNEL_ID = int(os.environ.get("STATUS_CHANNEL_ID", "0"))  # канал со статусом регистрации
STATUS_MESSAGE_ID = None  # ID сообщения статуса, заполняется при старте
MAX_MEMBERS      = 1000
REGISTRATION_OPEN = True  # управляется командами /bp_open и /bp_close
# ─────────────────────────────────────────

def active_count(data):
    return sum(1 for v in data.values() if not v.get("banned") and not v.get("left") and v.get("approved"))


def blacklist_only_record(info):
    """Оставляет только постоянную отметку ЧС, без статуса заявки или участника."""
    return {
        "discord_tag": info.get("discord_tag", ""),
        "notes": info.get("notes", []),
        "blacklisted": True,
        "blacklist_reason": info.get("blacklist_reason", ""),
        "blacklisted_at": info.get("blacklisted_at", ""),
        "blacklisted_by": info.get("blacklisted_by", ""),
        "approved": False,
        "banned": False,
        "left": False,
    }


async def update_status():
    """Обновляет или создаёт закреплённое сообщение статуса."""
    global STATUS_MESSAGE_ID
    if not STATUS_CHANNEL_ID:
        return
    ch = bot.get_channel(STATUS_CHANNEL_ID)
    if not ch:
        return

    data = load_data()
    active = active_count(data)
    free = MAX_MEMBERS - active
    status_text = "🟢 **ОТКРЫТА**" if REGISTRATION_OPEN else "🔴 **ЗАКРЫТА**"

    embed = discord.Embed(title="⚔️ Blood Pact — Статус регистрации", color=0x57F287 if REGISTRATION_OPEN else 0xED4245)
    embed.add_field(name="Регистрация", value=status_text, inline=False)
    embed.add_field(name="Участников", value=f"{active}/{MAX_MEMBERS}", inline=True)
    embed.add_field(name="Свободно мест", value=str(free), inline=True)
    if REGISTRATION_OPEN and free > 0:
        embed.add_field(name="Как вступить", value="Напиши `/apply` и укажи свой Game ID", inline=False)
    elif not REGISTRATION_OPEN:
        embed.add_field(name=" ", value="Регистрация временно приостановлена. Следи за объявлениями.", inline=False)
    else:
        embed.add_field(name=" ", value="Все места заняты. Следи за объявлениями.", inline=False)
    embed.set_footer(text="Обновляется автоматически")
    embed.timestamp = discord.utils.utcnow()

    # если уже знаем ID сообщения — редактируем его
    if STATUS_MESSAGE_ID:
        try:
            msg = await ch.fetch_message(STATUS_MESSAGE_ID)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden):
            STATUS_MESSAGE_ID = None

    # ищем закреплённые сообщения бота
    try:
        pins = await ch.pins()
        for msg in pins:
            if msg.author == bot.user:
                STATUS_MESSAGE_ID = msg.id
                await msg.edit(embed=embed)
                return
    except discord.Forbidden:
        pass

    # создаём новое сообщение и закрепляем
    try:
        msg = await ch.send(embed=embed)
        STATUS_MESSAGE_ID = msg.id
        await msg.pin()
    except discord.Forbidden:
        pass


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

        if uid not in data or not data[uid].get("applied"):
            await interaction.response.send_message(
                "⚠️ Эта заявка уже закрыта.", ephemeral=True
            )
            return

        if data.get(uid, {}).get("banned"):
            await interaction.response.send_message(
                "❌ Эту заявку нельзя одобрить.", ephemeral=True
            )
            return

        if data.get(uid, {}).get("approved"):
            await interaction.response.send_message("⚠️ Этот игрок уже одобрен.", ephemeral=True)
            return

        if active_count(data) >= MAX_MEMBERS:
            await interaction.response.send_message(f"❌ Нет свободных мест ({MAX_MEMBERS}/{MAX_MEMBERS}).", ephemeral=True)
            return

        record = data.setdefault(uid, {})
        record.update({
            "game_id": game_id,
            "discord_tag": record.get("discord_tag", ""),
            "joined": datetime.utcnow().isoformat(),
            "warnings": record.get("warnings", 0),
            "notes": record.get("notes", []),
            "invited": record.get("invited", False),
            "approved": True,
            "banned": False,
            "left": False
        })
        save_data(data)

        guild = interaction.guild
        member = guild.get_member(applicant_id)
        role = guild.get_role(MEMBER_ROLE_ID)
        if member and role:
            await member.add_roles(role)
        await update_status()

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

        if uid not in data or not data[uid].get("applied"):
            await interaction.response.send_message(
                "⚠️ Эта заявка уже закрыта.", ephemeral=True
            )
            return

        if data.get(uid, {}).get("approved"):
            await interaction.response.send_message("⚠️ Этот игрок уже был одобрен ранее.", ephemeral=True)
            return

        if uid in data:
            if data[uid].get("blacklisted"):
                data[uid] = blacklist_only_record(data[uid])
            else:
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
        await interaction.response.send_message("❌ Ты забанен и не можешь подать заявку.", ephemeral=True)
        return
    if discord_id in data and data[discord_id].get("approved") and not data[discord_id].get("left"):
        await interaction.response.send_message("❌ Ты уже являешься участником Blood Pact.", ephemeral=True)
        return
    if discord_id in data and data[discord_id].get("applied") and not data[discord_id].get("approved"):
        await interaction.response.send_message("⏳ Твоя заявка уже на рассмотрении.", ephemeral=True)
        return
    for uid, info in data.items():
        if info.get("game_id") == game_id and uid != discord_id and info.get("approved") and not info.get("left"):
            await interaction.response.send_message(f"❌ Game ID `{game_id}` уже используется другим участником.", ephemeral=True)
            return

    record = data.setdefault(discord_id, {})
    record.update({
        "game_id": game_id,
        "discord_tag": str(interaction.user),
        "applied": datetime.utcnow().isoformat(),
        "approved": False,
        "banned": False,
        "left": False,
        "invited": False,
        "warnings": 0,
        "comment": comment or "",
        "notes": record.get("notes", [])
    })
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
    await update_status()

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
    left_players = [
        (uid, info) for uid, info in data.items()
        if info.get("left") and not info.get("banned") and not info.get("blacklisted")
    ]

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
#  /bp_close_applications — закрыть ожидающие заявки (офицеры)
# ─────────────────────────────────────────
@tree.command(
    name="bp_close_applications",
    description="Закрыть все заявки на рассмотрении [офицеры]",
    guild=discord.Object(id=GUILD_ID),
)
async def bp_close_applications(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message(
            "❌ Только офицеры могут использовать эту команду.", ephemeral=True
        )
        return

    data = load_data()
    pending_ids = {
        uid
        for uid, info in data.items()
        if info.get("applied")
        and not info.get("approved")
        and not info.get("banned")
        and not info.get("left")
    }
    if not pending_ids:
        await interaction.response.send_message(
            "✅ Нет активных заявок на рассмотрении.", ephemeral=True
        )
        return

    view = ConfirmView(action="close_applications", officer_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ Будут закрыты все заявки на рассмотрении: **{len(pending_ids)}**.\n"
        "Одобренные участники и записи чёрного списка не изменятся.\n\n"
        "Подтверди действие:",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.confirmed:
        return

    # Перечитываем базу: пока офицер подтверждал, часть заявок могли обработать.
    data = load_data()
    pending_ids = {
        uid
        for uid, info in data.items()
        if info.get("applied")
        and not info.get("approved")
        and not info.get("banned")
        and not info.get("left")
    }
    if not pending_ids:
        await interaction.followup.send(
            "✅ Активных заявок больше нет.", ephemeral=True
        )
        return

    for uid in pending_ids:
        if data[uid].get("blacklisted"):
            data[uid] = blacklist_only_record(data[uid])
        else:
            del data[uid]
    save_data(data)

    # Закрываем карточки. Проверка в обработчиках кнопок также защищает карточки,
    # которые Discord не разрешил отредактировать.
    cards_closed = 0
    apply_ch = bot.get_channel(APPLY_CHANNEL_ID)
    remaining_ids = set(pending_ids)
    if apply_ch:
        try:
            async for message in apply_ch.history(limit=None):
                if not remaining_ids:
                    break
                if not message.embeds:
                    continue
                source_embed = message.embeds[0]
                if source_embed.title != "📨 Новая заявка в Blood Pact":
                    continue
                applicant_id = None
                for field in source_embed.fields:
                    if field.name == "Игрок":
                        applicant_id = field.value.strip("<@>")
                        break
                if applicant_id not in remaining_ids:
                    continue

                closed_embed = discord.Embed(
                    title="📨 Заявка в Blood Pact — ЗАКРЫТА", color=0x747F8D
                )
                for field in source_embed.fields:
                    closed_embed.add_field(
                        name=field.name, value=field.value, inline=field.inline
                    )
                closed_embed.set_footer(
                    text=(
                        f"Закрыто: {interaction.user} • "
                        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
                    )
                )
                await message.edit(embed=closed_embed, view=None)
                remaining_ids.remove(applicant_id)
                cards_closed += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(
            title="🗃️ Закрыты активные заявки", color=0x747F8D
        )
        embed.add_field(name="Закрыто заявок", value=str(len(pending_ids)), inline=True)
        embed.add_field(name="Обновлено карточек", value=str(cards_closed), inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        await log_ch.send(embed=embed)

    await interaction.followup.send(
        f"✅ Закрыто заявок: **{len(pending_ids)}**. "
        f"Обновлено карточек: **{cards_closed}**.",
        ephemeral=True,
    )


# ───────────────────────────────────────
#  /bp_reset — полный сброс лиги (офицеры)
# ───────────────────────────────────────
@tree.command(name="bp_reset", description="Полный сброс Blood Pact — новый сезон [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_reset(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры могут использовать эту команду.", ephemeral=True)
        return

    data = load_data()
    blacklisted = {
        uid: blacklist_only_record(info)
        for uid, info in data.items()
        if info.get("blacklisted")
    }
    total = len(data) - len(blacklisted)

    view = ConfirmView(action="reset", officer_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ **ПОЛНЫЙ СБРОС BLOOD PACT**\n\n"
        f"Это удалит **{total} игроков** из базы и снимет роль со всех участников на сервере.\n"
        f"Записи чёрного списка (**{len(blacklisted)}**) будут сохранены.\n"
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
    create_archive(archive_file, data)

    # очищаем сезонные данные, но сохраняем постоянный чёрный список
    save_data(blacklisted)

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
        f"В чёрном списке сохранено: **{len(blacklisted)}**.\n"
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
    if found.get("blacklisted"):
        embed.add_field(
            name="⛔ Чёрный список",
            value=found.get("blacklist_reason", "Причина не указана"),
            inline=False,
        )
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
#  /bp_blacklist — тихо добавить Discord ID в чёрный список
# ─────────────────────────────────────────
@tree.command(
    name="bp_blacklist",
    description="Добавить Discord ID в чёрный список [офицеры]",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(user_id="Discord ID пользователя", reason="Причина")
async def bp_blacklist(
    interaction: discord.Interaction, user_id: str, reason: str
):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    uid = user_id.strip().strip("<@!>")
    if not uid.isdigit() or int(uid) <= 0:
        await interaction.response.send_message(
            "❌ Укажи корректный Discord ID.", ephemeral=True
        )
        return

    reason = reason.strip()
    if not reason:
        await interaction.response.send_message(
            "❌ Причина не может быть пустой.", ephemeral=True
        )
        return
    if len(reason) > 1000:
        await interaction.response.send_message(
            "❌ Причина слишком длинная — максимум 1000 символов.", ephemeral=True
        )
        return

    data = load_data()
    record = data.setdefault(uid, {"notes": []})
    member = interaction.guild.get_member(int(uid))
    if member:
        record["discord_tag"] = str(member)

    now = datetime.utcnow()
    note = (
        f"[{now.strftime('%Y-%m-%d')}] ⛔ ЧЁРНЫЙ СПИСОК "
        f"от {interaction.user}: {reason}"
    )
    record.setdefault("notes", []).append(note)
    record.update(
        {
            "blacklisted": True,
            "blacklist_reason": reason,
            "blacklisted_at": now.isoformat(),
            "blacklisted_by": str(interaction.user.id),
        }
    )
    save_data(data)
    await update_status()

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="⛔ Добавлен в чёрный список", color=0x2B2D31)
        embed.add_field(name="Discord ID", value=f"`{uid}`", inline=True)
        embed.add_field(name="Пользователь", value=f"<@{uid}>", inline=True)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        await log_ch.send(embed=embed)

    # Намеренно не отправляем пользователю личное сообщение.
    await interaction.response.send_message(
        f"⛔ Discord ID `{uid}` сохранён в чёрном списке без уведомления пользователя.",
        ephemeral=True,
    )


# ─────────────────────────────────────────
#  /bp_list
# ─────────────────────────────────────────
class BPListView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(self, officer_id: int, groups: dict):
        super().__init__(timeout=300)
        self.officer_id = officer_id
        self.groups = groups
        self.category = "active"
        self.page = 0
        self._refresh_buttons()

    def _page_count(self):
        item_count = len(self.groups[self.category])
        return max(1, (item_count + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _refresh_buttons(self):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self._page_count() - 1

        category_buttons = {
            "active": self.show_active,
            "pending": self.show_pending,
            "left": self.show_left,
            "banned": self.show_banned,
        }
        for category, button in category_buttons.items():
            button.disabled = category == self.category
            button.style = (
                discord.ButtonStyle.primary
                if category == self.category
                else discord.ButtonStyle.secondary
            )

        items = self.groups[self.category]
        start = self.page * self.PAGE_SIZE
        page_items = items[start:start + self.PAGE_SIZE]
        if self.category == "active" and page_items:
            self.invite_toggle.disabled = False
            self.invite_toggle.placeholder = "Изменить отметку игрового инвайта"
            self.invite_toggle.options = [
                discord.SelectOption(
                    label=(str(info.get("game_id", "?")).strip() or "Без Game ID")[:100],
                    value=uid,
                    description=(
                        "Заинвайчен — нажмите, чтобы снять отметку"
                        if info.get("invited")
                        else "Не заинвайчен — нажмите, чтобы отметить"
                    ),
                    emoji="✅" if info.get("invited") else "❌",
                )
                for uid, info in page_items
            ]
        else:
            self.invite_toggle.disabled = True
            self.invite_toggle.placeholder = "Отметка инвайта доступна для активных"
            self.invite_toggle.options = [
                discord.SelectOption(label="Нет активных игроков", value="none")
            ]

    def make_embed(self):
        labels = {
            "active": ("✅ Активные", 0x57F287),
            "pending": ("⏳ Ожидают", 0x5865F2),
            "left": ("👋 Покинули", 0xFEE75C),
            "banned": ("🔨 Бан / ⛔ Чёрный список", 0xED4245),
        }
        title, color = labels[self.category]
        items = self.groups[self.category]
        page_count = self._page_count()
        start = self.page * self.PAGE_SIZE
        page_items = items[start:start + self.PAGE_SIZE]

        lines = []
        for number, (uid, info) in enumerate(page_items, start=start + 1):
            suffix = ""
            if self.category == "active":
                invite_status = (
                    "✅ заинвайчен" if info.get("invited") else "❌ не заинвайчен"
                )
                warnings = info.get("warnings", 0)
                comment = info.get("comment", "")
                if warnings:
                    suffix += f"  {'⚠️' * warnings}"
                if comment:
                    suffix += f" — *{comment}*"
            elif self.category == "left" and info.get("removed_by_officer"):
                invite_status = None
                suffix = "  🗑"
            elif self.category == "banned" and info.get("blacklisted"):
                invite_status = None
                reason = str(info.get("blacklist_reason", "причина не указана"))
                suffix = f"  ⛔ — *{reason[:120]}*"
            else:
                invite_status = None
            status = f" · {invite_status}" if invite_status else ""
            # Game ID стоит в конце строки: при клике Discord не захватывает
            # следующий за inline-code пробел в копируемый текст.
            lines.append(
                f"**{number}.** <@{uid}>{status}{suffix} — ID: `{info.get('game_id', '?')}`"
            )

        active_count_value = len(self.groups["active"])
        summary = (
            f"Мест занято: **{active_count_value}/{MAX_MEMBERS}** · "
            f"Свободно: **{MAX_MEMBERS - active_count_value}**\n"
            f"Активные: **{active_count_value}** · "
            f"Ожидают: **{len(self.groups['pending'])}** · "
            f"Покинули: **{len(self.groups['left'])}** · "
            f"Бан / ЧС: **{len(self.groups['banned'])}**"
        )
        embed = discord.Embed(
            title=f"⚔️ Blood Pact — {title}",
            description=f"{summary}\n\n" + ("\n".join(lines) if lines else "*Список пуст.*"),
            color=color,
        )
        embed.set_footer(
            text=f"Страница {self.page + 1}/{page_count} · по {self.PAGE_SIZE} игроков"
        )
        return embed

    async def _switch_category(self, interaction: discord.Interaction, category: str):
        if interaction.user.id != self.officer_id:
            await interaction.response.send_message(
                "❌ Эта панель открыта другим офицером.", ephemeral=True
            )
            return
        self.category = category
        self.page = 0
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.select(
        placeholder="Изменить отметку игрового инвайта",
        options=[discord.SelectOption(label="Нет активных игроков", value="none")],
        min_values=1,
        max_values=1,
        row=2,
    )
    async def invite_toggle(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.officer_id:
            await interaction.response.send_message(
                "❌ Эта панель открыта другим офицером.", ephemeral=True
            )
            return
        if self.category != "active" or select.values[0] == "none":
            await interaction.response.send_message(
                "❌ Отметку можно менять только у активных игроков.", ephemeral=True
            )
            return

        uid = select.values[0]
        data = load_data()
        if uid not in data:
            await interaction.response.send_message(
                "❌ Игрок больше не найден в базе. Открой `/bp_list` заново.",
                ephemeral=True,
            )
            return

        invited = not data[uid].get("invited", False)
        data[uid]["invited"] = invited
        save_data(data)

        for item_uid, info in self.groups["active"]:
            if item_uid == uid:
                info["invited"] = invited
                break

        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Активные", style=discord.ButtonStyle.primary, row=0)
    async def show_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_category(interaction, "active")

    @discord.ui.button(label="Ожидают", style=discord.ButtonStyle.secondary, row=0)
    async def show_pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_category(interaction, "pending")

    @discord.ui.button(label="Покинули", style=discord.ButtonStyle.secondary, row=0)
    async def show_left(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_category(interaction, "left")

    @discord.ui.button(label="Бан / ЧС", style=discord.ButtonStyle.secondary, row=0)
    async def show_banned(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch_category(interaction, "banned")

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.officer_id:
            await interaction.response.send_message(
                "❌ Эта панель открыта другим офицером.", ephemeral=True
            )
            return
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Вперёд ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.officer_id:
            await interaction.response.send_message(
                "❌ Эта панель открыта другим офицером.", ephemeral=True
            )
            return
        self.page = min(self._page_count() - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


@tree.command(name="bp_list", description="Список участников [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_list(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    groups = {
        "active": [
            (uid, info) for uid, info in data.items()
            if info.get("approved") and not info.get("banned") and not info.get("left")
        ],
        "pending": [
            (uid, info) for uid, info in data.items()
            if not info.get("approved") and not info.get("banned")
        ],
        "left": [
            (uid, info) for uid, info in data.items()
            if info.get("left") and not info.get("banned")
        ],
        "banned": [
            (uid, info) for uid, info in data.items()
            if info.get("banned") or info.get("blacklisted")
        ],
    }
    for items in groups.values():
        items.sort(
            key=lambda item: item[1].get("joined", item[1].get("applied", "")),
            reverse=True,
        )

    view = BPListView(interaction.user.id, groups)
    await interaction.response.send_message(
        embed=view.make_embed(), view=view, ephemeral=True
    )


# ─────────────────────────────────────────
#  /bp_export — резервная копия базы (офицеры)
# ─────────────────────────────────────────
@tree.command(name="bp_export", description="Скачать резервную копию базы игроков [офицеры]", guild=discord.Object(id=GUILD_ID))
async def bp_export(interaction: discord.Interaction):
    if not any(r.id == OFFICER_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
        return

    data = load_data()
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"players_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.json"
    backup = discord.File(io.BytesIO(payload), filename=filename)
    await interaction.response.send_message(
        f"✅ Резервная копия базы: **{len(data)}** записей.",
        file=backup,
        ephemeral=True,
    )



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
    set_setting("registration_open", True)
    await update_status()
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
    set_setting("registration_open", False)
    await update_status()
    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(title="🔒 Регистрация закрыта", color=0xED4245)
        embed.add_field(name="Офицер", value=interaction.user.mention, inline=True)
        await log_ch.send(embed=embed)
    await interaction.response.send_message("🔒 Регистрация в Blood Pact **закрыта**. Новые заявки не принимаются.", ephemeral=True)



@bot.event
async def on_message(message: discord.Message):
    """Удаляет обычные сообщения в канале статуса — оставляет только slash-команды."""
    if message.author == bot.user:
        return
    if STATUS_CHANNEL_ID and message.channel.id == STATUS_CHANNEL_ID:
        # slash-команды не создают обычных сообщений — удаляем всё что написано текстом
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return
    await bot.process_commands(message)

# ─────────────────────────────────────────
#  СТАРТ
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    global REGISTRATION_OPEN
    init_storage()
    REGISTRATION_OPEN = bool(get_setting("registration_open", True))
    bot.add_view(ApproveView())
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Blood Pact Bot запущен как {bot.user}")
    print(f"   Хранилище: {storage_description()}")
    print(f"   Участников: {active_count(load_data())}/{MAX_MEMBERS}")
    await update_status()

bot.run(BOT_TOKEN)
