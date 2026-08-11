# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Session authentication.** Login form and signed session cookie replace HTTP
  Basic. The admin password is stored as an argon2id hash in MongoDB,
  bootstrapped from `ADMIN_USERNAME`/`ADMIN_PASSWORD` on first run, with an
  environment fallback so a database outage cannot lock the operator out.
  Login attempts are throttled and failures do not reveal whether a user exists.
- **Deny-by-default authorization.** `AuthMiddleware` protects every path unless
  explicitly allowlisted, so a newly added route is secure without remembering
  to add a dependency.
- **MongoDB support** (`MONGODB_URI`), optional. Unset, the dashboard behaves
  exactly as before. Unreachable, it still boots and reports the failure.
- **Multiple projects.** Each is one LiveKit deployment with its own URL and
  API key pair; two may share a URL. A navbar switcher scopes every page. API
  secrets are encrypted at rest with `APP_ENCRYPTION_KEY`.
- **Usage tracking** via a new `POST /webhooks/livekit` endpoint, verified with
  `livekit.api.WebhookReceiver`. Deliveries are deduplicated by event id so
  retries cannot double-count. Records participant-minutes, room-hours, peak
  concurrency, egress/ingress minutes by type, and egress output bytes.
- **Bandwidth measurement** by scraping LiveKit's Prometheus endpoint, since
  webhooks carry no bandwidth. Counter resets are skipped rather than counted
  as a spike. Unmeasured bandwidth is reported as such, never estimated.
- **Billing page** pricing a month's usage against a LiveKit Cloud rate card and
  comparing it with your actual server cost. The card ships flagged unverified
  until confirmed. CSV export and per-project breakdown included.
- `/health/deep` reporting database and index state.
- `scripts/migrate_json_stores.py` to move existing `/tmp` JSON data into MongoDB.

### Changed

- The five operator stores (alerts, saved views, audit log, room annotations,
  notification config) now persist in MongoDB and are scoped per project. They
  previously wrote JSON files under `/tmp` that were lost on every restart and
  shared across projects. The JSON backend remains as the no-database fallback.
- `connection_minutes` on the overview page is now real, sourced from webhook
  data. With no data it renders an em-dash instead of `0`, which read as
  "nobody connected".
- `LiveKitClient` instances are pooled per project instead of constructed per
  request.
- Logout is a POST.

### Fixed

- **`/events/stream` was unauthenticated**, streaming live room and participant
  identities to anyone who asked.
- **CSRF tokens were not bound to a session.** Any validly-signed token
  validated on any request, so an attacker could fetch one from a public page
  and embed it in a form on their own site.
- **A LiveKit client and aiohttp session leaked on every request** — `close()`
  was never called.
- **The agent-dispatch cache was keyed by server URL**, so two projects sharing
  a URL with different API keys would have read each other's results.
- The SSE event stream polled LiveKit forever after the browser disconnected.
- `urllib` calls in async handlers blocked the event loop for up to 5 s.
- Middleware ordering placed the CSRF and readonly middleware outside
  `SessionMiddleware`, so neither could read the session.
- The settings page read credentials from the environment, which showed the
  wrong values under multi-project.
- `pytest-cov` was invoked by `make test-cov` but never declared as a dependency.
- **Unauthenticated memory exhaustion in the login throttle.** Its map was keyed
  by the submitted username and never evicted, so anyone could allocate an
  entry per request with an arbitrarily long username. Now bounded in key
  count and length, with expiry-based eviction.
- **Unauthenticated NoSQL operator injection** via the webhook JWT's `iss`
  claim, which was read before verification and passed straight into a Mongo
  query. A hand-crafted `{"$regex": ...}` could burn database CPU. Non-string
  issuers are now rejected. (Verification itself was never bypassable.)
- **The webhook body was fully buffered before its size was checked**, so the
  unauthenticated endpoint would accept gigabytes into memory before rejecting
  them. Now checks `Content-Length` and streams with a hard cap.
- `POST /projects/{id}/test` performed a server-side outbound connection with
  no CSRF check.
- `/health/deep` disclosed MongoDB connection errors — internal hostnames,
  ports, replica-set names — to anonymous callers.
- A username-enumeration timing oracle at login: argon2 ran only for existing
  accounts, so response time distinguished them despite the generic message.

### Removed

- `LiveKitClient.get_webhook_analytics()`, a zero-caller stub returning zeros
  with a TODO. Implemented for real in `app/services/usage.py`.

### Planned

- WebSocket live updates for real-time room/participant changes
- Recording download management
- Multi-user support with role-based access control
- Dark mode theme
- Mobile-responsive improvements

## [0.1.0] - 2025-01-XX

### Added

- Initial release of LiveKit Dashboard
- **Overview Dashboard**
  - Server status monitoring
  - Active rooms and participants count
  - SDK latency tracking
  - Recent rooms list
- **Room Management**
  - List all active rooms
  - Create new rooms with custom settings
  - View room details and metadata
  - Close/delete rooms
  - Search and filter rooms
- **Participant Management**
  - View participants in rooms
  - See participant tracks (audio/video)
  - Kick participants from rooms
  - View participant metadata
- **Token Generation**
  - Generate join tokens on-the-fly
  - Customize token permissions (publish, subscribe, data)
  - Set token TTL and metadata
  - Copy tokens to clipboard
  - Quick test links for LiveKit Meet
- **Egress/Recording**
  - List active egress jobs
  - Start room composite egress
  - Stop egress jobs
  - View file outputs and download URLs
  - Support for different layouts (grid, speaker)
  - Audio-only and video-only options
- **SIP Integration** (Optional)
  - View SIP trunks
  - Create outbound SIP calls
  - View inbound dispatch rules
  - Room-to-phone bridge management
- **Settings**
  - View LiveKit server configuration
  - Display masked API credentials
  - Feature flags status
  - Security settings overview
- **Sandbox/Testing**
  - Token generator with full customization
  - HMAC webhook verification helper
  - Quick test URL generation
- **Security**
  - HTTP Basic Authentication
  - CSRF protection on all POST forms
  - Security headers (HSTS, CSP, X-Frame-Options, etc.)
  - Constant-time credential comparison
  - Never log or display full API secrets
- **Architecture**
  - Stateless SSR with FastAPI + Jinja2
  - No database or Redis required
  - HTMX for progressive enhancement and auto-refresh
  - Docker support with multi-stage build
  - Health check endpoint
  - Comprehensive error handling
- **Developer Experience**
  - Makefile with common commands
  - Docker Compose for local development
  - Poetry for dependency management
  - pytest test suite
  - Black code formatting
  - Ruff linting
  - Type hints with mypy
  - Comprehensive documentation
  - Setup script for quick start

### Security

- All routes require authentication except `/health` and `/logout`
- CSRF tokens on all state-changing operations
- Secure password comparison using `secrets.compare_digest`
- Security headers on all responses
- API secrets never exposed in full in UI

### Documentation

- Comprehensive README with setup instructions
- Docker deployment guide
- Contributing guidelines
- API endpoint documentation
- Security best practices
- Environment variable reference

## Release Notes

### v0.1.0 - Initial Release

This is the first release of LiveKit Dashboard, a stateless, self-hosted management interface for LiveKit servers.

**Key Features:**

- Complete room and participant management
- Token generation and testing tools
- Egress recording capabilities
- Optional SIP integration
- Secure by default with HTTP Basic Auth
- Zero-dependency architecture (no DB/Redis)
- Docker-ready deployment

**Installation:**

```bash
git clone <repository-url>
cd livekit-dashboard
make setup
make dev
```

**Requirements:**

- Python 3.10+
- LiveKit server instance
- LiveKit API credentials

For detailed setup instructions, see [README.md](README.md).

---

## Version History

- **0.1.0** (2025-01-XX) - Initial release
