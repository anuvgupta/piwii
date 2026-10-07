# Game requests API

A request asks for a Wii game that isn't on the drive yet. The request
records which game is wanted, using its GameTDB entry. Someone then gets
the game onto the drive the normal way (upload, URL, or import). A request
shows as `done` once an upload of that game is handed off to the Pi, then
`on the drive` once the game's ID appears in the Pi's `library.json`. The web
page doesn't set statuses by hand, with one exception: a request marked
`error` has a **Retry** button, which sets it back to `requested` so the
fulfilling program picks it up again.

The page's **Add a game → Request** tab uses these endpoints. Other tools
can use them too; a program should use an [API token](#api-token-service-accounts).

The examples below use `-H "$AUTH"` (an API token) where a token can
call the route, and `-b "$JAR"` (the login cookie) where only a person
can.

piwii only stores requests. It doesn't notify anyone when a request is
made: no webhook, message or email. A program that fulfils requests
should poll `GET /api/requests?status=requested` and act on what comes
back. Polling every few seconds is fine: the list is answered from memory,
and a repeat poll with nothing new gets an empty `304`. See
[Polling](#polling) and [A typical fulfilment loop](#a-typical-fulfilment-loop),
and [adding-games.md](adding-games.md) for getting the game file onto
the server host.

## Base URL

| Where | URL |
|---|---|
| Internet (Cloudflare tunnel) | `https://wii.<PIWII_DOMAIN>`, e.g. `https://wii.example.com` |
| Home network (Caddy on the server host) | `https://wii.lan.<PIWII_DOMAIN>`, e.g. `https://wii.lan.example.com` |
| On the server host itself | `http://localhost:8790` |

## Authentication

Every `/api/*` route needs one of two things:

- **An API token**, for programs: a service account, such as the program
  that fulfils requests. **A program should use a token, not the
  username and password.**
- **The login cookie**, for people using the page (and `curl` by hand).

Without either, a route returns `401 {"detail": "login required"}`.

### API token (service accounts)

Send the token in an `Authorization` header on every call. There's no
login step and nothing expires:

```bash
BASE=https://wii.example.com
AUTH="Authorization: Bearer $PIWII_API_TOKEN"
curl -s -H "$AUTH" "$BASE/api/requests?status=requested"
```

Tokens are set in piwii's `server/.env` as `PIWII_API_TOKENS`, named and
comma-separated, one per program:

```
PIWII_API_TOKENS=fulfiller:<token>,otherbot:<token>
```

Make a token with `openssl rand -hex 32`. After changing the list,
recreate the container (`docker compose up -d`) so it reads the new
value. To revoke a program's access, remove its entry and recreate.

A token can call only what a program fulfilling requests needs:

| Route | Used for |
|---|---|
| `GET /api/requests` | Finding requests (with the `status` filter and ETag polling) |
| `PATCH /api/requests/{id}` | Claiming a request and setting its status and note |
| `GET /api/import` | Checking that a file arrived in `work/import/` |
| `POST /api/import` | Handing that file to piwii to convert and send to the Pi |
| `GET /api/jobs` | Following the game until it's on the drive |

Anything else answers `403 {"detail": "API tokens can't use this
route"}`, and piwii logs the token's name. That includes the page,
searching the catalog, making or deleting requests, uploads and link
downloads. A wrong or revoked token answers `401 {"detail": "invalid API
token"}`.

Why a token, and why only these routes:

- **The login is fragile for a program.** The cookie lasts 30 days, so a
  program has to notice the `401` and log in again. Five wrong passwords
  lock out its address. And changing the password, which every person
  uses, logs the program out too.
- **The login opens everything.** A program holding the username and
  password could do anything a person can. A token is limited to the
  routes above, so if one leaks it can't upload files, make piwii
  download from a link, or delete requests, and revoking it doesn't
  affect anyone else.
- **Tokens don't expire**, which is what makes the limit matter: the
  routes a token can't reach are what keep a leaked one cheap. A token
  is still a secret; keep it in the program's environment, not in code
  or logs.

Wrong tokens don't count toward the login lockout. A 32-byte random
token can't be guessed, and counting them would let anyone lock a
program out.

### Login cookie (people)

To get the cookie, post the username and password from `server/.env` to
`/login` as a form. A correct login returns `303` with `Set-Cookie`, and
the cookie is valid for 30 days. A wrong login returns `401`. After five
failures in 15 minutes from one client, `/login` returns `429` until that
window passes.

```bash
BASE=https://wii.example.com
JAR=~/.piwii-cookies   # keep this file private
curl -s -o /dev/null -w '%{http_code}\n' -c "$JAR" \
  --data-urlencode "username=$PIWII_USERNAME" \
  --data-urlencode "password=$PIWII_PASSWORD" \
  "$BASE/login"
# 303 = signed in. Pass -b "$JAR" on every API call after this.
```

Under `PIWII_DOMAIN`, the cookie is set for the whole domain the request came
in under, so one login works for both `wii.example.com` and
`wii.lan.example.com`. With several domains listed, each needs its own login.

Browsers can call the API from another origin only from
`https://wii.<domain>` and `https://wii.lan.<domain>` for each domain in
`PIWII_DOMAIN` (`PIWII_CORS_ORIGINS` overrides the list), using `credentials: 'include'`.

## The request object

```json
{
  "id": "SSQE01",
  "title": "Mario Party 9",
  "year": 2012,
  "region": "USA",
  "publisher": "Nintendo",
  "gametdb_url": "https://www.gametdb.com/Wii/SSQE01",
  "note": "USA copy please",
  "status": "requested",
  "requested_at": 1791115504,
  "updated_at": 1791115504
}
```

| Field | Meaning |
|---|---|
| `id` | GameTDB ID6. This is the key: there is at most one request per game. |
| `title`, `year`, `region`, `publisher`, `gametdb_url` | Copied from GameTDB when the request is made. `year` and `publisher` may be `null`. |
| `note` | Free text, up to 300 characters. |
| `status` | One of the statuses below. |
| `requested_at`, `updated_at` | Unix seconds. Requests made before status updates existed have no `updated_at`. |

### Status

| Status | Set by | Meaning |
|---|---|---|
| `requested` | default | Nobody has started on it. |
| `in progress` | `PATCH` | Someone is getting the game. |
| `throttled` | `PATCH` | The fulfilling tool is rate limited and will try again later. |
| `error` | `PATCH` | The fulfilling tool tried and failed; see the note. The page's **Retry** button sets it back to `requested`. |
| `done` | automatic, or `PATCH` | Fulfilled, but not seen on the drive yet. Shown automatically once an upload of the game is handed off to the Pi (in `staging/ready`) and waiting to sync. |
| `on the drive` | automatic | The game's ID is in the Pi's `library.json`. This overrides any stored status and can't be set by hand. |

Each request also stores the last status set by hand. If the game later
leaves the drive, the request shows that stored status again.

## Endpoints

### Search games: `GET /api/catalog?q=<text>&limit=<n>`

Searches GameTDB's retail Wii discs (IDs starting with `R` or `S`). Mods
and hacks, demo discs and GameCube games are left out. `q` matches title
words or an exact ID6. `limit` defaults to 30, maximum 100. Results come
in this order:

1. an exact ID match
2. titles starting with the first word typed
3. alphabetical, with USA, then Europe, Japan and Korea within one title

```bash
curl -s -b "$JAR" "$BASE/api/catalog?q=mario+kart&limit=2"
```

```json
{
  "results": [
    {"id": "RMCE01", "title": "Mario Kart Wii", "year": 2008, "region": "USA",
     "publisher": "Nintendo", "gametdb_url": "https://www.gametdb.com/Wii/RMCE01",
     "on_drive": true, "requested": false},
    {"id": "RMCP01", "title": "Mario Kart Wii", "year": 2008, "region": "Europe",
     "publisher": "Nintendo", "gametdb_url": "https://www.gametdb.com/Wii/RMCP01",
     "on_drive": false, "requested": false}
  ],
  "total": 4
}
```

`total` is the number of matches before `limit` was applied. An empty
`q` returns `{"results": []}`.

### List requests: `GET /api/requests`

Returns every request, or with `status`, only those showing one of the
given statuses. Repeat `status` to ask for several. It matches the status
the request shows, so `done` and `on the drive` work too. Open requests
(`requested`, `in progress`, `throttled`, `error`) come first, then
finished ones (`done`, `on the drive`). Within each group, the newest
come first.

```bash
curl -s -H "$AUTH" "$BASE/api/requests"
curl -s -H "$AUTH" "$BASE/api/requests?status=requested"
curl -s -H "$AUTH" "$BASE/api/requests?status=requested&status=in+progress"
```

Every response has an `ETag` header; see [Polling](#polling).

| Status | When |
|---|---|
| `304` | `If-None-Match` matches: nothing in the reply changed. |
| `400` | A `status` isn't one of the six statuses above. |

### Make a request: `POST /api/requests`

Body: `{"id": "<ID6>", "note": "<optional>"}`. The ID is case-insensitive.

```bash
curl -s -b "$JAR" -H 'Content-Type: application/json' \
  -d '{"id": "SSQE01", "note": "USA copy please"}' \
  "$BASE/api/requests"
```

Returns the request object. If that game already has a request, the
existing one is returned unchanged; the new note is ignored.

| Status | When |
|---|---|
| `400` | `id` isn't six letters or digits. |
| `404` | GameTDB doesn't know that ID. |
| `409` | The game is already on the drive. |

### Update a request: `PATCH /api/requests/{id}`

Body: any of `{"status": "requested" | "in progress" | "throttled" | "error" | "done", "note": "..."}`.
Fields you leave out are kept. `id` must be uppercase, as returned by the
API.

```bash
# Start on it
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "in progress", "note": "bought, ripping tonight"}' \
  "$BASE/api/requests/SSQE01"

# Mark it done
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "done"}' \
  "$BASE/api/requests/SSQE01"
```

Returns the stored request object. Its `status` is the stored status and
is never `on the drive`; only `GET /api/requests` and `/api/catalog`
check the drive.

| Status | When |
|---|---|
| `400` | `status` isn't one of the five allowed values. |
| `404` | There is no request for that ID. |

### Remove a request: `DELETE /api/requests/{id}`

Removes the request. This is used for both cancelling an open request and
clearing a finished one.

```bash
curl -s -b "$JAR" -X DELETE "$BASE/api/requests/SSQE01"
# {"ok": true}
```

Returns `404` if there is no request for that ID.

## Polling

`GET /api/requests` is cheap to call often, even every second. It's
answered from memory (no disk or network), and with `If-None-Match` an
unchanged list costs an empty `304`:

1. The first call returns `200`, the list, and a header like
   `ETag: "3f9a…"`. Keep the tag.
2. Send it back on the next call as `If-None-Match: "3f9a…"`.
3. If nothing in that reply changed, the answer is `304` with no body:
   keep using the list you have. Otherwise it's `200` with the new list
   and a new tag.

The tag is a hash of the reply itself, so it also changes when a game
reaches the drive or is handed off to the Pi, not only when a request is
edited. It depends on the `status` filter, so keep one tag per query.
Through Cloudflare the tag may come back weak (`W/"3f9a…"`); send it as
received, both forms match. Responses also carry `Cache-Control:
no-cache`, so browsers keep the body but check with the server before
reusing it, and get the `304`s on their own.

A polling loop with an API token, acting only when the open requests
change:

```bash
AUTH="Authorization: Bearer $PIWII_API_TOKEN"
etag=""
while true; do
  code=$(curl -s -H "$AUTH" -D headers.txt -o body.json -w '%{http_code}' \
    ${etag:+-H "If-None-Match: $etag"} "$BASE/api/requests?status=requested")
  case $code in
    200) # The open requests changed (or this is the first poll): act on them.
         etag=$(grep -i '^etag:' headers.txt | cut -d' ' -f2- | tr -d '\r')
         jq -r '.[] | "\(.id)  \(.title)"' body.json ;;
    304) ;;     # nothing new
    401) echo "piwii refused the API token" >&2; exit 1 ;;
    *)   echo "piwii answered $code" >&2 ;;
  esac
  sleep 5
done
```

Only the first request sends no `If-None-Match`; after that every poll
sends the last tag it got with a `200`. A token doesn't expire, so a
`401` means it was revoked or mistyped: stop and report it rather than
retrying. (A poller using the login cookie instead would log in again on
`401`, since the cookie lasts 30 days.)

## A typical fulfilment loop

```bash
# 1. What's wanted?
curl -s -H "$AUTH" "$BASE/api/requests?status=requested" \
  | jq -r '.[] | "\(.id)  \(.title) (\(.year), \(.region))  \(.note)"'

# 2. Claim one
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "in progress"}' "$BASE/api/requests/SSQE01"

# 3. Get the game onto the drive the usual way (the page's Upload tab).
#    Once the Pi syncs it, the request shows "on the drive" by itself.
#    Mark it done by hand if you want it closed out before then:
curl -s -H "$AUTH" -X PATCH -H 'Content-Type: application/json' \
  -d '{"status": "done"}' "$BASE/api/requests/SSQE01"
```

## Storage

Requests are kept in memory and saved to `server/work/requests.json` on
the server host, keyed by ID6. The file is replaced atomically on every
change, before the change is applied in memory, and read only at startup.
Requests survive restarts and redeploys. The `server/work/` folder is
gitignored.

`requests.json` is the web server's own storage, not an interface: change
requests only through the API. A hand edit while the server runs isn't
seen until it restarts, and the next change through the API overwrites it.
