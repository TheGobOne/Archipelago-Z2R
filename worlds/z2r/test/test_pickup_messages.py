import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from .. import Client


class PickupMessageTextTest(unittest.TestCase):
    def test_own_item(self) -> None:
        self.assertEqual(Client.pickup_message("Hammer", "Gob-Z2R", True), "GOT HAMMER")

    def test_other_players_item(self) -> None:
        self.assertEqual(Client.pickup_message("Silver Arrow", "Tetra", False), "SENT SILVER ARROW TO TETRA")

    def test_long_names_are_cut_to_fit(self) -> None:
        text = Client.pickup_message("Progressive Ultra Mega Upgrade", "A Very Long Player", False)
        self.assertLessEqual(len(text), Client.HUD_MESSAGE_WIDTH)
        self.assertEqual(text, "SENT PROGRESS TO A VERY LONG")

    def test_characters_the_font_lacks_become_spaces(self) -> None:
        self.assertEqual(Client.pickup_message("Erdrick's Token", "Gob", False), "SENT ERDRICK S TOKEN TO GOB")

    def test_buffer_entry_layout(self) -> None:
        data = Client.hud_message_bytes("GOT HAMMER")
        self.assertEqual(data[:3], bytes([0x20, 0x22, Client.HUD_MESSAGE_WIDTH]))
        self.assertEqual(len(data), 3 + Client.HUD_MESSAGE_WIDTH + 1)
        self.assertEqual(data[-1], 0xFF)
        self.assertEqual(data[3], ord("G") + 0x99)
        self.assertEqual(data[-2], Client.BLANK_TILE)


class HudMessageCheckTest(unittest.TestCase):
    def make_ctx(self) -> SimpleNamespace:
        sent = []

        async def send_msgs(msgs):
            sent.extend(msgs)

        return SimpleNamespace(bizhawk_ctx=None, missing_locations={5, 3}, sent=sent, send_msgs=send_msgs)

    def run_check(self, client, ctx, mode, now, buffer_free=True) -> list:
        writes = []

        async def guarded_write(_ctx, w, _guards):
            if buffer_free:
                writes.extend(w)
            return buffer_free

        with mock.patch.object(Client.bizhawk, "guarded_write", guarded_write), \
                mock.patch.object(Client.time, "monotonic", lambda: now):
            asyncio.run(client.hud_message_check(ctx, mode))
        return writes

    def test_shows_then_clears_after_five_seconds(self) -> None:
        client, ctx = Client.Z2RClient(), self.make_ctx()
        client.guards = []
        client.pickup_messages = ["GOT HAMMER"]
        shown = self.run_check(client, ctx, Client.HUD_MODE, 100.0)
        self.assertEqual(shown[0][1], Client.hud_message_bytes("GOT HAMMER"))
        self.assertEqual(self.run_check(client, ctx, Client.HUD_MODE, 104.9), [])
        cleared = self.run_check(client, ctx, Client.HUD_MODE, 105.0)
        self.assertEqual(cleared[0][1], Client.hud_message_bytes(""))
        self.assertEqual(client.pickup_messages, [])

    def test_scouts_every_missing_location_once(self) -> None:
        client, ctx = Client.Z2RClient(), self.make_ctx()
        client.guards = []
        self.run_check(client, ctx, Client.HUD_MODE, 1.0)
        self.run_check(client, ctx, Client.HUD_MODE, 2.0)
        self.assertEqual(ctx.sent, [{"cmd": "LocationScouts", "locations": [3, 5], "create_as_hint": 0}])

    def test_waits_outside_side_view_and_for_a_free_buffer(self) -> None:
        client, ctx = Client.Z2RClient(), self.make_ctx()
        client.guards = []
        client.pickup_messages = ["GOT HAMMER"]
        self.assertEqual(self.run_check(client, ctx, 0x05, 1.0), [])
        self.assertEqual(self.run_check(client, ctx, Client.HUD_MODE, 1.0, buffer_free=False), [])
        self.assertEqual(client.pickup_messages, ["GOT HAMMER"])

    def test_pickup_text_needs_the_scouted_item(self) -> None:
        client = Client.Z2RClient()
        ctx = SimpleNamespace(slot=1, locations_info={7: SimpleNamespace(item=10, player=2)},
                              item_names=SimpleNamespace(lookup_in_slot=lambda item, player: "Hookshot"),
                              player_names={2: "Tetra"})
        self.assertEqual(client.pickup_text(ctx, 7), "SENT HOOKSHOT TO TETRA")
        self.assertIsNone(client.pickup_text(ctx, 8))
