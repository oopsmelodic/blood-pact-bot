import tempfile
import unittest
from pathlib import Path

import discord
from discord.ext import commands

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

    def test_parse_helper_item_table(self):
        payload = """
        <table><tr><th>Id</th><th>En</th><th>Type</th><th>Game ID</th><th>Weapon Type</th></tr>
        <tr><td>helmet_colossal_avenger</td><td>The Colossal Avenger</td>
        <td>0</td><td>1</td><td></td></tr></table>
        """
        parsed = parse_helper_item_table(payload)
        self.assertEqual(parsed["helmet_colossal_avenger"]["game_id"], 1)
        self.assertEqual(parsed["helmet_colossal_avenger"]["game_type"], 0)


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
        self.assertEqual(len(view.children), 4)
        view.stop()
        await bot.close()


if __name__ == "__main__":
    unittest.main()
