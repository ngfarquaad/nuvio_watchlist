# Nuvio Watchlist

**Your picks. Your home screen.**

A self-hosted add-on for Nuvio that republishes your own Nuvio Library as a catalog on your home screen or in a collection folder. You choose the row layout, sort order, card shape and artwork from a web editor, with optional TMDB artwork and a local image cache.

---

## Features

- **Your Library as a Catalog**: reads a Nuvio profile's Library and serves it as a Movies row and a Series row, or as one combined row.
- **Any Number of Profiles**: each add-on profile has its own settings, add-on link and cache, and points at one Nuvio profile (1 to 6). Several can point at the same one.
- **Web Editor**: a password-protected page where you set everything up and install the add-on into Nuvio with one button.
- **Layout and Sorting**: row names, sort order (added, released, name or random), card shape, titles per page, and whether the row shows on the home screen.
- **TMDB Artwork (Optional)**: posters and landscape images in your language, resized to save bandwidth.
- **Image Cache (Optional)**: serve artwork from your own server, and cut landscape images from posters for titles that have none.
- **Background Refresh**: the catalog is rebuilt on a schedule, and the last good catalog keeps being served if Nuvio can't be reached.

---

## What You Need

- **Docker**: [Docker Desktop](https://docs.docker.com/get-docker/) on Windows or macOS, Docker Engine on Linux.
- **A Nuvio Account**: the email and password you use in Nuvio, so the add-on can read your Library.
- **Nuvio's Public API Key**: already filled in the examples below, so there is nothing to paste. Change it only if Nuvio changes its key.
- **A TMDB API Key (Optional)**: only needed for TMDB artwork and landscape images cropped from posters.

---

## Installation

### Option 1: Docker Compose (Recommended)

1. Create a folder for it, for example `nuvio_watchlist`.
2. In that folder, create a file named `docker-compose.yml` with this content, and change the two marked values:

   ```yaml
   services:
     nuvio_watchlist:
       image: ghcr.io/ngfarquaad/nuvio_watchlist:latest
       container_name: nuvio_watchlist
       restart: unless-stopped
       ports:
         - "7000:7000"
       environment:
         ADMIN_PASSWORD: "change-me"                # 1. the password for the editor
         NUVIO_API_KEY: "sb_publishable_1Clq8rlTVACkdcZuqr6_AD__xUUC_EN"   # Nuvio's public API key, already filled in
         HOST_NAME: "http://192.168.1.50:7000"      # 2. the address your Nuvio apps use to reach this machine
       volumes:
         - nuvio_watchlist_data:/data

   volumes:
     nuvio_watchlist_data:
   ```

   `HOST_NAME` must be an address your phone or TV can reach, so not `localhost`. On a home network that is the machine's LAN address plus the port (on Windows, run `ipconfig` and use the IPv4 address). Behind a reverse proxy, use your public `https://` address.
3. In that folder, run:

   ```bash
   docker compose up -d
   ```

4. Open `http://localhost:7000/configure` and sign in with your `ADMIN_PASSWORD`.

To update later: `docker compose pull`, then `docker compose up -d`. To stay on one version, use `:1.0.0` instead of `:latest` in the image line.

### Option 2: Docker Run

```bash
docker run -d --name nuvio_watchlist --restart unless-stopped -p 7000:7000 \
  -e ADMIN_PASSWORD="change-me" \
  -e NUVIO_API_KEY="sb_publishable_1Clq8rlTVACkdcZuqr6_AD__xUUC_EN" \
  -e HOST_NAME="http://192.168.1.50:7000" \
  -v nuvio_watchlist_data:/data \
  ghcr.io/ngfarquaad/nuvio_watchlist:latest
```

On Windows PowerShell, end each line with a backtick (`` ` ``) instead of `\`.

### Option 3: Build from Source

Download this repository (**Code**, then **Download ZIP**), unzip it, and copy `docker-compose.example.yml` to `docker-compose.yml`. Replace its `image:` line with `build: .`, set the two marked values, then run `docker compose up -d --build` in that folder.

### First-Time Setup

1. **Account tab:** enter your Nuvio email and password and select **Validate Login**. The first successful check also saves the login, which unlocks the **+** tab. A TMDB API key is optional: enter it, select **Validate Key**, then **Save Account**.
2. **+ tab:** adds an add-on profile. Choose which **Nuvio Profile** it shows, adjust the settings, and select **Save Profile**.
3. Select **Refresh Catalog** to build it right away.
4. Select **Install to Nuvio** to add it for you, or copy the **Install URL** into Nuvio under Settings, Add-ons. Your existing add-ons are kept.

Nothing saves automatically. Save the Account tab first, then the profile, then use Refresh Catalog or Install to Nuvio. The editor tells you if something is still unsaved.

### Reaching the Add-on from Nuvio

Nuvio runs on your phone or TV, so the add-on's address (`HOST_NAME`) must be one that **that device** can reach. The Install URL in the editor is built from it, so check the Install URL before you install.

| Where Nuvio is                  | Use for `HOST_NAME`                                                                 |
|---------------------------------|-------------------------------------------------------------------------------------|
| On your home network            | The server's LAN address and the port on the host, for example `http://192.168.1.50:7000`. |
| Away from home                  | A public `https://` address that leads to the add-on, through a reverse proxy or a tunnel. |

- Do not use `localhost`, `127.0.0.1` or the container name (`nuvio_watchlist:7000`). Those only work on the server itself or between containers on the same Docker network, and never from a phone or TV. Install to Nuvio refuses a localhost address.
- If you map a different port on the host, such as `8080:7000`, use the host side (`8080`) in `HOST_NAME`. The container side stays `7000`.
- If you change `HOST_NAME`, open the editor again and use the new Install URL. Links you already installed keep the old address.

---

## Configuration

### Environment Variables

| Variable         | Default                | Required | Meaning |
|------------------|------------------------|----------|---------|
| `ADMIN_PASSWORD` | none                   | Yes      | Password for the web editor. |
| `NUVIO_API_KEY`  | none                   | Yes      | Nuvio's public API key. It is the same for every Nuvio user and is already filled in the examples above. Change it only if Nuvio changes its key. |
| `HOST_NAME`      | from the request       | Advised  | The public base URL, no trailing slash. It builds the add-on link and cached image links. |
| `PORT`           | `7000`                 | No       | Port the app listens on inside the container. If you change it, change the container side of the port mapping to match. |
| `DATA_DIR`       | `/data`                | No       | Where the database, session key and image caches are stored. Mount it as a volume. |
| `LOG_LEVEL`      | `INFO`                 | No       | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `NUVIO_BASE_URL` | `https://api.nuvio.tv` | No       | Nuvio's backend. Change it only to use a different server. |

### Account Tab

The Nuvio email and password the add-on signs in with, and an optional TMDB API key (a v3 key or a v4 read access token). Without a TMDB key, no TMDB request is ever made.

### Profile Settings

| Setting                            | Default                  | What it does |
|------------------------------------|--------------------------|--------------|
| Profile Name                       | `Profile N`              | Labels the tab and the log lines. |
| Nuvio Profile                      | 1                        | Which Nuvio profile's Library is shown, and where Install to Nuvio adds the add-on. |
| Combine Movies and Series          | Off                      | Off gives a Movies row and a Series row. On gives one row. |
| Catalog Name / Movies / Series     | `Nuvio Watchlist`, `Nuvio Watchlist - Movies`, `Nuvio Watchlist - Series` | The row names on Nuvio's home screen. |
| Show on the Home Screen            | On                       | Off keeps the rows only in Nuvio's catalog list. |
| Sort Order                         | Newest Added First       | Added, released, name or random. Random reshuffles on every refresh. |
| Card Shape                         | Automatic                | Automatic keeps each title's saved shape. Portrait or Landscape forces one. In Landscape, a title with no landscape artwork keeps its portrait poster unless Crop Landscape Images from Posters is on. |
| Titles per Page                    | 20                       | How many titles Nuvio loads at a time (1 to 100). |
| Catalog Cache TTL                  | 3600 s                   | How long a built catalog is served before it expires (300 to 604,800). |
| Refresh in Background              | On                       | Rebuilds the catalog on a schedule so Nuvio is always answered from memory. |
| Cache Posters / Landscape Images   | Off                      | Download artwork to this server and serve it from there. **Cache Storage** shows what is stored, and **Delete Cache** removes it. |
| Artwork Language                   | `en`                     | Two-letter language code for TMDB artwork with the title printed on it. Changing it looks up every title again, so a large Library takes a few minutes; each title keeps its current artwork until its new lookup succeeds. |
| Artwork Lookup TTL                 | 14 days                  | How often each title is looked up again on TMDB (7 to 365). |
| Smaller Posters / Landscape Images | On                       | Request TMDB images at 500 px and 780 px wide instead of the original size. |
| Crop Landscape Images from Posters | Off                      | For titles with no landscape art, cut one from the poster. Needs Cache Landscape Images and a TMDB key. |

The TMDB settings (language, lookup TTL, smaller images and the crop) are locked until a TMDB key is entered on the Account tab. Every value is checked against these limits when you save.

---

## How It Works

- **Building:** a build downloads the Library, drops titles without an ID and anything that is not a movie or a series (Nuvio's `tv` counts as a series), sorts them and turns each into a catalog entry. The catalog lives in memory, per profile.
- **Refreshing:** with Refresh in Background on, the catalog is rebuilt when its cache expires. If Nuvio can't be reached, the last catalog keeps being served and the next try comes five minutes later. A Library that suddenly comes back empty does not replace a catalog that has titles.
- **Saving:** saving a profile only saves. The catalog picks up the new settings at its next refresh, or when you select **Refresh Catalog**.
- **Artwork:** with a TMDB key, a background job looks up each title and picks the best poster and landscape image in your language, falling back to the Library's own artwork. Nothing is looked up without a key.
- **Image cache:** downloaded images are stored once per distinct link, kept for 30 days, then checked against their source before being kept again.
- **New profiles** stay idle, fetching nothing from Nuvio, until they are first saved, refreshed or installed.
- **Limits:** Nuvio numbers its profiles 1 to 6. The add-on serves a catalog only, with no streams, metadata or subtitles.

---

## Security

- The editor signs you out after an hour without activity, and five wrong passwords in a row lock the login for a minute. Changing `ADMIN_PASSWORD` signs everyone out. Put it behind HTTPS and a reverse proxy.
- Your Nuvio password and TMDB key are stored in plain text in the database, because the add-on has to sign in for you. Keep the data folder private.
- An add-on link contains the profile's ID, and anyone with it can read that profile's catalog. The add-on routes need no sign-in so that Nuvio can use them.
- **Delete Account** (Danger Zone) deletes everything the add-on stores and signs out every browser. Your Nuvio account is not touched, and deleting a profile does not remove the add-on from Nuvio.

---

## Troubleshooting

- **Sign-in fails for everyone with "Invalid Nuvio credentials or NUVIO_API_KEY":** check your email and password first. If they are right, Nuvio may have changed its public API key or address, so look in Nuvio's official channels for the new values and set `NUVIO_API_KEY` (and `NUVIO_BASE_URL` if the address changed).
- **Permission errors on `/data`:** the container runs as user ID `10001`, not as root. The named volume in the examples needs nothing extra. If you mount a folder from your computer instead, give that user write access to it, for example `sudo chown -R 10001 /path/to/folder`.

---

## Development

```bash
pip install -r requirements.txt      # Flask, requests, gunicorn, Pillow
ADMIN_PASSWORD=... NUVIO_API_KEY=... DATA_DIR=./data \
  gunicorn -w 1 --threads 16 -b 0.0.0.0:7000 'app.web:create_app()'
```

- Run these commands from the repository's top folder, the one that contains the `app` folder. Keep one worker, since each profile keeps its catalog in memory and refreshes it from a background thread.
- `python -m app` runs Flask's built-in server for local development only.
- Pillow is only needed for cropping landscape images from posters.
- Dependency versions are pinned in `requirements.txt`. Update them deliberately, and test, when you prepare a release.

---

## License

MIT, see [LICENSE](LICENSE).

---

## Credits

- [Nuvio](https://nuvio.tv)
- [TMDB](https://www.themoviedb.org/)
- [Docker](https://www.docker.com/)
- [Flask](https://flask.palletsprojects.com/)

This product uses the TMDB API but is not endorsed or certified by TMDB.

---

## Disclaimer

Nuvio Watchlist is an independent, self-hosted add-on. It is not made by or affiliated with Nuvio, and it signs in to your Nuvio account only to read your own Library. Artwork availability depends on your Library and on TMDB.
