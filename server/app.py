"""piwii web server: accept a Wii game, convert it with wit, and hand it to the Pi.

Flow for each game:
  1. The game arrives in WORK_DIR, either uploaded from the browser in chunks
     (under Cloudflare's 100 MB request limit, and resumable) or fetched from
     a URL.
  2. wit converts it to a split WBFS. The ID6 and title are read from the
     result to name the folder `Title [ID6]`, which is what USB Loader GX
     expects under /wbfs.
  3. The finished folder is written to STAGING_DIR/.incoming/ (the Pi's NFS
     export), then renamed into STAGING_DIR/ready/. The rename is atomic, so
     the Pi never sees a half-written game. If the copy fails (Pi or NFS
     down), it's retried with backoff until it works, across restarts.
  4. A systemd path unit on the Pi notices ready/ is non-empty and runs the
     sync: detach the gadget, copy onto the game drive, reattach. The Pi also writes
     STAGING_DIR/library.json, which the web server shows as the installed list.

All conversion happens before the Pi detaches, so the Wii only loses the drive
for the final copy.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Queue

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.requests import ClientDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

STAGING_DIR = Path(os.environ.get("PIWII_STAGING_DIR", "/staging"))
# A file the Pi keeps at the root of its staging export. STAGING_DIR is a folder
# the server host mounts the share on; while the share is down it's an empty local
# folder, so nothing is read from or written to it unless this file is there.
STAGING_MARKER = STAGING_DIR / ".piwii-staging"
WORK_DIR = Path(os.environ.get("PIWII_WORK_DIR", "/work"))
WIT = os.environ.get("PIWII_WIT", "wit")

GAME_SUFFIXES = {".iso", ".wbfs", ".ciso", ".wia", ".wdf", ".gcz"}
CHUNK = 4 * 1024 * 1024

# Chunked uploads: each chunk is written straight into WORK_DIR/uploads/<id>.part
# at its offset, so there's no join step. 50 MB chunks stay under Cloudflare's
# 100 MB per-request limit. Uploads idle for a day are deleted.
UPLOADS_DIR = WORK_DIR / "uploads"
UPLOAD_CHUNK = 50 * 1024 * 1024
UPLOAD_MAX_CHUNK = 64 * 1024 * 1024
UPLOAD_MAX_SIZE = 20 * 1024**3
UPLOAD_IDLE_TTL = 24 * 3600
STATIC = Path(__file__).parent / "static"

# GameTDB's ID -> English title list, the same database USB Loader GX uses.
# Display only: folders keep the disc-header title wit reports.
TITLES_URL = "https://www.gametdb.com/titles.txt?LANG=EN"
TITLES_FILE = WORK_DIR / "gametdb-titles.txt"
TITLES_MAX_AGE = 7 * 24 * 3600

# GameTDB cover art, cached in WORK_DIR/covers (on the server host's SSD, not backed up).
# A cache miss is fetched from GameTDB, saved, then served; IDs GameTDB has no
# cover for are remembered for a day so they aren't re-requested constantly.
COVERS_DIR = WORK_DIR / "covers"
COVER_URL = "https://art.gametdb.com/wii/cover/{region}/{id}.png"
COVER_MISS_TTL = 24 * 3600
COVER_TIMEOUT = 30  # GameTDB's art server sometimes takes 10-20 s
# The 4th ID letter is the region; GameTDB's cover folders are per language.
COVER_REGIONS = {"E": "US", "P": "EN", "J": "JA", "K": "KO", "D": "DE", "F": "FR", "S": "ES",
                 "I": "IT", "H": "NL", "X": "EN", "Y": "EN", "Z": "EN", "U": "AU"}


@dataclass
class Job:
    id: str
    source: str
    state: str = "queued"  # queued | receiving | converting | handing-off | done | installed | failed
    detail: str = ""
    game_id: str = ""
    title: str = ""
    folder: str = ""  # "Title [ID6]" as handed to the Pi
    created: float = field(default_factory=time.time)


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
queue: Queue[tuple[Job, Path]] = Queue()


def update(job: Job, **changes: str) -> None:
    with jobs_lock:
        for k, v in changes.items():
            setattr(job, k, v)


# --- titles ------------------------------------------------------------------

titles: dict[str, str] = {}


def load_titles() -> None:
    """Load GameTDB titles from the cached file, refreshing it if older than a week."""
    global titles
    try:
        stale = not TITLES_FILE.exists() or time.time() - TITLES_FILE.stat().st_mtime > TITLES_MAX_AGE
        if stale:
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            tmp = TITLES_FILE.with_suffix(".tmp")
            with urllib.request.urlopen(TITLES_URL, timeout=30) as resp:
                tmp.write_bytes(resp.read())
            os.replace(tmp, TITLES_FILE)
    except Exception as e:  # noqa: BLE001 - keep serving with whatever we have
        print(f"piwii: GameTDB titles refresh failed: {e}")
    if TITLES_FILE.exists():
        parsed = {}
        for line in TITLES_FILE.read_text("utf-8", "replace").splitlines():
            game_id, sep, title = line.partition(" = ")
            if sep and len(game_id) == 6:
                parsed[game_id] = title.strip()
        titles = parsed


def titles_refresher() -> None:
    while True:
        load_titles()
        time.sleep(24 * 3600)


# GameTDB's full Wii database (synopsis, developer, publisher, release date,
# genres, players, controllers, online features, rating). Parsed once into
# WORK_DIR/gametdb-info.json; refreshed weekly, retried hourly while
# gametdb.com is unreachable.
GAMEINFO_URL = "https://www.gametdb.com/wiitdb.zip?LANG=EN"
GAMEINFO_FILE = WORK_DIR / "gametdb-info-v2.json"  # v2: adds "type"
GAMEINFO_MAX_AGE = 7 * 24 * 3600
gameinfo: dict[str, dict] = {}
gameinfo_state = {"loaded_at": None, "last_error": None}


def parse_wiitdb(xml_bytes: bytes) -> dict[str, dict]:
    """Pull the fields the page shows out of wiitdb.xml, keyed by game ID."""
    out: dict[str, dict] = {}
    for _, el in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if el.tag != "game":
            continue
        game_id = (el.findtext("id") or "").strip()
        if len(game_id) == 6:
            locales = el.findall("locale")
            loc = next((l for l in locales if l.get("lang") == "EN"), locales[0] if locales else None)
            date = el.find("date")
            rating = el.find("rating")
            wifi = el.find("wi-fi")
            inp = el.find("input")
            out[game_id] = {
                "title": (loc.findtext("title") if loc is not None else None) or el.get("name"),
                "type": (el.findtext("type") or "").strip(),  # "" for retail discs, "CUSTOM" for mods/hacks
                "synopsis": ((loc.findtext("synopsis") if loc is not None else None) or "").strip() or None,
                "developer": el.findtext("developer") or None,
                "publisher": el.findtext("publisher") or None,
                "date": {k: date.get(k) for k in ("year", "month", "day") if date is not None and date.get(k)} or None,
                "genres": [g.strip() for g in (el.findtext("genre") or "").split(",") if g.strip()],
                "region": el.findtext("region") or None,
                "languages": [x.strip() for x in (el.findtext("languages") or "").split(",") if x.strip()],
                "rating": {"type": rating.get("type"), "value": rating.get("value"),
                           "descriptors": [d.text for d in rating.findall("descriptor") if d.text]} if rating is not None and rating.get("value") else None,
                "players": int(inp.get("players")) if inp is not None and (inp.get("players") or "").isdigit() else None,
                "controls": [{"type": c.get("type"), "required": c.get("required") == "true"} for c in inp.findall("control")] if inp is not None else [],
                "online_players": int(wifi.get("players")) if wifi is not None and (wifi.get("players") or "").isdigit() else None,
                "online_features": [f.text for f in wifi.findall("feature") if f.text] if wifi is not None else [],
            }
        el.clear()
    return out


def load_gameinfo() -> bool:
    """Load the cached database, downloading a fresh one if missing or stale. True if usable."""
    global gameinfo
    try:
        stale = not GAMEINFO_FILE.exists() or time.time() - GAMEINFO_FILE.stat().st_mtime > GAMEINFO_MAX_AGE
        if stale:
            req = urllib.request.Request(GAMEINFO_URL, headers={"User-Agent": "piwii"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml_name = next(n for n in z.namelist() if n.endswith(".xml"))
                parsed = parse_wiitdb(z.read(xml_name))
            if not parsed:
                raise ValueError("wiitdb.xml had no games")
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            tmp = GAMEINFO_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(parsed))
            os.replace(tmp, GAMEINFO_FILE)
            gameinfo_state["last_error"] = None
    except Exception as e:  # noqa: BLE001 - keep serving the old copy, retry later
        gameinfo_state["last_error"] = f"{type(e).__name__}: {e}"[:200]
        print(f"piwii: GameTDB database refresh failed: {gameinfo_state['last_error']}")
    if GAMEINFO_FILE.exists() and (not gameinfo or gameinfo_state["loaded_at"] is None
                                   or GAMEINFO_FILE.stat().st_mtime > gameinfo_state["loaded_at"]):
        gameinfo = json.loads(GAMEINFO_FILE.read_text())
        gameinfo_state["loaded_at"] = GAMEINFO_FILE.stat().st_mtime
    return bool(gameinfo) and gameinfo_state["last_error"] is None


def gameinfo_refresher() -> None:
    while True:
        ok = load_gameinfo()
        time.sleep(24 * 3600 if ok else 3600)


# --- Pi status ---------------------------------------------------------------

# The Pi writes staging/status/pi-status.json every 5 s and
# staging/status/sync-status.json at each sync step (status/ is RAM on the Pi). They're read in a background thread so a hung NFS mount
# (Pi down, hard mount) only makes the snapshot go stale instead of blocking
# requests. Older than PI_STALE_AFTER seconds counts as offline, unless a copy
# to the Pi wrote data within that time: over the Pi's Wi-Fi a multi-GB copy
# holds up the small status reads for minutes, but data still flowing proves
# the Pi and the share are fine.
PI_STALE_AFTER = 60
pi_snapshot: dict = {"pi": None, "sync": None, "read_at": 0.0}
copy_progress: dict = {"at": 0.0}  # when a hand-off last wrote a chunk to the share


class ShareNotMounted(OSError):
    """STAGING_DIR is the bare local folder: the Pi's share isn't mounted on it."""


def require_share() -> None:
    if not STAGING_MARKER.exists():
        raise ShareNotMounted(f"the Pi's staging share isn't mounted ({STAGING_MARKER} missing)")


def read_json(name: str) -> dict | None:
    path = STAGING_DIR / name
    return json.loads(path.read_text()) if path.exists() else None


def pi_poller() -> None:
    failing = False
    while True:
        try:
            require_share()
            snap = {"pi": read_json("status/pi-status.json"), "sync": read_json("status/sync-status.json"), "read_at": time.time()}
            pi_snapshot.update(snap)
            failing = False
        except Exception as e:  # noqa: BLE001 - keep the last good snapshot
            if not failing:
                print(f"piwii: reading Pi status failed: {e}")
            failing = True
        time.sleep(5)


def pi_view() -> dict:
    pi, sync = pi_snapshot["pi"], pi_snapshot["sync"]
    now = time.time()
    age = now - pi["updated"] if pi else None
    fresh = age is not None and age < PI_STALE_AFTER and now - pi_snapshot["read_at"] < PI_STALE_AFTER
    copying = now - copy_progress["at"] < PI_STALE_AFTER
    busy = bool(sync) and sync.get("phase") not in (None, "idle")
    return {"online": fresh or copying, "copying": copying, "age_s": None if age is None else int(age),
            "pi": pi, "sync": sync, "syncing": busy}


# --- staging snapshot --------------------------------------------------------

# What the staging share holds (the Pi's library.json and the folder names in
# ready/, failed/ and duplicates/), read every STAGING_POLL_SECS in a background
# thread. Pages answer from this snapshot, so a hung NFS mount stops it updating
# instead of blocking requests. library.json is re-read only when its mtime
# changes, and the last good copy is saved to LIBRARY_CACHE so the library
# still shows after a restart while the Pi is unreachable.
STAGING_POLL_SECS = 5
LIBRARY_CACHE = WORK_DIR / "library-cache.json"
staging: dict = {"library": None, "ready": set(), "failed": set(), "duplicates": set(), "read_at": 0.0}
# Set when the share reads again after failing, so a waiting hand-off retries now.
staging_recovered = threading.Event()


def load_library_cache() -> None:
    try:
        staging["library"] = json.loads(LIBRARY_CACHE.read_text())
    except (FileNotFoundError, ValueError):
        pass


def save_library_cache(lib: dict) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib))
    os.replace(tmp, LIBRARY_CACHE)


def folder_names(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()} if path.is_dir() else set()


def staging_poller() -> None:
    lib_mtime: float | None = None
    failing = False
    while True:
        try:
            require_share()
            lib_file = STAGING_DIR / "library.json"
            try:
                mtime = lib_file.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            snap = {name: folder_names(STAGING_DIR / name) for name in ("ready", "failed", "duplicates")}
            if mtime != lib_mtime:
                lib = json.loads(lib_file.read_text()) if mtime is not None else None
                if lib is not None:
                    save_library_cache(lib)
                snap["library"] = lib
                lib_mtime = mtime
            snap["read_at"] = time.time()
            staging.update(snap)
            if failing:
                print("piwii: staging share readable again")
                staging_recovered.set()
            failing = False
        except Exception as e:  # noqa: BLE001 - keep the last good snapshot
            if not failing:
                print(f"piwii: reading staging share failed: {e}")
            failing = True
        time.sleep(STAGING_POLL_SECS)


# --- covers --------------------------------------------------------------------

cover_locks: dict[str, threading.Lock] = {}
cover_locks_lock = threading.Lock()


def fetch_cover(game_id: str) -> bytes | None:
    """Fetch a cover from GameTDB, trying the game's region, then EN, then US.

    Returns None if GameTDB has no cover; raises on network errors.
    """
    for region in dict.fromkeys([COVER_REGIONS.get(game_id[3], "EN"), "EN", "US"]):
        req = urllib.request.Request(COVER_URL.format(region=region, id=game_id), headers={"User-Agent": "piwii"})
        try:
            with urllib.request.urlopen(req, timeout=COVER_TIMEOUT) as resp:
                data = resp.read()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return None


def get_cover(game_id: str) -> Path | None:
    """Cached cover path, fetching from GameTDB on a miss. None if GameTDB has none."""
    path = COVERS_DIR / f"{game_id}.png"
    miss = COVERS_DIR / f"{game_id}.miss"
    with cover_locks_lock:
        lock = cover_locks.setdefault(game_id, threading.Lock())
    with lock:  # one GameTDB fetch per ID even if many tiles ask at once
        if path.exists():
            return path
        if miss.exists() and time.time() - miss.stat().st_mtime < COVER_MISS_TTL:
            return None
        data = fetch_cover(game_id)
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        if data is None:
            miss.touch()
            return None
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        miss.unlink(missing_ok=True)
        return path


def cover_prefetcher() -> None:
    """Keep covers cached for every game on the drive, so the page never waits on GameTDB."""
    while True:
        lib = None
        try:
            lib = read_library()
        except Exception:  # noqa: BLE001 - Pi unreachable; try again next round
            pass
        for g in (lib or {}).get("games", []):
            try:
                get_cover(g.get("id", ""))
            except Exception:  # noqa: BLE001 - GameTDB slow or down; next round
                pass
        time.sleep(600)


# --- conversion ---------------------------------------------------------------


def read_wbfs_header(path: Path) -> tuple[str, str]:
    """Return (ID6, title) from a WBFS file.

    A WBFS file starts with a 512-byte "WBFS" head sector. The next sector
    holds a copy of the disc header: ID6 at offset 0, title at 0x20 (64 bytes).
    """
    with path.open("rb") as f:
        head = f.read(0x260)
    if head[:4] != b"WBFS":
        raise ValueError(f"{path.name} is not a WBFS file")
    disc = head[0x200:]
    game_id = disc[:6].decode("ascii", "replace")
    title = disc[0x20:0x60].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[A-Z0-9]{6}", game_id):
        raise ValueError(f"unexpected game ID {game_id!r} in {path.name}")
    return game_id, title or game_id


def fat_safe(name: str) -> str:
    """Make a title safe for a FAT32 folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:64] or "Untitled"


def convert(job: Job, src: Path) -> Path:
    """Convert src into WORK_DIR/<job>.out/<Title [ID6]>/ and return that folder."""
    out_dir = WORK_DIR / f"{job.id}.out"
    out_dir.mkdir(parents=True)
    tmp = out_dir / "game.wbfs"

    update(job, state="converting", detail="wit copy --wbfs --split")
    # --split keeps every part under FAT32's 4 GiB limit: game.wbfs, game.wbf1, ...
    # With no size given, wit splits WBFS output at DEF_SPLIT_SIZE_ISO =
    # 0xffff8000 = 4 GiB - 32 KiB, the usual USB loader size (wiimms-iso-tools
    # lib-file.c SetupSplitWFile). The "4 GB" in `wit help copy` is the
    # DEF_SPLIT_SIZE for other formats (WDF/WIA), not WBFS.
    proc = subprocess.run(
        [WIT, "copy", str(src), "--wbfs", "--split", "--overwrite", "--dest", str(tmp)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"wit failed: {(proc.stderr or proc.stdout).strip()[-500:]}")

    game_id, title = read_wbfs_header(tmp)
    update(job, game_id=game_id, title=titles.get(game_id, title))

    folder = f"{fat_safe(title)} [{game_id}]"
    final = out_dir / folder
    final.mkdir()
    for part in sorted(out_dir.glob("game.wb*")):
        # game.wbfs -> RTZE08.wbfs, game.wbf1 -> RTZE08.wbf1
        part.rename(final / f"{game_id}{part.suffix}")

    update(job, state="handing-off", detail="waiting to copy to the Pi", folder=folder)
    return final


def worker() -> None:
    while True:
        job, src = queue.get()
        try:
            handoffs.put((job, convert(job, src)))
        except Exception as e:  # noqa: BLE001 - report any failure on the job
            update(job, state="failed", detail=str(e))
            shutil.rmtree(WORK_DIR / f"{job.id}.out", ignore_errors=True)
        finally:
            src.unlink(missing_ok=True)
            queue.task_done()


# --- hand-off -----------------------------------------------------------------

# A converted game waits in WORK_DIR/<job>.out/<Title [ID6]>/ until it's copied
# to the Pi. A failed copy (Pi off, NFS error) is retried forever, backing off
# from HANDOFF_RETRY_MIN to HANDOFF_RETRY_MAX between tries, and right away
# when the staging share reads again. Pending copies survive restarts: startup
# re-queues every converted folder still in WORK_DIR.
HANDOFF_RETRY_MIN = 30
HANDOFF_RETRY_MAX = 30 * 60
handoffs: Queue[tuple[Job, Path]] = Queue()


class AlreadyStaged(Exception):
    """A folder with this name is already in ready/; retrying won't help."""


COPY_SYNC_EVERY = 64 * 1024 * 1024


def copy_folder(src: Path, dest: Path, job: Job) -> None:
    """Copy src's files into dest, recording progress on the job and in copy_progress.

    Syncs every COPY_SYNC_EVERY bytes so progress means data on the Pi, not
    data in the server host's page cache (which would otherwise finish "instantly" and
    then flush for minutes on close).
    """
    files = sorted(p for p in src.iterdir() if p.is_file())
    total, done, shown = sum(p.stat().st_size for p in files) or 1, 0, -1
    dest.mkdir()
    for path in files:
        with path.open("rb") as r, (dest / path.name).open("wb") as w:
            unsynced = 0
            while chunk := r.read(CHUNK):
                w.write(chunk)
                unsynced += len(chunk)
                if unsynced >= COPY_SYNC_EVERY:
                    w.flush()
                    os.fsync(w.fileno())
                    unsynced = 0
                done += len(chunk)
                copy_progress["at"] = time.time()
                if (pct := done * 100 // total) != shown:
                    update(job, detail=f"copying to Pi staging as {job.folder} ({pct}%)")
                    shown = pct


def hand_off(folder_path: Path, job: Job) -> None:
    require_share()  # never write into the bare local folder
    incoming = STAGING_DIR / ".incoming" / job.folder
    ready = STAGING_DIR / "ready" / job.folder
    if ready.exists():
        raise AlreadyStaged(f"{job.folder} is already waiting in ready/ for the Pi")
    shutil.rmtree(incoming, ignore_errors=True)
    incoming.parent.mkdir(parents=True, exist_ok=True)
    ready.parent.mkdir(parents=True, exist_ok=True)
    copy_folder(folder_path, incoming, job)
    os.rename(incoming, ready)  # atomic: the Pi only ever sees complete folders


def for_humans(secs: int) -> str:
    return f"{secs // 60} min" if secs >= 60 else f"{secs} s"


def handoff_worker() -> None:
    while True:
        job, final = handoffs.get()
        delay = HANDOFF_RETRY_MIN
        while True:
            update(job, state="handing-off", detail=f"copying to Pi staging as {job.folder}")
            staging_recovered.clear()
            try:
                hand_off(final, job)
                update(job, state="done", detail="waiting for the Pi to sync")
                break
            except AlreadyStaged as e:
                update(job, state="failed", detail=str(e))
                break
            except Exception as e:  # noqa: BLE001 - Pi unreachable: keep the game and try again
                update(job, detail=f"couldn't copy to the Pi ({e}); retrying in {for_humans(delay)}")
                staging_recovered.wait(delay)
                delay = min(delay * 2, HANDOFF_RETRY_MAX)
        shutil.rmtree(final.parent, ignore_errors=True)
        handoffs.task_done()


def resume_jobs() -> None:
    """Pick up jobs a restart interrupted, so no game is lost or left on disk.

    A converted folder (<job>.out/<Title [ID6]>/) still needs copying to the
    Pi. A source file (<job>.src.<ext>) is deleted as soon as conversion
    finishes, so one still here was queued or mid-conversion: its partial
    output is discarded and it's converted again.
    """
    converted = set()
    for final in sorted(WORK_DIR.glob("*.out/*")):
        m = re.fullmatch(r"(.*) \[([A-Z0-9]{6})\]", final.name)
        if not (m and final.is_dir()):
            continue
        job = Job(id=final.parent.name.removesuffix(".out"), source="resumed after restart", state="handing-off",
                  detail="waiting to copy to the Pi", game_id=m.group(2), title=m.group(1), folder=final.name)
        converted.add(job.id)
        with jobs_lock:
            jobs[job.id] = job
        handoffs.put((job, final))
    for src in sorted(WORK_DIR.glob("*.src.*")):
        job_id = src.name.split(".", 1)[0]
        if job_id in converted:  # restart between converting and deleting the source
            src.unlink(missing_ok=True)
            continue
        shutil.rmtree(WORK_DIR / f"{job_id}.out", ignore_errors=True)
        job = Job(id=job_id, source="resumed after restart", detail="waiting for converter")
        with jobs_lock:
            jobs[job.id] = job
        queue.put((job, src))


# --- intake ------------------------------------------------------------------


def new_job(source: str, suffix: str) -> tuple[Job, Path]:
    if suffix.lower() not in GAME_SUFFIXES:
        raise HTTPException(400, f"unsupported file type {suffix!r}; expected one of {sorted(GAME_SUFFIXES)}")
    job = Job(id=uuid.uuid4().hex[:12], source=source)
    with jobs_lock:
        jobs[job.id] = job
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return job, WORK_DIR / f"{job.id}.src{suffix.lower()}"


def fetch(job: Job, url: str, dest: Path) -> None:
    try:
        update(job, state="receiving", detail="downloading")
        with urllib.request.urlopen(url) as resp, dest.open("wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(CHUNK):
                f.write(chunk)
                done += len(chunk)
                pct = f" ({done * 100 // total}%)" if total else ""
                update(job, detail=f"downloaded {done >> 20} MiB{pct}")
        queue.put((job, dest))
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        update(job, state="failed", detail=f"download failed: {e}")


app = FastAPI(title="piwii")


# --- auth ----------------------------------------------------------------------
#
# Every page and API needs a login (PIWII_USERNAME / PIWII_PASSWORD from
# server/.env), on the LAN and through the tunnel alike. A session is an
# HMAC-signed cookie, valid SESSION_DAYS, keyed to the password so changing it
# logs everyone out. Five wrong tries from one client lock it out for 15 min.
# With no credentials configured, everything is refused (fails closed).

AUTH_USER = os.environ.get("PIWII_USERNAME", "")
AUTH_PASS = os.environ.get("PIWII_PASSWORD", "")
SESSION_COOKIE = "piwii_session"
SESSION_DAYS = 30
LOCKOUT_TRIES, LOCKOUT_SECS = 5, 15 * 60
PUBLIC_PATHS = {"/login", "/healthz", "/healthz/pi", "/favicon.png", "/favicon-32.png", "/favicon.ico"}

# Service accounts, like the program that fulfils requests, use an API token
# instead of the login: "Authorization: Bearer <token>" on every call, with no
# cookie to expire and no lockout to trip. PIWII_API_TOKENS holds named tokens
# (name:token, comma-separated) so each service can be revoked on its own. A
# token only opens API_TOKEN_ROUTES, what a fulfiller needs: never the pages,
# uploads or deleting requests, so a leaked token can do little.
API_TOKENS = {name.strip(): token.strip() for name, sep, token in
              (item.partition(":") for item in os.environ.get("PIWII_API_TOKENS", "").split(","))
              if sep and name.strip() and token.strip()}
API_TOKEN_ROUTES = [
    ("GET", re.compile(r"/api/requests")),
    ("PATCH", re.compile(r"/api/requests/[^/]+")),
    ("GET", re.compile(r"/api/import")),
    ("POST", re.compile(r"/api/import")),
    ("GET", re.compile(r"/api/jobs")),
]

# The domains piwii is served under (PIWII_DOMAIN, comma-separated, e.g.
# example.com,example.net): for each, the page is at wii.<domain> from the
# internet (through a tunnel) and wii.lan.<domain> on the home network. The
# page on wii.<domain> sends uploads straight to wii.lan.<domain> of the same
# domain when it can reach it, since the login cookie only covers that domain.
# Unset, piwii just works on whatever host it's reached at, with no
# cross-origin uploads or shared cookie.
DOMAINS = [d for d in (x.strip().lower() for x in os.environ.get("PIWII_DOMAIN", "").split(",")) if d]
# These origins may call the API cross-origin with the shared login cookie.
CORS_ORIGINS = set(filter(None, os.environ.get(
    "PIWII_CORS_ORIGINS", ",".join(f"https://wii.{d},https://wii.lan.{d}" for d in DOMAINS)).split(",")))
# The login cookie is set for whichever of these domains the request came in
# under, so one login covers wii.<domain> and wii.lan.<domain>.
COOKIE_DOMAINS = [d for d in (x.strip().lower() for x in os.environ.get("PIWII_COOKIE_DOMAIN", ",".join(DOMAINS)).split(",")) if d]


def request_host(request: Request) -> str:
    return (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0].lower()


def domain_under(host: str, domains: list[str]) -> str | None:
    """The most specific of domains that host is, or is a subdomain of."""
    return max((d for d in domains if host == d or host.endswith("." + d)), key=len, default=None)


def cookie_domain_for(request: Request) -> str | None:
    return domain_under(request_host(request), COOKIE_DOMAINS)


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"


def set_session_cookie(resp: Response, request: Request, value: str) -> None:
    resp.set_cookie(SESSION_COOKIE, value, max_age=SESSION_DAYS * 86400, httponly=True,
                    secure=is_https(request), samesite="lax", domain=cookie_domain_for(request))


def add_cors(resp: Response, origin: str | None) -> Response:
    if origin in CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    return resp
login_failures: dict[str, list[float]] = {}


def session_secret() -> bytes:
    """Per-install signing key, kept in the work dir (persists across restarts)."""
    path = WORK_DIR / "session-secret"
    if not path.exists():
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


SESSION_KEY = hmac.new(session_secret(), f"{AUTH_USER}\0{AUTH_PASS}".encode(), hashlib.sha256).digest()


def make_session(expires: int) -> str:
    sig = hmac.new(SESSION_KEY, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session(cookie: str | None) -> bool:
    if not cookie or not AUTH_USER or not AUTH_PASS:
        return False
    expires, _, sig = cookie.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, make_session(int(expires)).partition(".")[2])


def api_token_name(token: str) -> str | None:
    """The service a bearer token belongs to. Every token is compared, in
    constant time, so the time taken doesn't hint at a near match."""
    found = None
    for name, known in API_TOKENS.items():
        if hmac.compare_digest(token.encode(), known.encode()):
            found = name
    return found


def api_token_allowed(method: str, path: str) -> bool:
    return any(method == m and rx.fullmatch(path) for m, rx in API_TOKEN_ROUTES)


def client_key(request: Request) -> str:
    """Who to count login failures against.

    LAN clients reach the container with their real address. Tunnel traffic
    arrives from cloudflared on the server host itself, so it shows up as Docker's
    gateway (172.16/12) or loopback; only then is Cloudflare's
    Cf-Connecting-IP trusted, so a LAN client can't fake it to dodge the lockout.
    """
    host = request.client.host if request.client else "?"
    via_cloudflared = host.startswith("127.") or re.match(r"172\.(1[6-9]|2\d|3[01])\.", host)
    return (via_cloudflared and request.headers.get("cf-connecting-ip")) or host


@app.middleware("http")
async def require_login(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method == "OPTIONS" and origin in CORS_ORIGINS:
        # CORS preflight (no cookies on these). Chrome's Private/Local Network
        # Access also asks before a public page may reach a home-network address.
        resp = Response(status_code=204, headers={
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
            "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "content-type"),
            "Access-Control-Max-Age": "600",
        })
        if request.headers.get("access-control-request-private-network") == "true":
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return add_cors(resp, origin)
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and request.url.path not in PUBLIC_PATHS:
        name = api_token_name(auth[7:].strip())
        if name is None:
            return JSONResponse({"detail": "invalid API token"}, status_code=401)
        if not api_token_allowed(request.method, request.url.path):
            print(f"piwii: API token {name!r} refused for {request.method} {request.url.path}")
            return JSONResponse({"detail": "API tokens can't use this route"}, status_code=403)
        return await call_next(request)
    cookie = request.cookies.get(SESSION_COOKIE)
    if request.url.path in PUBLIC_PATHS or valid_session(cookie):
        resp = await call_next(request)
        # Sessions from before the shared cookie were host-only; re-issue them
        # for the whole domain on page loads so the LAN address works too.
        if request.method == "GET" and request.url.path == "/" and valid_session(cookie) and cookie_domain_for(request):
            set_session_cookie(resp, request, cookie)
        return add_cors(resp, origin)
    if request.url.path.startswith("/api/") or request.url.path == "/upload.js":
        return add_cors(JSONResponse({"detail": "login required"}, status_code=401), origin)
    nxt = urllib.parse.quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>piwii · sign in</title>
<link rel="icon" href="/favicon-32.png?v=4" type="image/png" sizes="32x32"><link rel="apple-touch-icon" href="/favicon.png?v=4">
<style>
 :root {{ --bg:#eceef1; --ink:#3c4148; --muted:#8a9099; --line:#cdd2d9; --wii:#34bfea; --deep:#0f9fd0; --bad:#e0565b; }}
 body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; box-sizing:border-box;
   background:var(--bg) repeating-linear-gradient(0deg,#e4e7eb 0 2px,transparent 2px 6px); color:var(--ink); font:600 15px/1.4 system-ui,sans-serif; }}
 form {{ width:min(340px,100%); background:#fff; border:3px solid var(--line); border-radius:22px; padding:24px; box-shadow:0 2px 0 #c3c8cf,0 8px 20px rgba(40,50,70,.12); }}
 h1 {{ margin:0 0 16px; font-size:26px; color:#9aa1aa; }} h1 b {{ color:var(--wii); }}
 label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:12px 0 4px; }}
 input {{ width:100%; box-sizing:border-box; padding:10px 12px; border-radius:12px; border:2px solid var(--line); font:inherit; }}
 button {{ width:100%; margin-top:18px; border:0; border-radius:999px; padding:11px; font:inherit; font-weight:800; color:#fff;
   background:linear-gradient(#4fd0f5,var(--deep)); box-shadow:0 3px 0 #0b7fa8; cursor:pointer; }}
 .err {{ color:var(--bad); margin-top:12px; font-size:14px; }}
</style></head><body>
<form method="post" action="/login"><h1><b>pi</b>wii</h1>
<input type="hidden" name="next" value="{next}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button>Sign in</button>{error}</form></body></html>"""


def safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


def login_page(nxt: str, error: str = "", status: int = 200) -> HTMLResponse:
    import html
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return HTMLResponse(LOGIN_PAGE.format(next=html.escape(safe_next(nxt)), error=err), status_code=status)


@app.get("/login")
def login_form(request: Request, next: str = "/") -> Response:
    if valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(safe_next(next), status_code=303)  # already signed in
    if not AUTH_USER or not AUTH_PASS:
        return login_page(next, "Login isn't configured on the server (PIWII_USERNAME / PIWII_PASSWORD).", 503)
    return login_page(next)


@app.post("/login")
async def login_submit(request: Request) -> Response:
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    user, pw, nxt = (form.get(k, [""])[0] for k in ("username", "password", "next"))
    key, now = client_key(request), time.time()
    recent = [t for t in login_failures.get(key, []) if now - t < LOCKOUT_SECS]
    if len(recent) >= LOCKOUT_TRIES:
        return login_page(nxt, "Too many failed attempts. Try again in 15 minutes.", 429)
    ok = bool(AUTH_USER and AUTH_PASS) and hmac.compare_digest(user.encode(), AUTH_USER.encode()) \
        & hmac.compare_digest(pw.encode(), AUTH_PASS.encode())
    if not ok:
        login_failures[key] = recent + [now]
        return login_page(nxt, "Wrong username or password.", 401)
    login_failures.pop(key, None)
    resp = RedirectResponse(safe_next(nxt), status_code=303)
    set_session_cookie(resp, request, make_session(int(now) + SESSION_DAYS * 86400))
    return resp


@app.get("/logout")
def logout(request: Request) -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)  # any old host-only cookie
    if cookie_domain_for(request):
        resp.delete_cookie(SESSION_COOKIE, domain=cookie_domain_for(request))
    return resp


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated health check for Docker: is the web server up?

    Never touches NFS: a slow, unreachable or stale share is never fixed by
    restarting piwii. The server host keeps the share mounted and remounts it
    (server/host/piwii-staging-mount.sh). Pi health is /healthz/pi.
    """
    return {"ok": True}


@app.get("/healthz/pi")
def healthz_pi() -> JSONResponse:
    """Unauthenticated Pi check for Uptime Kuma: is the Pi's status file fresh over NFS?

    Reads the background snapshot, never NFS, so a hung mount reports offline
    instead of blocking. 503 when offline.
    """
    view = pi_view()
    body = {"online": view["online"], "copying": view["copying"], "age_s": view["age_s"]}
    return JSONResponse(body, status_code=200 if view["online"] else 503)


@app.on_event("startup")
def start_worker() -> None:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    load_library_cache()
    load_requests()
    resume_jobs()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=handoff_worker, daemon=True).start()
    threading.Thread(target=staging_poller, daemon=True).start()
    threading.Thread(target=titles_refresher, daemon=True).start()
    threading.Thread(target=pi_poller, daemon=True).start()
    threading.Thread(target=cover_prefetcher, daemon=True).start()
    threading.Thread(target=gameinfo_refresher, daemon=True).start()
    threading.Thread(target=upload_janitor, daemon=True).start()


def page(name: str, request: Request) -> HTMLResponse:
    """Serve a page with upload.js linked by a hash of its contents.

    Cloudflare caches .js at the edge for hours regardless of our headers, so
    a content-addressed URL is the only way a changed upload.js reaches
    browsers immediately. The page itself is never cached.
    """
    js_v = hashlib.sha256((STATIC / "upload.js").read_bytes()).hexdigest()[:10]
    # upload.js reads the hostnames from here, so it holds no domain itself.
    # They're for the domain this request came in under, if any.
    domain = domain_under(request_host(request), DOMAINS) or ""
    config = json.dumps({"domain": domain, "publicHost": f"wii.{domain}" if domain else "",
                         "lanHost": f"wii.lan.{domain}" if domain else ""})
    html = (STATIC / name).read_text().replace(
        '<script src="/upload.js?v=2"></script>',
        f'<script>window.piwiiConfig = {config};</script>\n<script src="/upload.js?v={js_v}"></script>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index(request: Request) -> FileResponse:
    return page("wii.html", request)


# Pages link /favicon-32.png?v=N (tab) and /favicon.png?v=N (180px, home
# screens): bump N when the icon changes, since Firefox caches favicons by URL.
# The icon files aren't in the repo (no redistributable icon); each deployment
# adds its own to server/static/ (see the README). Without them, these 404.
def icon(name: str) -> FileResponse:
    path = STATIC / name
    if not path.is_file():
        raise HTTPException(404, f"no {name}: add one to server/static/")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.png")
def favicon_large() -> FileResponse:
    return icon("favicon.png")


@app.get("/favicon-32.png")
@app.get("/favicon.ico")  # browsers ask for this by default; a PNG works there
def favicon() -> FileResponse:
    return icon("favicon-32.png")


@app.get("/upload.js")
def upload_js() -> FileResponse:
    return FileResponse(STATIC / "upload.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/legacy")
def legacy(request: Request) -> FileResponse:
    return page("legacy.html", request)


class UploadStart(BaseModel):
    filename: str
    size: int


upload_locks: dict[str, threading.Lock] = {}
upload_locks_lock = threading.Lock()


def upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{12}", upload_id):
        raise HTTPException(400, "bad upload ID")
    part, meta = UPLOADS_DIR / f"{upload_id}.part", UPLOADS_DIR / f"{upload_id}.json"
    if not meta.exists():
        raise HTTPException(404, "no such upload (finished, cancelled, or expired)")
    return part, meta


def upload_lock(upload_id: str) -> threading.Lock:
    with upload_locks_lock:
        return upload_locks.setdefault(upload_id, threading.Lock())


def upload_view(upload_id: str, meta: dict, part: Path) -> dict:
    return {"id": upload_id, "filename": meta["filename"], "size": meta["size"],
            "received": part.stat().st_size if part.exists() else 0, "chunk_size": UPLOAD_CHUNK}


@app.post("/api/uploads")
def start_upload(body: UploadStart) -> dict:
    suffix = Path(body.filename).suffix.lower()
    if suffix not in GAME_SUFFIXES:
        raise HTTPException(400, f"unsupported file type {suffix!r}; expected one of {sorted(GAME_SUFFIXES)}")
    if not 0 < body.size <= UPLOAD_MAX_SIZE:
        raise HTTPException(400, f"size must be between 1 byte and {UPLOAD_MAX_SIZE >> 30} GiB")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(UPLOADS_DIR).free
    if free < body.size * 2:  # the upload plus wit's converted copy
        raise HTTPException(507, f"not enough space on the server: need {body.size * 2 >> 20} MiB, have {free >> 20} MiB")
    upload_id = uuid.uuid4().hex[:12]
    meta = {"filename": Path(body.filename).name, "size": body.size, "created": time.time()}
    (UPLOADS_DIR / f"{upload_id}.part").touch()
    (UPLOADS_DIR / f"{upload_id}.json").write_text(json.dumps(meta))
    return upload_view(upload_id, meta, UPLOADS_DIR / f"{upload_id}.part")


@app.get("/api/uploads/{upload_id}")
def upload_status(upload_id: str) -> dict:
    part, meta = upload_paths(upload_id)
    return upload_view(upload_id, json.loads(meta.read_text()), part)


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, offset: int, request: Request) -> dict:
    """Write one chunk at `offset`. Chunks must arrive in order; re-sending one is harmless."""
    part, meta_path = upload_paths(upload_id)
    meta = json.loads(meta_path.read_text())
    lock = upload_lock(upload_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "another chunk for this upload is still being written")
    try:
        received = part.stat().st_size
        if offset > received or offset < 0:
            # A gap would leave a hole in the file; tell the client where to resume.
            raise HTTPException(409, f"expected offset <= {received}")
        written = 0
        with part.open("r+b") as f:
            f.seek(offset)
            try:
                async for data in request.stream():
                    written += len(data)
                    if written > UPLOAD_MAX_CHUNK or offset + written > meta["size"]:
                        raise HTTPException(413, "chunk too large or past the end of the file")
                    f.write(data)
            except ClientDisconnect:
                # Browser cancelled or dropped mid-chunk; the client resyncs from
                # `received` and resends, so this is routine, not an error.
                raise HTTPException(499, "client disconnected")
        os.utime(meta_path)  # marks the upload active for the idle cleanup
        return upload_view(upload_id, meta, part)
    finally:
        lock.release()


@app.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str) -> dict:
    part, meta_path = upload_paths(upload_id)
    meta = json.loads(meta_path.read_text())
    with upload_lock(upload_id):
        received = part.stat().st_size
        if received != meta["size"]:
            raise HTTPException(409, f"upload incomplete: {received} of {meta['size']} bytes")
        job, dest = new_job(f"upload: {meta['filename']}", Path(meta["filename"]).suffix)
        os.replace(part, dest)  # same filesystem: instant, no copy
        meta_path.unlink(missing_ok=True)
    queue.put((job, dest))
    update(job, state="queued", detail="waiting for converter")
    return asdict(job)


@app.delete("/api/uploads/{upload_id}")
def cancel_upload(upload_id: str) -> dict:
    part, meta = upload_paths(upload_id)
    with upload_lock(upload_id):
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
    return {"id": upload_id, "cancelled": True}


def upload_janitor() -> None:
    """Delete uploads nobody has touched for a day, so abandoned ones don't fill the SSD."""
    while True:
        cutoff = time.time() - UPLOAD_IDLE_TTL
        for meta in UPLOADS_DIR.glob("*.json") if UPLOADS_DIR.exists() else []:
            try:
                if meta.stat().st_mtime < cutoff:
                    meta.with_suffix(".part").unlink(missing_ok=True)
                    meta.unlink(missing_ok=True)
            except OSError:
                pass
        time.sleep(3600)


class UrlRequest(BaseModel):
    url: str


@app.post("/api/url")
def from_url(body: UrlRequest) -> dict:
    parsed = urllib.parse.urlparse(body.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "only http(s) URLs are supported")
    job, dest = new_job(f"url: {body.url}", Path(parsed.path).suffix)
    threading.Thread(target=fetch, args=(job, body.url, dest), daemon=True).start()
    return asdict(job)


# --- import folder -------------------------------------------------------------

# A drop folder for games copied straight onto the server host (rsync, scp, cp, or
# another container with this folder mounted) instead of uploaded over HTTP.
# Nothing is picked up automatically: POST /api/import names a file, and it
# is moved into a job exactly like a finished upload. Writers should copy
# under a temporary name and rename when done; dotfiles (rsync's temp files)
# and anything that isn't a game file type are never listed or imported.
IMPORT_DIR = WORK_DIR / "import"
IMPORT_SETTLE_SECS = 5  # a file modified more recently than this may still be copying
import_lock = threading.Lock()


def importable(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in GAME_SUFFIXES


@app.get("/api/import")
def list_import() -> dict:
    """Files waiting in the import folder."""
    now = time.time()
    files = []
    for p in sorted(IMPORT_DIR.iterdir()) if IMPORT_DIR.exists() else []:
        if importable(p):
            st = p.stat()
            files.append({"filename": p.name, "size": st.st_size, "modified": int(st.st_mtime),
                          "settling": now - st.st_mtime < IMPORT_SETTLE_SECS})
    return {"dir": str(IMPORT_DIR), "files": files}


class ImportRequest(BaseModel):
    filename: str


@app.post("/api/import")
def import_file(body: ImportRequest) -> dict:
    """Move a file from the import folder into the converter queue."""
    name = body.filename
    if not name or name != Path(name).name or name in (".", ".."):
        raise HTTPException(400, "filename must be a plain file name inside the import folder")
    with import_lock:  # two imports of the same file: the second gets 404
        src = IMPORT_DIR / name
        if not importable(src):
            if src.is_file():
                raise HTTPException(400, f"only game files ({', '.join(sorted(GAME_SUFFIXES))}) can be imported, and not dotfiles")
            raise HTTPException(404, f"{name!r} isn't in the import folder")
        st = src.stat()
        if time.time() - st.st_mtime < IMPORT_SETTLE_SECS:
            raise HTTPException(409, "file is still changing; wait until the copy finishes")
        job, dest = new_job(f"import: {name}", src.suffix)
        free = shutil.disk_usage(WORK_DIR).free
        if free < st.st_size:  # room for wit's converted copy next to the source
            with jobs_lock:
                jobs.pop(job.id, None)
            raise HTTPException(507, f"not enough space on the server: need {st.st_size >> 20} MiB, have {free >> 20} MiB")
        shutil.move(src, dest)  # same filesystem: a rename, no copy
    queue.put((job, dest))
    update(job, state="queued", detail="waiting for converter")
    return asdict(job)


def read_library() -> dict | None:
    """The Pi's library.json, from the staging snapshot (never NFS directly)."""
    return staging["library"]


def pi_status(job: Job, installed_ids: set[str]) -> tuple[str, str] | None:
    """Where a handed-off game is now, judged from the staging snapshot."""
    if job.folder in staging["ready"]:
        view = pi_view()
        if view["syncing"] and view["sync"].get("detail"):
            return "done", f"Pi: {view['sync']['detail']}"
        return "done", "waiting for the Pi to sync"
    if job.folder in staging["failed"]:
        return "failed", "the Pi couldn't copy it; see staging/failed/"
    if job.folder in staging["duplicates"]:
        return "installed", "already on the drive; the upload was set aside in staging/duplicates/"
    if job.game_id in installed_ids:
        return "installed", "on the drive"
    return None


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    lib = read_library() or {}
    installed_ids = {g.get("id") for g in lib.get("games", [])}
    with jobs_lock:
        for job in jobs.values():
            if job.folder and job.state in ("done", "installed"):
                status = pi_status(job, installed_ids)
                if status:
                    job.state, job.detail = status
        return [asdict(j) for j in sorted(jobs.values(), key=lambda j: j.created, reverse=True)]


@app.get("/api/cover/{game_id}")
def cover(game_id: str) -> Response:
    if not re.fullmatch(r"[A-Z0-9]{6}", game_id):
        raise HTTPException(400, "bad game ID")
    try:
        path = get_cover(game_id)
    except Exception as e:  # noqa: BLE001 - GameTDB unreachable: don't cache, try again later
        raise HTTPException(502, f"GameTDB unavailable: {e}")
    if path is None:
        raise HTTPException(404, "no cover on GameTDB")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/game/{game_id}")
def game_info(game_id: str) -> dict:
    """GameTDB details for one game, from the cached database."""
    info = gameinfo.get(game_id)
    if info is None:
        status = "not in GameTDB" if gameinfo else "GameTDB database not downloaded yet"
        return {"id": game_id, "info": None, "status": status, "last_error": gameinfo_state["last_error"]}
    return {"id": game_id, "info": info, "status": "ok"}


@app.get("/api/pi")
def pi() -> dict:
    return pi_view()


@app.get("/api/library")
def library() -> dict:
    """Games on the game drive (written by the Pi after each sync) and games waiting to sync."""
    installed = read_library()
    if installed:  # copy: the snapshot is shared
        installed = {**installed, "games": [{**g, "title": titles.get(g.get("id", ""))} for g in installed.get("games", [])]}
    return {"installed": installed, "pending": sorted(staging["ready"]), "read_at": staging["read_at"]}


# --- game requests -----------------------------------------------------------

# Games someone wants on the drive but nobody has yet. A request is just a
# GameTDB entry (ID6, title, year, region) plus who asked; whoever fulfills it
# gets the game onto the game drive the normal way (upload, URL, or a later import),
# and the request shows as "done" once an upload of that ID is handed off to
# the Pi, then "on the drive" once the ID appears in library.json. Its status
# can also be set by hand (requested, in progress, done), e.g. by the app that
# fulfils requests.
# Requests live in memory, so reads never touch the disk and fulfillers can poll
# GET /api/requests often. WORK_DIR/requests.json is this server's own storage:
# read once at startup and rewritten on every change, before memory is updated,
# so the two always agree. Nothing else may edit it; a hand edit is only picked
# up after a restart and is overwritten by the next change. Other tools use the
# API instead.
REQUESTS_FILE = WORK_DIR / "requests.json"
REGIONS = {"E": "USA", "P": "Europe", "J": "Japan", "K": "Korea", "D": "Germany", "F": "France",
           "S": "Spain", "I": "Italy", "X": "Europe", "Y": "Europe", "U": "Australia", "W": "Taiwan"}
requests_lock = threading.Lock()
game_requests: dict[str, dict] = {}  # ID6 -> request; entries are replaced, never changed in place


def load_requests() -> None:
    try:
        game_requests.update(json.loads(REQUESTS_FILE.read_text()))
    except FileNotFoundError:
        pass


def save_requests(reqs: dict[str, dict]) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REQUESTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reqs, indent=1))
    os.replace(tmp, REQUESTS_FILE)


def catalog_entry(game_id: str) -> dict:
    """What a request records about a game, from GameTDB."""
    info = gameinfo.get(game_id) or {}
    date = info.get("date") or {}
    return {
        "id": game_id,
        "title": info.get("title") or titles.get(game_id) or game_id,
        "year": int(date["year"]) if str(date.get("year", "")).isdigit() else None,
        "region": REGIONS.get(game_id[3], game_id[3]),
        "publisher": info.get("publisher"),
        "gametdb_url": f"https://www.gametdb.com/Wii/{game_id}",
    }


def installed_ids() -> set[str]:
    return {g.get("id") for g in (read_library() or {}).get("games", [])}


def handed_off_ids() -> set[str]:
    """IDs of uploads sitting in staging/ready, waiting for the Pi to sync them."""
    return {m.group(1) for name in staging["ready"] if (m := re.search(r"\[([A-Z0-9]{6})\]$", name))}


@app.get("/api/catalog")
def catalog(q: str = "", limit: int = 30) -> dict:
    """Search GameTDB's Wii list by title words or ID, for the request form."""
    words = q.lower().split()
    if not words:
        return {"results": []}
    # Retail Wii discs only (IDs starting R or S): no mods/hacks (type
    # CUSTOM), demo discs (D...) or GameCube games (G...). Before the database
    # has loaded, fall back to titles.txt.
    names = {i: g.get("title") or i for i, g in gameinfo.items() if not g.get("type") and i[0] in "RS"} or \
            {i: t for i, t in titles.items() if i[0] in "RS"}
    qid = q.strip().upper()
    hits = [i for i, t in names.items() if i == qid or all(w in f"{t} {i}".lower() for w in words)]
    first = words[0]
    region_order = "EPJK"  # within a title: USA, Europe, Japan, Korea, then the rest
    hits.sort(key=lambda i: (i != qid, not names[i].lower().startswith(first), names[i].lower(),
                             region_order.find(i[3]) % 99, i))
    have = installed_ids()
    out = []
    for i in hits[:max(1, min(limit, 100))]:
        e = catalog_entry(i)
        e["on_drive"] = i in have
        e["requested"] = i in game_requests
        out.append(e)
    return {"results": out, "total": len(hits)}


REQUEST_STATUSES = ("requested", "in progress", "done", "throttled", "error")


class GameRequest(BaseModel):
    id: str
    note: str = ""


class RequestUpdate(BaseModel):
    status: str | None = None
    note: str | None = None


SHOWN_STATUSES = (*REQUEST_STATUSES, "on the drive")


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match against our tag. Cloudflare weakens tags (W/"...") when it
    compresses a response, and clients send back what they got, so W/ is ignored."""
    tags = [t.strip().removeprefix("W/") for t in (if_none_match or "").split(",")]
    return "*" in tags or etag in tags


@app.get("/api/requests")
def list_requests(request: Request, status: list[str] = Query(default=[])) -> Response:
    """Every request, or only those whose shown status is one of `status`.

    Made to be polled: the ETag is a hash of the body, so a client sending it
    back in If-None-Match gets an empty 304 until something it would see changes
    (including a game reaching the drive, which changes no stored request).
    """
    if bad := [s for s in status if s not in SHOWN_STATUSES]:
        raise HTTPException(400, f"unknown status {bad[0]!r}; use: {', '.join(SHOWN_STATUSES)}")
    have, uploaded = installed_ids(), handed_off_ids()
    with requests_lock:
        reqs = list(game_requests.values())
    # Being on the drive wins over whatever status was set by hand; an upload
    # of the game that's waiting for the Pi to sync it counts as done.
    out = [{**r, "status": "on the drive" if r["id"] in have else "done" if r["id"] in uploaded else r.get("status", "requested")}
           for r in reqs]
    if status:
        out = [r for r in out if r["status"] in status]
    order = {"requested": 0, "in progress": 0, "throttled": 0, "error": 0, "done": 1, "on the drive": 1}
    out.sort(key=lambda r: (order[r["status"]], -r["requested_at"]))
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:20]}"'
    # no-cache: browsers may keep the body but must check with us before reusing it.
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@app.post("/api/requests")
def add_request(body: GameRequest) -> dict:
    game_id = body.id.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", game_id):
        raise HTTPException(400, "bad game ID")
    if game_id not in titles and game_id not in gameinfo:
        raise HTTPException(404, "not a Wii game GameTDB knows about")
    if game_id in installed_ids():
        raise HTTPException(409, "already on the drive")
    with requests_lock:
        if game_id not in game_requests:
            r = {**catalog_entry(game_id), "note": body.note.strip()[:300], "status": "requested",
                 "requested_at": int(time.time()), "updated_at": int(time.time())}
            save_requests({**game_requests, game_id: r})
            game_requests[game_id] = r
        return game_requests[game_id]


@app.patch("/api/requests/{game_id}")
def update_request(game_id: str, body: RequestUpdate) -> dict:
    if body.status is not None and body.status not in REQUEST_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(REQUEST_STATUSES)}")
    with requests_lock:
        if game_id not in game_requests:
            raise HTTPException(404, "no such request")
        r = dict(game_requests[game_id])
        if body.status is not None:
            r["status"] = body.status
        if body.note is not None:
            r["note"] = body.note.strip()[:300]
        r["updated_at"] = int(time.time())
        save_requests({**game_requests, game_id: r})
        game_requests[game_id] = r
        return r


@app.delete("/api/requests/{game_id}")
def remove_request(game_id: str) -> dict:
    with requests_lock:
        if game_id not in game_requests:
            raise HTTPException(404, "no such request")
        save_requests({k: r for k, r in game_requests.items() if k != game_id})
        del game_requests[game_id]
    return {"ok": True}
