# ra-jit site

https://ra-jit.ix.dev. Every page is a [Flecs](https://github.com/SanderMertens/flecs) script. A Rust renderer
(`renderer/`, compiled to wasm) turns them into HTML, at build time for the static pages and per request in a
Cloudflare Worker for the pages with comments; TS is the host around it: the build, the worker's routing, auth, D1 and
headers, and the browser scripts.

## Architecture: Rust core, TS host

```
content/*.flecs ─┬─ gen/tree.ts (flecs-doc.wasm) ── bake: highlight.js               ──┐
                 └───────────────────────────────── site-snapshot.wasm ◀────────────────┘
                                                            │ snapshot (.gen/site.cbor)
                                                            ▼
                 gen/build.ts ── site.wasm ──▶ dist/*.html (every page, no comments)
                 worker/      ── site.wasm ──▶ discussable pages (comments from D1, the viewer), per request
```

- `renderer/`: the renderer, in Rust (`renderer/README.md`). `core` turns a snapshot of the site, a page's comments
  and a viewer into one page's HTML; it is sans-IO. Two wasm modules with a small C ABI and no imports:
  `site.wasm` (the renderer) and `site-snapshot.wasm` (the content scripts run in Flecs, compiled in, to the snapshot;
  build time only). Both are platform-independent build outputs, never checked in.
- `gen/build.ts`: the build, under Bun, with no native code. It runs the scripts under `flecs/flecs-doc.wasm` to bake
  highlighted code, gets the snapshot from `site-snapshot.wasm`,
  renders every page with `site.wasm` into `dist/`, bundles `client/*.ts`, concatenates `styles/` and copies `static/`.
  For the worker it writes `.gen/`: `site.wasm`, `site.cbor` (the snapshot), `pages.json`, `discussable.json` and
  `build.json` (a hash of the module, the snapshot and the client scripts).
- `worker/`: the Worker (below). `worker/site-wasm.ts` is the host side of both modules (about 100 lines, shared with
  the build); `worker/renderer.ts` loads `site.wasm` and the snapshot once per isolate.
- `content/*.flecs`: the pages. `schema.flecs` defines the element types (`P`, `Li`, `Code`, `Section`, `Issue`, ...),
  the relationships (`Status`, `Cites`, `DependsOn`) and the page prefabs (`Page`, `Doc`, `Rfc`); `refs.flecs` the
  references pages cite. An entity's path is its URL: `site.rfcs.capabilities` is `/rfcs/capabilities`.
- `flecs/`: Flecs compiled to wasm (`flecs-doc.wasm`, checked in; `python3 flecs/build.py <flecs checkout>` rebuilds it
  with zig). The build's bake step and the editor's `.flecs` type checking (`content/tsconfig.json`) read the scripts
  with it.
- `client/`: browser scripts (comment enhancements, the signup form, copy buttons).

The flecs skill (`.claude/skills/flecs/`) has the script syntax and its traps.

### The wasm modules: build outputs, fetched

CI builds both modules on every run of the `site` workflow and uploads them as the `site-renderer` artifact. The build
looks for them in `renderer/target/site/` (or `SITE_WASM_DIR`) and, when they are missing, prints how to get them. A
machine without the Rust toolchain fetches them; changing `renderer/` itself means building them where Rust builds
run (a dev host, CI) and fetching from there:

```
uv run --script renderer/fetch.py                                 # the latest green CI run: this branch, else main
uv run --script renderer/fetch.py --scp HOST:DIR [--port 9999]    # DIR = renderer/target/site of a build.py run
uv run --script renderer/build.py                                 # with the Rust toolchain, clang and wasm-opt
```

## Commands

```
bun install
uv run --script renderer/fetch.py   # once, and again when renderer/ changes
bun run build    # dist/ and .gen/ in about 150 ms
bun run check    # tsc over worker/, client/, gen/ and content/
bun run dev      # wrangler dev: rebuilds on change; apply the D1 migrations with --local first
bun run deploy   # build + wrangler deploy
```

## RFCs and comments

An RFC is a file in `content/rfcs/`: an instance of the `Rfc` prefab under `site.rfcs`. Every block in a `Doc` (the
charter, RFCs) has a name; its path under the page is the anchor comments attach to, so renaming a block orphans its
comments (they still show, with their quote, in the page's discussion).

## Narration (`audio/`)

Each page can be listened to. What is said is not the page read out: the core derives a spoken version (code and
reference lists skipped, tables read row by row, figures by their alt text), and a block can say something else with
`Say: {"..."}`, or nothing with `Silent`. `audio/generate.py` voices it on a Mac (Fish Audio Drama 3 on MLX); the build
adds the player only to pages whose audio matches their current text. [audio/README.md](audio/README.md) has the
details, and the steps for serving the files from R2.

## Worker (`worker/`)

`worker/index.ts` is the Worker entry (plain `export default { fetch }`). Workers static assets serve `dist/` (built by
`bun gen/build.ts`); the paths in `wrangler.jsonc` `assets.run_worker_first` (`/api/*`, `/auth/*`, `/updates`,
`/charter`, `/rfcs/*`) reach the worker first, and anything else it is handed goes to `env.ASSETS`. A missing page gets
`dist/404.html`. The build fails when a discussable page has no pattern there.

- `auth.ts`: GitHub sign-in and the session cookie.
- `pages.ts`: discussable pages, rendered with their comments (below).
- `renderer.ts`, `site-wasm.ts`, `cbor.ts`: the Rust renderer's module, its C ABI and the CBOR its inputs go in.
  `modules.d.ts` types the module imports: wrangler bundles `.gen/site.wasm` as a compiled module and `.gen/site.cbor`
  as bytes (the `Data` rule in `wrangler.jsonc`).
- `comments.ts`: `/api/comments`.
- `updates.ts`: the `/updates` form.
- `http.ts`: cookies, origin check, `next` validation, tokens. `env.ts`: the `Env` type.

The worker imports `.gen/` (written by the build), so build before `wrangler dev` or `deploy` (both run the build
themselves):

```
bun gen/build.ts
bunx wrangler d1 migrations apply ra-jit-updates --local
bunx wrangler dev --var GITHUB_CLIENT_ID:<dev app id>     # secrets for dev go in .dev.vars (gitignored)
bunx tsc --noEmit -p tsconfig.json                         # type-checks worker/ only
```

Secrets (`wrangler secret put`, or `.dev.vars` locally; `.dev.vars*` is gitignored): `GITHUB_CLIENT_SECRET`,
`TURNSTILE_SECRET_KEY`.

The cookies are `__Host-` cookies, so they are `Secure`. `wrangler dev` serves plain http://localhost by default:
Firefox keeps `__Host-` cookies there, but Safari drops every `Secure` cookie on http://localhost and Chrome has
dropped `__Host-` ones (crbug 40202941), so sign-in loses its cookie. For local sign-in run
`bunx wrangler dev --local-protocol https` (and register `https://localhost:8787/auth/github/callback` on the dev
OAuth App). Anything without sign-in works over http.

## Comments (`/api/comments`, `/auth/*`)

Discussable pages are rendered by the worker (`worker/pages.ts`): it loads the doc's comments in one D1 query and
hands the rows, CBOR-encoded, to `site.wasm` with the viewer (signed-in user and admin, or null). The renderer hangs
each comment under its block (or under the page, for `_page` and for blocks gone from the page) for that one render,
and escapes comment text as it escapes everything else. If the module traps or returns an error, the worker logs the
detail (`render failed <url> ...`) and answers a plain 500 `Server error.`: it never sends part of a page, and the
cache is not written. After a trap the next request makes a new instance. The page works without JS: each block's count links to its
thread (shown by CSS `:target`), posting and deleting are plain form posts, and signed out each thread links to
sign-in. `client/comments.ts` takes the markup over: threads toggle in place, posts and deletes go through the JSON API
and then swap the thread for the worker's fresh rendering of it, times show relative.

Caching: an anonymous request is served from the Cache API under
`<origin><path>?v=<version>&b=<build hash>&w=<worker version>&s=<sign-in on>`, where the version is `doc_versions`
(`migrations/0003_doc_versions.sql`), bumped by triggers in the same statement that writes, deletes or erases a comment
(and when a commenter's profile changes), so an entry is never stale and nothing is purged. The worker version is the
`CF_VERSION` binding's (`version_metadata`), new on every deploy, so a change of code or vars moves every key;
`wrangler dev` gives a random id per session. The version is memoised for 2 s per isolate. A request with a valid session is rendered for that reader and
sent `private, no-store`; it never reads or writes the cache, and the only code that writes it renders with no viewer.
Anonymous pages go to the browser as `no-cache, Vary: cookie`. Worker-served HTML carries a CSP (`script-src 'self'`;
`/updates` also allows Turnstile); the `x-render` header says how it was served (`hit`, `miss`, `anonymous`, `private`).

Pages tagged `Discussable` take comments, per block or on the page as a whole (block `_page`). The build lists them
(from the renderer, `site_discussable`) in `.gen/discussable.json` (doc -> title and each block's id and first 280 chars of text); the worker accepts only
docs and blocks listed there, and stores the first 140 chars of the block's text as the comment's `quote` (none for
`_page`). Tables `users`, `sessions`, `comments` (`migrations/0002_comments.sql`).

Sign-in is a GitHub OAuth App with no scopes: `GET /auth/github?next=/path` (the state is in the `__Host-gh_oauth`
cookie, 10 minutes), callback `/auth/github/callback`, `POST /auth/logout` (form field `next`). The access token is
used once to read `/user`, then revoked (`DELETE /applications/{client_id}/token`, in the background). The
`__Host-session` cookie (30 days) holds a random token; D1 keeps only its SHA-256. Both cookies are HttpOnly, Secure,
SameSite=Lax, Path=/, no Domain (the `__Host-` prefix makes browsers enforce that, so a subdomain cannot plant one).
`next` is parsed as a URL against a placeholder origin: a different origin (`//host`, `/\host`, ...), a control
character, or more than 1024 chars gives `/`; otherwise the parsed path, query and fragment (percent-encoded ASCII).

- `GET /api/comments?doc=<doc>`: `{comments, me, signIn, admin}`; 404 for a doc that takes no comments.
- `POST /api/comments` (JSON `{doc, block, body}`): 201 `{comment}`. Needs a session (401), same Origin (403), JSON
  (415), at most 64 KiB (413, checked from Content-Length before reading and again while reading). Body 1 to 4000
  chars after trimming. Limit: 10 per 10 minutes and 100 per day per user (429), counted from `comments`, deleted
  ones included, inside the INSERT itself (so parallel posts cannot slip past it).
- `POST /api/comments` as a form (`doc`, `block`, `body`; no JS): the same checks; 303 to the page `#c-<id>`, or to
  `?c-error=<code>&c-block=<block>#block-<block>`, which shows a fixed message in that block's form. A cross-site
  post, an unknown doc or block, or a body over 64 KiB get a plain-text error.
- `POST /api/comments/<id>/delete` (form, no JS): the same as DELETE; 303 to the page `#block-<block>`. Without a
  session, a plain 401 before the comment is looked up (so it does not tell which page the comment is on).
- `DELETE /api/comments/<id>`: the author or a user id in `COMMENT_ADMINS`. 204. Deleting erases the comment's text:
  the row stays (so ids and threads stay stable) with `deleted_at` set, `body` emptied and `quote` cleared. D1 Time Travel keeps earlier states of the
  database for up to 30 days, so the old text is gone from the site at once and from backups after that. Pages
  rendered before the delete can stay in edge caches under their old keys for up to 24 h; they are served again only
  if `doc_versions` goes backwards (a D1 Time Travel restore, say), so deploy after a restore, which moves every key.

Setup: create a GitHub OAuth App with the callback `https://ra-jit.ix.dev/auth/github/callback`, put its client id in
`GITHUB_CLIENT_ID` (`wrangler.jsonc` `vars`; empty turns sign-in off) and its secret with
`wrangler secret put GITHUB_CLIENT_SECRET`. `COMMENT_ADMINS` is a comma-separated list of numeric GitHub user ids
(`curl https://api.github.com/users/<login>` shows `id`), compared with the signed-in user's id; logins are not used,
since a renamed login can be registered by someone else. Apply `0002_comments.sql` and `0003_doc_versions.sql` with `wrangler d1 migrations apply ra-jit-updates --remote`.

Reading comments:

```
npx wrangler d1 execute ra-jit-updates --remote --command \
  "select c.id, c.doc, c.block, u.login, c.body, c.created_at from comments c join users u on u.id = c.user_id where c.deleted_at is null order by c.id desc"
```

## Signups (`/updates`)

`worker/updates.ts`. GET serves `dist/updates.html` with the Turnstile widget (when `TURNSTILE_SITE_KEY` is set) and
the visitor's Referer filled in through HTMLRewriter. POST with `Accept: application/json` (`client/updates.ts`)
answers `{ok: true}` or `{error}`; a plain form post (no JS) gets the page back with the error shown, or the thank-you
note. Cross-site posts are refused (403).

One row per email in `signups` (email lowercased; a repeat signup updates `company` and `why`, keeps the first-touch
metadata). Metadata: `request.cf` country, region, city, asn, asOrganization, timezone; user-agent; referer (the page
before `/updates`); accept-language; `src` and `utm_*` from the `/updates` URL (the README button uses `?src=readme`).
No IPs are stored. Rate limit: 3 signups per email and 60 overall per minute (429), logged in `signup_attempts` by
the same statement that checks the limit. Only posts with a valid email that passed Turnstile are logged, so failed
checks cannot fill the global cap. A form post over 64 KiB is refused (413).

Turnstile: set `TURNSTILE_SITE_KEY` in `wrangler.jsonc` `vars` and the secret with `wrangler secret put`. With no
secret, verification is skipped; with no site key, the widget is not shown (dev).

### One-time setup

```
npx wrangler d1 create ra-jit-updates      # paste the printed database_id into wrangler.jsonc (TODO there)
npx wrangler d1 migrations apply ra-jit-updates --remote
npx wrangler secret put TURNSTILE_SECRET_KEY
bun gen/build.ts && npx wrangler deploy
```

Local dev: `npx wrangler d1 migrations apply ra-jit-updates --local`.

### Reading signups

```
npx wrangler d1 execute ra-jit-updates --remote --command \
  "select email, company, why, src, country, city, as_organization, created_at from signups order by created_at desc"
npx wrangler d1 execute ra-jit-updates --remote --command "select count(*) from signups"
```

Add `--json` for machine-readable output.
