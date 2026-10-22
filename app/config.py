"""Server settings (environment) and the two settings schemas the web editor edits.

GENERAL_FIELDS belong to the Nuvio account this server signs in with; PROFILE_FIELDS belong to each
add-on profile. Each schema drives three things at once: validation on save, the defaults a new profile
starts with, and the form the editor draws.
"""

import os

from .languages import LANGUAGES

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")


def parse_bool(value):
    """True or False for a boolean or its usual spellings; None for anything else."""
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    return True if word in TRUE_WORDS else False if word in FALSE_WORDS else None


class ServerConfig:
    """Things that belong to the container, not to the account or a profile."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.admin_password = env.get("ADMIN_PASSWORD", "")
        self.data_dir = (env.get("DATA_DIR", "/data").rstrip("/")) or "/data"
        self.host_name = env.get("HOST_NAME", "").strip().rstrip("/")
        try:
            port = int(env.get("PORT", 7000))
        except ValueError:
            port = 7000
        self.port = port if 1 <= port <= 65535 else 7000
        self.log_level = env.get("LOG_LEVEL", "INFO").upper()
        self.nuvio_base_url = env.get("NUVIO_BASE_URL", "https://api.nuvio.tv").rstrip("/")
        self.nuvio_api_key = env.get("NUVIO_API_KEY", "").strip()   # Nuvio's public key, the same for every user

    def problems(self):
        found = []
        if not self.admin_password.strip():
            found.append("ADMIN_PASSWORD is not set (it protects the web editor)")
        if not self.nuvio_api_key:
            found.append("NUVIO_API_KEY is not set (Nuvio's public API key; see the compose example)")
        return found


def _f(key, label, kind, default, group, help, **extra):
    return dict(key=key, label=label, kind=kind, default=default, group=group, help=help, **extra)


# ---------------------------------------------------------------- general (the account)
GENERAL_GROUPS = [
    ("account", "Nuvio Account", "Your Nuvio login credentials."),
    ("tmdb", "TMDB", "Without a key, artwork comes from your Nuvio Library. A key adds TMDB posters and "
                     "title-text landscape images in your chosen language.", "Optional"),
    ("danger", "Danger Zone", "Permanently delete your account and all associated data. This action is irreversible."),
]

GENERAL_FIELDS = [
    _f("NUVIO_EMAIL", "Email", "text", "", "account",
       "The email address you sign in to Nuvio with.", required=True, placeholder="you@example.com", maxlength=254),
    _f("NUVIO_PASSWORD", "Password", "secret", "", "account",
       "Your Nuvio password. It is stored on this server so the add-on can sign in for you.",
       required=True, maxlength=256, keep_spaces=True),
    _f("TMDB_API_KEY", "TMDB API Key", "secret", "", "tmdb",
       "A v3 API key or a v4 read access token.", maxlength=512),
]

# ---------------------------------------------------------------- per profile
# Nuvio's own limit (MAX_PROFILES in the Nuvio TV app): its profiles are numbered 1 to 6.
NUVIO_MAX_PROFILES = 6

PROFILE_GROUPS = [
    ("catalog", "Catalog", "How this catalog appears in Nuvio."),
    ("refresh", "Library Sync",
     "Controls when this catalog is refreshed from your Nuvio Library. Recently added or removed titles "
     "appear once the catalog has been refreshed."),
    ("images", "Artwork Cache Warming",
     "Downloads artwork to this server and serves it to Nuvio instead of the original source. An image is "
     "downloaded again only when its source artwork changes."),
    ("tmdb", "TMDB Integration", "How artwork is sourced from TMDB. Requires a TMDB API key on the Account tab."),
]

SORTS = [
    {"value": "added_desc", "label": "Newest Added First"},
    {"value": "added_asc", "label": "Oldest Added First"},
    {"value": "name", "label": "Name, A to Z"},
    {"value": "name_desc", "label": "Name, Z to A"},
    {"value": "released_desc", "label": "Newest Released First"},
    {"value": "released_asc", "label": "Oldest Released First"},
    {"value": "random", "label": "Random"},
]
SHAPES = [
    {"value": "auto", "label": "Automatic"},
    {"value": "poster", "label": "Portrait"},
    {"value": "landscape", "label": "Landscape"},
]
NUVIO_PROFILES = [{"value": str(n), "label": str(n)} for n in range(1, NUVIO_MAX_PROFILES + 1)]

# "depends" greys a field out unless every listed field has the listed value. The saved value is kept,
# except for fields marked clears_when_off, which are switched off (set back to their default) instead.
PROFILE_FIELDS = [
    _f("NUVIO_PROFILE", "Nuvio Profile", "choice", "1", "profile",
       "Which of your Nuvio profiles this catalog shows the library of, and installs to. "
       "Nuvio numbers its profiles 1 to 6, in the order they were created.", choices=NUVIO_PROFILES),

    _f("CATALOG_COMBINE_TYPES", "Combine Movies and Series", "bool", False, "catalog",
       "Show one row with both, instead of a separate Movies row and Series row."),
    # "shows_when" hides a field (not just greys it) unless every listed field has the listed value;
    # a hidden field keeps its saved value.
    _f("CATALOG_NAME", "Catalog Name", "text", "Nuvio Watchlist", "catalog",
       "The name of the row on Nuvio's home screen.",
       maxlength=60, shows_when=[{"key": "CATALOG_COMBINE_TYPES", "value": True}]),
    _f("CATALOG_MOVIES_NAME", "Movies Catalog Name", "text", "Nuvio Watchlist - Movies", "catalog",
       "The name of the Movies row on Nuvio's home screen.",
       maxlength=60, shows_when=[{"key": "CATALOG_COMBINE_TYPES", "value": False}]),
    _f("CATALOG_SERIES_NAME", "Series Catalog Name", "text", "Nuvio Watchlist - Series", "catalog",
       "The name of the Series row on Nuvio's home screen.",
       maxlength=60, shows_when=[{"key": "CATALOG_COMBINE_TYPES", "value": False}]),
    _f("CATALOG_SHOW_IN_HOME", "Show on the Home Screen", "bool", True, "catalog",
       "Display the Combined row on Nuvio's home screen. Turn off to keep it only in Nuvio's catalog list.",
       help_alt={"when": {"key": "CATALOG_COMBINE_TYPES", "value": False},
                 "text": "Display the Movies and Series rows on Nuvio's home screen. Turn off to keep them only in Nuvio's catalog list."}),
    _f("CATALOG_SORT", "Sort Order", "choice", "added_desc", "catalog",
       "The order titles appear in.", choices=SORTS),
    _f("CATALOG_POSTER_SHAPE", "Card Shape", "choice", "auto", "catalog",
       "Sets each row's card shape. Automatic uses the shape saved from your Library. Portrait or Landscape forces "
       "that shape for every title. Nuvio's own landscape setting overrides this when enabled.", choices=SHAPES),
    _f("CATALOG_LIST_ITEMS_SIZE", "Titles per Page", "int", 20, "catalog",
       "How many titles Nuvio loads at a time.", min=1, max=100, digits=True),

    _f("CATALOG_TTL", "Catalog Cache TTL", "int", 3600, "refresh",
       "How long the catalog remains cached (in seconds) before it expires. Click **Refresh Catalog** to update "
       "it manually.",
       min=300, max=604800, display="duration", digits=True),
    _f("CATALOG_WARMUP_ENABLED", "Refresh in Background", "bool", True, "refresh",
       "Refreshes the catalog automatically as soon as the cache expires, so Nuvio is always served from the "
       "cache. When off, an expired catalog is refreshed on the next fetch from Nuvio, which can take several "
       "seconds depending on library size."),

    _f("CACHE_WARM_POSTERS", "Cache Posters", "bool", False, "images",
       "Download posters to this server."),
    # tool="clear_cache": the editor adds the Cache Storage row (a button, not a setting) right after this one
    _f("CACHE_WARM_LANDSCAPE", "Cache Landscape Images", "bool", False, "images",
       "Download landscape images to this server. Required for landscape images cropped from posters.",
       tool="clear_cache"),

    _f("TMDB_ART_LANGUAGE", "Artwork Language", "text", "en", "tmdb",
       "Two-letter language code (for example, en) used to pick TMDB posters and landscape images with the title "
       "printed on them. Titles with no image in that language keep their Nuvio Library poster, and use a "
       "textless image for landscape.", short=True, maxlength=2, placeholder="en", language=True, needs_key=True),
    _f("TMDB_REFRESH_DAYS", "Artwork Lookup TTL", "int", 14, "tmdb",
       "How often each title is queried again for new artwork.", min=7, max=365, unit="days", digits=True,
       needs_key=True),
    _f("PREFER_SMALLER_POSTERS_TMDB", "Smaller Posters", "bool", True, "tmdb",
       "Use 500-pixel-wide posters instead of the original size.", needs_key=True),
    _f("PREFER_SMALLER_LANDSCAPE_TMDB", "Smaller Landscape Images", "bool", True, "tmdb",
       "Use 780-pixel-wide landscape images instead of the original.", needs_key=True),
    _f("GENERATE_LANDSCAPE_FROM_POSTER", "Crop Landscape Images from Posters", "bool", False, "tmdb",
       "For titles with no landscape artwork, crop one from the poster image using poster-focused framing. A "
       "cropped image is replaced by official landscape artwork once a lookup finds it.",
       badge="Cache Landscape Images Required",
       depends=[{"key": "CACHE_WARM_LANDSCAPE", "value": True}],
       clears_when_off=True, tool="crop_test", needs_key=True),
]



def defaults(fields):
    return {f["key"]: f["default"] for f in fields}


def schema_for_ui():
    return {
        "general": {"groups": [{"id": g[0], "title": g[1], "help": g[2], "badge": g[3] if len(g) > 3 else ""} for g in GENERAL_GROUPS], "fields": GENERAL_FIELDS},
        "languages": LANGUAGES,
        "profile": {"groups": [{"id": g[0], "title": g[1], "help": g[2], "note": g[3] if len(g) > 3 else ""}
                               for g in PROFILE_GROUPS], "fields": PROFILE_FIELDS},
    }


def clean_settings(fields, raw, existing=None, enforce_required=False):
    """Validate a settings dict against a field list. Returns (settings, errors).

    A key missing from `raw` keeps its existing value (or the default). A blank value becomes the default
    (a blank secret clears it). Text must be text and within its maximum length, whole numbers must be digits
    within their minimum and maximum, and choices must be one of the listed values."""
    raw = raw if isinstance(raw, dict) else {}
    base = existing or {}
    out, errors = {}, {}
    for f in fields:
        key, kind = f["key"], f["kind"]
        if key not in raw:
            out[key] = base.get(key, f["default"])
            continue
        value = raw[key]
        blank = value is None or (isinstance(value, str) and value.strip() == "")
        if blank:
            out[key] = "" if kind == "secret" else f["default"]
        elif kind == "bool":
            flag = parse_bool(value)
            if flag is None:
                errors[key] = "Choose On or Off."
            out[key] = f["default"] if flag is None else flag
        elif kind in ("text", "secret"):
            if not isinstance(value, str):
                if f.get("language"):   # anything that is not a listed code is English
                    out[key] = "en"
                else:
                    errors[key] = "Enter text."
                continue
            text = value if f.get("keep_spaces") else value.strip()
            if f.get("maxlength") and not f.get("language") and len(text) > f["maxlength"]:
                errors[key] = f"Use at most {f['maxlength']} characters."
            out[key] = text
        elif kind == "int":
            if isinstance(value, bool) or not (isinstance(value, int) or (str(value).strip().isascii() and str(value).strip().isdigit())):
                errors[key] = "Enter a whole number, digits only."
                continue
            number = int(value)
            if "min" in f and number < f["min"]:
                errors[key] = f"Must be at least {f['min']:,}."
            elif "max" in f and number > f["max"]:
                errors[key] = f"Must be at most {f['max']:,}."
            out[key] = number
        elif kind == "choice":
            if str(value) not in [c["value"] for c in f["choices"]]:
                errors[key] = "Pick one of the listed options."
            out[key] = str(value)
    for f in fields:   # a setting that can't run without others is off, and not just greyed out, while they are off
        if f.get("clears_when_off") and any(out.get(d["key"]) != d["value"] for d in f.get("depends", [])):
            out[f["key"]] = f["default"]
    if "TMDB_ART_LANGUAGE" in out:   # a blank or unlisted language is English, with no error
        language = str(out["TMDB_ART_LANGUAGE"]).strip().lower().split("-")[0]
        out["TMDB_ART_LANGUAGE"] = language if language in LANGUAGES else "en"
    if enforce_required:
        for f in fields:
            if f.get("required") and not out.get(f["key"]):
                errors.setdefault(f["key"], "Required.")
    return out, errors


def general_ready(general):
    return all(general.get(k) for k in ("NUVIO_EMAIL", "NUVIO_PASSWORD"))
