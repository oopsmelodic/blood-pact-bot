import asyncio
import io
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from rapidfuzz import fuzz, process


CODEX_URL = "https://parazeya.github.io/hs-map/data/codex.json"
CODEX_RU_URL = "https://parazeya.github.io/hs-map/data/codex.ru.json"
HELPER_ITEMLIST_URL = "https://hero-siege-helper.vercel.app/data/itemlist"
MAX_SCREENSHOT_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_ITEMS_PER_ROLL = 10


def _cache_path() -> Path:
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    base = Path(volume) if volume else Path(".")
    return base / "hero_siege_items.json"


def normalize_name(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = value.replace("´", "'").replace("’", "'").replace("`", "'")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


@dataclass(frozen=True)
class LootItem:
    item_id: str
    name_en: str
    name_ru: str = ""
    rarity: str = ""
    tier: str = ""
    item_type: str = ""
    level: int | None = None
    game_type: int | None = None
    game_id: int | None = None
    weapon_type: int | None = None
    confidence: int = 100

    @property
    def display_name(self) -> str:
        return self.name_en or self.name_ru

    @property
    def bilingual_name(self) -> str:
        if self.name_en and self.name_ru and self.name_en != self.name_ru:
            return f"{self.name_en} / {self.name_ru}"
        return self.display_name

    @classmethod
    def manual(cls, name: str) -> "LootItem":
        normalized = normalize_name(name) or "item"
        return cls(
            item_id=f"manual_{normalized[:64].replace(' ', '_')}",
            name_en=name.strip(),
            confidence=0,
        )


class HelperItemTableParser(HTMLParser):
    """Читает серверную HTML-таблицу Item List Translations из HS Helper."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _optional_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None


def parse_helper_item_table(html: str) -> dict[str, dict]:
    parser = HelperItemTableParser()
    parser.feed(html)
    result: dict[str, dict] = {}
    for row in parser.rows:
        if len(row) < 4 or row[0].casefold() == "id":
            continue
        item_id, name_en = row[0].strip(), row[1].strip()
        if not item_id or not name_en:
            continue
        result[item_id] = {
            "name_en": name_en,
            "game_type": _optional_int(row[2]),
            "game_id": _optional_int(row[3]),
            "weapon_type": _optional_int(row[4]) if len(row) > 4 else None,
        }
    return result


class ItemCatalog:
    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or _cache_path()
        self.items: list[LootItem] = []
        self._aliases: list[str] = []
        self._alias_item_indexes: list[int] = []

    def _build_index(self) -> None:
        self._aliases = []
        self._alias_item_indexes = []
        for index, item in enumerate(self.items):
            seen: set[str] = set()
            for name in (item.name_en, item.name_ru):
                alias = normalize_name(name)
                if len(alias) < 3 or alias in seen:
                    continue
                seen.add(alias)
                self._aliases.append(alias)
                self._alias_item_indexes.append(index)

    def load_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.items = [LootItem(**entry) for entry in payload.get("items", [])]
            self._build_index()
            return bool(self.items)
        except (OSError, ValueError, TypeError):
            return False

    async def refresh(self) -> int:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": "Blood-Pact-Discord-Bot/1.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(CODEX_URL) as response:
                response.raise_for_status()
                base = await response.json(content_type=None)
            async with session.get(CODEX_RU_URL) as response:
                response.raise_for_status()
                ru = json.loads(await response.text())
            helper_items: dict[str, dict] = {}
            try:
                async with session.get(HELPER_ITEMLIST_URL) as response:
                    response.raise_for_status()
                    helper_items = parse_helper_item_table(await response.text())
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                # Codex остаётся полноценным источником, если Helper временно недоступен.
                pass

        base_items = base.get("items", {})
        ru_items = ru.get("items", {})
        if not isinstance(base_items, dict) or not base_items:
            raise ValueError("Каталог Hero Siege не содержит предметов")

        items: list[LootItem] = []
        for item_id, raw in base_items.items():
            if not isinstance(raw, dict):
                continue
            names = raw.get("names") or {}
            name_en = names.get("en", "") if isinstance(names, dict) else ""
            translated = ru_items.get(item_id, {}) if isinstance(ru_items, dict) else {}
            name_ru = translated.get("names", "") if isinstance(translated, dict) else ""
            if not name_en and not name_ru:
                continue
            level = raw.get("lvl")
            items.append(
                LootItem(
                    item_id=str(item_id),
                    name_en=str(name_en or name_ru),
                    name_ru=str(name_ru or ""),
                    rarity=str(raw.get("rarity") or ""),
                    tier=str(raw.get("tier") or ""),
                    item_type=str(raw.get("type") or ""),
                    level=int(level) if isinstance(level, (int, float)) else None,
                )
            )

        item_indexes = {item.item_id: index for index, item in enumerate(items)}
        for item_id, helper in helper_items.items():
            if item_id in item_indexes:
                index = item_indexes[item_id]
                current = asdict(items[index])
                current.update(
                    game_type=helper["game_type"],
                    game_id=helper["game_id"],
                    weapon_type=helper["weapon_type"],
                )
                items[index] = LootItem(**current)
            else:
                items.append(
                    LootItem(
                        item_id=item_id,
                        name_en=helper["name_en"],
                        game_type=helper["game_type"],
                        game_id=helper["game_id"],
                        weapon_type=helper["weapon_type"],
                    )
                )

        self.items = items
        self._build_index()
        payload = {
            "sources": [CODEX_URL, CODEX_RU_URL, HELPER_ITEMLIST_URL],
            "items": [asdict(item) for item in self.items],
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)
        return len(self.items)

    def _best_match(self, text: str, threshold: int) -> LootItem | None:
        normalized = normalize_name(text)
        if len(normalized) < 3 or not self._aliases:
            return None
        result = process.extractOne(normalized, self._aliases, scorer=fuzz.WRatio)
        if not result or result[1] < threshold:
            return None
        alias_index = int(result[2])
        item = self.items[self._alias_item_indexes[alias_index]]
        return LootItem(**{**asdict(item), "confidence": round(float(result[1]))})

    def match_ocr_text(self, text: str, limit: int = MAX_ITEMS_PER_ROLL) -> list[LootItem]:
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates: list[str] = []
        for start in range(len(raw_lines)):
            for width in (1, 2):
                chunk = " ".join(raw_lines[start:start + width]).strip()
                if 3 <= len(chunk) <= 120:
                    candidates.append(chunk)

        matches: dict[str, LootItem] = {}
        for candidate in candidates:
            item = self._best_match(candidate, threshold=73)
            if item and (
                item.item_id not in matches
                or item.confidence > matches[item.item_id].confidence
            ):
                matches[item.item_id] = item

        return sorted(
            matches.values(), key=lambda item: item.confidence, reverse=True
        )[:limit]

    def resolve_manual(self, text: str, limit: int = MAX_ITEMS_PER_ROLL) -> list[LootItem]:
        names = [
            part.strip()
            for part in re.split(r"[\n;,]+", text)
            if part.strip()
        ]
        result: list[LootItem] = []
        seen: set[str] = set()
        for name in names[:limit]:
            item = self._best_match(name, threshold=68) or LootItem.manual(name)
            if item.item_id not in seen:
                seen.add(item.item_id)
                result.append(item)
        return result


def extract_text_from_image(image_bytes: bytes) -> str:
    try:
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image, ImageEnhance, UnidentifiedImageError
    except ImportError as error:
        raise RuntimeError(
            "OCR-зависимости не установлены. Проверь requirements.txt."
        ) from error

    custom_command = os.environ.get("TESSERACT_CMD", "").strip()
    if custom_command:
        pytesseract.pytesseract.tesseract_cmd = custom_command

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Discord-вложение не является читаемым изображением") from error
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ValueError("Скриншот слишком большой; максимум 24 мегапикселя")

    image = ImageEnhance.Contrast(image).enhance(1.6)
    array = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    scale = min(3.0, 4000 / max(array.shape))
    if scale > 1:
        array = cv2.resize(
            array, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(array)
    threshold = cv2.adaptiveThreshold(
        enhanced,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )

    try:
        languages = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError as error:
        raise RuntimeError(
            "Tesseract не установлен на сервере. Проверь nixpacks.toml или TESSERACT_CMD."
        ) from error
    selected = "+".join(lang for lang in ("eng", "rus") if lang in languages)
    if not selected:
        raise RuntimeError("Tesseract не содержит английский или русский языковой пакет")

    outputs: list[str] = []
    for prepared, psm in ((enhanced, 11), (threshold, 6)):
        value = pytesseract.image_to_string(
            prepared, lang=selected, config=f"--oem 3 --psm {psm}"
        )
        if value.strip():
            outputs.append(value)
    return "\n".join(outputs)


def _is_officer(member: discord.abc.User, officer_role_id: int) -> bool:
    return isinstance(member, discord.Member) and any(
        role.id == officer_role_id for role in member.roles
    )


def _participant_allowed(
    member: discord.abc.User, member_role_id: int, officer_role_id: int
) -> bool:
    if not isinstance(member, discord.Member):
        return False
    allowed = {member_role_id, officer_role_id}
    return any(role.id in allowed for role in member.roles)


class LootRollView(discord.ui.View):
    def __init__(
        self,
        manager: "LootManager",
        item: LootItem,
        creator_id: int,
        duration: int,
    ):
        super().__init__(timeout=duration)
        self.manager = manager
        self.item = item
        self.creator_id = creator_id
        self.duration = duration
        self.entries: dict[int, str] = {}
        self.message: discord.Message | None = None
        self.finished = False
        self._lock = asyncio.Lock()

    def _mentions(self, choice: str) -> str:
        users = [uid for uid, selected in self.entries.items() if selected == choice]
        if not users:
            return "—"
        shown = " ".join(f"<@{uid}>" for uid in users[:35])
        if len(users) > 35:
            shown += f"\n…и ещё {len(users) - 35}"
        return shown[:1024]

    def make_embed(self, result: str | None = None) -> discord.Embed:
        color = 0x57F287 if result else 0x5865F2
        embed = discord.Embed(
            title=f"🎲 Разрол: {self.item.display_name}"[:256],
            description=(
                result
                or f"Выберите приоритет. Разрол завершится через **{self.duration} сек.**"
            ),
            color=color,
        )
        details = []
        if self.item.name_ru and self.item.name_en != self.item.name_ru:
            details.append(f"RU: {self.item.name_ru}")
        if self.item.rarity:
            details.append(self.item.rarity)
        if self.item.tier:
            details.append(f"Tier {self.item.tier}")
        if self.item.item_type:
            details.append(self.item.item_type)
        if self.item.level is not None:
            details.append(f"ур. {self.item.level}")
        if details:
            embed.add_field(name="Предмет", value=" · ".join(details)[:1024], inline=False)
        embed.add_field(name="⚔️ Нужно", value=self._mentions("main"), inline=False)
        embed.add_field(name="Участников", value=str(sum(v != "pass" for v in self.entries.values())), inline=True)
        embed.set_footer(text="Blood Pact Loot")
        return embed

    async def _claim(self, interaction: discord.Interaction, choice: str) -> None:
        if self.finished:
            await interaction.response.send_message("Разрол уже завершён.", ephemeral=True)
            return
        if not _participant_allowed(
            interaction.user,
            self.manager.member_role_id,
            self.manager.officer_role_id,
        ):
            await interaction.response.send_message(
                "❌ Участвовать могут только участники Blood Pact.", ephemeral=True
            )
            return
        self.entries[interaction.user.id] = choice
        labels = {"main": "Нужно", "pass": "Пас"}
        await interaction.response.edit_message(embed=self.make_embed(), view=self)
        await interaction.followup.send(
            f"✅ Ваш выбор: **{labels[choice]}**", ephemeral=True
        )

    @discord.ui.button(label="Нужно", emoji="⚔️", style=discord.ButtonStyle.success)
    async def main_roll(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._claim(interaction, "main")

    @discord.ui.button(label="Пас", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def pass_roll(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._claim(interaction, "pass")

    @discord.ui.button(label="Завершить", emoji="🔒", style=discord.ButtonStyle.danger)
    async def finish_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _is_officer(interaction.user, self.manager.officer_role_id):
            await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.finish()
        await interaction.followup.send("✅ Разрол завершён.", ephemeral=True)

    async def on_timeout(self) -> None:
        await self.finish()

    async def finish(self) -> None:
        async with self._lock:
            if self.finished:
                return
            self.finished = True
            self.stop()
            for child in self.children:
                child.disabled = True

            main_pool = [uid for uid, choice in self.entries.items() if choice == "main"]
            pool = main_pool
            result_lines: list[str] = []
            winner: int | None = None
            if pool:
                rolls = {uid: secrets.randbelow(100) + 1 for uid in pool}
                best = max(rolls.values())
                tied = [uid for uid, value in rolls.items() if value == best]
                winner = tied[secrets.randbelow(len(tied))]
                result_lines.append(f"🏆 Победитель: <@{winner}> — **{rolls[winner]}**")
                roll_text = " · ".join(
                    f"<@{uid}>: {value}"
                    for uid, value in sorted(rolls.items(), key=lambda pair: pair[1], reverse=True)
                )
                result_lines.append(f"Результаты: {roll_text[:700]}")
            else:
                result_lines.append("Никто не участвовал в разроле.")

            if self.message:
                try:
                    await self.message.edit(
                        embed=self.make_embed("\n".join(result_lines)), view=self
                    )
                    if winner is not None:
                        await self.message.reply(
                            f"🎉 <@{winner}>, вы выиграли **{self.item.bilingual_name}**!",
                            mention_author=False,
                        )
                except (discord.NotFound, discord.Forbidden):
                    pass


class EditLootModal(discord.ui.Modal, title="Исправить список предметов"):
    def __init__(self, preview: "LootPreviewView"):
        super().__init__()
        self.preview = preview
        self.items_input = discord.ui.TextInput(
            label="Один предмет на строку",
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=True,
            default="\n".join(item.display_name for item in preview.items)[:1500],
        )
        self.add_item(self.items_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items = self.preview.manager.catalog.resolve_manual(str(self.items_input))
        if not items:
            await interaction.response.send_message(
                "❌ Список предметов пуст.", ephemeral=True
            )
            return
        self.preview.items = items
        await interaction.response.edit_message(
            embed=self.preview.make_embed(), view=self.preview
        )


class LootPreviewView(discord.ui.View):
    def __init__(
        self,
        manager: "LootManager",
        owner_id: int,
        items: list[LootItem],
        duration: int,
        ocr_text: str = "",
    ):
        super().__init__(timeout=300)
        self.manager = manager
        self.owner_id = owner_id
        self.items = items
        self.duration = duration
        self.ocr_text = ocr_text

    def make_embed(self) -> discord.Embed:
        lines = []
        for index, item in enumerate(self.items, start=1):
            confidence = f" · {item.confidence}%" if item.confidence else " · вручную"
            metadata = " / ".join(
                value for value in (item.rarity, item.tier, item.item_type) if value
            )
            suffix = f" — {metadata}" if metadata else ""
            lines.append(f"**{index}.** {item.bilingual_name}{suffix}{confidence}")
        embed = discord.Embed(
            title="🔎 Проверка распознанных предметов",
            description="\n".join(lines)[:4000],
            color=0xFEE75C,
        )
        embed.add_field(
            name="Настройки",
            value=f"Предметов: **{len(self.items)}** · таймер: **{self.duration} сек.**",
            inline=False,
        )
        embed.set_footer(text="Проверьте список перед запуском разрола")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Эту проверку открыл другой офицер.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Запустить разрол", emoji="🎲", style=discord.ButtonStyle.success)
    async def start_rolls(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=self.make_embed(), view=self)
        count = await self.manager.publish_rolls(
            self.items, interaction.user.id, self.duration
        )
        await interaction.followup.send(
            f"✅ В <#{self.manager.trade_channel_id}> запущено разролов: **{count}**.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Исправить", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_items(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(EditLootModal(self))

    @discord.ui.button(label="Отмена", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Разрол отменён.", embed=None, view=self
        )
        self.stop()


class LootManager:
    def __init__(
        self,
        bot: commands.Bot,
        trade_channel_id: int,
        member_role_id: int,
        officer_role_id: int,
    ):
        self.bot = bot
        self.trade_channel_id = trade_channel_id
        self.member_role_id = member_role_id
        self.officer_role_id = officer_role_id
        self.catalog = ItemCatalog()
        self._initialized = False
        self.catalog_error = ""

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.catalog.load_cache()
        try:
            await self.catalog.refresh()
            self.catalog_error = ""
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            self.catalog_error = str(error)
            print(f"⚠️ Каталог предметов не обновлён: {error}")

    async def scan(self, image_bytes: bytes) -> tuple[list[LootItem], str]:
        if len(image_bytes) > MAX_SCREENSHOT_BYTES:
            raise ValueError("Скриншот больше 15 МБ")
        text = await asyncio.to_thread(extract_text_from_image, image_bytes)
        return self.catalog.match_ocr_text(text), text

    async def publish_rolls(
        self, items: Iterable[LootItem], creator_id: int, duration: int
    ) -> int:
        channel = self.bot.get_channel(self.trade_channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.trade_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("TRADE_CHANNEL_ID не указывает на текстовый канал")
        count = 0
        for item in items:
            view = LootRollView(self, item, creator_id, duration)
            message = await channel.send(embed=view.make_embed(), view=view)
            view.message = message
            count += 1
        return count


def register_loot_commands(
    tree: app_commands.CommandTree,
    manager: LootManager,
    guild_id: int,
) -> None:
    guild = discord.Object(id=guild_id)

    async def validate_officer_channel(interaction: discord.Interaction) -> bool:
        if not _is_officer(interaction.user, manager.officer_role_id):
            await interaction.response.send_message("❌ Только офицеры.", ephemeral=True)
            return False
        if interaction.channel_id != manager.trade_channel_id:
            await interaction.response.send_message(
                f"❌ Используйте эту команду в <#{manager.trade_channel_id}>.",
                ephemeral=True,
            )
            return False
        return True

    @tree.command(
        name="loot_scan",
        description="Распознать несколько предметов со скриншота [офицеры]",
        guild=guild,
    )
    @app_commands.describe(
        screenshot="Скриншот с названиями предметов",
        duration="Время разрола в секундах (15–600)",
    )
    async def loot_scan(
        interaction: discord.Interaction,
        screenshot: discord.Attachment,
        duration: app_commands.Range[int, 15, 600] = 60,
    ) -> None:
        if not await validate_officer_channel(interaction):
            return
        if screenshot.size > MAX_SCREENSHOT_BYTES:
            await interaction.response.send_message(
                "❌ Скриншот больше 15 МБ.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            image_bytes = await screenshot.read()
            items, text = await manager.scan(image_bytes)
        except (ValueError, RuntimeError, discord.HTTPException) as error:
            await interaction.followup.send(f"❌ OCR: {error}", ephemeral=True)
            return
        if not items:
            excerpt = text.strip()[:1200] or "текст не распознан"
            await interaction.followup.send(
                "❌ Не удалось уверенно найти предметы в каталоге. "
                "Используйте `/loot_manual` или сделайте скриншот, где видны названия.\n\n"
                f"Распознано OCR:\n```{excerpt}```",
                ephemeral=True,
            )
            return
        view = LootPreviewView(
            manager, interaction.user.id, items, int(duration), text
        )
        await interaction.followup.send(
            embed=view.make_embed(), view=view, ephemeral=True
        )

    @tree.command(
        name="loot_manual",
        description="Создать несколько разролов из списка предметов [офицеры]",
        guild=guild,
    )
    @app_commands.describe(
        items="Названия через запятую или с новой строки (до 10)",
        duration="Время разрола в секундах (15–600)",
    )
    async def loot_manual(
        interaction: discord.Interaction,
        items: str,
        duration: app_commands.Range[int, 15, 600] = 60,
    ) -> None:
        if not await validate_officer_channel(interaction):
            return
        resolved = manager.catalog.resolve_manual(items)
        if not resolved:
            await interaction.response.send_message(
                "❌ Укажите хотя бы один предмет.", ephemeral=True
            )
            return
        view = LootPreviewView(
            manager, interaction.user.id, resolved, int(duration)
        )
        await interaction.response.send_message(
            embed=view.make_embed(), view=view, ephemeral=True
        )

    @tree.command(
        name="loot_update_items",
        description="Обновить локальную базу предметов Hero Siege [офицеры]",
        guild=guild,
    )
    async def loot_update_items(interaction: discord.Interaction) -> None:
        if not await validate_officer_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await manager.catalog.refresh()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            await interaction.followup.send(
                f"❌ Не удалось обновить каталог: {error}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Каталог обновлён: **{count}** предметов.", ephemeral=True
        )
