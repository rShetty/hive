# LLD 10 — Frontend

The frontend is a **multi-page app (MPA)** of static HTML files served by the FastAPI backend — no separate frontend server, no build step. Each page uses **Tailwind CSS** + **Alpine.js** for reactivity.

## How it's served (`backend/main.py:216-234`)

- `frontend_path` resolves to `../frontend` (dev) or `/app/frontend` (Docker, matches `Dockerfile` `COPY frontend/`).
- Static mounts: `/static` (whole dir), `/js` (`frontend/js`), `/css` (`frontend/css`).
- HTML routes via `_serve_frontend(filename)` (`main.py:236-243`) → `FileResponse` or 404. Individual `@app.get` routes map URL → HTML file (`main.py:246-344`).

## Pages (`frontend/`)

| File | Route | Purpose |
|------|-------|---------|
| `index.html` | `/` | Landing/dashboard. Hero + stats. Fetches `/api/agents/stats/overview`, `/api/skills`, `/api/agents?limit=6`. |
| `agents.html` | `/agents` | Marketplace browse grid. Filters + pagination. |
| `agent-detail.html` | `/agents/{id}`, `/agent-detail` | Single-agent view: details, restart, delete, logs. |
| `agent-config.html` | `/agent-config` | Largest config page (49 KB). LLM providers, skills attach, Telegram. Heavy `apiFetch`. |
| `deploy.html` | `/deploy` | Deploy form. Loads `/api/skills`, `/api/mcp-servers`; POSTs `/deploy-hosted` + `/deploy-openclaw`. |
| `teams.html` | `/teams` | Teams list grid. |
| `team-detail.html` | `/teams/{id}` | Team detail + org chart + run modal + run history + **live SSE stream**. |
| `workflows.html` | `/workflows` | Workflows list + run trigger. |
| `workflow-builder.html` | `/workflows/new`, `/workflows/{id}` | Largest page (56 KB). Visual "paperclip" pipeline builder: agent palette → step cards → save/run + **SSE stream**. |
| `tasks.html` | `/tasks` | Delegation task hub (renamed from `/delegate`). Wallet, marketplace, delegations, logs, estimate, delegate + **SSE stream**. |
| `skills.html` | `/skills` | Skills CRUD registry. |
| `mcp.html` | `/mcp` | MCP server registry + per-agent grants + OAuth connect. |
| `settings.html` | `/settings` | User profile + provider API keys. |
| `login.html` | `/login` | Login form. Stores `access_token` to `localStorage`. |
| `signup.html` | `/signup` | Registration + auto-login. |

Legacy redirect: `/delegate` → `/tasks` (302, preserving query) at `main.py:322-329`.

CSS: `frontend/css/theme.css` (352 lines) — single shared stylesheet.

## JavaScript (`frontend/js/`)

Three framework-less JS files mounted at `/js`.

### `app.js` (96 lines) — core API + auth utilities
- **Auth tokens**: `getToken()` reads JWT from `localStorage.token`; `authHeaders()` returns `{Authorization: 'Bearer <token>'}`; `isAuthenticated()`; `logout()` clears token, fire-and-forget `POST /api/auth/logout`, redirects to `/login`.
- **Token refresh**: `refreshAccessToken()` POSTs `/api/auth/refresh` with `credentials:'include'` (sends httpOnly refresh cookie), stores new `access_token`. Uses a single in-flight promise `_refreshing` to deduplicate concurrent refresh requests.
- **`apiFetch(path, options)`** (`:56-72`): wraps `fetch` with `authHeaders()` + `credentials:'include'`. On **401**, attempts one refresh+retry; if refresh fails, calls `logout()`. **This is the canonical API helper** — most pages use it.
- **Formatting**: `timeAgo()`, `statusBadgeClass()`.

### `nav.js` (60 lines) — top nav (older component)
Renders into `#nav-root`/`#nav` via `renderNav({active, dark})`. Different links authed vs unauthed. (Newer pages use `sidebar.js` instead.)

### `sidebar.js` (209 lines) — left sidebar tree
Renders into `#sidebar-root` via `renderSidebar({active})`.
- **`SIDEBAR_TREE`** (`:30-70`): five groups — Overview (Dashboard), Agents (Browse, Deploy), Registry (Skills, MCP), Workflow (Tasks, Workflows, Teams), Account (Settings).
- Inline SVG icons via `sbIcon()`.
- **State persistence** in `localStorage`: collapsed state `sb-collapsed`; per-group open/close `sb-group-<group>`.
- Mobile hamburger injected into `document.body` (NOT inside the sidebar element) via `ensureMobileToggle()` (`:153-165`) — because the sidebar uses `transform: translateX()` on mobile, which becomes the containing block for `position: fixed` descendants, so an inside toggle would be dragged off-screen.
- Outside-click dismiss handler (`:197-209`).

## How API calls are made

- Most pages use `apiFetch(path, options)` from `app.js` — auto-injects `Authorization: Bearer` + handles 401 refresh.
- Some pages use raw `fetch` with manual `Authorization` header construction (notably `team-detail.html`, `teams.html`, `agents.html`, `agent-detail.html`, `login.html`, `signup.html`, `index.html`) — an inconsistency worth documenting.
- All calls use relative paths (same origin) and `credentials: 'include'` to send the refresh cookie.

## Auth flow

1. User submits login → `POST /api/auth/login` → backend sets `hive_refresh` httpOnly cookie (path `/api/auth`) + `hive_token` non-httpOnly cookie (path `/`) and returns `{access_token, ...}` in the JSON body.
2. Frontend stores `access_token` in `localStorage.token`.
3. Subsequent `apiFetch` calls send `Authorization: Bearer <localStorage.token>` + the refresh cookie.
4. On 401, `apiFetch` calls `refreshAccessToken()` → `POST /api/auth/refresh` (cookie auth) → stores new token → retries the original call.
5. Refresh failure → `logout()`.

## SSE consumption

Three pages consume SSE via `new EventSource(url + '?token=...')` (EventSource can't set headers, so JWT goes in the query):

- `team-detail.html:497` → `GET /api/teams/${team.id}/runs/${runId}/stream?token=...`
- `workflow-builder.html:800-801` → `GET /api/workflows/${workflowId}/runs/${run.id}/stream?token=...`
- `tasks.html:489` → `GET /api/delegate/{id}/user-stream?token=...`

Each listens for `message` events, parses JSON, updates the DOM (Alpine reactivity or direct DOM manipulation). Reconnect is automatic; the backend replays missed events from `DelegationLog` on reconnect.

## Cache busting

Static assets include cache-busting query strings (commit `3b63b80`) — e.g. `/js/sidebar.js?v=...` — so updated JS/CSS is fetched on deploy rather than served from browser cache.
