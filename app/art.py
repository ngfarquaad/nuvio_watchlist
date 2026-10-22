"""Artwork: TMDB sizing, the per-profile image cache, TMDB art lookup, poster crops."""

import hashlib
import io
import json
import os
import shutil
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image  # only needed for landscape images cropped from posters
except ImportError:  # pragma: no cover
    Image = None

HAVE_PIL = Image is not None

GENERATED_PREFIX = "poster-crop:"  # marks a generated image's "upstream URL" inside the addon
GENERATE_CROP_TOP = 0.05           # the 16:9 slice starts this far down the (border-trimmed) poster
GENERATE_SOURCE_SIZE = "w780"      # TMDB poster width the crop is cut from
WARM_WORKERS = 8
LOOKUP_WORKERS = 8
IMAGE_VALID_DAYS = 30              # a cached image is trusted this long before its source is asked whether it changed

_TMDB_RE = re.compile(r"^(https?://image\.tmdb\.org/t/p/)(?:original|w\d+|h\d+)(/.+)$")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")


def tmdb_resize(url, size):
    if (url or "").split("?", 1)[0].lower().endswith(".svg"):   # vector images have no smaller sizes
        return url
    match = _TMDB_RE.match(url or "")
    return f"{match.group(1)}{size}{match.group(2)}" if match else url


def upstream_art(kind, url, s):
    """Upstream artwork URL, sized by the profile's settings."""
    if not url:
        return None
    if kind == "poster" and s.PREFER_SMALLER_POSTERS_TMDB:
        return tmdb_resize(url, "w500")
    if kind == "landscape" and s.PREFER_SMALLER_LANDSCAPE_TMDB:
        return tmdb_resize(url, "w780")
    return url


def art_key(url):
    """Public file name for a cached image: a hash of the upstream URL plus an image
    extension. New artwork gets a new name, and the extension lets a CDN cache it."""
    path = url.split("?", 1)[0].lower()
    ext = next((e for e in _IMAGE_EXTS if path.endswith(e)), ".jpg")
    if url.startswith(GENERATED_PREFIX):
        ext = ".jpg"
    return hashlib.sha1(url.encode()).hexdigest()[:20] + ext


def real_url(url):
    """Where to send a client if a generated image can't be made: the portrait poster."""
    return url[len(GENERATED_PREFIX):] if url.startswith(GENERATED_PREFIX) else url


def trim_black_borders(img, dark=18, max_fraction=0.15):
    """Cut solid black bars off the edges of a poster. A row or column is border while
    at least ~99.5% of its pixels are near-black; at most max_fraction goes from a side."""
    box = Image.Resampling.BOX if hasattr(Image, "Resampling") else Image.BOX
    w, h = img.size
    bright = img.convert("L").point(lambda p: 255 if p >= dark else 0)
    rows = bright.resize((1, h), box).tobytes()
    cols = bright.resize((w, 1), box).tobytes()

    def border(values, limit):
        n = 0
        while n < limit and values[n] <= 1:
            n += 1
        return n

    top, bottom = border(rows, int(h * max_fraction)), border(rows[::-1], int(h * max_fraction))
    left, right = border(cols, int(w * max_fraction)), border(cols[::-1], int(w * max_fraction))
    if top or bottom or left or right:
        return img.crop((left, top, w - right, h - bottom))
    return img


def centre_crop(data):
    """JPEG bytes: the middle 16:9 band of a poster, which is what Nuvio shows when it has to fit a
    portrait poster into a landscape card (it scales to fill the card and crops from the centre)."""
    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGB")
    w, h = img.size
    tall = round(w * 9 / 16)
    top = max(0, (h - tall) // 2)
    out = io.BytesIO()
    img.crop((0, top, w, min(h, top + tall))).save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def thumbnail(jpeg, width=360):
    img = Image.open(io.BytesIO(jpeg))
    img.load()
    img = img.convert("RGB").resize((width, max(1, round(img.height * width / img.width))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


def landscape_from_poster(data):
    """JPEG bytes: a 16:9 slice of a portrait poster, starting 5% down after trimming borders."""
    img = Image.open(io.BytesIO(data))
    img.load()
    img = trim_black_borders(img.convert("RGB"))
    w, h = img.size
    tall = round(w * 9 / 16)
    if h > tall:
        top = min(round(h * GENERATE_CROP_TOP), h - tall)
        box = (0, top, w, top + tall)
    else:  # already 16:9 or wider: trim the sides instead
        wide = round(h * 16 / 9)
        left = max(0, (w - wide) // 2)
        box = (left, 0, left + wide, h)
    out = io.BytesIO()
    img.crop(box).save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def dir_info(root):
    """(image files, bytes) in a profile's cache folder. Only the images count, not their small info files."""
    files = size = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0, 0
    for name in names:
        if name.endswith((".json", ".tmp")):
            continue
        try:
            size += os.path.getsize(os.path.join(root, name))
            files += 1
        except OSError:
            pass
    return files, size


def dir_summary(root, known):
    """What a profile's cache folder holds: image files, bytes, and how many of the images are portrait posters,
    landscape images and landscape crops. Each image's info file says which it is; `known` remembers what has
    been read ({image name: (info file time, kind)}), so only new or changed info files are opened. Images with
    no kind recorded count toward the files and bytes only."""
    counts = {"poster": 0, "landscape": 0, "crop": 0}
    files = size = 0
    try:
        names = os.listdir(root)
    except OSError:
        known.clear()
        return {"files": 0, "bytes": 0, "portrait": 0, "landscape": 0, "crops": 0}
    for name in names:
        if name.endswith((".json", ".tmp")):
            continue
        path = os.path.join(root, name)
        try:
            size += os.path.getsize(path)
            files += 1
            stamp = os.stat(f"{path}.json").st_mtime_ns
        except OSError:
            stamp = None   # no info file: nothing to read
        seen = known.get(name)
        if seen is None or seen[0] != stamp:
            kind = None
            if stamp is not None:
                try:
                    with open(f"{path}.json") as fh:
                        kind = json.load(fh).get("kind")
                except (OSError, ValueError, AttributeError):
                    pass
            seen = known[name] = (stamp, kind)
        if seen[1] in counts:
            counts[seen[1]] += 1
    for name in [n for n in known if n not in names]:
        del known[name]
    return {"files": files, "bytes": size, "portrait": counts["poster"], "landscape": counts["landscape"],
            "crops": counts["crop"]}


def clear_dir(root):
    """Delete a profile's whole image cache. Returns (image files, bytes) that were there."""
    info = dir_info(root)
    shutil.rmtree(root, ignore_errors=True)
    return info


def human_size(n):
    """1536 -> '1.5 KB', for the log."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class PosterCache:
    """Disk cache for one profile. Files are named by a hash of the upstream URL, so new artwork is fetched
    fresh and files nobody references any more are removed on sync. A file is trusted for IMAGE_VALID_DAYS
    from the day it was stored; the first sync after that asks the source whether it changed."""

    def __init__(self, root, http, log, alive=lambda: True, deleted=lambda: False):
        self.root = root
        self.alive = alive      # False once the instance that owns this has been stopped: stop writing
        self.deleted = deleted  # True once the profile itself is gone: remove what is left
        self.http = http
        self.log = log
        self._locks = {}
        self._guard = threading.Lock()
        self._running = False   # a background sync is in progress
        self._next = None       # the link list asked for while that sync was running: it goes round once more
        self._failed = {}
        self.kinds = {}         # image link -> "poster" or "landscape", set by the catalog with every build
        self.on_done = None     # called when a background sync ends

    def _path(self, url):
        return os.path.join(self.root, hashlib.sha1(url.encode()).hexdigest())

    def _lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def _due(path):
        """True once a stored file is older than IMAGE_VALID_DAYS (its modified time is when it was stored or last confirmed)."""
        try:
            return time.time() - os.path.getmtime(path) > IMAGE_VALID_DAYS * 86400
        except OSError:
            return False

    def any_due(self, urls):
        return any(self._due(self._path(u)) for u in urls)

    @staticmethod
    def _prepare(url, resp):
        if url.startswith(GENERATED_PREFIX):
            return landscape_from_poster(resp.content), "image/jpeg"
        return resp.content, resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

    def _kind(self, url):
        """What an image is, for the cache status: "poster", "landscape" or "crop" (a generated landscape image)."""
        return "crop" if url.startswith(GENERATED_PREFIX) else self.kinds.get(url)

    def _note_kind(self, path, info, kind):
        """Record on a stored image what it is, when it was stored without that."""
        info = dict(info, kind=kind)
        try:
            with self._lock(path):
                with open(f"{path}.json", "w") as fh:
                    json.dump(info, fh)
        except OSError:
            pass
        return info

    def _write(self, path, content, ctype, resp, kind=None):
        os.makedirs(self.root, exist_ok=True)
        info = {"type": ctype, "etag": hashlib.md5(content).hexdigest()}
        if kind:
            info["kind"] = kind
        headers = getattr(resp, "headers", None) or {}
        if headers.get("ETag"):
            info["src_etag"] = headers["ETag"]            # what the source called this version, for the next check
        if headers.get("Last-Modified"):
            info["src_modified"] = headers["Last-Modified"]
        with open(f"{path}.tmp", "wb") as fh:
            fh.write(content)
        with open(f"{path}.json", "w") as fh:
            json.dump(info, fh)
        os.replace(f"{path}.tmp", path)
        return info

    def get(self, url):
        """(path, info) for url, downloading on a miss; None if unavailable. A stored file is served as it is,
        however old; the re-check happens in sync()."""
        path = self._path(url)
        cached = self._read(path)
        if cached:
            return cached
        if time.monotonic() - self._failed.get(path, -1e9) < 300:
            return None
        with self._lock(path):
            cached = self._read(path)
            if cached:
                return cached
            try:
                resp = self.http.get(real_url(url), timeout=20)
                resp.raise_for_status()
                content, ctype = self._prepare(url, resp)
                if not self.alive():
                    return None
                info = self._write(path, content, ctype, resp, self._kind(url))
                if self.deleted():  # the profile was deleted while this was downloading: leave nothing behind
                    shutil.rmtree(self.root, ignore_errors=True)
                    return None
                self._failed.pop(path, None)
                return path, info
            except Exception as exc:
                self._failed[path] = time.monotonic()
                if self.deleted():  # deleted mid-download: whatever was half written goes too
                    shutil.rmtree(self.root, ignore_errors=True)
                    return None
                if not self.alive():
                    return None
                self.log.warning("[cache-warming] fetch failed for %s: %s", url, exc)
                return None

    @staticmethod
    def _read(path):
        if not os.path.isfile(path):
            return None
        try:
            with open(f"{path}.json") as fh:
                return path, json.load(fh)
        except (OSError, ValueError):
            with open(path, "rb") as fh:
                return path, {"type": "image/jpeg", "etag": hashlib.md5(fh.read()).hexdigest()}

    def _recheck(self, url, path, cached):
        """Ask the source whether a stored file changed. Not modified (or identical bytes): keep it and start its
        days again. Changed: store the new bytes. Source unreachable: keep serving the old file; the next sync tries again."""
        info = cached[1]
        headers = {}
        if info.get("src_etag"):
            headers["If-None-Match"] = info["src_etag"]
        elif info.get("src_modified"):
            headers["If-Modified-Since"] = info["src_modified"]
        with self._lock(path):
            if not self._due(path):   # another thread just did it
                return self._read(path) or cached
            try:
                resp = self.http.get(real_url(url), headers=headers, timeout=20)
                if resp.status_code == 304:
                    os.utime(path)
                    return cached
                resp.raise_for_status()
                content, ctype = self._prepare(url, resp)
                if not self.alive():
                    return cached
                if hashlib.md5(content).hexdigest() == info.get("etag"):
                    os.utime(path)
                    return cached
                return path, self._write(path, content, ctype, resp, self._kind(url))
            except Exception as exc:
                self.log.debug("[cache-warming] could not re-check %s: %s", url, exc)
                return cached

    def refresh(self, url):
        if not self.alive():
            return None
        path = self._path(url)
        cached = self._read(path)
        if not cached:
            return self.get(url)
        kind = self._kind(url)
        if kind and cached[1].get("kind") != kind:
            cached = (path, self._note_kind(path, cached[1], kind))
        if self._due(path):
            return self._recheck(url, path, cached)
        return cached

    def sync(self, urls):
        wanted = {os.path.basename(self._path(u)) for u in urls}
        removed = 0
        if os.path.isdir(self.root):
            for name in os.listdir(self.root):
                if name.split(".", 1)[0] not in wanted:
                    try:
                        os.remove(os.path.join(self.root, name))
                        removed += 1
                    except OSError:
                        pass
        due = sum(1 for u in urls if self._due(self._path(u)))
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
            results = list(pool.map(self._warm, urls))
        sizes = [size for how, size in results if how == "new"]
        failed = sum(1 for how, _ in results if how == "failed")
        cached = sum(1 for how, _ in results if how == "cached")
        self.log.info("[cache-warming] %d/%d image(s) ready in %.1fs: %d downloaded (%s), %d already cached, "
                      "%d failed, %d stale file(s) removed, %d re-checked",
                      len(urls) - failed, len(urls), time.monotonic() - start, len(sizes), human_size(sum(sizes)),
                      cached, failed, removed, due)

    def _warm(self, url):
        """One image of a sync: ("new", bytes) if downloaded now, ("cached", 0) if already stored, ("failed", 0) if unavailable."""
        had = os.path.isfile(self._path(url))
        got = self.refresh(url)
        if not got:
            return "failed", 0
        if had:
            return "cached", 0
        try:
            return "new", os.path.getsize(got[0])
        except OSError:
            return "new", 0

    def syncing(self):
        return self._running

    def sync_in_background(self, urls):
        """Start a sync. One already running is not interrupted: it goes round again with this list when it ends."""
        with self._guard:
            if self._running:
                self._next = list(urls)
                return
            self._running = True

        def run():
            todo = list(urls)
            while todo is not None:
                try:
                    self.sync(todo)
                except Exception as exc:
                    self.log.warning("[cache-warming] sync failed: %s", exc)
                with self._guard:
                    todo, self._next = self._next, None
                    if todo is None:
                        self._running = False
            if self.on_done:
                self.on_done()
        threading.Thread(target=run, daemon=True).start()


TMDB_API = "https://api.themoviedb.org/3"
TMDB_ORIGINAL = "https://image.tmdb.org/t/p/original"


def _best(images):
    """The highest rated of a list of TMDB images, then the widest."""
    if not images:
        return None
    top = max(images, key=lambda b: (b.get("vote_average") or 0, b.get("width") or 0))
    return f"{TMDB_ORIGINAL}{top['file_path']}" if top.get("file_path") else None


def _in_language(images, language):
    return [i for i in images or [] if i.get("iso_639_1") == language]


class TmdbArt:
    """Looks up artwork per title on TMDB: a textless backdrop, plus a backdrop and a poster in the chosen
    language. Results are kept in a JSON file together with the language they were fetched in. An entry is
    looked up again when it is older than refresh_days or was fetched in a different language, and is still
    served until that succeeds."""

    def __init__(self, path, api_key, language, refresh_days, http, log, alive=lambda: True):
        self.path = path
        self.api_key = api_key
        self.language = language
        self.refresh_days = refresh_days
        self.http = http
        self.log = log
        self.alive = alive
        self._lock = threading.Lock()
        self.data = {}
        try:
            with open(path) as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _key(item):
        return f"{item.get('content_type') or 'movie'}:{item.get('content_id')}"

    def get(self, item):
        return self.data.get(self._key(item))

    @staticmethod
    def _lookup_id(item):
        return bool(re.match(r"^(tt\d+|tmdb:\d+)", str(item.get("content_id") or "")))

    def resolved(self, item):
        """True once TMDB has answered for this title (or it can't be looked up)."""
        return not self._lookup_id(item) or self._key(item) in self.data

    def _stale(self, entry):
        return time.time() - entry.get("at", 0) > self.refresh_days * 86400

    def _wants(self, item):
        if not self._lookup_id(item):
            return False
        entry = self.data.get(self._key(item))
        return entry is None or self._stale(entry) or entry.get("language") != self.language

    def _call(self, path, **params):
        headers = {}
        if "." in self.api_key:  # v4 read access token (a JWT)
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        for attempt in (1, 2):
            resp = self.http.get(f"{TMDB_API}{path}", params=params, headers=headers, timeout=20)
            if resp.status_code == 429 and attempt == 1:
                time.sleep(min(10.0, float(resp.headers.get("Retry-After", 2))))
                continue
            resp.raise_for_status()
            return resp.json()

    def _fetch(self, item):
        cid = str(item.get("content_id"))
        kinds = ["movie", "tv"] if (item.get("content_type") or "movie") == "movie" else ["tv", "movie"]
        try:
            tmdb_id = kind = None
            match = re.match(r"^tmdb:(\d+)", cid)
            if match:
                tmdb_id, kind = match.group(1), kinds[0]
            else:
                imdb_id = re.match(r"^(tt\d+)", cid).group(1)
                found = self._call(f"/find/{imdb_id}", external_source="imdb_id")
                for k in kinds:
                    hits = found.get(f"{k}_results") or []
                    if hits:
                        tmdb_id, kind = hits[0]["id"], k
                        break
            entry = {"at": time.time(), "language": self.language,
                     "background": None, "landscape": None, "poster": None}
            if tmdb_id:
                images = self._call(f"/{kind}/{tmdb_id}/images",
                                    include_image_language=f"{self.language},null")
                backdrops = images.get("backdrops") or []
                entry["background"] = _best([b for b in backdrops if not b.get("iso_639_1")])
                entry["landscape"] = _best(_in_language(backdrops, self.language))
                entry["poster"] = _best(_in_language(images.get("posters"), self.language))
            with self._lock:
                self.data[self._key(item)] = entry
            return True
        except Exception as exc:
            self.log.debug("[tmdb-art-refresh] lookup failed for %s: %s", cid, exc)
            return False

    def _save(self):
        if not self.alive():
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock:
                body = json.dumps(self.data, separators=(",", ":"))
            with open(f"{self.path}.tmp", "w") as fh:
                fh.write(body)
            os.replace(f"{self.path}.tmp", self.path)
        except OSError as exc:
            self.log.warning("[tmdb-art-refresh] could not save %s: %s", self.path, exc)

    @staticmethod
    def _links(entry):
        entry = entry or {}
        return (entry.get("poster"), entry.get("landscape"), entry.get("background"))

    def pending(self, items):
        """Titles whose record is missing, older than the lookup TTL, or made in another language."""
        return [i for i in items if self._wants(i)]

    def next_due(self, items):
        """When the soonest stored record of these titles runs past the lookup TTL (epoch seconds), or None."""
        due = None
        for i in items:
            entry = self.data.get(self._key(i)) if self._lookup_id(i) else None
            if entry:
                at = entry.get("at", 0) + self.refresh_days * 86400
                due = at if due is None else min(due, at)
        return due

    def lookup(self, todo):
        """Look these titles up, LOOKUP_WORKERS at a time, and save. Returns (changed, failed): how many titles
        ended with different artwork links than before, and how many lookups did not go through."""
        started = time.monotonic()
        before = {self._key(i): self._links(self.data.get(self._key(i))) for i in todo}
        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            results = list(pool.map(self._fetch, todo))
        self._save()
        changed = sum(1 for i in todo if self._links(self.data.get(self._key(i))) != before[self._key(i)])
        failed = results.count(False)
        self.log.info("[tmdb-art-refresh] %d/%d title(s) looked up in %.1fs: %d with new artwork, %d failed",
                      len(todo) - failed, len(todo), time.monotonic() - started, changed, failed)
        return changed, failed

    def prune(self, items):
        """Forget the records of titles that are no longer in the Library."""
        keep = {self._key(i) for i in items}
        with self._lock:
            gone = [k for k in self.data if k not in keep]
            for k in gone:
                del self.data[k]
        if gone:
            self._save()
            self.log.info("[tmdb-art-refresh] removed %d record(s) of titles no longer in the Library", len(gone))
