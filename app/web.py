"""Flask app: add-on routes under /<profile id>/, plus the admin login, API and editor page."""

import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import re
import secrets
import sys
import threading
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import unquote, urlsplit

import requests
from flask import Blueprint, Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

from . import art
from .catalog import ART_KINDS, VERSION, Instance
from .config import (GENERAL_FIELDS, PROFILE_FIELDS, ServerConfig, clean_settings, defaults, general_ready,
                     schema_for_ui)
from .nuvio import NuvioClient, NuvioError
from .store import Store

log = logging.getLogger("nuvio_watchlist")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
UNREACHABLE_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
TMDB_API = "https://api.themoviedb.org/3"
TMDB_CHECK_URL = f"{TMDB_API}/configuration"
PROFILE_LABEL_MAX = 60
SAMPLE_TMDB_ID = "1299018"  # the example title in the crop test: The Razor's Edge


def tmdb_auth(key):
    """(headers, params) for a TMDB v3 API key or a v4 read access token."""
    return ({"Authorization": f"Bearer {key}"}, {}) if "." in key else ({}, {"api_key": key})


class Registry:
    """The running instances, one per stored profile."""

    def __init__(self, server, store):
        self.server = server
        self.store = store
        self._lock = threading.Lock()
        self._items = {}

    def start(self, row, build=True):
        """(Re)start a profile. build=False is for saving settings: it builds nothing and keeps serving the
        catalog the previous instance had built, until the next refresh."""
        settings, _ = clean_settings(PROFILE_FIELDS, {}, row["settings"])  # every setting present, defaults filled in
        inst = Instance(dict(row, settings=settings), self.store.general(), self.server, tag_of=self.tag)
        with self._lock:
            old = self._items.get(row["id"])
            self._items[row["id"]] = inst
        if old:
            inst.adopt(old)
            old.stop()
        inst.start(build=build)
        return inst

    def tag(self, pid, label):
        """What starts this profile's log lines: its name, plus the start of its id when another profile has the same name."""
        rows = self.store.list()
        for r in rows:
            if r["id"] == pid:
                label = r["label"]
        same = label.strip().casefold()
        if any(r["id"] != pid and r["label"].strip().casefold() == same for r in rows):
            return f"{label} {pid[:8]}"
        return label

    def load(self):
        for row in self.store.list():
            self.start(row)

    def restart_all(self):
        """The account settings changed: every profile signs in with the new ones the next time it refreshes."""
        for row in self.store.list():
            self.start(row, build=False)

    def get(self, pid):
        with self._lock:
            return self._items.get(pid)

    def all(self):
        with self._lock:
            items = dict(self._items)
        return [items[r["id"]] for r in self.store.list() if r["id"] in items]

    def remove(self, pid):
        with self._lock:
            inst = self._items.pop(pid, None)
        if inst:
            inst.deleted = True
            inst.stop()
            inst.delete_data()
        self.store.delete(pid)


class Throttle:
    """Slows down password guessing: five wrong passwords in a row lock the login for a minute. The count is kept for
    the editor as a whole, not per address, so a forged X-Forwarded-For header cannot get around it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}

    def blocked(self, who):
        with self._lock:
            return self._fails.get(who, (0, 0))[1] > time.time()

    def fail(self, who):
        with self._lock:
            count, _ = self._fails.get(who, (0, 0))
            count += 1
            self._fails[who] = (0, time.time() + 60) if count >= 5 else (count, 0)

    def ok(self, who):
        with self._lock:
            self._fails.pop(who, None)


def _secret_key(data_dir, admin_password):
    """The key that signs the login cookie: a random key kept in the data folder, mixed with the admin password so that
    changing the password signs everyone out."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "session.key")
    try:
        with open(path) as fh:
            key = fh.read().strip()
    except OSError:
        key = ""
    if not key:
        key = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
    return hashlib.sha256(f"{key}\0{admin_password}".encode()).hexdigest()


class _Sessions(SecureCookieSessionInterface):
    """The login cookie is marked Secure when the request came in over HTTPS, so plain-HTTP home setups still work."""

    def get_cookie_secure(self, app):
        return request.is_secure


def create_app(env=None):
    env = os.environ if env is None else env
    server = ServerConfig(env)
    logging.basicConfig(stream=sys.stdout, level=getattr(logging, server.log_level, logging.INFO),
                        format="[%(levelname)s] %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    problems = server.problems()
    if problems:
        for line in problems:
            log.error("Missing configuration: %s", line)
        raise SystemExit(1)

    store = Store(server.data_dir)
    registry = Registry(server, store)
    saved = store.general()
    lines = ["", f"Nuvio Watchlist {VERSION}",
             f"• Port {server.port} · {'Account saved' if general_ready(saved) else 'Account not set up'}"
             f" · TMDB {'enabled' if saved.get('TMDB_API_KEY') else 'disabled'}"]
    rows = store.list()
    for r in rows:
        lines.append(f"• {registry.tag(r['id'], r['label'])} → Nuvio profile {r['settings'].get('NUVIO_PROFILE', '1')}")
    if not rows:
        lines.append("• No profiles yet")
    print("\n".join(lines), flush=True)
    registry.load()
    throttle = Throttle()

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # behind a reverse proxy or tunnel
    app.secret_key = _secret_key(server.data_dir, server.admin_password)
    app.session_interface = _Sessions()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      # signed out after 1 hour without a request; every request renews it, so it only runs out when idle
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=1), SESSION_REFRESH_EACH_REQUEST=True)
    app.extensions["nuvio_registry"] = registry
    app.extensions["nuvio_store"] = store

    def base_url():
        return server.host_name or request.host_url.rstrip("/")

    def json_body():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------ addon routes
    addon = Blueprint("addon", __name__)

    @addon.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    def instance_or_404(uid):
        inst = registry.get(uid)
        if not inst:
            abort(404)
        return inst

    def json_response(body, cache_control, headers=None):
        resp = Response(body, mimetype="application/json")
        resp.headers["Cache-Control"] = cache_control
        for key, value in (headers or {}).items():
            resp.headers[key] = value
        return resp

    def parse_extra(extra):
        out = {}
        for part in re.split(r"[&/]", extra or ""):
            if "=" in part:
                key, value = part.split("=", 1)
                out[key] = unquote(value)
        return out

    @addon.route("/<uid>/manifest.json")
    def manifest(uid):
        inst = instance_or_404(uid)
        if request.headers.get("If-None-Match") == inst.manifest_etag:
            return Response(status=304)
        return json_response(inst.manifest_body, "no-cache, no-store, must-revalidate, max-age=0, s-maxage=0",
                             {"ETag": inst.manifest_etag})

    @addon.route("/<uid>/configure")
    def configure_profile(uid):
        """Where a client's Configure button for this add-on lands: the editor, on this profile's tab."""
        inst = instance_or_404(uid)
        return redirect(f"/configure#profile-{inst.number}")

    @addon.route("/<uid>/catalog/<catalog_type>/<catalog_id>.json")
    @addon.route("/<uid>/catalog/<catalog_type>/<catalog_id>/<path:extra>.json")
    def catalog(uid, catalog_type, catalog_id, extra=None):
        inst = instance_or_404(uid)
        if catalog_id not in inst.filters:
            abort(404)
        size = inst.s.CATALOG_LIST_ITEMS_SIZE
        try:
            skip = max(0, int(parse_extra(extra).get("skip") or request.args.get("skip") or 0))
        except ValueError:
            skip = 0
        start = skip
        try:
            metas = inst.get()
        except Exception as exc:
            inst.log.warning("[catalog] could not load the Nuvio Library: %s", exc)
            return json_response('{"metas":[]}', "no-cache, must-revalidate, max-age=0")
        wanted_type = inst.filters[catalog_id]
        if wanted_type:
            metas = [m for m in metas if m.get("type") == wanted_type]
        base = f"{base_url()}/{uid}"
        page = [inst.with_cached_art(m, base) for m in metas[start:start + size]]
        headers = {"Pragma": "no-cache", "Expires": "0"}
        if inst.built_at:
            headers["Last-Modified"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(inst.built_at))
        return json_response(json.dumps({"metas": page}, separators=(",", ":")),
                             "no-cache, must-revalidate, max-age=0", headers)

    @addon.route("/<uid>/poster-cache/<kind>/<name>")
    def poster_cache_route(uid, kind, name):
        inst = instance_or_404(uid)
        if not inst.poster_cache or kind not in ART_KINDS or not inst.cache_enabled_for(kind):
            abort(404)
        url = inst.art_index.get(name)
        if not url:
            abort(404)
        result = inst.poster_cache.get(url)
        if not result:
            return redirect(art.real_url(url), code=302)
        path, info = result
        resp = send_file(path, mimetype=info.get("type") or "image/jpeg", etag=False, conditional=False)
        resp.set_etag(info["etag"])
        # The file name is a hash of the source URL, so changed artwork gets a new URL and the
        # bytes behind one URL never change.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp.make_conditional(request)

    # ------------------------------------------------------------ admin: editor and API
    admin = Blueprint("admin", __name__)

    @admin.before_request
    def guard():
        # Cookies are SameSite=Strict; this header adds a second barrier against cross-site writes.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "nuvio_watchlist":
            return jsonify(error="Missing request header."), 403

    def admin_only(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                return jsonify(error="Sign in first."), 401
            return fn(*args, **kwargs)
        return wrapper

    @admin.route("/")
    def home():
        return redirect("/configure")

    @admin.route("/configure")
    def configure():
        resp = send_from_directory(STATIC_DIR, "configure.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @admin.route("/api/login", methods=["POST"])
    def login():
        who = "admin"
        if throttle.blocked(who):
            return jsonify(error="Too many wrong passwords. Try again in a minute."), 429
        given = str(json_body().get("password", ""))
        if hmac.compare_digest(given.encode(), server.admin_password.encode()):
            throttle.ok(who)
            session.clear()
            session["admin"] = True
            session.permanent = True
            return jsonify(ok=True)
        throttle.fail(who)
        return jsonify(error="That password is not right."), 401

    @admin.route("/api/logout", methods=["POST"])
    def logout():
        session.clear()
        return jsonify(ok=True)

    def profile_json(row, inst):
        return {"id": row["id"], "number": row["number"], "label": row["label"], "settings": clean_settings(PROFILE_FIELDS, {}, row["settings"])[0],
                "install_url": f"{base_url()}/{row['id']}/manifest.json",
                "status": inst.status() if inst else {"state": "not_started"}}

    def fingerprint(*parts):
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def nuvio_fp(g):
        return fingerprint(g.get("NUVIO_EMAIL", ""), g.get("NUVIO_PASSWORD", ""))

    def tmdb_fp(g):
        return fingerprint(g.get("TMDB_API_KEY", ""))

    def verified_flags(g):
        return {"nuvio": general_ready(g) and session.get("verified_nuvio") == nuvio_fp(g),
                "tmdb": bool(g.get("TMDB_API_KEY")) and session.get("verified_tmdb") == tmdb_fp(g)}

    def url_warning():
        host = urlsplit(base_url()).hostname or ""
        if host in UNREACHABLE_HOSTS:
            return ("This page was opened as " + host + ", which Nuvio on your TV or phone can't reach. Open this page "
                    "using your server's address, or set HOST_NAME in the compose file, so the add-on link is correct.")
        return None

    @admin.route("/api/state")
    @admin_only
    def state():
        general = store.general()
        general_for_editor = clean_settings(GENERAL_FIELDS, {}, general)[0]
        resp = jsonify(
            schema=schema_for_ui(), general=general_for_editor,
            general_ready=general_ready(general), verified=verified_flags(general), url_warning=url_warning(),
            nuvio_profile_count=session.get("verified_nuvio_profiles") if session.get("verified_nuvio") == nuvio_fp(general) else None,
            server={"has_pillow": art.HAVE_PIL, "sample_tmdb_id": SAMPLE_TMDB_ID, "label_max": PROFILE_LABEL_MAX},
            profiles=[profile_json(r, registry.get(r["id"])) for r in store.list()])
        resp.headers["Cache-Control"] = "no-store"   # it carries the saved passwords for the show/hide fields
        return resp

    @admin.route("/api/session")
    @admin_only
    def keep_session():
        """Called by the page while someone is using it without saving or loading anything, so the 1 hour
        of inactivity are counted from their last action and not from their last request."""
        return jsonify(ok=True)

    @admin.route("/api/status")
    @admin_only
    def statuses():
        return jsonify(profiles={i.uid: i.status() for i in registry.all()})

    @admin.route("/api/general", methods=["PUT"])
    @admin_only
    def save_general():
        settings, errors = clean_settings(GENERAL_FIELDS, json_body().get("settings"), store.general(), enforce_required=True)
        if errors:
            return jsonify(error="Some settings need fixing.", errors=errors), 400
        # the editor validates the login and the key before saving; this makes sure of it for any other caller
        if session.get("verified_nuvio") != nuvio_fp(settings):
            error, count = check_nuvio(settings["NUVIO_EMAIL"], settings["NUVIO_PASSWORD"])
            if error:
                return jsonify(error=error), 400
            session["verified_nuvio"] = nuvio_fp(settings)
            session["verified_nuvio_profiles"] = count
        if settings["TMDB_API_KEY"] and session.get("verified_tmdb") != tmdb_fp(settings):
            error = check_tmdb(settings["TMDB_API_KEY"])
            if error:
                return jsonify(error=error), 400
            session["verified_tmdb"] = tmdb_fp(settings)
        store.set_general(settings)
        registry.restart_all()
        log.info("[Account] Saved the account settings")
        return jsonify(ok=True)

    def check_tmdb(key):
        """None when TMDB accepts this key, otherwise a sentence saying why not."""
        headers, params = tmdb_auth(key)
        try:
            resp = requests.get(TMDB_CHECK_URL, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            return f"Could not reach TMDB ({exc.__class__.__name__})."
        if resp.status_code == 200:
            return None
        if resp.status_code in (401, 403):
            return "TMDB rejected this key."
        return f"TMDB answered with HTTP {resp.status_code}."

    def check_nuvio(email, password):
        """(None, number of Nuvio profiles or None) when Nuvio accepts the sign-in, otherwise (a sentence saying why not, None)."""
        http = requests.Session()
        try:
            client = NuvioClient(http, server.nuvio_base_url, server.nuvio_api_key, email, password)
            client._access_token()  # signs in; raises NuvioError with a readable message when it fails
            try:
                return None, len(client.profiles())
            except NuvioError:
                return None, None
        except NuvioError as exc:
            return str(exc), None
        finally:
            http.close()

    @admin.route("/api/tmdb/verify", methods=["POST"])
    @admin_only
    def verify_tmdb():
        given = json_body().get("key")
        key = (given if isinstance(given, str) and given else store.general().get("TMDB_API_KEY", "")).strip()
        if not key:
            return jsonify(ok=False, error="Enter a TMDB API key first.")
        error = check_tmdb(key)
        if error:
            return jsonify(ok=False, error=error)
        session["verified_tmdb"] = tmdb_fp({"TMDB_API_KEY": key})
        return jsonify(ok=True)

    @admin.route("/api/nuvio/verify", methods=["POST"])
    @admin_only
    def verify_nuvio():
        body = json_body()
        saved = store.general()
        creds = {k: body.get(k) if isinstance(body.get(k), str) and body.get(k) else saved.get(k, "")
                 for k in ("NUVIO_EMAIL", "NUVIO_PASSWORD")}
        creds["NUVIO_EMAIL"] = creds["NUVIO_EMAIL"].strip()
        missing = [label for k, label in (("NUVIO_EMAIL", "email"), ("NUVIO_PASSWORD", "password")) if not creds[k]]
        if missing:
            return jsonify(ok=False, error="Enter your Nuvio " + " and ".join(missing) + " first.")
        error, count = check_nuvio(creds["NUVIO_EMAIL"], creds["NUVIO_PASSWORD"])
        if error:
            return jsonify(ok=False, error=error)
        session["verified_nuvio"] = nuvio_fp(creds)
        session["verified_nuvio_profiles"] = count
        first_time = not general_ready(saved)
        if first_time:   # the first successful login is kept, so no separate save is needed before adding profiles
            settings, _ = clean_settings(GENERAL_FIELDS, creds, saved)
            store.set_general(settings)
            registry.restart_all()
            log.info("[Account] Saved the Nuvio login after its first successful check")
        return jsonify(ok=True, profiles=count, saved=first_time)

    @admin.route("/api/crop-test", methods=["POST"])
    @admin_only
    def crop_test():
        """Crop one title's TMDB poster the way this add-on does and the way Nuvio does, side by side."""
        if not art.HAVE_PIL:
            return jsonify(ok=False, error="Pillow is not installed on this server, so images can't be cropped.")
        body = json_body()
        # only the key as typed in the Account tab is used, never the saved one, so it can be tried before saving
        key = str(body.get("key") or "").strip()
        if not key:
            return jsonify(ok=False, error="Add a TMDB API key on the Account tab first.")
        text = str(body.get("id") or "").strip()
        headers, params = tmdb_auth(key)

        def tmdb(path, **extra):
            return requests.get(f"{TMDB_API}{path}", headers=headers, params={**params, **extra}, timeout=15)

        try:
            match = re.search(r"(tt\d{5,})", text, re.I)
            link = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", text)
            if match:
                resp = tmdb(f"/find/{match.group(1).lower()}", external_source="imdb_id")
                resp.raise_for_status()
                found = resp.json()
                hit = next(iter(found.get("movie_results") or found.get("tv_results") or []), None)
            else:
                number = link.group(2) if link else (text if text.isdigit() else "")
                if not number:
                    return jsonify(ok=False, error="Enter an IMDb id like tt1234567, or a TMDB id or page link.")
                kinds = [link.group(1)] if link else ["movie", "tv"]
                hit = None
                for kind in kinds:
                    resp = tmdb(f"/{kind}/{number}")
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    hit = resp.json()
                    break
            if not hit:
                return jsonify(ok=False, error="TMDB has no movie or show with that id.")
            if not hit.get("poster_path"):
                return jsonify(ok=False, error="That title has no poster on TMDB.")
            title = hit.get("title") or hit.get("name") or text
            http = requests.Session()
            try:
                image = http.get(f"https://image.tmdb.org/t/p/w780{hit['poster_path']}", timeout=20)
                image.raise_for_status()
            finally:
                http.close()
            original = art.thumbnail(image.content, 240)
            smart = art.thumbnail(art.landscape_from_poster(image.content))
            centre = art.thumbnail(art.centre_crop(image.content))
        except requests.RequestException as exc:
            if getattr(exc, "response", None) is not None and exc.response.status_code in (401, 403):
                return jsonify(ok=False, error="TMDB rejected the API key.")
            return jsonify(ok=False, error=f"Could not reach TMDB ({exc.__class__.__name__}).")
        except (OSError, ValueError) as exc:
            return jsonify(ok=False, error=f"That poster could not be read ({exc.__class__.__name__}).")
        as_url = lambda data: "data:image/jpeg;base64," + base64.b64encode(data).decode()
        return jsonify(ok=True, title=title, poster=as_url(original), smart=as_url(smart), centre=as_url(centre))

    @admin.route("/api/account", methods=["DELETE"])
    @admin_only
    def delete_account():
        """Danger Zone: stop every profile and delete everything kept in the data volume."""
        if json_body().get("confirm") != "DELETE":
            return jsonify(error="Type DELETE to confirm."), 400
        for inst in registry.all():
            registry.remove(inst.uid)
        store.wipe()
        keep = {"nuvio_watchlist.db", "nuvio_watchlist.db-wal", "nuvio_watchlist.db-shm", "nuvio_watchlist.db-journal"}
        for name in os.listdir(server.data_dir):
            if name in keep:
                continue
            path = os.path.join(server.data_dir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
        app.secret_key = _secret_key(server.data_dir, server.admin_password)   # a new key: any other signed-in browser is signed out
        session.clear()
        log.warning("[Account] The account and all stored data were deleted")
        return jsonify(ok=True)

    @admin.route("/api/nuvio/profiles")
    @admin_only
    def nuvio_profiles():
        """The profiles (number and name) in the saved Nuvio account, for the profile dropdown."""
        general = store.general()
        if not general_ready(general):
            return jsonify(ok=False, error="Add your Nuvio details on the Account tab first.")
        http = requests.Session()
        try:
            client = NuvioClient(http, server.nuvio_base_url, server.nuvio_api_key,
                                 general["NUVIO_EMAIL"], general["NUVIO_PASSWORD"])
            found = client.profiles()
        except NuvioError as exc:
            return jsonify(ok=False, error=str(exc))
        finally:
            http.close()
        profiles = sorted(({"number": p["profile_index"], "name": (p.get("name") or "").strip() or f"Profile {p['profile_index']}"}
                           for p in found if isinstance(p.get("profile_index"), int)), key=lambda p: p["number"])
        return jsonify(ok=True, profiles=profiles)

    @admin.route("/api/profiles", methods=["POST"])
    @admin_only
    def create_profile():
        if not general_ready(store.general()):
            return jsonify(error="Add your Nuvio email and password on the Account tab first."), 400
        row = store.create(defaults(PROFILE_FIELDS))
        inst = registry.start(row)
        inst.log.info("Created")
        return jsonify(profile=profile_json(row, inst)), 201

    @admin.route("/api/profiles/<pid>", methods=["PUT"])
    @admin_only
    def update_profile(pid):
        row = store.get(pid)
        if not row:
            return jsonify(error="No such profile."), 404
        body = json_body()
        settings, errors = clean_settings(PROFILE_FIELDS, body.get("settings"), row["settings"])
        given = body.get("label")
        label = (given if isinstance(given, str) else row["label"]).strip() or f"Profile {row['number']}"   # blank = the default name
        if len(label) > PROFILE_LABEL_MAX:
            errors["label"] = f"Use at most {PROFILE_LABEL_MAX} characters."
        general = store.general()
        if not general_ready(general):
            return jsonify(error="Add your Nuvio email and password on the Account tab first."), 400
        if not errors and settings["NUVIO_PROFILE"] != "1":   # profile 1 always exists; the others must be in the Nuvio account
            http = requests.Session()
            try:
                NuvioClient(http, server.nuvio_base_url, server.nuvio_api_key, general["NUVIO_EMAIL"],
                            general["NUVIO_PASSWORD"]).require_profile(int(settings["NUVIO_PROFILE"]))
            except NuvioError as exc:
                errors["NUVIO_PROFILE"] = str(exc)
            finally:
                http.close()
        if errors:
            return jsonify(error="Some settings need fixing.", errors=errors), 400
        before, _ = clean_settings(PROFILE_FIELDS, {}, row["settings"])
        changed = [f["label"] for f in PROFILE_FIELDS if before.get(f["key"]) != settings.get(f["key"])]
        if label != row["label"]:
            changed.insert(0, "Profile Name")
        store.update(pid, label, settings)
        row = store.get(pid)
        inst = registry.start(row, build=False)   # saving only saves: the catalog refreshes when its time is up
        inst.log.info("Saved (%s)", "changed: " + ", ".join(changed) if changed else "no changes")
        return jsonify(profile=profile_json(row, inst))

    @admin.route("/api/profiles/<pid>", methods=["DELETE"])
    @admin_only
    def delete_profile(pid):
        row = store.get(pid)
        if not row:
            return jsonify(error="No such profile."), 404
        tag = registry.tag(pid, row["label"])
        registry.remove(pid)
        log.info("[%s] Deleted", tag)
        return jsonify(ok=True)

    @admin.route("/api/profiles/<pid>/rebuild", methods=["POST"])
    @admin_only
    def rebuild_profile(pid):
        inst = registry.get(pid)
        if not inst:
            return jsonify(error="No such profile."), 404
        if inst.dormant:  # the first refresh is what starts a new profile
            store.activate(pid)
            registry.start(store.get(pid))
        else:
            inst.rebuild_in_background("manual")
        return jsonify(ok=True)

    @admin.route("/api/profiles/<pid>/clear-cache", methods=["POST"])
    @admin_only
    def clear_cache(pid):
        inst = registry.get(pid)
        if not inst:
            return jsonify(error="No such profile."), 404
        files, size = inst.clear_cache()
        inst.log.info("Cleared the image cache: %d image(s), %d bytes (%s)", files, size, art.human_size(size))
        return jsonify(ok=True, files=files, bytes=size, cache=inst.cache_info())

    @admin.route("/api/profiles/<pid>/install", methods=["POST"])
    @admin_only
    def install_profile(pid):
        row = store.get(pid)
        if not row:
            return jsonify(error="No such profile."), 404
        general = store.general()
        if not general_ready(general):
            return jsonify(ok=False, error="Add your Nuvio email and password on the Account tab first.")
        warning = url_warning()
        if warning:
            return jsonify(ok=False, error=warning)
        manifest_url = f"{base_url()}/{pid}/manifest.json"
        target = int(row["settings"]["NUVIO_PROFILE"])
        http = requests.Session()
        try:
            client = NuvioClient(http, server.nuvio_base_url, server.nuvio_api_key,
                                 general["NUVIO_EMAIL"], general["NUVIO_PASSWORD"])
            result = client.install_addon(target, manifest_url)
        except NuvioError as exc:
            log.warning("[%s] Install to Nuvio failed (Nuvio profile %d): %s", registry.tag(pid, row["label"]), target, exc)
            return jsonify(ok=False, error=str(exc))
        finally:
            http.close()
        if not row["activated"]:  # it is in Nuvio now, so Nuvio will ask for its catalog
            store.activate(pid)
            registry.start(store.get(pid))
        log.info("[%s] Install to Nuvio: %s Nuvio profile %d", registry.tag(pid, row["label"]),
                 "installed to" if result == "installed" else "already installed in", target)
        return jsonify(ok=True, result=result, number=target)

    app.register_blueprint(admin)
    app.register_blueprint(addon)

    return app
