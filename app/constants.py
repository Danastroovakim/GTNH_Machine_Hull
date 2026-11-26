import re
from datetime import timezone, timedelta

APP_NAME = "GTNH Machine Hull"
APP_VERSION = "7.0"
APP_WINDOW_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_USER_MODEL_ID = "GTNH.MachineHull.Desktop.7"

# Public out-of-the-box defaults: strictly NO personal secrets or server IDs.
DEFAULT_CONFIG = {
    "panel_url": "https://mgr.bisquit.host",
    "api_key": "",
    "server_id": "",
    "max_panel_backups": 3,
}

DEFAULT_UPDATER_PROFILE = {
    "prism_instance_dir": "",
    "last_client_zip": "",
    "last_server_zip": "",
    "last_server_url": "",
    "server_source_mode": "zip",
    "update_mode": "client",
    "custom_client_mods": [],
    "custom_server_mods": [],
    "extra_server_mod_paths": [],
    "custom_resourcepacks": [],
    "protected_configs": [
        "angelica.cfg",
        "angelica-options.json",
        "shaders.properties",
        "chromatictooltips.cfg",
    ],
}

ARCHIVE_EXTENSIONS = (".zip", ".tar.gz", ".tgz", ".7z", ".tar", ".zst", ".rar")
MOD_FILE_EXTENSIONS = (".jar", ".zip", ".litemod", ".jar.disabled", ".zip.disabled", ".litemod.disabled")

# Non-JAR runtime/resource folders inside mods/ that must never be flagged as custom mod jars
MOD_KNOWN_SUBDIRS = {"1.7.10", "ic2", "opensecurity", "carpentersblocks"}

# Extra server root dirs included in full world backups and Overlay Restore
EXTRA_MOD_DIRS = ("serverutilities", "visualprospecting", "journeymap", "TCNodeTracker", "saves")

GTNH_WORLD_MOD_DIRS = (
    "betterquesting", "AE2", "wayfarmap", "buildcraft", "gtnteams",
    "playerdata", "EnderStorage", "hee", "JABBA", "logisticspipes",
    "LootBagStorage", "NEI", "RedstoneEther", "serverutilities",
    "stats", "backpacks", "gregtech", "galacticraft", "gadomancy",
)

GTNH_WORLD_DAT_FILES = (
    "level.dat", "forcedchunks.dat", "pocketplane.dat",
    "labyrinth.dat", "multipart.dat", "QuestLoot.json", "nicknames.json",
)

GTNH_DIM_NAMES = {
    "DIM-1": "Nether (Ад)",
    "DIM1": "The End (Энд)",
    "DIM7": "Twilight Forest (Сумеречный лес)",
    "DIM28": "Moon (Луна)",
    "DIM29": "Mars (Марс)",
    "DIM30": "Asteroids (Астероиды)",
    "DIM31": "Phobos (Фобос)",
    "DIM32": "Deimos (Деймос)",
    "DIM33": "Ceres (Церера)",
    "DIM35": "Io (Ио)",
    "DIM36": "Europa (Европа)",
    "DIM37": "Ganymede (Ганимед)",
    "DIM38": "Callisto (Каллисто)",
    "DIM39": "Venus (Венера)",
    "DIM40": "Mercury (Меркурий)",
    "DIM41": "Enceladus (Энцелад)",
    "DIM42": "Titan (Титан)",
    "DIM43": "Miranda (Миранда)",
    "DIM44": "Oberon (Оберон)",
    "DIM45": "Proteus (Протей)",
    "DIM46": "Triton (Тритон)",
    "DIM47": "Pluto (Плутон)",
    "DIM48": "Kuiper Belt (Пояс Койпера)",
    "DIM49": "Haumea (Хаумеа)",
    "DIM50": "Makemake (Макемаке)",
    "DIM63": "Ross128b (Росс 128b)",
    "DIM64": "Ross128ba (Росс 128ba)",
    "DIM100": "Underdark (Глубокая Тьма)",
    "DIM112": "Last Millenium (Последнее Тысячелетие)",
}

# Strictly protected client directories inside .minecraft/
CLIENT_STRICT_PROTECTED_DIRS = {
    "saves", "journeymap", "visualprospecting", "tcnodetracker",
    "wayfarmap", "schematics", "blueprints", "screenshots",
    "shaderpacks", "texturepacks", "fonts", "server-resource-packs",
    "lootbagstorage", "backups", "angelica",
}

# Strictly protected client files inside .minecraft/
CLIENT_STRICT_PROTECTED_FILES = {
    "options.txt", "optionsnf.txt", "localconfig.cfg", "servers.dat",
    "knownkeys.txt", "usernamecache.json", "botaniavars.dat",
}

# Strictly protected server root files & folders (never overwritten/deleted on update)
SERVER_STRICT_PROTECTED_NAMES = {
    "world", "backups", "logs", "crash-reports", "visualprospecting",
    "serverutilities", "journeymap", "blueprints", "coretweaks",
    "falsepattern", "nestedmods", "scripts",
    "server.properties", "ops.json", "whitelist.json",
    "banned-ips.json", "banned-players.json", "eula.txt",
    "usercache.json", "usernamecache.json", "server-icon.png",
    "startserver-java9.sh", "startserver-java9.bat", "java9args.txt",
}

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CHANGELOG_RE = re.compile(r"^changelog\s+from\s+(.+?)\s+to\s+(.+?)\.md$", re.IGNORECASE)
MSK_TZ = timezone(timedelta(hours=3))
