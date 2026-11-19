"""Real item sprites for this slot's own items.

Z2R draws every AP item with one 8x16 sprite: the 1-up's tiles, which the Z2R app replaces with half of the
Archipelago logo. The ROM is built before the multiworld exists, so it can't know what each location holds. The client
does: when Link is in a side view room whose item is one of this slot's own Z2R items, it copies that item's tiles over
the AP tiles in every CHR bank, and puts the logo back in any other room.

Which room holds which location comes from the item presence flags in slot data: the game keeps one bit per item, at
table base + map / 2, with the bit picked by the map's parity and the item's page (ApManifestBuilder in the app).
"""
from typing import NamedTuple

CHR_DOMAIN = "CHR VROM"
BANK_SIZE = 0x2000
AP_TILE_OFFSET = 0x0A80  # the 1-up's tiles in each 8 KB CHR bank (CHR.ONEUP is 0x8A80, in bank 4)
REFERENCE_BANK = 4
TILE_BYTES = 32  # an 8x16 sprite: the top 8x8 tile, then the bottom one
# Half of the Archipelago logo as the Z2R app draws it (ArchipelagoLogo.Logo), for when the ROM's copy was overwritten
AP_LOGO = bytes.fromhex("0002007dfdffc6380307037dfcfec60054b4ab27570707032848535727070703")

# RAM (ram.inc in the Z2R app)
MAP_NUMBER = 0x0561
REGION_NUMBER = 0x0706  # 0 west, 1 Death Mountain, 2 east, 3 Maze Island
WORLD_NUMBER = 0x0707  # 0 caves, 1 west towns, 2 east towns, 3 palaces 1/2/5, 4 palaces 3/4/6, 5 Great Palace
LINK_PAGE = 0x003B  # which page of the room Link is in

CAVE_TABLES = {0: 0x0600, 1: 0x0620, 2: 0x0640, 3: 0x0620}
WORLD_TABLES = {1: 0x0660, 2: 0x0660, 3: 0x0680, 4: 0x06A0}


class Tile(NamedTuple):
    address: int  # CHR address of an 8x16 tile pair (CHR.cs in the app)
    x: int = 0  # 0 left, 1 right half of the 16 pixel wide item
    flip: bool = False  # drawn mirrored


# How the game draws each item (CHR.COLLECTABLE_TILES in the app). One tile is 8 pixels wide like the AP item; a
# 16 pixel item is squeezed to 8.
ITEM_TILES: dict[str, list[Tile]] = {
    "Candle": [Tile(0x88C0)],
    "Glove": [Tile(0x88E0)],
    "Raft": [Tile(0x8900)],
    "Boots": [Tile(0x8920)],
    "Flute": [Tile(0x8940)],
    "Cross": [Tile(0x8960)],
    "Hammer": [Tile(0x8980)],
    "Magic Key": [Tile(0x89A0)],
    "Small Key": [Tile(0x8660)],
    "Small P-Bag": [Tile(0x8720)],
    "Large P-Bag": [Tile(0x8720)],
    "Blue Magic Jar": [Tile(0x88A0)],
    "Red Magic Jar": [Tile(0x88A0)],
    "Magic Container": [Tile(0x1820), Tile(0x1820, 1, True)],
    "Heart Container": [Tile(0x1800), Tile(0x1800, 1, True)],
    "Trophy": [Tile(0x32E0), Tile(0x32E0, 1, True)],
    "Medicine": [Tile(0x3300), Tile(0x3300, 1, True)],
    "Child": [Tile(0x5300), Tile(0x5300, 1, True)],
    "Mirror": [Tile(0x1A260)],
    "Bagu's Note": [Tile(0x1A280)],
    "Water": [Tile(0x1A2C0)],
    "Upstab": [Tile(0x1A200)],
    "Downstab": [Tile(0x1A220)],
    "Shield Spell": [Tile(0x1A3A0), Tile(0x1A3A0, 1, True)],
    "Jump Spell": [Tile(0x1A140), Tile(0x1A120, 1)],
    "Life Spell": [Tile(0x1A160), Tile(0x1A180, 1, True)],
    "Fairy Spell": [Tile(0x86A0)],
    "Fire Spell": [Tile(0x0520)],
    "Reflect Spell": [Tile(0x1A1A0), Tile(0x1A1C0, 1, True)],
    "Spell Spell": [Tile(0x1A300), Tile(0x1A300, 1, True)],
    "Thunder Spell": [Tile(0x1A360), Tile(0x1A380, 1, True)],
    "Dash Spell": [Tile(0x1A0E0), Tile(0x1A140, 1)],
}

SOURCE_ADDRESSES = sorted({tile.address for tiles in ITEM_TILES.values() for tile in tiles})


def decode(data: bytes) -> list[list[int]]:
    """An 8x16 tile pair as 16 rows of 8 pixel values (0-3)."""
    rows = []
    for half in (0, 16):
        for y in range(8):
            low, high = data[half + y], data[half + 8 + y]
            rows.append([((low >> (7 - x)) & 1) | (((high >> (7 - x)) & 1) << 1) for x in range(8)])
    return rows


def encode(rows: list[list[int]]) -> bytes:
    """16 rows of 8 pixel values back into an 8x16 tile pair."""
    out = bytearray(TILE_BYTES)
    for y, row in enumerate(rows):
        half, line = (0, y) if y < 8 else (16, y - 8)
        for x, value in enumerate(row):
            out[half + line] |= (value & 1) << (7 - x)
            out[half + 8 + line] |= (value >> 1) << (7 - x)
    return bytes(out)


def item_sprite(name: str, sources: dict[int, bytes]) -> bytes | None:
    """The AP tiles drawn as this Z2R item, from the ROM's own tiles (so custom item sprites carry over), or None if it
    isn't a Z2R item with a sprite. A 16 pixel wide item is squeezed to 8, keeping a drawn pixel of each pair."""
    tiles = ITEM_TILES.get(name)
    if not tiles or any(tile.address not in sources for tile in tiles):
        return None
    if len(tiles) == 1 and not tiles[0].flip:
        return bytes(sources[tiles[0].address])
    wide = [[0] * 16 for _ in range(16)]
    for tile in tiles:
        for y, row in enumerate(decode(sources[tile.address])):
            for x, value in enumerate(reversed(row) if tile.flip else row):
                wide[y][tile.x * 8 + x] = value
    return encode([[row[2 * x] or row[2 * x + 1] for x in range(8)] for row in wide])


def flag_table(world: int, region: int) -> int | None:
    """The item presence table of the side view Link is in, or None where items are never AP items."""
    if world == 0:
        return CAVE_TABLES.get(region)
    return WORLD_TABLES.get(world)


def page_mask(map_number: int, page: int) -> int:
    return (0x80 if map_number % 2 == 0 else 0x08) >> page


def room_locations(locations: dict[str, dict], world: int, region: int, map_number: int, link_page: int) -> list[str]:
    """Names of the item presence locations in this room, the one nearest Link's page first."""
    table = flag_table(world, region)
    if table is None:
        return []
    address = table + (map_number >> 1)
    found = []
    for name, data in locations.items():
        if data.get("address") != address or data.get("collected", 1) != 0:
            continue
        for page in range(4):
            if data.get("mask") == page_mask(map_number, page):
                found.append((abs(page - link_page), page, name))
    return [name for _distance, _page, name in sorted(found)]
