import logging
import random
import time
from typing import NamedTuple, TYPE_CHECKING

from NetUtils import ClientStatus

import worlds._bizhawk as bizhawk
from worlds._bizhawk.client import BizHawkClient
from .Items import CONTAINER_ITEMS, ITEM_TABLE
from .Locations import LOCATION_NAME_TO_ID
from .place_names import PLACE_NAMES

if TYPE_CHECKING:
    from worlds._bizhawk.context import BizHawkClientContext

logger = logging.getLogger("Client")

# The Z2R app writes a 64 byte signature at CPU $FCB0 in bank 7. That is file offset 0x1FCC0, or 0x1FCB0 in
# BizHawk's "PRG ROM" domain, which does not include the 16 byte iNES header.
# Layout: "Z2AP", version, 3 reserved, 16 byte slot name, 32 byte seed text, 4 byte little endian seed hash
SIGNATURE_ADDRESS = 0x1FCB0
SIGNATURE_SIZE = 0x40
SIGNATURE_MAGIC = b"Z2AP"
SIGNATURE_VERSION = 1

# NES CPU RAM, used for everything below
RAM = "RAM"
# Item presence tables, wizard flags and misc flags all live in this range
FLAGS_START = 0x0600
FLAGS_SIZE = 0x0200
# Nonzero bit 7 once the ending sequence has started (GameComplete in the Z2R app's ram.inc)
GAME_COMPLETE_ADDRESS = 0x0128
GAME_COMPLETE_MASK = 0x80


class Consumable(NamedTuple):
    address: int  # RAM address of the count
    maximum: int  # the most the client will raise the count to
    amount: int = 1  # added per item received
    scale_address: int | None = None  # if set, the amount is multiplied by the byte at this address
    size: int = 1  # bytes in the count, stored big endian (high byte at the lowest address)
    xp_table_index: int | None = None  # if set, the amount is the P-bag XP value for this bag size (0 = small)


# Items that are spent in game, so they are added one at a time instead of being set. The number already given is
# kept in the server's data storage so a reconnect doesn't give them again.
CONSUMABLE_ITEMS: dict[str, Consumable] = {
    "Small Key": Consumable(0x0793, 64),
    "1-Up": Consumable(0x0700, 99),
    # $070c is the magic the game still has to add to the player's magic, which it pays out over the next frames and
    # clamps to the maximum. The jar pickup routine at $E84B adds to it: 16 for a blue jar, 16 per magic container
    # for a red jar ($0783 is the number of magic containers).
    # $0755/$0756 (high, low) is the experience the game still has to award, like the magic buffer above.
    # P-bags give an amount from a 4 entry table in the ROM (one byte per bag size), which the Z2R app can shuffle.
    "Small P-Bag": Consumable(0x0755, 0xFFFF, size=2, xp_table_index=0),
    "Large P-Bag": Consumable(0x0755, 0xFFFF, size=2, xp_table_index=2),
    "Blue Magic Jar": Consumable(0x070C, 0xFF, 16),
    "Red Magic Jar": Consumable(0x070C, 0xFF, 16, scale_address=0x0783),
}
# CPU $E7F0 in the ROM's fixed bank: one byte per P-bag size, an index into XP_VALUES
PBAG_XP_TABLE_ADDRESS = 0x1E7F0  # in the "PRG ROM" domain
# The experience each table index stands for (XP_VALUES in the Z2R app's DropRandomizer.cs)
XP_VALUES = [0, 2, 3, 5, 10, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000]
# The title screen hash: 6 characters at file offset 0x17C2C, each followed by a spacer tile (so 12 bytes). A tile is
# 0xD0 + a 5 bit value, with 0xD0-0xD9 being digits and 0xDA onwards letters.
HASH_ADDRESS = 0x17C1C  # in the "PRG ROM" domain
HASH_LENGTH = 6
# The basement under New Kasuto opens once the player has enough magic containers. The game sets this bit itself when
# a container is picked up, which AP items don't do. HaveBasementAccess in ram.inc.
BASEMENT_ACCESS_ADDRESS = 0x079D
BASEMENT_ACCESS_MASK = 0x08
# Raising a container count also refills the matching bar. The game does it by putting an amount in a buffer it pays
# out over the following frames, with the normal animation and sound: $070C for magic (16 per magic container) and
# $070D for life (0xF0, a full refill).
MAGIC_REFILL_ADDRESS = 0x070C
LIFE_REFILL_ADDRESS = 0x070D
LIFE_REFILL_AMOUNT = 0xF0
# The game only counts as "in control of Link" when the game event is the main loop and the game mode has settled
# (GameMode == GameModeCurrent) on the overworld or a side view. Item presence bits read as "collected" and the flag
# area is meaningless on the title screen and file select, so nothing is read or written otherwise.
GAME_EVENT_ADDRESS = 0x076C
GAME_EVENT_MAIN_LOOP = 1
GAME_EVENT_ENDING = (3, 4)  # waking up Zelda, rolling the credits
GAME_MODE_ADDRESS = 0x0736  # followed by GameModeCurrent at 0x0737
GAME_MODES_IN_CONTROL = (0x05, 0x0B)  # overworld, side view (towns, palaces, caves, encounters)

# Town signs. A wizard holding an AP item gets a sign reading "AP ITEM TOWN", padded with end bytes to SIGN_SIZE
# bytes, with the town type in the last byte (CustomTexts.ArchipelagoSignPlaceholder in the Z2R app). Once the
# multiworld is known, the client rewrites it in the "PRG ROM" domain with the item's name, or leaves "AP ITEM" for
# another player's item. The game reads sign text from the ROM each time a sign is shown.
SIGN_SIZE = 22
SIGN_RECHECK_SECONDS = 5  # rewrite the signs this often in case the ROM was reloaded
SCOUT_RETRY_SECONDS = 10  # ask for the items again if no reply came (a reconnect forgets them)
# The text tables are placed anywhere in bank 3, so they are found through their pointers at CPU $B423 (west) and
# $B425 (east) in bank 3. Bank 3 starts at 0xC000 in "PRG ROM" and is mapped at CPU $8000.
TEXT_TABLE_POINTERS = 0xF423
BANK3_PRG = 0xC000
WEST_TEXT_COUNT = 52
# The text index of each town's sign, by town type (CustomTexts.TOWN_SIGN_INDEXES)
TOWN_SIGN_INDEXES = {1: 11, 2: 20, 3: 29, 4: 41, 5: 62, 6: 76, 7: 86, 8: 94}
# Wizard locations use flag address WIZARD_FLAGS, with one bit per town type, Rauru first
WIZARD_FLAGS = 0x077A
TOWN_BITS = [0x08, 0x04, 0x02, 0x01, 0x80, 0x40, 0x20, 0x10]
# Sign text for each Z2R item ($ starts a new line), matching Collectable.EnglishText in the app
SIGN_NAMES = {
    "Candle": "CANDLE", "Glove": "GLOVE", "Raft": "RAFT", "Boots": "BOOTS", "Flute": "FLUTE", "Cross": "CROSS",
    "Hammer": "HAMMER", "Magic Key": "MAGIC KEY", "Small Key": "SMALL KEY", "Small P-Bag": "P BAG",
    "Large P-Bag": "P BAG", "Magic Container": "MAGIC$CONTAINER", "Heart Container": "HEART$CONTAINER",
    "Blue Magic Jar": "BLUE JAR", "Red Magic Jar": "RED JAR", "1-Up": "LINK DOLL", "Child": "CHILD",
    "Trophy": "TROPHY", "Medicine": "MEDICINE", "Upstab": "UP STAB", "Downstab": "DOWN STAB", "Mirror": "MIRROR",
    "Bagu's Note": "BAGUS$LETTER", "Water": "WATER", "Shield Spell": "SHIELD$SPELL", "Jump Spell": "JUMP$SPELL",
    "Life Spell": "LIFE$SPELL", "Fairy Spell": "FAIRY$SPELL", "Fire Spell": "FIRE$SPELL", "Dash Spell": "DASH$SPELL",
    "Reflect Spell": "REFLECT$SPELL", "Spell Spell": "SPELL$SPELL", "Thunder Spell": "THUNDER$SPELL",
}
OTHER_ITEM_SIGN = "AP$ITEM"


def encode_text(text: str) -> bytes:
    """Encode text the way the Z2R app's Util.ToGameText does, with the end byte."""
    out = bytearray()
    for c in text.upper():
        if "0" <= c <= "9":
            out.append(ord(c) + 0xA0)
        elif "A" <= c <= "Z":
            out.append(ord(c) + 0x99)
        elif c in TEXT_SYMBOLS:
            out.append(TEXT_SYMBOLS[c])
    out.append(0xFF)
    return bytes(out)


TEXT_SYMBOLS = {".": 0xCF, "/": 0xCE, ",": 0x9C, "!": 0x36, "?": 0x34, "*": 0x32, " ": 0xF4, "$": 0xFD, "-": 0xF6,
                "_": 0xC5}


def sign_bytes(text: str, town: int) -> bytes | None:
    """The SIGN_SIZE bytes of a sign reading text for the given town type, or None if it doesn't fit."""
    encoded = encode_text(text)
    if len(encoded) > SIGN_SIZE - 1:
        return None
    return encoded + b"\xFF" * (SIGN_SIZE - 1 - len(encoded)) + bytes([town])


def legacy_sign_bytes(name: str, town: int) -> bytes:
    """A sign in the older "<NAME> TOWN" form: the Z2R app's placeholder ("AP ITEM TOWN") and what earlier
    clients wrote."""
    text = encode_text(name + "$TOWN")
    return text + b"\xFF" * (SIGN_SIZE - 1 - len(text)) + bytes([town])


SIGN_LINE_LENGTH = 11
SIGN_MAX_CHARS = SIGN_SIZE - 2  # text characters that fit before the end byte and the town byte


# Pickup messages: when Link picks up an AP item, the blank HUD row above MAGIC and LIFE shows what it was for
# HUD_MESSAGE_SECONDS, written through the game's PPU buffer (address, length, tiles, then $FF), so Link keeps moving.
HUD_MODE = 0x0B  # side view, the only mode with the HUD
PPU_BUFFER = 0x0302  # PpuAddrHi; $FF when the buffer is empty
HUD_MESSAGE_ADDRESS = 0x2022  # nametable row 1, column 2
HUD_MESSAGE_WIDTH = 28
HUD_MESSAGE_SECONDS = 5.0
HUD_MESSAGE_SECONDS_BUSY = 2.5  # each message's time when more are waiting
ERROR_HUD_SECONDS = 8.0  # how often an error is put back in the HUD row
HUD_MESSAGE_STALE = 10.0  # a message older than this is dropped: it no longer matches what just happened
BLANK_TILE = 0xF4
PPU_BUFFER_LENGTH = 0x0301  # index of the buffer's $FF end; the game appends its own entries there
# The HUD's tiles exist in every CHR bank only for letters, digits, - and /; other symbols are dialog-font only
HUD_CHARACTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/ ")


def hud_message_bytes(text: str) -> bytes:
    """The PPU buffer entry that writes text (cut and padded to HUD_MESSAGE_WIDTH) into the HUD message row."""
    shown = "".join(c if c in HUD_CHARACTERS else " " for c in text.upper())
    tiles = encode_text(shown)[:-1][:HUD_MESSAGE_WIDTH]
    tiles += bytes([BLANK_TILE]) * (HUD_MESSAGE_WIDTH - len(tiles))
    return bytes([HUD_MESSAGE_ADDRESS >> 8, HUD_MESSAGE_ADDRESS & 0xFF, HUD_MESSAGE_WIDTH]) + tiles + bytes([0xFF])


def _shown(text: str) -> str:
    """Text as the game can show it: upper case, characters the font has, single spaces."""
    kept = "".join(c if "A" <= c <= "Z" or "0" <= c <= "9" or c in ".,!?-/" else " "
                   for c in text.upper().replace("'", ""))
    return " ".join(kept.split())


def _fit_item(item_name: str, room: int) -> str:
    """The item name in at most `room` characters: the full name, else its sign short form (FIGHTER'S RING ->
    F-RING), else the name cut at the last whole word that fits (cut mid-word only if one word is too long)."""
    for candidate in (item_name, SIGN_SHORT_FORMS.get(item_name.upper(), "")):
        shown = _shown(candidate)
        if shown and len(shown) <= room:
            return shown
    shown = _shown(item_name)
    head = shown[:room + 1]
    if " " in head:  # cut at the last whole word that fits
        return head.rsplit(" ", 1)[0].rstrip()
    return "AP ITEM"  # even the first word is too long: a fragment would only confuse


def pickup_message(item_name: str, player_name: str, own: bool) -> str:
    """"GOT <item>" for an item of this slot, else "SENT <item> TO <player>", with the player cut at 11 characters
    and the item shortened so the whole message fits HUD_MESSAGE_WIDTH."""
    if own:
        return f"GOT YOUR {_fit_item(item_name, HUD_MESSAGE_WIDTH - len('GOT YOUR '))}"
    player = short_player_name(player_name) or "ANOTHER PLAYER"
    room = HUD_MESSAGE_WIDTH - len("SENT  TO ") - len(player)
    return f"SENT {_fit_item(item_name, room)} TO {player}"


def received_message(item_name: str, player_name: str) -> str:
    """"GOT <item> FROM <player>" for an item of this slot that another player sent, fitted to HUD_MESSAGE_WIDTH."""
    player = short_player_name(player_name) or "ANOTHER PLAYER"
    room = HUD_MESSAGE_WIDTH - len("GOT  FROM ") - len(player)
    return f"GOT {_fit_item(item_name, room)} FROM {player}"


# Item dialogs: the Z2R app reserves ITEM_TEXT_SIZE bytes for the dialog a town location shows when it gives its item,
# "AP ITEM" padded with end bytes, with 0xC0 + the dialog's number last (CustomTexts.ArchipelagoItemTextPlaceholder)
ITEM_TEXT_SIZE = 49
ITEM_TEXT_BASE = 0xC0
ITEM_TEXT_LINE = 11


def item_text_bytes(text: str, number: int) -> bytes | None:
    """The ITEM_TEXT_SIZE bytes of an item dialog, or None if the text is too long to fit."""
    encoded = encode_text(text)
    if len(encoded) > ITEM_TEXT_SIZE - 1:
        return None
    padding = bytes([0xFF]) * (ITEM_TEXT_SIZE - 1 - len(encoded))
    return encoded + padding + bytes([ITEM_TEXT_BASE + number])


def item_dialog_text(item_name: str, player_name: str, own: bool, own_sign_name: str | None = None) -> str:
    """The dialog for an item: "YOU GOT$YOUR$<item>" for this slot's item, else "YOU SENT$<item>$TO <player>" (the
    player cut to the 8 characters left on the line). The item is two lines at most; AP$ITEM if it can't fit."""
    item = (own_sign_name if own else None) or short_item_name(item_name, ITEM_TEXT_LINE) or "AP ITEM"
    if own:
        return f"YOU GOT$YOUR${item}"
    player = (short_player_name(player_name) or "ANOTHER")[:ITEM_TEXT_LINE - len("TO ")].rstrip()
    return f"YOU SENT${item}$TO {player}"


def short_player_name(name: str) -> str | None:
    """A player name as text the game can show, cut off at one sign line (11 characters), or None if nothing shows."""
    shown = "".join(c for c in name.upper() if "A" <= c <= "Z" or "0" <= c <= "9" or c in ".,!?-_ ").strip()
    return shown[:SIGN_LINE_LENGTH].rstrip() or None


def other_player_sign(player: int, player_name: str, item_name: str) -> str:
    """Another player's item: "<player>$<item>", the name cut off at 11 characters ("P<number>" if nothing in it
    shows). The item is its full name if it fits beside the name, else its short form (SIGN_SHORT_FORMS), else a
    narrower short name, else "AP$ITEM"."""
    name = short_player_name(player_name) or f"P{player}"
    room = SIGN_MAX_CHARS - 1 - len(name)

    def fits(text: str | None) -> bool:
        return bool(text) and len(text) <= room and all(len(line) <= SIGN_LINE_LENGTH for line in text.split("$"))

    candidates = [short_item_name(item_name, SIGN_LINE_LENGTH), SIGN_SHORT_FORMS.get(item_name.upper())]
    candidates += [short_item_name(item_name, width) for width in range(SIGN_LINE_LENGTH - 1, 5, -1)]
    item = next((text for text in candidates if fits(text)), OTHER_ITEM_SIGN)
    return f"{name}${item}"


# One-line forms of two-line item names, used when the full form would cut the player's name
SIGN_SHORT_FORMS = {"MAGIC CONTAINER": "MC", "HEART CONTAINER": "HC",
                    "BLUE MAGIC JAR": "BLUE JAR", "RED MAGIC JAR": "RED JAR", "REFLECT SPELL": "REFLECT",
                    "SHIELD SPELL": "SHIELD", "THUNDER SPELL": "THUNDER",
                    # ZORA
                    "ARROW UPGRADE": "ARROW UP", "BOMB UPGRADE": "BOMB UP", "BOOMERANG UPGRADE": "BOOMER UP", "POWER BRACELET": "PB",
                    "BOOTS UPGRADE": "BOOTS UP", "CANDLE UPGRADE": "CANDLE UP", "RING UPGRADE": "RING UP",
                    "SWORD UPGRADE": "SWORD UP", "MAGICAL BOOMERANG": "M BOOMERANG", "MAGICAL BOOTS": "M BOOTS",
                    "MAGICAL SWORD": "M SWORD", "PEGASUS BOOTS": "P BOOTS", "SILVER ARROW": "SILVERS",
                    "WOODEN ARROW": "ARROWS",
                    # DWR
                    "DRAGON'S SCALE": "D-SCALE", "ERDRICK'S ARMOR": "E-ARMOR", "ERDRICK'S SWORD": "E-SWORD",
                    "ERDRICK'S TOKEN": "E-TOKEN", "FIGHTER'S RING": "F-RING", "STAFF OF RAIN": "STAFF",
                    "STONES OF SUNLIGHT": "STONES"}


# Signs the client may overwrite: the app's placeholder, or a sign an earlier client wrote in the "TOWN" form
KNOWN_SIGNS = {town: {legacy_sign_bytes(name, town) for name in (*SIGN_NAMES.values(), OTHER_ITEM_SIGN)}
               for town in TOWN_SIGN_INDEXES}

# Helpful hints. With helpful hints on, the Z2R app reserves HINT_SIZE bytes for each hint NPC reading "AP HINT",
# padded with end bytes, with 0x80 + the hint number in the last byte (CustomTexts.ArchipelagoHintPlaceholder). The
# YAML lists each hint's text indexes (hint_slots) and the start of a hint about each location (hint_places).
HINT_SIZE = 52
HINT_LINE_LENGTH = 10


def hint_bytes(text: str, number: int) -> bytes | None:
    """The HINT_SIZE bytes of a hint, or None if the text is too long to fit."""
    encoded = encode_text(text)
    if len(encoded) > HINT_SIZE - 1:
        return None
    return encoded + b"\xFF" * (HINT_SIZE - 1 - len(encoded)) + bytes([0x80 + number])


# Words dropped from a long item name before anything is cut short
FILLER_WORDS = {"THE", "OF", "A", "AN", "AND", "TO", "FOR", "WITH", "IN", "ON"}
# Common words in item names and shorter forms that still read clearly
WORD_SWAPS = {
    "PROGRESSIVE": "PROG", "CONTAINER": "CONT", "CONTAINERS": "CONT", "UPGRADE": "UPGRD", "UPGRADES": "UPGRD",
    "CAPACITY": "CAP", "EXPANSION": "EXP", "MAXIMUM": "MAX", "MINIMUM": "MIN", "LEGENDARY": "LEGEND",
    "MAGICAL": "MAGIC", "COLLECTIBLE": "COLLECT", "FRAGMENT": "FRAG", "FRAGMENTS": "FRAGS", "ABILITY": "ABIL",
    "CHARACTER": "CHAR", "EQUIPMENT": "EQUIP", "INCREASE": "UP", "DOUBLE": "2X", "TRIPLE": "3X",
    "PROJECTILE": "SHOT", "ACCESSORY": "ACCESS", "TRANSFORMATION": "FORM", "TELEPORTER": "TELEPORT",
}


def _fit(words: list[str], width: int | None = None) -> list[str] | None:
    """Pack words into lines of `width` (HINT_LINE_LENGTH) characters, or None if a word is too long."""
    width = width or HINT_LINE_LENGTH
    lines: list[str] = []
    for word in words:
        if len(word) > width:
            return None
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


HINT_SENTENCE_WIDTH = 11  # the dialog box line, as on the town signs
HINT_SENTENCE_LINES = 4


def hint_sentence(place: str, item_name: str, owner: str | None) -> str:
    """"<PLACE> HAS YOUR <ITEM>." for this slot's item (owner None), else "<PLACE> HAS <OWNER>S <ITEM>." (the font
    has no apostrophe). Word-wrapped into at most four 11-character lines. A long item name falls back to its sign
    short form, then AP ITEM."""
    head = f"{place} HAS YOUR" if owner is None else f"{place} HAS {owner}S"
    candidates = [item_name, SIGN_SHORT_FORMS.get(item_name.upper(), ""), "AP ITEM"]
    for candidate in candidates:
        item = _shown(candidate)
        if not item:
            continue
        lines = _fit(f"{head} {item}".split(), HINT_SENTENCE_WIDTH)
        if lines and len(lines) <= HINT_SENTENCE_LINES:
            return "$".join(lines)
    # still too long (a long place and a long owner): drop the owner, then the place
    for fallback in (f"{place} HAS AP ITEM", "AP ITEM"):
        lines = _fit(fallback.split(), HINT_SENTENCE_WIDTH)
        if lines and len(lines) <= HINT_SENTENCE_LINES:
            return "$".join(lines)
    return "AP ITEM"


def short_item_name(name: str, width: int | None = None) -> str | None:
    """An item name as at most two lines of HINT_LINE_LENGTH characters ($ between them), or None if it can't be
    made to fit and stay readable. Tries, in order: the full name; "Progressive X" as "X UPGRADE"; without filler
    words; with common words swapped for shorter forms; then cutting the longest words down a letter at a time."""
    # keep only what the game can show: A-Z, 0-9 and a few symbols; anything else splits words
    cleaned = "".join(c if (c.isascii() and c.isalnum()) or c in ".,!?-" else " "
                      for c in name.upper().replace("'", ""))
    words = cleaned.split()
    if not words:
        return None
    if len(words) > 1 and words[0] == "PROGRESSIVE":
        words = words[1:] + ([] if words[-1] in ("UPGRADE", "UPGRADES") else ["UPGRADE"])

    def done(candidate: list[str]) -> str | None:
        lines = _fit(candidate, width)
        return "$".join(lines) if lines and len(lines) <= 2 else None

    attempts = [words]
    kept = [w for w in words if w not in FILLER_WORDS] or words
    attempts.append(kept)
    attempts.append([WORD_SWAPS.get(w, w) for w in kept])
    for attempt in attempts:
        if result := done(attempt):
            return result

    # cut the longest word by one letter at a time, keeping at least 3 letters of each
    words = attempts[-1][:]
    while True:
        longest = max(range(len(words)), key=lambda i: len(words[i]))
        if len(words[longest]) <= 3:
            return None
        words[longest] = words[longest][:-1]
        if result := done(words):
            return result


class Z2RClient(BizHawkClient):
    game = "Z2R"
    system = "NES"

    slot_name: str
    rom_seed: str
    rom_hash: str
    seed_mismatch_logged: bool
    guards: list[tuple[int, list[int], str]]
    last_state: tuple[int, int, int] | None
    sent_events: set[str]
    signs_scouted: bool
    signs_scout_retry: float
    signs_written: dict[int, bytes]
    next_sign_check: float
    hints_scouted: bool
    hints_scout_retry: float
    next_hint_check: float
    hints_written: dict[int, bytes]
    pickups_scout_retry: float
    next_item_text_check: float
    pickup_messages: list[tuple[str, float]]
    hud_message_until: float | None
    next_error_hud: float
    no_slots_logged: bool
    received_seen: int | None

    def __init__(self) -> None:
        self.slot_name = ""
        self.rom_seed = ""
        self.rom_hash = ""
        self.seed_mismatch_logged = False
        self.guards = []
        self.last_state = None
        self.sent_events = set()
        self.signs_scouted = False
        self.signs_scout_retry = 0.0
        self.signs_written = {}
        self.next_sign_check = 0.0
        self.hints_scouted = False
        self.hints_scout_retry = 0.0
        self.next_hint_check = 0.0
        self.hints_written = {}
        self.pickups_scout_retry = 0.0
        self.next_item_text_check = 0.0
        self.pickup_messages = []
        self.hud_message_until = None
        self.next_error_hud = 0.0
        self.no_slots_logged = False
        self.received_seen = None

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        try:
            if await bizhawk.get_memory_size(ctx.bizhawk_ctx, "PRG ROM") < SIGNATURE_ADDRESS + SIGNATURE_SIZE:
                return False
            block, hash_tiles = await bizhawk.read(ctx.bizhawk_ctx, [
                (SIGNATURE_ADDRESS, SIGNATURE_SIZE, "PRG ROM"), (HASH_ADDRESS, HASH_LENGTH * 2, "PRG ROM")])
        except bizhawk.RequestFailedError:
            return False

        if block[:4] != SIGNATURE_MAGIC or block[4] != SIGNATURE_VERSION:
            return False

        self.slot_name = block[0x08:0x18].split(b"\x00")[0].decode("ascii", errors="replace")
        self.rom_seed = block[0x18:0x38].split(b"\x00")[0].decode("ascii", errors="replace")
        self.rom_hash = "".join(self.decode_hash_tile(hash_tiles[i * 2]) for i in range(HASH_LENGTH))
        self.seed_mismatch_logged = False
        self.sent_events = set()
        self.signs_scouted = False
        self.signs_scout_retry = 0.0
        self.signs_written = {}
        self.next_sign_check = 0.0
        self.hints_scouted = False
        self.hints_scout_retry = 0.0
        self.next_hint_check = 0.0
        self.hints_written = {}
        self.pickups_scout_retry = 0.0
        self.next_item_text_check = 0.0
        self.pickup_messages = []
        self.hud_message_until = None
        self.next_error_hud = 0.0
        self.no_slots_logged = False
        self.received_seen = None
        ctx.game = self.game
        ctx.items_handling = 0b111
        ctx.want_slot_data = True
        return True

    @staticmethod
    def decode_hash_tile(tile: int) -> str:
        if 0xD0 <= tile <= 0xD9:
            return chr(tile - 0xA0)
        if 0xDA <= tile <= 0xF3:
            return chr(tile - 0x99)
        return "?"

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        ctx.auth = self.slot_name

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        if ctx.server is None or ctx.server.socket.closed or ctx.slot_data is None:
            return

        # The signature holds the first 32 characters of the seed, with non-ASCII characters replaced by "?"
        expected_seed = "".join(c if " " <= c <= "~" else "?" for c in ctx.slot_data.get("rom_seed", ""))[:32]
        expected_hash = ctx.slot_data.get("rom_hash", "")
        if (expected_seed and expected_seed != self.rom_seed) or (expected_hash and expected_hash != self.rom_hash):
            if not self.seed_mismatch_logged:
                logger.error(f"This ROM is seed '{self.rom_seed}' with hash '{self.rom_hash}', but the multiworld "
                             f"expects seed '{expected_seed}' with hash '{expected_hash}'. Open the ROM that "
                             f"matches your YAML.")
                self.seed_mismatch_logged = True
            await self.show_error_on_hud(ctx, "WRONG ROM FOR THIS SEED")
            return

        try:
            await self.sign_check(ctx)
            await self.hint_check(ctx)
            await self.item_text_check(ctx)
            event_bytes, mode_bytes = await bizhawk.read(
                ctx.bizhawk_ctx, [(GAME_EVENT_ADDRESS, 1, RAM), (GAME_MODE_ADDRESS, 2, RAM)])
            event, mode, mode_current = event_bytes[0], mode_bytes[0], mode_bytes[1]
            if (event, mode, mode_current) != self.last_state:
                self.last_state = (event, mode, mode_current)
                logger.debug(f"Z2R game event {event:#04x}, game mode {mode:#04x}/{mode_current:#04x}")

            if event in GAME_EVENT_ENDING:
                # Link can't be controlled during the ending, but the game complete flag is set by then
                complete = (await bizhawk.read(ctx.bizhawk_ctx, [(GAME_COMPLETE_ADDRESS, 1, RAM)]))[0][0]
                if complete & GAME_COMPLETE_MASK and not ctx.finished_game:
                    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                    ctx.finished_game = True
                return

            if event != GAME_EVENT_MAIN_LOOP or mode != mode_current or mode not in GAME_MODES_IN_CONTROL:
                return  # not in control of Link (title screen, file select, loading, dying, ...)

            # Every read and write below only goes through if the game is still in this exact state
            self.guards = [(GAME_EVENT_ADDRESS, [event], RAM), (GAME_MODE_ADDRESS, [mode, mode_current], RAM)]
            state = await bizhawk.guarded_read(
                ctx.bizhawk_ctx, [(FLAGS_START, FLAGS_SIZE, RAM), (GAME_COMPLETE_ADDRESS, 1, RAM)], self.guards)
            if state is None:
                return
            flags, game_complete = state[0], state[1][0]

            await self.location_check(ctx, flags)
            await self.hud_message_check(ctx, mode)
            await self.event_check(ctx, flags)
            if game_complete & GAME_COMPLETE_MASK and not ctx.finished_game:
                await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                ctx.finished_game = True
            await self.received_items_check(ctx, flags)
            for name in CONSUMABLE_ITEMS:
                await self.consumable_check(ctx, name)
        except bizhawk.RequestFailedError:
            # The connector didn't respond. Exit handler and return to main loop to reconnect
            pass

    async def location_check(self, ctx: "BizHawkClientContext", flags: bytes) -> None:
        checked: list[int] = []
        for name, data in ctx.slot_data["locations"].items():
            location_id = LOCATION_NAME_TO_ID.get(name)
            if location_id is None or location_id not in ctx.missing_locations:
                continue
            offset = data["address"] - FLAGS_START
            if not 0 <= offset < FLAGS_SIZE:
                continue
            is_set = bool(flags[offset] & data["mask"])
            if is_set == bool(data["collected"]):
                checked.append(location_id)

        for location_id in await ctx.check_locations(checked):
            ctx.locations_checked.add(location_id)
            # a location whose dialog names the item (item_text_slots) needs no HUD message
            in_dialog = ctx.location_names.lookup_in_game(location_id) in ctx.slot_data.get("item_text_slots", {})
            message = None if in_dialog else self.pickup_text(ctx, location_id)
            if message:
                self.pickup_messages.append((message, time.monotonic()))
            logger.info(f"New Check: {ctx.location_names.lookup_in_game(location_id)} "
                        f"({len(ctx.locations_checked)}/{len(ctx.missing_locations) + len(ctx.checked_locations)})")

    def pickup_text(self, ctx: "BizHawkClientContext", location_id: int) -> str | None:
        """The pickup message for a location this slot just checked, or None if its item isn't known yet."""
        info = ctx.locations_info.get(location_id)
        if info is None:
            return None
        item_name = ctx.item_names.lookup_in_slot(info.item, info.player)
        return pickup_message(item_name, ctx.player_names.get(info.player, ""), info.player == ctx.slot)

    async def item_text_check(self, ctx: "BizHawkClientContext") -> None:
        """Fill the item dialogs (wizards, trainers, Saria's table, Nabooru's fountain, Bagu) with the item each
        location holds, from slot data (item_text_slots). Only a placeholder or text this client wrote is
        overwritten. Repeats every SIGN_RECHECK_SECONDS, since reloading the ROM brings the placeholders back."""
        now = time.monotonic()
        if now < self.next_item_text_check:
            return
        slots = ctx.slot_data.get("item_text_slots", {})
        if not slots:
            self.next_item_text_check = float("inf")
            return
        ready = [(name, index, number) for name, (index, number) in slots.items()
                 if LOCATION_NAME_TO_ID.get(name) in ctx.locations_info]
        if not ready:
            return  # wait for the scout reply
        self.next_item_text_check = now + SIGN_RECHECK_SECONDS
        addresses = await self.text_addresses(ctx, [index for _name, index, _number in ready])
        current = await bizhawk.read(ctx.bizhawk_ctx, [(address, ITEM_TEXT_SIZE, "PRG ROM") for address in addresses])
        writes = []
        for (name, _index, number), address, now_bytes in zip(ready, addresses, current):
            info = ctx.locations_info[LOCATION_NAME_TO_ID[name]]
            own = info.player == ctx.slot
            if own:
                item_name = ctx.item_names.lookup_in_game(info.item, self.game)
            else:
                item_name = ctx.item_names.lookup_in_slot(info.item, info.player)
            text = item_dialog_text(item_name, ctx.player_names.get(info.player, ""), own, SIGN_NAMES.get(item_name))
            wanted = item_text_bytes(text, number)
            if wanted is None or now_bytes == wanted:
                continue
            if now_bytes == item_text_bytes("AP ITEM", number) or now_bytes == self.hints_written.get(address):
                writes.append((address, wanted, "PRG ROM"))
                self.hints_written[address] = wanted
            else:
                self.note_missing_slots("item dialog")
        if writes:
            await bizhawk.write(ctx.bizhawk_ctx, writes)
            logger.debug(f"Wrote {len(writes)} item dialog(s)")

    async def hud_message_check(self, ctx: "BizHawkClientContext", mode: int) -> None:
        """Show queued pickup messages in the HUD one at a time for HUD_MESSAGE_SECONDS each, then blank the row.
        Scouts every location (again after a reconnect) so a pickup's item is known when it happens. Writes only in side view, and only
        when the game's PPU buffer is empty, so the game's own screen updates are never overwritten."""
        now = time.monotonic()
        wanted = ctx.missing_locations | ctx.checked_locations
        known = getattr(ctx, "locations_info", {})
        # a reconnect clears what the server told us, so ask again (at most every SCOUT_RETRY_SECONDS) until known
        if any(location_id not in known for location_id in wanted) and now >= self.pickups_scout_retry:
            await ctx.send_msgs([{"cmd": "LocationScouts", "locations": sorted(wanted), "create_as_hint": 0}])
            self.pickups_scout_retry = now + SCOUT_RETRY_SECONDS
        if mode != HUD_MODE:
            return
        if self.hud_message_until is not None:
            if now < self.hud_message_until:
                return
            if await self.write_hud_message(ctx, ""):
                self.hud_message_until = None
            return
        while self.pickup_messages and now - self.pickup_messages[0][1] > HUD_MESSAGE_STALE:
            self.pickup_messages.pop(0)
        if self.pickup_messages and await self.write_hud_message(ctx, self.pickup_messages[0][0]):
            self.pickup_messages.pop(0)
            busy = bool(self.pickup_messages)
            self.hud_message_until = now + (HUD_MESSAGE_SECONDS_BUSY if busy else HUD_MESSAGE_SECONDS)

    async def show_error_on_hud(self, ctx: "BizHawkClientContext", text: str) -> None:
        """Put an error in the HUD row (every ERROR_HUD_SECONDS, since a redraw erases it), for problems the player
        would otherwise only find in the client window. Only in side view, and only if the buffer is free."""
        now = time.monotonic()
        if now < self.next_error_hud:
            return
        try:
            event_bytes, mode_bytes = await bizhawk.read(
                ctx.bizhawk_ctx, [(GAME_EVENT_ADDRESS, 1, RAM), (GAME_MODE_ADDRESS, 2, RAM)])
        except bizhawk.RequestFailedError:
            return
        event, mode, mode_current = event_bytes[0], mode_bytes[0], mode_bytes[1]
        if event != GAME_EVENT_MAIN_LOOP or mode != HUD_MODE or mode_current != HUD_MODE:
            return
        self.guards = [(GAME_EVENT_ADDRESS, [event], RAM), (GAME_MODE_ADDRESS, [mode, mode_current], RAM)]
        if await self.write_hud_message(ctx, text):
            self.next_error_hud = now + ERROR_HUD_SECONDS

    def note_missing_slots(self, kind: str) -> None:
        """Say once that the ROM has none of the Z2R app's AP text slots (a seed made by an older app or without
        Archipelago mode), which is why no AP text appears."""
        if not self.no_slots_logged:
            self.no_slots_logged = True
            logger.warning(f"This ROM has no AP {kind} slots to fill in, so that text stays as the ROM has it. It was "
                           f"probably made by an older Z2R build or without Archipelago mode: make a new seed.")

    async def write_hud_message(self, ctx: "BizHawkClientContext", text: str) -> bool:
        """Queue text for the HUD message row in the game's PPU buffer if it is empty. True if written."""
        entry = hud_message_bytes(text)
        # the buffer must be empty; set its length to our entry's end so the game appends after it, not over it
        guards = [*self.guards, (PPU_BUFFER_LENGTH, [0], RAM), (PPU_BUFFER, [0xFF], RAM)]
        return await bizhawk.guarded_write(
            ctx.bizhawk_ctx, [(PPU_BUFFER_LENGTH, bytes([len(entry) - 1]) + entry, RAM)], guards)

    async def event_check(self, ctx: "BizHawkClientContext", flags: bytes) -> None:
        """Publish the game events that have happened (such as PALACE1:crystal) to the server's data storage, under
        z2r_events_<team>_<slot>, for trackers. Events use the same flag format as locations."""
        reached: dict[str, bool] = {}
        for name, data in ctx.slot_data.get("events", {}).items():
            if name in self.sent_events:
                continue
            offset = data["address"] - FLAGS_START
            if not 0 <= offset < FLAGS_SIZE:
                continue
            if bool(flags[offset] & data["mask"]) == bool(data["collected"]):
                reached[name] = True
        if not reached:
            return

        await ctx.send_msgs([{"cmd": "Set", "key": f"z2r_events_{ctx.team}_{ctx.slot}", "default": {},
                              "want_reply": False, "operations": [{"operation": "update", "value": reached}]}])
        self.sent_events.update(reached)
        logger.info(f"Events reached: {', '.join(reached)}")

    def announce_received(self, ctx: "BizHawkClientContext") -> None:
        """Queue a HUD message for each item another player sent this slot since the last call. The first call after
        connecting only counts the items, so the older history is not shown again."""
        total = len(ctx.items_received)
        if self.received_seen is None or total < self.received_seen:
            self.received_seen = total  # first sync, or the server sent the whole list again
            return
        for network_item in ctx.items_received[self.received_seen:]:
            if network_item.player == ctx.slot:
                continue  # found in this slot's own world: the pickup message covers it
            item_name = ctx.item_names.lookup_in_slot(network_item.item, ctx.slot)
            sender = ctx.player_names.get(network_item.player, "")
            self.pickup_messages.append((received_message(item_name, sender), time.monotonic()))
        self.received_seen = total

    async def received_items_check(self, ctx: "BizHawkClientContext", flags: bytes) -> None:
        """Make the game's inventory match everything received. Every write is absolute, so it is safe to repeat
        after a reconnect or a save reload."""
        self.announce_received(ctx)
        received: dict[str, int] = {}
        for network_item in ctx.items_received:
            name = ctx.item_names.lookup_in_game(network_item.item, ctx.game)
            received[name] = received.get(name, 0) + 1

        # Read what the game currently holds in every address we may write to
        addresses = sorted({data.address for name, data in ITEM_TABLE.items()
                            if name in received and data.address is not None}
                           | {CONTAINER_ITEMS[name][0] for name in CONTAINER_ITEMS if name in received}
                           | {BASEMENT_ACCESS_ADDRESS, CONTAINER_ITEMS["Magic Container"][0]})
        current = {address: value[0] for address, value in zip(addresses, await bizhawk.read(
            ctx.bizhawk_ctx, [(address, 1, RAM) for address in addresses]))}
        desired = dict(current)

        for name, count in received.items():
            if name in CONTAINER_ITEMS:
                address, maximum = CONTAINER_ITEMS[name]
                start = ctx.slot_data["start_magic_containers" if name == "Magic Container"
                                      else "start_heart_containers"]
                desired[address] = max(desired[address], min(start + count, maximum))
                continue
            data = ITEM_TABLE[name]
            if data.address is None:
                continue  # filler, not delivered
            if data.mask == 0xFF:
                desired[data.address] = 1
            else:
                desired[data.address] |= data.mask

        # This also covers the containers the ROM starts with, which may already meet the requirement
        if desired[CONTAINER_ITEMS["Magic Container"][0]] >= ctx.slot_data["new_kasuto_basement_requirement"]:
            desired[BASEMENT_ACCESS_ADDRESS] |= BASEMENT_ACCESS_MASK

        writes = [(address, [value], RAM) for address, value in desired.items() if value != current[address]]
        # Mirror the game's own pickup: a container that was just added refills its bar
        magic_address = CONTAINER_ITEMS["Magic Container"][0]
        heart_address = CONTAINER_ITEMS["Heart Container"][0]
        if heart_address in desired and desired[heart_address] > current[heart_address]:
            writes.append((LIFE_REFILL_ADDRESS, [LIFE_REFILL_AMOUNT], RAM))
        if desired[magic_address] > current[magic_address]:
            writes.append((MAGIC_REFILL_ADDRESS, [(desired[magic_address] << 4) & 0xFF], RAM))
        if writes:
            await bizhawk.guarded_write(ctx.bizhawk_ctx, writes, self.guards)

    def wizard_towns(self, ctx: "BizHawkClientContext") -> dict[int, int]:
        """Location ID -> town type for each wizard location in this slot."""
        towns: dict[int, int] = {}
        for name, data in ctx.slot_data.get("locations", {}).items():
            location_id = LOCATION_NAME_TO_ID.get(name)
            if location_id is not None and data.get("address") == WIZARD_FLAGS and data.get("mask") in TOWN_BITS:
                towns[location_id] = TOWN_BITS.index(data["mask"]) + 1
        return towns

    def sign_name(self, ctx: "BizHawkClientContext", location_id: int) -> str:
        """This slot's Z2R item by its sign name ("HAMMER"); another player's as "P<number>" and its short name."""
        item = ctx.locations_info[location_id]
        if item.player == ctx.slot:
            name = ctx.item_names.lookup_in_game(item.item, self.game)
            return SIGN_NAMES.get(name, OTHER_ITEM_SIGN) + "$TOWN"
        return other_player_sign(item.player, ctx.player_names.get(item.player, ""),
                                 ctx.item_names.lookup_in_slot(item.item, item.player))

    async def sign_check(self, ctx: "BizHawkClientContext") -> None:
        """Write the item each AP wizard gives onto its town sign. Repeats every SIGN_RECHECK_SECONDS, since reloading
        the ROM brings back the placeholders."""
        now = time.monotonic()
        if now < self.next_sign_check:
            return
        towns = self.wizard_towns(ctx)
        if not towns:
            self.next_sign_check = float("inf")
            return
        missing = [location_id for location_id in towns if location_id not in ctx.locations_info]
        if missing:
            if not self.signs_scouted or now >= self.signs_scout_retry:
                await ctx.send_msgs([{"cmd": "LocationScouts", "locations": missing, "create_as_hint": 0}])
                self.signs_scouted = True
                self.signs_scout_retry = now + SCOUT_RETRY_SECONDS  # a reconnect forgets earlier replies
            return  # wait for the LocationInfo reply
        self.next_sign_check = now + SIGN_RECHECK_SECONDS

        table_pointers = (await bizhawk.read(ctx.bizhawk_ctx, [(TEXT_TABLE_POINTERS, 4, "PRG ROM")]))[0]
        west = BANK3_PRG + int.from_bytes(table_pointers[0:2], "little") - 0x8000
        east = BANK3_PRG + int.from_bytes(table_pointers[2:4], "little") - 0x8000
        entries = []
        for location_id, town in towns.items():
            index = TOWN_SIGN_INDEXES[town]
            entries.append((west + index * 2 if index < WEST_TEXT_COUNT else east + (index - WEST_TEXT_COUNT) * 2,
                            location_id, town))
        pointers = await bizhawk.read(ctx.bizhawk_ctx, [(entry, 2, "PRG ROM") for entry, _, _ in entries])
        signs = [(BANK3_PRG + int.from_bytes(pointer, "little") - 0x8000, location_id, town)
                 for pointer, (_, location_id, town) in zip(pointers, entries)]
        current = await bizhawk.read(ctx.bizhawk_ctx, [(address, SIGN_SIZE, "PRG ROM") for address, _, _ in signs])

        writes = []
        for (address, location_id, town), now_bytes in zip(signs, current):
            wanted = sign_bytes(self.sign_name(ctx, location_id), town) or sign_bytes(OTHER_ITEM_SIGN + "$TOWN", town)
            # Only overwrite a sign the Z2R app left room for: its placeholder ("AP ITEM TOWN"), a sign an earlier
            # client wrote, or one this client wrote. A ROM made without the placeholders (an older app) is left alone.
            if now_bytes != wanted and (now_bytes in KNOWN_SIGNS[town] or now_bytes == self.signs_written.get(address)):
                writes.append((address, wanted, "PRG ROM"))
                self.signs_written[address] = wanted
        if writes:
            await bizhawk.write(ctx.bizhawk_ctx, writes)
            logger.debug(f"Wrote {len(writes)} town sign(s)")

    @staticmethod
    async def text_addresses(ctx: "BizHawkClientContext", indexes: list[int]) -> list[int]:
        """The "PRG ROM" address of each text, through the text tables' pointers in bank 3."""
        table_pointers = (await bizhawk.read(ctx.bizhawk_ctx, [(TEXT_TABLE_POINTERS, 4, "PRG ROM")]))[0]
        west = BANK3_PRG + int.from_bytes(table_pointers[0:2], "little") - 0x8000
        east = BANK3_PRG + int.from_bytes(table_pointers[2:4], "little") - 0x8000
        entries = [west + i * 2 if i < WEST_TEXT_COUNT else east + (i - WEST_TEXT_COUNT) * 2 for i in indexes]
        pointers = await bizhawk.read(ctx.bizhawk_ctx, [(entry, 2, "PRG ROM") for entry in entries])
        return [BANK3_PRG + int.from_bytes(pointer, "little") - 0x8000 for pointer in pointers]

    def hint_item_text(self, ctx: "BizHawkClientContext", location_id: int) -> str:
        """The item part of a hint: a Z2R item of this slot by its sign name, another item by a short form of its name
        that fits two lines, or AP ITEM."""
        item = ctx.locations_info[location_id]
        if item.player == ctx.slot:
            name = ctx.item_names.lookup_in_game(item.item, self.game)
            return SIGN_NAMES.get(name) or short_item_name(name) or OTHER_ITEM_SIGN
        name = ctx.item_names.lookup_in_slot(item.item, item.player)
        return short_item_name(name) or OTHER_ITEM_SIGN

    def hint_text(self, ctx: "BizHawkClientContext", location_id: int, location_name: str) -> str:
        """A helpful hint naming the place and item: "P1 HAS YOUR <ITEM>." for this slot's own item, else
        "P1 HAS <PLAYER>S <ITEM>."."""
        info = ctx.locations_info[location_id]
        if info.player == ctx.slot:
            name = ctx.item_names.lookup_in_game(info.item, self.game)
        else:
            name = ctx.item_names.lookup_in_slot(info.item, info.player)
        owner = None if info.player == ctx.slot else (
            (short_player_name(ctx.player_names.get(info.player, "")) or f"PLAYER{info.player}")[:10].rstrip())
        return hint_sentence(PLACE_NAMES.get(location_name, "SOMEWHERE"), name, owner)

    def choose_hints(self, ctx: "BizHawkClientContext", count: int) -> list[int]:
        """Pick the locations to hint at: progression items first, in an order fixed by the seed and slot so a
        reconnect writes the same hints."""
        checked = getattr(ctx, "checked_locations", set())
        candidates = sorted(LOCATION_NAME_TO_ID[name] for name in ctx.slot_data.get("hint_places", {})
                            if name in LOCATION_NAME_TO_ID and LOCATION_NAME_TO_ID[name] not in checked)
        seed = getattr(ctx, "server_seed_name", None) or ctx.seed_name
        rng = random.Random(f"{seed}-{ctx.slot}-z2r-hints")
        rng.shuffle(candidates)
        candidates.sort(key=lambda location_id: not ctx.locations_info[location_id].flags & 0b001)
        return candidates[:count]

    async def hint_check(self, ctx: "BizHawkClientContext") -> None:
        """Fill the helpful hint NPCs with hints about items in this world. Repeats every SIGN_RECHECK_SECONDS, since
        reloading the ROM brings back the placeholders."""
        now = time.monotonic()
        if now < self.next_hint_check:
            return
        slots = ctx.slot_data.get("hint_slots", [])
        places = ctx.slot_data.get("hint_places", {})
        if not slots or not places:
            self.next_hint_check = float("inf")
            return
        location_ids = [LOCATION_NAME_TO_ID[name] for name in places if name in LOCATION_NAME_TO_ID]
        missing = [location_id for location_id in location_ids if location_id not in ctx.locations_info]
        if missing:
            if not self.hints_scouted or now >= self.hints_scout_retry:
                await ctx.send_msgs([{"cmd": "LocationScouts", "locations": missing, "create_as_hint": 0}])
                self.hints_scouted = True
                self.hints_scout_retry = now + SCOUT_RETRY_SECONDS
            return  # wait for the LocationInfo reply
        self.next_hint_check = now + SIGN_RECHECK_SECONDS

        names = {location_id: name for name, location_id in LOCATION_NAME_TO_ID.items()}
        chosen = self.choose_hints(ctx, len(slots))
        pairs = [(number, index) for number, indexes in enumerate(slots) for index in indexes]
        pair_addresses = await self.text_addresses(ctx, [index for _number, index in pairs])
        numbers: dict[int, int] = {}  # address -> hint number; merged texts share one address
        for (number, _index), address in zip(pairs, pair_addresses):
            if number < len(chosen):
                numbers.setdefault(address, number)
        addresses = list(numbers)
        current = await bizhawk.read(ctx.bizhawk_ctx, [(address, HINT_SIZE, "PRG ROM") for address in addresses])
        writes = []
        for address, now_bytes in zip(addresses, current):
            number = numbers[address]
            location_id = chosen[number]
            text = self.hint_text(ctx, location_id, names[location_id])
            wanted = hint_bytes(text, number)
            if wanted is None or now_bytes == wanted:
                continue
            # Only overwrite the app's placeholder or a hint this client wrote. Other text is left alone.
            if now_bytes == hint_bytes("AP HINT", number) or now_bytes == self.hints_written.get(address):
                writes.append((address, wanted, "PRG ROM"))
                self.hints_written[address] = wanted
            else:
                self.note_missing_slots("hint")
        if writes:
            await bizhawk.write(ctx.bizhawk_ctx, writes)
            logger.debug(f"Wrote {len(writes)} helpful hint(s)")

    async def consumable_check(self, ctx: "BizHawkClientContext", name: str) -> None:
        """Give one received copy of a consumable item per call, as long as the game can hold it."""
        consumable = CONSUMABLE_ITEMS[name]
        address = consumable.address
        storage_key = f"z2r_given_{name.lower().replace(' ', '_')}_{ctx.team}_{ctx.slot}"
        ctx.set_notify(storage_key)
        if storage_key not in ctx.stored_data:
            return  # the server hasn't told us how many we've already given
        given = ctx.stored_data[storage_key] or 0

        received = sum(1 for item in ctx.items_received
                       if ctx.item_names.lookup_in_game(item.item, ctx.game) == name)
        if given >= received:
            return

        reads = [(address, consumable.size, RAM)]
        if consumable.scale_address is not None:
            reads.append((consumable.scale_address, 1, RAM))
        if consumable.xp_table_index is not None:
            reads.append((PBAG_XP_TABLE_ADDRESS + consumable.xp_table_index, 1, "PRG ROM"))
        values = await bizhawk.read(ctx.bizhawk_ctx, reads)
        count_bytes = values[0]
        count = int.from_bytes(count_bytes, "big")
        amount = consumable.amount
        if consumable.scale_address is not None:
            amount *= values[1][0]
        if consumable.xp_table_index is not None:
            amount = XP_VALUES[values[-1][0] & 0x0F]
        if amount and count + amount > consumable.maximum:
            return  # wait until some is spent
        if amount:  # an amount of 0 (e.g. a 0 XP bag) has nothing to write but still counts as given
            new_bytes = list((count + amount).to_bytes(consumable.size, "big"))
            if not await bizhawk.guarded_write(
                    ctx.bizhawk_ctx, [(address, new_bytes, RAM)],
                    [*self.guards, (address, list(count_bytes), RAM)]):
                return

        ctx.stored_data[storage_key] = given + 1
        await ctx.send_msgs([{"cmd": "Set", "key": storage_key, "default": 0, "want_reply": False,
                              "operations": [{"operation": "add", "value": 1}]}])
