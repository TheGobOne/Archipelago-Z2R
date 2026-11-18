import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from .. import Client
from ..Items import ITEM_NAME_TO_ID
from ..Locations import LOCATION_NAME_TO_ID


class FakeRam:
    def __init__(self, values: dict[int, int]) -> None:
        self.values = values
        self.writes: list[tuple[int, list[int], str]] = []

    async def read(self, _ctx, reads):
        return [bytes(self.values.get(address + i, 0) for i in range(size)) for address, size, _domain in reads]

    async def guarded_write(self, _ctx, writes, _guards):
        self.writes += writes
        return True


class TestContainers(unittest.TestCase):
    def run_check(self, received: list[str], ram: dict[int, int]) -> FakeRam:
        fake = FakeRam(ram)
        names = {ITEM_NAME_TO_ID[name]: name for name in received}
        ctx = SimpleNamespace(
            bizhawk_ctx=None, game="Z2R",
            items_received=[SimpleNamespace(item=ITEM_NAME_TO_ID[name]) for name in received],
            item_names=SimpleNamespace(lookup_in_game=lambda item, game: names[item]),
            slot_data={"start_magic_containers": 4, "start_heart_containers": 4,
                       "new_kasuto_basement_requirement": 8})
        client = Client.Z2RClient()
        with mock.patch.object(Client.bizhawk, "read", fake.read), \
                mock.patch.object(Client.bizhawk, "guarded_write", fake.guarded_write):
            asyncio.run(client.received_items_check(ctx, b""))
        return fake

    def test_magic_container_refills_magic(self) -> None:
        fake = self.run_check(["Magic Container"], {0x0783: 4})
        written = {address: value for address, value, _ in fake.writes}
        self.assertEqual(written[0x0783], [5])
        self.assertEqual(written[Client.MAGIC_REFILL_ADDRESS], [5 << 4])

    def test_heart_container_refills_life(self) -> None:
        fake = self.run_check(["Heart Container"], {0x0784: 4, 0x0783: 4})
        written = {address: value for address, value, _ in fake.writes}
        self.assertEqual(written[0x0784], [5])
        self.assertEqual(written[Client.LIFE_REFILL_ADDRESS], [0xF0])
        self.assertNotIn(Client.MAGIC_REFILL_ADDRESS, written)

    def test_no_refill_when_nothing_was_added(self) -> None:
        fake = self.run_check(["Magic Container", "Heart Container"], {0x0783: 5, 0x0784: 5})
        self.assertEqual(fake.writes, [])


class FakeRom:
    """A "PRG ROM" with text tables in bank 3 and an AP sign placeholder for Mido (west) and Old Kasuto (east)."""
    WEST_TABLE, EAST_TABLE = 0xA000, 0xA100  # CPU addresses in bank 3
    MIDO_SIGN, OLD_KASUTO_SIGN = 0xA200, 0xA300

    def __init__(self, mido_sign: bytes | None = None) -> None:
        self.data = bytearray(0x20000)
        self.put_word(Client.TEXT_TABLE_POINTERS, self.WEST_TABLE)
        self.put_word(Client.TEXT_TABLE_POINTERS + 2, self.EAST_TABLE)
        self.put_word(self.prg(self.WEST_TABLE) + 41 * 2, self.MIDO_SIGN)
        self.put_word(self.prg(self.EAST_TABLE) + (94 - 52) * 2, self.OLD_KASUTO_SIGN)
        self.put(self.MIDO_SIGN, mido_sign or Client.sign_bytes("AP$ITEM", 4))
        self.put(self.OLD_KASUTO_SIGN, Client.sign_bytes("AP$ITEM", 8))
        self.writes: list[tuple[int, bytes, str]] = []

    @staticmethod
    def prg(cpu: int) -> int:
        return Client.BANK3_PRG + cpu - 0x8000

    def put_word(self, offset: int, value: int) -> None:
        self.data[offset:offset + 2] = value.to_bytes(2, "little")

    def put(self, cpu: int, data: bytes) -> None:
        self.data[self.prg(cpu):self.prg(cpu) + len(data)] = data

    def sign(self, cpu: int) -> bytes:
        return bytes(self.data[self.prg(cpu):self.prg(cpu) + Client.SIGN_SIZE])

    async def read(self, _ctx, reads):
        return [bytes(self.data[address:address + size]) for address, size, _domain in reads]

    async def write(self, _ctx, writes):
        for address, data, _domain in writes:
            self.data[address:address + len(data)] = data
        self.writes += writes


class TestTownSigns(unittest.TestCase):
    MIDO = "WEST_TOWN_MIDO:wizard"
    OLD_KASUTO = "EAST_TOWN_OLD_KASUTO:wizard"

    def make_ctx(self, items: dict[str, tuple[int, str]]) -> SimpleNamespace:
        """items: location name -> (player, item name)"""
        ids = {name: ITEM_NAME_TO_ID.get(item, 999) for name, (_player, item) in items.items()}
        names = {ITEM_NAME_TO_ID[item]: item for _player, item in items.values() if item in ITEM_NAME_TO_ID}
        sent = []

        async def send_msgs(msgs):
            sent.extend(msgs)

        return SimpleNamespace(
            bizhawk_ctx=None, slot=1, sent=sent, send_msgs=send_msgs,
            # Mido is town type 4 (bit 0x01), Old Kasuto town type 8 (bit 0x10)
            slot_data={"locations": {self.MIDO: {"address": 0x077A, "mask": 0x01, "collected": 1},
                                     self.OLD_KASUTO: {"address": 0x077A, "mask": 0x10, "collected": 1},
                                     "WEST_GRASS": {"address": 0x0600, "mask": 0x01, "collected": 0}}},
            locations_info={LOCATION_NAME_TO_ID[name]: SimpleNamespace(player=player, item=ids[name])
                            for name, (player, _item) in items.items()},
            item_names=SimpleNamespace(lookup_in_game=lambda item, game: names[item]))

    def run_check(self, ctx, rom: FakeRom, client: Client.Z2RClient | None = None) -> Client.Z2RClient:
        client = client or Client.Z2RClient()
        with mock.patch.object(Client.bizhawk, "read", rom.read), mock.patch.object(Client.bizhawk, "write", rom.write):
            asyncio.run(client.sign_check(ctx))
        return client

    def test_own_item_is_named_and_other_players_item_is_generic(self) -> None:
        rom = FakeRom()
        ctx = self.make_ctx({self.MIDO: (1, "Hammer"), self.OLD_KASUTO: (2, "Hammer")})
        self.run_check(ctx, rom)
        self.assertEqual(rom.sign(FakeRom.MIDO_SIGN), Client.sign_bytes("HAMMER", 4))
        self.assertEqual(rom.sign(FakeRom.OLD_KASUTO_SIGN), Client.sign_bytes("AP$ITEM", 8))
        self.assertEqual(len(rom.writes), 1)  # Old Kasuto already reads AP ITEM TOWN

    def test_longest_name_fits_and_keeps_town_byte(self) -> None:
        rom = FakeRom()
        self.run_check(self.make_ctx({self.MIDO: (1, "Magic Container"), self.OLD_KASUTO: (1, "Thunder Spell")}), rom)
        mido = rom.sign(FakeRom.MIDO_SIGN)
        self.assertEqual(mido, Client.encode_text("MAGIC$CONTAINER$TOWN") + bytes([4]))
        self.assertEqual(rom.sign(FakeRom.OLD_KASUTO_SIGN)[-1], 8)

    def test_scouts_first_then_writes(self) -> None:
        rom = FakeRom()
        ctx = self.make_ctx({})
        client = self.run_check(ctx, rom)
        self.assertEqual(ctx.sent, [{"cmd": "LocationScouts", "create_as_hint": 0,
                                     "locations": [LOCATION_NAME_TO_ID[self.MIDO],
                                                   LOCATION_NAME_TO_ID[self.OLD_KASUTO]]}])
        self.assertEqual(rom.writes, [])
        self.run_check(ctx, rom, client)
        self.assertEqual(len(ctx.sent), 1)  # does not scout again while waiting for the reply

    def test_rom_without_placeholder_is_left_alone(self) -> None:
        old_sign = Client.encode_text("JUMP$SPELL$TOWN").ljust(Client.SIGN_SIZE, b"\x00")
        rom = FakeRom(mido_sign=old_sign)
        self.run_check(self.make_ctx({self.MIDO: (1, "Hammer"), self.OLD_KASUTO: (1, "Raft")}), rom)
        self.assertEqual(rom.sign(FakeRom.MIDO_SIGN), old_sign)
        self.assertEqual(rom.sign(FakeRom.OLD_KASUTO_SIGN), Client.sign_bytes("RAFT", 8))

    def test_signs_rewritten_after_rom_reload(self) -> None:
        rom = FakeRom()
        ctx = self.make_ctx({self.MIDO: (1, "Hammer"), self.OLD_KASUTO: (1, "Raft")})
        client = self.run_check(ctx, rom)
        rom.put(FakeRom.MIDO_SIGN, Client.sign_bytes("AP$ITEM", 4))  # the ROM was reloaded
        client.next_sign_check = 0
        self.run_check(ctx, rom, client)
        self.assertEqual(rom.sign(FakeRom.MIDO_SIGN), Client.sign_bytes("HAMMER", 4))
