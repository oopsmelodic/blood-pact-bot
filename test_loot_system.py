import asyncio
import io
import tempfile
import unittest
from pathlib import Path

import cv2
import discord
import numpy as np
from PIL import Image
from discord.ext import commands

from icon_recognition import ItemIconMatcher

from loot_system import (
    ItemCatalog,
    LootItem,
    LootManager,
    LootRollView,
    normalize_name,
    parse_helper_item_table,
    register_loot_commands,
)


class ItemCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.catalog = ItemCatalog(Path(self.temporary.name) / "items.json")
        self.catalog.items = [
            LootItem(
                item_id="helmet_colossal_avenger",
                name_en="The Colossal Avenger",
                name_ru="Колоссальный Мститель",
                rarity="Heroic",
                tier="SS",
            ),
            LootItem(
                item_id="helmet_harlequin_crest",
                name_en="Harlequinn's Crest",
                name_ru="Герб Арлекина",
                rarity="Satanic",
                tier="S",
            ),
            LootItem(
                item_id="ring_parasite_loop",
                name_en="Parasite Loop",
                name_ru="Цикл паразитов",
                rarity="Heroic",
                tier="SS",
            ),
        ]
        self.catalog._build_index()

    def tearDown(self):
        self.temporary.cleanup()

    def test_normalize_russian_and_apostrophes(self):
        self.assertEqual(normalize_name("  ГЕРБ  АрлЁкина! "), "герб арлекина")
        self.assertEqual(normalize_name("King’s Crown"), "king s crown")

    def test_resolve_manual_fuzzy_and_unknown(self):
        items = self.catalog.resolve_manual(
            "Колосальный Мститель; Harlequinns Crest; Совсем новый предмет"
        )
        self.assertEqual(items[0].item_id, "helmet_colossal_avenger")
        self.assertEqual(items[1].item_id, "helmet_harlequin_crest")
        self.assertTrue(items[2].item_id.startswith("manual_"))

    def test_match_multiple_ocr_lines(self):
        text = """
        КОЛОССАЛЬНЫЙ МСТИТЕЛЬ
        Tier SS
        Герб Арлекина
        Уровень 92
        """
        items = self.catalog.match_ocr_text(text)
        ids = {item.item_id for item in items}
        self.assertIn("helmet_colossal_avenger", ids)
        self.assertIn("helmet_harlequin_crest", ids)

    def test_ocr_ui_noise_does_not_invent_items(self):
        self.assertEqual(
            self.catalog.match_ocr_text("III O0 |||\nSSS 90 FPS\nInventory"),
            [],
        )

    def test_parse_helper_item_table(self):
        payload = """
        <table><tr><th>Id</th><th>En</th><th>Type</th><th>Game ID</th><th>Weapon Type</th></tr>
        <tr><td>helmet_colossal_avenger</td><td>The Colossal Avenger</td>
        <td>0</td><td>1</td><td></td></tr></table>
        """
        parsed = parse_helper_item_table(payload)
        self.assertEqual(parsed["helmet_colossal_avenger"]["game_id"], 1)
        self.assertEqual(parsed["helmet_colossal_avenger"]["game_type"], 0)

    def test_icon_matcher_keeps_two_copies_of_the_same_item(self):
        atlas = np.zeros((32, 32, 4), dtype=np.uint8)
        atlas[:8, :8, :3] = (40, 120, 230)
        atlas[:8, :8, 3] = 255
        atlas[1:7:2, :, :3] = (220, 60, 30)
        atlas_path = Path(self.temporary.name) / "icons.webp"
        cv2.imwrite(str(atlas_path), atlas)

        # Four repeated grid lines establish the native 2x UI scale.
        screenshot = np.full((256, 256, 3), (6, 6, 15), dtype=np.uint8)
        for coordinate in (0, 64, 128, 192, 255):
            screenshot[:, max(0, coordinate - 1):coordinate + 1] = (25, 17, 18)
            screenshot[max(0, coordinate - 1):coordinate + 1, :] = (25, 17, 18)
        reference = cv2.resize(atlas[:8, :8, :3], (16, 16), interpolation=cv2.INTER_NEAREST)
        # OpenCV stores BGR while the PNG encoder below receives RGB.
        screenshot[24:40, 24:40] = reference
        screenshot[88:104, 24:40] = reference
        buffer = io.BytesIO()
        Image.fromarray(cv2.cvtColor(screenshot, cv2.COLOR_BGR2RGB)).save(buffer, "PNG")

        item = LootItem(item_id="two_copies", name_en="Two Copies", icon=(0, 0, 8, 8))
        matches = ItemIconMatcher(atlas_path, [item]).match(buffer.getvalue())
        self.assertEqual([match.item.item_id for match in matches], ["two_copies", "two_copies"])


class DiscordLootTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_and_roll_buttons_are_registered(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        manager = LootManager(
            bot=bot,
            trade_channel_id=1544351122028896267,
            member_role_id=2,
            officer_role_id=3,
        )
        register_loot_commands(bot.tree, manager, guild_id=1)
        names = {
            command.name
            for command in bot.tree.get_commands(guild=discord.Object(id=1))
        }
        self.assertEqual(
            names, {"loot_scan", "loot_manual", "loot_update_items"}
        )

        view = LootRollView(manager, LootItem.manual("Test Item"), 1, 60)
        self.assertEqual(len(view.children), 3)
        self.assertEqual(
            [child.label for child in view.children],
            ["Нужно", "Пас", "Завершить"],
        )
        view.stop()
        await bot.close()

    async def test_roll_embed_contains_uploaded_screenshot(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        manager = LootManager(bot, 1, 2, 3)
        image_url = "https://cdn.discordapp.com/attachments/example/loot.png"
        view = LootRollView(
            manager, LootItem.manual("Test Item"), 1, 60, image_url=image_url
        )

        self.assertEqual(view.make_embed().image.url, image_url)

        view.stop()
        await bot.close()

    async def test_only_one_roll_batch_can_be_active(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        manager = LootManager(bot, 1, 2, 3)
        blocker = asyncio.Event()
        manager._roll_batch_task = asyncio.create_task(blocker.wait())

        with self.assertRaisesRegex(RuntimeError, "Уже идёт другой разрол"):
            await manager.publish_rolls([LootItem.manual("Second Item")], 1, 60)

        blocker.set()
        await manager._roll_batch_task
        await bot.close()


if __name__ == "__main__":
    unittest.main()
