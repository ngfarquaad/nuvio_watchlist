"""Nuvio (Supabase) sign-in, library download and add-on install for one account.

The add-on calls mirror what the Nuvio TV app itself does (AddonSyncService): the add-on list of a
profile is the `addons` table filtered by user and profile, and it is written back as a whole with the
`sync_push_addons` RPC, so an install reads the current list first and sends it back with ours added.
"""

import base64
import json
import secrets
import string
import threading
import time

import requests

PAGE_SIZE = 500
_CLIENT_ID = "nuvio_watchlist-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))


class NuvioError(RuntimeError):
    """A problem to show the person as it is: the message is written for them."""


def canonical_addon_url(url):
    """The same normalisation the Nuvio app uses to decide two add-on URLs are the same."""
    url = (url or "").strip().rstrip("/")
    path, sep, query = url.partition("?")
    if path.lower().endswith("/manifest.json"):
        path = path[: -len("/manifest.json")]
    return path.rstrip("/") + sep + query


def _jwt_subject(token):
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body)).get("sub")
    except Exception:
        return None


class NuvioClient:
    def __init__(self, http, base_url, api_key, email, password):
        self.http = http
        self.base = base_url
        self.api_key = api_key
        self.email = email
        self.password = password
        self._lock = threading.Lock()
        self._token = None
        self._refresh = None
        self._user_id = None
        self._expires_at = 0.0

    # ------------------------------------------------------------ sign-in
    def _auth_request(self, grant_type, body):
        try:
            resp = self.http.post(f"{self.base}/auth/v1/token", params={"grant_type": grant_type},
                                  headers={"Content-Type": "application/json", "apikey": self.api_key},
                                  json=body, timeout=30)
        except requests.RequestException as exc:
            raise NuvioError(f"Could not reach Nuvio: {exc.__class__.__name__}.") from exc
        if resp.status_code in (400, 401, 403):
            raise NuvioError("Invalid Nuvio credentials or NUVIO_API_KEY.")
        self._check(resp)
        data = resp.json()
        self._token = data["access_token"]
        self._refresh = data.get("refresh_token", self._refresh)
        self._user_id = (data.get("user") or {}).get("id") or _jwt_subject(self._token) or self._user_id
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))

    def _login(self):
        self._auth_request("password", {"email": self.email, "password": self.password})

    def _access_token(self, force=False):
        with self._lock:
            if force or self._token is None:
                self._login()
            elif time.monotonic() >= self._expires_at - 60:
                try:
                    self._auth_request("refresh_token", {"refresh_token": self._refresh})
                except Exception:
                    self._login()
            return self._token

    def _headers(self, force=False):
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self._access_token(force)}",
                "apikey": self.api_key}

    @staticmethod
    def _check(resp):
        if resp.status_code >= 400:
            raise NuvioError(f"Nuvio answered with HTTP {resp.status_code}: {resp.text[:160].strip()}")

    def _request(self, method, path, **kwargs):
        try:
            resp = self.http.request(method, f"{self.base}{path}", headers=self._headers(), timeout=30, **kwargs)
            if resp.status_code == 401:
                resp = self.http.request(method, f"{self.base}{path}", headers=self._headers(force=True),
                                         timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise NuvioError(f"Could not reach Nuvio: {exc.__class__.__name__}.") from exc
        self._check(resp)
        return resp

    # ------------------------------------------------------------ library
    def _library_page(self, profile, offset, limit):
        payload = {"p_profile_id": profile, "p_limit": limit, "p_offset": offset}
        return self._request("POST", "/rest/v1/rpc/sync_pull_library", json=payload).json()

    def fetch_library(self, profile):
        if profile != 1:  # Nuvio answers "empty" for a profile that doesn't exist, so ask for the profile list first
            self.require_profile(profile)
        items, offset = [], 0
        while True:
            page = self._library_page(profile, offset, PAGE_SIZE)
            if not page:   # only an empty page means the end: a server that sends fewer rows than asked for can't cut the Library short
                return items
            items.extend(page)
            offset += len(page)

    # ------------------------------------------------------------ add-ons
    def profiles(self):
        """Nuvio's own profile list (index, name, whether it shares the main profile's add-ons)."""
        return self._request("POST", "/rest/v1/rpc/sync_pull_profiles", json={}).json()

    def require_profile(self, profile):
        """The account's entry for this Nuvio profile; raises if the account has no such profile.
        Profile 1 always exists. If Nuvio's profile list can't be read the check is skipped."""
        try:
            known = self.profiles()
        except NuvioError:
            return None
        entry = next((p for p in known if p.get("profile_index") == profile), None)
        if profile != 1 and entry is None:
            raise NuvioError(f"Your Nuvio account has no profile {profile}. Pick a profile number you have created in Nuvio.")
        return entry

    def addons(self, profile):
        self._access_token()
        params = {"user_id": f"eq.{self._user_id}", "profile_id": f"eq.{profile}", "select": "*",
                  "order": "sort_order.asc"}
        return self._request("GET", "/rest/v1/addons", params=params).json()

    def install_addon(self, profile, manifest_url):
        """Add manifest_url to a Nuvio profile's add-ons, keeping everything already there.

        Returns "installed" or "already". Raises NuvioError with a message fit to show."""
        entry = self.require_profile(profile)
        if entry and profile != 1 and entry.get("uses_primary_addons"):
            raise NuvioError(
                f"Nuvio profile {profile} uses the main profile's add-ons, so it has no add-on list of its own. "
                "Turn that off for the profile in Nuvio, or install the add-on from the main profile.")

        existing = self.addons(profile)
        wanted = canonical_addon_url(manifest_url)
        if any(canonical_addon_url(a.get("url")) == wanted for a in existing):
            return "already"

        merged = []
        for index, addon in enumerate(existing):
            item = {"url": addon["url"], "sort_order": index, "enabled": addon.get("enabled", True)}
            if (addon.get("name") or "").strip():
                item["name"] = addon["name"]
            merged.append(item)
        merged.append({"url": manifest_url, "sort_order": len(merged), "enabled": True})
        self._request("POST", "/rest/v1/rpc/sync_push_addons",
                      json={"p_addons": merged, "p_profile_id": profile, "p_origin_client_id": _CLIENT_ID})

        if not any(canonical_addon_url(a.get("url")) == wanted for a in self.addons(profile)):
            raise NuvioError("Nuvio accepted the request, but the add-on is not in the profile's list. "
                             "Add the link in Nuvio yourself under Settings, Add-ons.")
        return "installed"
