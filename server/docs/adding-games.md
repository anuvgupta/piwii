# Adding games to the web server

Every game ends up in the same pipeline:

1. The game becomes a job.
2. `wit` converts it to a split WBFS in `Title [ID6]/`.
3. The folder is handed to the Pi (`.incoming/` → `ready/`).
4. The Pi syncs it onto the game drive.

This page covers the ways a file can enter that pipeline, and which one
to use for which job. Every API call needs the login cookie or, for a
program, an API token; see
[requests-api.md → Authentication](requests-api.md#authentication).
An API token opens what a program fulfilling requests needs (reading
and updating requests, the import folder routes and `/api/jobs`), not
uploads or link downloads.

| Way in | Use it when | Status |
|---|---|---|
| [Chunked upload API](#1-chunked-upload-api) | Sending a file over HTTP from a browser or an app | Available |
| [Link download](#2-link-download) | The file is at a direct `http(s)` URL | Available |
| [Import folder](#3-import-folder) | Copying the file onto the server host with rsync, scp, cp or SMB, or from another container | Available |

Accepted file types for all three: `.iso`, `.wbfs`, `.ciso`, `.wia`,
`.wdf`, `.gcz`.

## Where files live on the server host

The web server runs in Docker. Its `/work` folder is a direct mount of a
folder on the server host, so the host and the container see the same files.
Paths on the host are this setup's (an Unraid server host, with the repo at
`/mnt/user/workspace/piwii`); on another host, use your clone's path.

| On the server host | In the container | What's there |
|---|---|---|
| `/mnt/user/workspace/piwii/server/work/` | `/work/` | The web server's working area: jobs being converted, GameTDB caches, covers, `requests.json` |
| `…/work/uploads/` | `/work/uploads/` | In-progress chunked uploads only (`<id>.part` + `<id>.json`) |
| `…/work/import/` | `/work/import/` | Drop folder for copies made outside the API ([import folder](#3-import-folder)) |

### No extra mount is needed for the import folder

piwii's compose file mounts the whole `server/work/` folder
(`./work:/work`), so `work/import/` is already a real folder on the server host's
disk. piwii creates it at startup. Nothing in piwii's container
config has to change for files to be dropped in. Containers can't share
files with each other directly: sharing always goes through a folder on
the host, and this is that folder.

How each kind of writer reaches it:

| Writer | How |
|---|---|
| Another machine (rsync, scp) | Write to `host:/mnt/user/workspace/piwii/server/work/import/` over SSH. `ssh host` logs in as root, which owns `work/`. |
| The server host's own shell | `cp` or `mv` into `/mnt/user/workspace/piwii/server/work/import/`. |
| Another container | Mount that same folder on the server host into it (see [Option B](#option-b-write-the-file-into-a-shared-folder-then-notify)). |

Give other containers only `work/import/`, not all of `work/`, so they
can't touch piwii's jobs, caches or `requests.json`.

**Don't copy files into `work/` or `work/uploads/` by hand.**

- `work/` only processes files that already belong to a job. A loose file
  there is ignored.
- `work/uploads/` belongs to the upload API. A file without its matching
  `.json` record is never picked up. Uploads untouched for 24 hours are
  deleted.

## 1. Chunked upload API

This is what the web page uses. It suits gradual uploads: each chunk is
written straight into place, so a dropped connection or a restart loses
nothing, and the client resumes from `received`. Chunks are 50 MB, under
Cloudflare's 100 MB request limit.

| Call | Does |
|---|---|
| `POST /api/uploads` `{"filename": "...", "size": <bytes>}` | Starts an upload. Returns `{id, filename, size, received, chunk_size}`. |
| `PUT /api/uploads/{id}?offset=<n>` (raw bytes as the body) | Writes one chunk at `offset`. Returns the same object with the new `received`. |
| `GET /api/uploads/{id}` | Shows how much has arrived; use this to resume. |
| `POST /api/uploads/{id}/complete` | Hands the finished file to the converter and returns the job. |
| `DELETE /api/uploads/{id}` | Cancels the upload and deletes the partial file. |

Rules:

- Send chunks in order, one at a time. `offset` may be at or before
  `received`, but not past it, because that would leave a hole.
- A chunk may be at most 64 MB, and the file at most 20 GiB.
- `POST /api/uploads` returns `507` if the server host lacks twice the file's size
  in free space (the upload plus `wit`'s converted copy).
- `complete` returns `409` until every byte has arrived.

```bash
BASE=https://wii.example.com   # or https://wii.lan.example.com at home
F="Mario Party 9.iso"
SIZE=$(stat -f%z "$F")        # macOS; on Linux: stat -c%s "$F"
ID=$(curl -s -b "$JAR" -H 'Content-Type: application/json' \
  -d "{\"filename\": \"$F\", \"size\": $SIZE}" "$BASE/api/uploads" | jq -r .id)

CHUNK=$((50 * 1000 * 1000)); OFF=0
while [ "$OFF" -lt "$SIZE" ]; do
  # Cut the next chunk out of the file and send it.
  tail -c +$((OFF + 1)) "$F" | head -c "$CHUNK" | \
    curl -s -b "$JAR" -X PUT --data-binary @- "$BASE/api/uploads/$ID?offset=$OFF" > /dev/null
  OFF=$(curl -s -b "$JAR" "$BASE/api/uploads/$ID" | jq .received)
done

curl -s -b "$JAR" -X POST "$BASE/api/uploads/$ID/complete"
```

## 2. Link download

`POST /api/url` `{"url": "https://..."}` makes the web server download the file
itself, then convert it. Use this only for a direct link to the file:
no login pages and no redirects to HTML.

## 3. Import folder

This is for copying a file straight onto the server host, with no HTTP upload:
rsync or scp from another machine, or `cp`/`mv` from elsewhere on
the server host's storage.

### Copying the file in

The server host is reached over SSH (as root, which owns `work/`; `host` below stands for its SSH alias). Because of the
bind mount, the container sees the file as soon as it lands on the server host.

```bash
rsync -av --progress "Mario Party 9.iso" \
  host:/mnt/user/workspace/piwii/server/work/import/
```

A file must only appear under its final name once it's complete:

- **rsync** already does this. It writes a hidden temp file
  (`.Mario Party 9.iso.XXXXXX`) and renames it at the end.
- **scp / cp:** copy to a temporary name, then rename:
  ```bash
  scp "Mario Party 9.iso" "host:/mnt/user/workspace/piwii/server/work/import/Mario Party 9.iso.partial"
  ssh host 'cd /mnt/user/workspace/piwii/server/work/import && mv "Mario Party 9.iso.partial" "Mario Party 9.iso"'
  ```

### Processing it

Nothing in the folder is processed until it's named in an import call,
or until someone presses **Import** next to it in the page (**Add a
game → Upload**, "In the import folder on the server"; the list only shows
when the folder has files).

| Call | Does |
|---|---|
| `GET /api/import` | Lists the files that can be imported. |
| `POST /api/import` `{"filename": "Mario Party 9.iso"}` | Moves that file into the normal pipeline. Returns the job, exactly like a finished upload. |

`GET /api/import` returns:

```json
{
  "dir": "/work/import",
  "files": [
    {"filename": "Mario Party 9.iso", "size": 4699979776, "modified": 1791121009, "settling": false}
  ]
}
```

`settling` is `true` while the file was modified in the last 5 seconds,
meaning the copy may still be running. Only game file types are listed;
dotfiles (rsync's temp files) and other names like `.partial` or `.txt`
never appear.

`POST /api/import` returns the job object (see `GET /api/jobs`). The
move is a rename on the same disk, so it's instant, and the file leaves
the import folder. The job then shows in the page's activity banner and
History like any upload.

| Status | When |
|---|---|
| `400` | `filename` is a path or `..`, or the file isn't a game type (or is a dotfile). |
| `404` | No such file in the import folder, e.g. it was already imported. |
| `409` | The file was modified in the last 5 seconds. Wait and retry. |
| `507` | The server host doesn't have the file's size free for `wit`'s converted copy. |

## After a job starts

Every route (upload, link, import) ends the same way. The file becomes
`work/<job>.src.<ext>` and a job, and from then on piwii doesn't lose it:

- **Pi down or the share unreachable:** the converted game waits in
  `work/` and the copy to the Pi retries with backoff (30 s, doubling to
  30 min), and immediately once the share is back. The job shows the
  reason and the next retry time. Nothing needs to be re-sent.
- **piwii restarts (deploy, crash, server host reboot):** on startup it copies
  converted games still in `work/`, and converts again any source file
  that was queued or mid-conversion. The job reappears in History as
  "resumed after restart".
- **Not covered:** a file still sitting in `import/`, or an unfinished
  chunked upload, until it's imported or completed. Neither is lost; it
  just waits for that call.

Details: the README's "NFS staging share" section.

## From another program or container on the server host

Another program running on the server host (in its own container or not) can add
games the same two ways. It talks to the piwii API over the server host's local
network instead of the internet.

### Reaching the API

Pick whichever fits the other container:

| From | Base URL | Setup |
|---|---|---|
| Any container, or the server host itself | `http://192.168.4.32:8790` | None. Port 8790 is published on the server host's LAN address. |
| A container on piwii's Docker network | `http://piwii:8790` | Join the network `server_default` (see below). |
| The server host's own shell | `http://localhost:8790` | None. |

To join piwii's network from another compose file:

```yaml
services:
  myapp:
    networks: [piwii]
networks:
  piwii:
    external: true
    name: server_default
```

### Authenticating

Every API call needs authentication, even locally. Give the program its
own [API token](requests-api.md#api-token-service-accounts), not the
username and password: add `name:token` to `PIWII_API_TOKENS` in piwii's
`server/.env`, recreate piwii, and pass the token to the program through
its own environment. Send it on every call:

```bash
BASE=http://piwii:8790
AUTH="Authorization: Bearer $PIWII_API_TOKEN"
curl -s -H "$AUTH" "$BASE/api/requests?status=requested"
```

A token doesn't expire and isn't affected by the login lockout or a
password change. It opens only the request-fulfilling routes, which
covers Option B below. Option A (chunked upload) needs the login cookie;
see [requests-api.md → Login cookie](requests-api.md#login-cookie-people).

### Option A: stream it over HTTP (no shared folder)

Use the [chunked upload API](#1-chunked-upload-api) exactly as above,
with the local base URL. The other container needs no access to piwii's
files. Calling `complete` is what tells piwii to process the file.
Uploads need the login cookie: an API token can't use them.

### Option B: write the file into a shared folder, then notify

This uses the [import folder](#3-import-folder). It suits a
program that already has the file on disk, because there's no
re-sending over HTTP.

1. Mount piwii's import folder into the other container:
   ```yaml
   services:
     myapp:
       volumes:
         - /mnt/user/workspace/piwii/server/work/import:/piwii-import
   ```
   Both containers then see the same files. Anything written to
   `/piwii-import` inside `myapp` shows up as `/work/import` inside
   piwii. Mount only this folder, not `server/work/` as a whole.
2. Write the file under a temporary name, then rename it when it's
   complete (`/piwii-import/Game.iso.partial` →
   `/piwii-import/Game.iso`). The rename only stays instant and
   all-at-once if the file is written inside the mounted folder itself,
   not elsewhere and then moved in.
3. Tell piwii to process it:
   ```bash
   curl -s -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"filename": "Game.iso"}' "$BASE/api/import"
   ```
   The response is the job. Poll `GET /api/jobs` to follow it through
   `converting` → `done` (handed to the Pi) → `installed`.

piwii runs as root, so it can read whatever the other container writes.
If the other container runs as a non-root user, make the import folder
writable for it (on Unraid, typically `chown 99:100`, which is `nobody:users`).

## Fulfilling a game request end to end

### How requests reach the other app

People request games on the piwii page (**Add a game → Request**).
piwii stores each request and does nothing else: it doesn't send a
webhook, message or email. The program that fulfils requests has to ask
for them. It polls `GET /api/requests?status=requested`, which returns
only the requests nobody has started on:

```bash
curl -s -H "$AUTH" "$BASE/api/requests?status=requested" \
  | jq -r '.[] | "\(.id)\t\(.title)\t\(.year)\t\(.region)\t\(.note)"'
```

Polling every few seconds is fine. Send back the `ETag` from the last
`200` as `If-None-Match`, and while nothing changes the answer is an
empty `304`. [requests-api.md → Polling](requests-api.md#polling) has a
complete loop.

A request is keyed by its GameTDB ID6, which tells you the exact disc
and region. `title`, `year`, `region`, `publisher` and `gametdb_url` are
included for display. Setting `"in progress"` marks a request as claimed,
so the poller doesn't pick it up twice. See
[requests-api.md](requests-api.md) for every field and status.

### Fulfilling one

A request is fulfilled like this:

```bash
# Claim the request
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "in progress"}' "$BASE/api/requests/SSQE01"

# Copy the game file onto the server host
rsync -av "Mario Party 9.iso" host:/mnt/user/workspace/piwii/server/work/import/

# Process it
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"filename": "Mario Party 9.iso"}' "$BASE/api/import"

# Optional: close the request now. It shows "on the drive" by itself once
# the Pi syncs the game.
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "done"}' "$BASE/api/requests/SSQE01"
```

Without SSH access to the server host, send the file with the
[chunked upload API](#1-chunked-upload-api) instead of rsync and the
import call. That needs the login cookie, since uploads aren't open to
API tokens.
