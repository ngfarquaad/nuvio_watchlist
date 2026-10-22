"""One running profile: its Nuvio client, caches, catalog, background refresh and manifest."""

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import types

import requests

from . import art
from .config import general_ready
from .nuvio import NuvioClient

log = logging.getLogger("nuvio_watchlist")
VERSION = "1.0.0"
EXPIRED_GRACE = 120   # seconds past expiry before a request rebuilds for itself because the schedule seems stuck

# meta field -> image cache class; the class is also the <kind> in /poster-cache/<kind>/<file>.
# A catalog entry carries only a poster and a landscape image (no background or logo: this add-on is not a
# metadata source, and Nuvio gets those from the user's metadata add-on), and both can be cached.
ART_FIELDS = {"poster": "poster", "landscapePoster": "landscape"}
ART_KINDS = ("poster", "landscape")
LOOKUP_RETRY = 900   # seconds the timer waits before another lookup pass after some lookups failed


class Tagged(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        tag = self.extra["tag"]
        return f"[{tag() if callable(tag) else tag}] {msg}", kwargs


class Instance:
    def __init__(self, row, general, server, tag_of=None):
        uid = row["id"]
        self.uid = uid
        self.tag_of = tag_of
        self.number = row["number"]              # this profile's number in the editor; unrelated to Nuvio's
        self.label = row["label"]
        self.manifest_id = f"org.nuvio.watchlist.{uid[:8]}"
        self.s = types.SimpleNamespace(**row["settings"])
        self.nuvio_profile = int(self.s.NUVIO_PROFILE)   # which Nuvio profile's library this catalog shows
        self.server = server
        self.log = Tagged(log, {"tag": self.log_tag})
        self.http = requests.Session()
        self.stop_event = threading.Event()
        self.deleted = False                      # set when the profile itself is deleted (a save only replaces the instance)
        self.account = general.get("NUVIO_EMAIL", "")
        s = self.s

        self.needs_setup = not general_ready(general)
        self.dormant = not row.get("activated", True)   # new and never saved, refreshed or installed: fetch nothing yet
        self.client = NuvioClient(self.http, server.nuvio_base_url, server.nuvio_api_key,
                                  general.get("NUVIO_EMAIL", ""), general.get("NUVIO_PASSWORD", ""))
        self.cache_root = os.path.join(server.data_dir, "poster-cache", uid)
        self.tmdb_file = os.path.join(server.data_dir, "tmdb-art", f"{uid}.json")
        tmdb_key = general.get("TMDB_API_KEY", "")
        self.tmdb = (art.TmdbArt(self.tmdb_file, tmdb_key, s.TMDB_ART_LANGUAGE, s.TMDB_REFRESH_DAYS,
                                 self.http, self.log, alive=lambda: not self.stop_event.is_set())
                     if tmdb_key else None)
        self.poster_cache = (art.PosterCache(self.cache_root, self.http, self.log,
                                         alive=lambda: not self.stop_event.is_set(), deleted=lambda: self.deleted)
                             if (s.CACHE_WARM_POSTERS or s.CACHE_WARM_LANDSCAPE) else None)
        if self.poster_cache:
            self.poster_cache.on_done = self._forget_cache_size

        self.warnings = []
        self._kinds_seen = {}       # what the cache status has already read from the cache folder's info files
        # Cropping needs Cache Landscape Images on, and a TMDB key (without a key nothing TMDB-related runs, and the
        # crop is cut from a TMDB poster); clean_settings switches it off when landscape caching is off
        self.generate_active = bool(s.GENERATE_LANDSCAPE_FROM_POSTER and art.HAVE_PIL
                                    and s.CACHE_WARM_LANDSCAPE and self.tmdb)
        if s.GENERATE_LANDSCAPE_FROM_POSTER and not art.HAVE_PIL:
            self.warnings.append("Landscape images from posters are switched off because Pillow is not installed.")

        # catalog state
        self.metas = None
        self.by_id = {}
        self.art_index = {}
        self.expires_at = 0.0       # monotonic
        self.built_at = 0.0         # wall clock
        self.last_error = None
        self._queued = False        # a manual refresh has been asked for and has not started yet
        self._build_lock = threading.Lock()
        self._failed_at = -1e9
        self._art_urls = None
        self._generated = None
        self.crop_count = 0
        self._items = []            # the Library titles of the last build, which the artwork lookup job works through
        self._lookup_lock = threading.Lock()
        self._lookup_again = False
        self._lookup_hold = 0.0     # monotonic time before which the timer does not start another lookup pass (after failures)
        self._cache_memo = None

        self._make_manifest()

    # ------------------------------------------------------------ manifest
    def _entry(self, cid, ctype, name):
        return {"id": cid, "type": ctype, "name": name, "pageSize": self.s.CATALOG_LIST_ITEMS_SIZE,
                "extra": [{"name": "skip"}], "showInHome": self.s.CATALOG_SHOW_IN_HOME}

    def _make_manifest(self):
        s = self.s
        if not s.CATALOG_COMBINE_TYPES:
            catalogs = [self._entry("nuvio_watchlist_movie", "movie", s.CATALOG_MOVIES_NAME),
                        self._entry("nuvio_watchlist_series", "series", s.CATALOG_SERIES_NAME)]
            self.filters = {"nuvio_watchlist_movie": "movie", "nuvio_watchlist_series": "series"}
        else:
            catalogs = [self._entry("nuvio_watchlist", "all", s.CATALOG_NAME)]
            self.filters = {"nuvio_watchlist": None}
        manifest = {
            "id": self.manifest_id, "version": VERSION, "name": "Nuvio Watchlist",
            "description": "Display your own Nuvio Library as a catalog on your home screen or in a collection folder. Choose the row layout, sort order, card shape and artwork with TMDB integration.", "resources": ["catalog"],
            "types": sorted({c["type"] for c in catalogs}), "idPrefixes": ["tt", "tmdb:"],
            "catalogs": catalogs,
            "behaviorHints": {"configurable": True, "configurationRequired": False},
        }
        self.manifest_body = json.dumps(manifest, separators=(",", ":"))
        self.manifest_etag = 'W/"' + hashlib.md5(self.manifest_body.encode()).hexdigest() + '"'

    # ------------------------------------------------------------ artwork
    def cache_enabled_for(self, kind):
        s = self.s
        return {"poster": s.CACHE_WARM_POSTERS, "landscape": s.CACHE_WARM_LANDSCAPE}.get(kind, False)

    def cacheable_art(self, meta):
        """(field, class, upstream url) for every image of a meta the cache holds."""
        for field, kind in ART_FIELDS.items():
            url = meta.get(field)
            if not url:
                continue
            if field == "poster" and meta.get("posterShape") == "landscape" and self.cache_enabled_for("landscape"):
                kind = "landscape"
            if self.cache_enabled_for(kind):
                yield field, kind, url

    def with_cached_art(self, meta, base):
        """Point cacheable artwork at this server. Anything left over that is only a marker for a generated
        image (settings changed since the catalog was built) goes back to the real poster URL."""
        out = dict(meta)
        cached = set()
        if self.poster_cache:
            for field, kind, url in self.cacheable_art(meta):
                out[field] = f"{base}/poster-cache/{kind}/{art.art_key(url)}"
                cached.add(field)
        for field in ART_FIELDS:
            url = out.get(field)
            if field not in cached and isinstance(url, str) and url.startswith(art.GENERATED_PREFIX):
                out[field] = art.real_url(url)
        return out

    # ------------------------------------------------------------ metas
    def sort_items(self, items):
        mode = self.s.CATALOG_SORT
        if mode == "added_desc":
            return sorted(items, key=lambda i: i.get("added_at") or 0, reverse=True)
        if mode == "added_asc":
            return sorted(items, key=lambda i: i.get("added_at") or 0)
        if mode == "name":
            return sorted(items, key=lambda i: (i.get("name") or "").lower())
        if mode == "name_desc":
            return sorted(items, key=lambda i: (i.get("name") or "").lower(), reverse=True)
        if mode in ("released_desc", "released_asc"):
            # the year in release_info ("2026", "2023-"); titles without one go last either way, and titles
            # from the same year keep their newest-added-first order (the sort is stable)
            def year(item):
                found = re.match(r"\s*(\d{4})", str(item.get("release_info") or ""))
                return int(found.group(1)) if found else None
            newest_added = sorted(items, key=lambda i: i.get("added_at") or 0, reverse=True)
            sign = -1 if mode == "released_desc" else 1
            return sorted(newest_added, key=lambda i: (1, 0) if year(i) is None else (0, sign * year(i)))
        if mode == "random":   # shuffled on every refresh, then fixed until the next one so paging stays consistent
            shuffled = list(items)
            random.shuffle(shuffled)
            return shuffled
        return list(items)

    def _usable(self, library):
        """The Library titles this catalog can show: those with an id that are movies or series. Nuvio calls a series
        "tv" in places, so that counts as a series; anything else (a live channel, say) is left out in both row modes."""
        out, skipped = [], 0
        for item in library:
            kind = str(item.get("content_type") or "movie").strip().lower()
            kind = "series" if kind == "tv" else kind
            if not item.get("content_id"):
                continue
            if kind not in ("movie", "series"):
                skipped += 1
                continue
            out.append({**item, "content_type": kind})
        if skipped:
            self.log.debug("left out %d title(s) that are not movies or series", skipped)
        return out

    def build_meta(self, item):
        s = self.s
        mode = s.CATALOG_POSTER_SHAPE
        shape = (item.get("poster_shape") or "poster").lower()
        if shape not in ("poster", "landscape", "square"):
            shape = "poster"
        found = (self.tmdb.get(item) if self.tmdb else None) or {}
        # TMDB artwork in the chosen language comes first; the Nuvio Library's own image is the fallback.
        # (A textless TMDB backdrop, or the Library's background, only serves as a landscape image when no
        # title-text one exists. The background is also sent as it is, in its own field.)
        poster_src = found.get("poster") or item.get("poster")
        landscape_src = found.get("landscape") or found.get("background") or item.get("background")
        if (not landscape_src and self.generate_active and shape != "landscape" and poster_src
                and self.tmdb.resolved(item)):
            landscape_src = art.GENERATED_PREFIX + art.tmdb_resize(poster_src, art.GENERATE_SOURCE_SIZE)
        # Resizing TMDB links is part of the TMDB integration: with no key, links go out exactly as the Library has them
        poster = art.upstream_art("poster", poster_src, s) if self.tmdb else poster_src
        landscape = art.upstream_art("landscape", landscape_src, s) if self.tmdb else landscape_src
        if mode == "poster":
            shape = "poster"
        elif mode == "landscape":
            if shape != "landscape" and landscape:
                poster = landscape
            shape = "landscape"
        if shape == "landscape":
            # a landscape card gets landscape art; with TMDB on, "poster" would otherwise be TMDB's portrait poster
            poster = (landscape if self.tmdb else None) or poster or landscape
            landscape = landscape or poster
        meta = {
            "id": item["content_id"], "type": item["content_type"], "name": item.get("name"),
            "poster": poster, "landscapePoster": landscape, "posterShape": shape,
            # background and logo go out exactly as the Nuvio Library stores them (no lookups, no caching, no
            # resizing); Nuvio's home hero carousel only takes titles that have a background
            "background": item.get("background"), "logo": item.get("logo"),
            "description": item.get("description"),
            "releaseInfo": item.get("release_info"), "genres": item.get("genres") or None,
        }
        try:
            if item.get("imdb_rating") is not None:
                meta["imdbRating"] = f"{float(item['imdb_rating']):.1f}"
        except (TypeError, ValueError):
            pass
        return {k: v for k, v in meta.items() if v not in (None, "", [])}

    def log_tag(self):
        """The name that starts this profile's log lines (the registry adds the id when two profiles share a name)."""
        return self.tag_of(self.uid, self.label) if self.tag_of else self.label

    # ------------------------------------------------------------ building
    def _build(self, source):
        start = time.monotonic()
        items = self.sort_items(self._usable(self.client.fetch_library(self.nuvio_profile)))
        if self.stop_event.is_set():
            return
        metas = [self.build_meta(i) for i in items]

        if self.metas and not metas:
            self.log.warning("[%s] Nuvio returned an empty library; keeping the previous %d item(s)",
                             source, len(self.metas))
            self.expires_at = time.monotonic() + self.s.CATALOG_TTL
            return

        old_ids = set(self.by_id) if self.metas is not None else None
        self._items = items
        if self.tmdb and items:   # a title that has left the Library takes its lookup record with it
            self.tmdb.prune(items)
        self.metas = metas
        self.by_id = {m["id"]: m for m in metas}
        self.art_index = {art.art_key(url): url for m in metas for _f, _k, url in self.cacheable_art(m)}
        self.expires_at = time.monotonic() + self.s.CATALOG_TTL
        self.built_at = time.time()

        if self.generate_active:
            generated = sum(1 for m in metas if str(m.get("landscapePoster", "")).startswith(art.GENERATED_PREFIX))
            self.crop_count = generated
            if generated != self._generated:
                self._generated = generated
                self.log.info("[landscape-from-poster] %d title(s) have no landscape art and use a poster crop",
                              generated)

        took = time.monotonic() - start
        new_ids = set(self.by_id)
        movies = sum(1 for m in metas if m.get("type") == "movie")
        series = sum(1 for m in metas if m.get("type") == "series")
        split = f"{movies} movie, {series} series"
        if old_ids is None:
            self.log.info("[%s] catalog built: %d item(s) (%s) from Nuvio profile %d in %.2fs", source, len(metas),
                          split, self.nuvio_profile, took)
        elif old_ids != new_ids:
            self.log.info("[%s] catalog rebuilt: +%d / -%d, %d item(s) (%s) in %.2fs", source,
                          len(new_ids - old_ids), len(old_ids - new_ids), len(metas), split, took)

        if self.poster_cache:
            self.poster_cache.kinds = {url: kind for m in metas for _f, kind, url in self.cacheable_art(m)}
            urls = sorted(self.poster_cache.kinds)
            # sync when the set of links changed (new downloads, files of removed titles deleted), or when a stored
            # image has passed its 30 days and is due a check; a sync still running goes round again with the new list
            if urls != self._art_urls or self.poster_cache.any_due(urls):
                self.poster_cache.sync_in_background(urls)
                self._art_urls = urls
        self.kick_lookups()

    def rebuild(self, source, force=False):
        """Blocking rebuild. Concurrent callers wait for the one in flight. force=True waits for it and then builds
        again, for a caller whose new data the build in flight may have missed."""
        if not self._build_lock.acquire(blocking=False):
            with self._build_lock:
                if not force:
                    return
                self._build_locked(source)
                return
        try:
            self._build_locked(source)
        finally:
            self._build_lock.release()

    def _forget_cache_size(self):
        self._cache_memo = None

    def _build_locked(self, source):
        self._cache_memo = None   # the size is read fresh once a refresh has started
        try:
            self._build(source)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)[:300]
            raise

    def rebuild_in_background(self, source="manual"):
        if self.needs_setup or self.dormant:
            return

        def run():
            try:
                self.rebuild(source)
            except Exception as exc:
                self.log.warning("[%s] rebuild failed: %s", source, exc)
            finally:
                self._queued = False
        self._queued = True   # so the editor sees "refreshing" straight away, before the thread has the lock
        threading.Thread(target=run, daemon=True).start()

    def get(self):
        """The saved catalog. With background refresh on, the schedule (see _run) rebuilds it, so Nuvio never
        waits except on the very first request or if the schedule has been failing. With it off, the first
        request after expiry rebuilds it."""
        s = self.s
        if self.needs_setup:
            raise RuntimeError("Add your Nuvio details on the Account tab first.")
        if self.dormant:
            raise RuntimeError("Choose this profile's Nuvio profile number, then save it or press Refresh Catalog.")
        remaining = self.expires_at - time.monotonic()
        grace = EXPIRED_GRACE if s.CATALOG_WARMUP_ENABLED else 0   # with the schedule on, a little past expiry it is mid-rebuild
        if self.metas is not None and remaining > -grace:
            return self.metas
        if self.metas is None and time.monotonic() - self._failed_at < 30:
            raise RuntimeError("Nuvio unavailable (retrying shortly)")
        try:
            self.rebuild("catalog request")
        except Exception as exc:
            if self.metas is None:
                self._failed_at = time.monotonic()
                raise
            self.log.warning("Rebuild failed, serving the expired catalog: %s", exc)
            self.expires_at = time.monotonic() + min(300, s.CATALOG_TTL)
        return self.metas or []

    # ------------------------------------------------------------ background refresh
    def adopt(self, old):
        """Saving settings replaces the instance but not the catalog: keep serving what the old one built, with
        its refresh time, until the next refresh builds it with the new settings. A catalog from a different
        Nuvio account or profile is not carried over."""
        if old.metas is None or old.account != self.account or old.nuvio_profile != self.nuvio_profile:
            return
        self.metas, self.by_id = old.metas, old.by_id
        self.built_at, self.expires_at = old.built_at, old.expires_at
        self.crop_count, self._generated = old.crop_count, old._generated
        self._items = old._items
        self.last_error = old.last_error
        self.art_index = {art.art_key(url): url for m in self.metas for _f, _k, url in self.cacheable_art(m)}

    def start(self, build=True):
        """build=False (after a save) builds nothing now: the catalog refreshes when its time is up, when Nuvio
        asks for one that has none, or when Refresh Catalog is pressed."""
        if self.needs_setup or self.dormant:
            return
        threading.Thread(target=self._run, args=(build,), daemon=True).start()
        if self.tmdb:
            threading.Thread(target=self._lookup_timer, daemon=True).start()

    def stop(self):
        self.stop_event.set()

    def _run(self, build=True):
        """Build when the server starts; with background refresh on, then rebuild whenever the cache time is up while requests keep getting the saved copy."""
        failed = False
        if build:
            try:
                self.rebuild("startup")
            except Exception as exc:
                failed = True
                self.log.warning("[startup] build failed, will retry: %s", exc)
        if not self.s.CATALOG_WARMUP_ENABLED:
            return
        if failed and self.stop_event.wait(300):
            return
        self._warm_loop(wait_for_first=not build)

    def _warm_loop(self, wait_for_first=False):
        while not self.stop_event.is_set():
            if wait_for_first and self.metas is None:   # nothing was built yet: the timer starts with the first catalog
                if self.stop_event.wait(5):
                    return
                continue
            wait = self.expires_at - time.monotonic()
            if wait > 0:
                if self.stop_event.wait(min(wait, 300)):
                    return
                continue
            try:
                self.rebuild("startup" if self.metas is None else "scheduled")
            except Exception as exc:
                self.log.warning("[scheduled] rebuild failed, retrying in 5 minutes: %s", exc)
                if self.stop_event.wait(300):
                    return

    # ------------------------------------------------------------ artwork lookups
    # A job of its own: a catalog build never waits for TMDB. After every build (so new Library titles are looked
    # up straight away) and whenever a stored record passes the lookup TTL, the job looks up the titles that need
    # it, and the catalog is rebuilt in the background only if a title's artwork link actually changed.
    def kick_lookups(self, timer=False):
        if not self.tmdb or self.stop_event.is_set():
            return
        if timer and time.monotonic() < self._lookup_hold:
            return
        if not self._lookup_lock.acquire(blocking=False):
            self._lookup_again = True   # one is running: it goes round once more when it is done
            return
        threading.Thread(target=self._lookup_run, daemon=True).start()

    def _lookup_run(self):
        try:
            while not self.stop_event.is_set():
                self._lookup_again = False
                todo = self.tmdb.pending(self._items)
                if not todo:
                    return
                changed, failed = self.tmdb.lookup(todo)
                if failed:
                    self._lookup_hold = time.monotonic() + LOOKUP_RETRY
                if changed and not self.stop_event.is_set():
                    try:
                        self.rebuild("artwork", force=True)
                    except Exception as exc:
                        self.log.warning("[artwork] rebuild failed: %s", exc)
                if failed or not self._lookup_again:
                    return
        finally:
            self._lookup_lock.release()

    def _lookup_timer(self):
        """Wakes the job when the soonest stored record runs past the lookup TTL."""
        while not self.stop_event.is_set():
            wait = 300.0
            due = self.tmdb.next_due(self._items) if self._items else None
            if due is not None:
                left = due - time.time()
                if left <= 0:
                    self.kick_lookups(timer=True)
                else:
                    wait = min(wait, left)
            if self.stop_event.wait(max(wait, 1.0)):
                return

    # ------------------------------------------------------------ image cache size and clearing
    def caching(self):
        """True while images are being downloaded to the cache."""
        return bool(self.poster_cache and self.poster_cache.syncing())

    def cache_info(self):
        now = time.monotonic()
        busy = self.caching() or self.refreshing
        if self._cache_memo and now - self._cache_memo[0] < 3 and not busy:
            return self._cache_memo[1]
        info = art.dir_summary(self.cache_root, self._kinds_seen)
        self._cache_memo = None if busy else (now, info)   # a reading taken mid-refresh or mid-download is never kept
        return info

    def clear_cache(self):
        """Delete every downloaded image of this profile. The next build checks the cache against the catalog again,
        and an image Nuvio asks for before then is downloaded on the spot."""
        files, size = art.clear_dir(self.cache_root)
        self._art_urls = None
        self._cache_memo = None
        if self.poster_cache:
            self.poster_cache._failed.clear()
        return files, size

    # ------------------------------------------------------------ status for the editor
    @property
    def refreshing(self):
        return self._build_lock.locked() or self._queued

    def status(self):
        if self.needs_setup:
            state = "needs_setup"
        elif self.dormant:
            state = "not_started"
        elif self.metas is None:   # nothing loaded yet: loading, or the first load failed
            state = "error" if (self.last_error and not self.refreshing) else "building"
        else:
            state = "ready"        # serving a catalog; `error` says whether the latest refresh failed
        return {"state": state, "refreshing": self.refreshing,
                "items": len(self.metas) if self.metas is not None else None,
                "built_at": self.built_at or None, "error": self.last_error,
                "warnings": list(self.warnings), "crops": self.crop_count if self.generate_active else None,
                "caching": self.caching(), "cache": self.cache_info()}

    def delete_data(self):
        import shutil
        shutil.rmtree(self.cache_root, ignore_errors=True)
        try:
            os.remove(self.tmdb_file)
        except OSError:
            pass
