# LiveKit Dashboard Architecture

## Overview

LiveKit Dashboard is a **stateless**, server-side rendered (SSR) web application built with FastAPI and Jinja2 templates. It provides a management interface for LiveKit servers using only the LiveKit Python SDK.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Browser                            │
│              (Session cookie authentication)                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTPS (Production)
                            │ HTTP (Development)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Reverse Proxy (Optional)                     │
│              nginx / Caddy / Traefik with TLS                   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                   FastAPI Application (SSR)                     │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │               Middleware Stack                            │ │
│  │  • SessionMiddleware (CSRF token storage)                 │ │
│  │  • CORSMiddleware (restrictive by default)                │ │
│  │  • Security Headers (HSTS, CSP, X-Frame-Options)          │ │
│  └───────────────────────┬───────────────────────────────────┘ │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                  Route Handlers                           │ │
│  │  • overview.py    - Dashboard/health                      │ │
│  │  • rooms.py       - Room management                       │ │
│  │  • egress.py      - Recording management                  │ │
│  │  • sip.py         - SIP telephony (optional)              │ │
│  │  • settings.py    - Configuration view                    │ │
│  │  • sandbox.py     - Token generator                       │ │
│  │  • auth.py        - Authentication                        │ │
│  └───────────────────────┬───────────────────────────────────┘ │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │              Security Layer                               │ │
│  │  • basic_auth.py  - Session auth helpers for routes       │ │
│  │  • csrf.py        - CSRF token generation/validation      │ │
│  └───────────────────────┬───────────────────────────────────┘ │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │           Services / Business Logic                       │ │
│  │  • livekit.py     - LiveKit SDK wrapper                   │ │
│  │    - RoomServiceClient                                    │ │
│  │    - EgressServiceClient                                  │ │
│  │    - SIPServiceClient (optional)                          │ │
│  │    - Token generation                                     │ │
│  └───────────────────────┬───────────────────────────────────┘ │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │            Template Rendering (Jinja2)                    │ │
│  │  • base.html.j2       - Base layout                       │ │
│  │  • index.html.j2      - Overview                          │ │
│  │  • rooms/*.html.j2    - Room pages                        │ │
│  │  • egress/*.html.j2   - Egress pages                      │ │
│  │  • sip/*.html.j2      - SIP pages                         │ │
│  │  • settings.html.j2   - Settings                          │ │
│  │  • sandbox.html.j2    - Token generator                   │ │
│  └───────────────────────┬───────────────────────────────────┘ │
│                          ▼                                       │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │              Static Assets                                │ │
│  │  • CSS (Bootstrap + custom)                               │ │
│  │  • JavaScript (HTMX + utilities)                          │ │
│  └───────────────────────────────────────────────────────────┘ │
└───────────────────────────┬─────────────────────────────────────┘
                            │ LiveKit SDK API Calls
                            │ (WebSocket & HTTP)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    LiveKit Server                               │
│  • Room Management                                              │
│  • Participant Management                                       │
│  • Egress (Recording)                                           │
│  • SIP (Telephony)                                              │
└─────────────────────────────────────────────────────────────────┘
```

## Request Flow

### Typical Request Flow

1. **User Request**
   - User accesses a page (e.g., `/rooms`)
   - Browser sends HTTP request with the session cookie

2. **Authentication**
   - Request reaches FastAPI middleware
   - `requires_admin` dependency verifies credentials
   - If invalid, returns 401 Unauthorized
   - If valid, proceeds to route handler

3. **CSRF Protection (POST requests)**
   - For POST/PUT/DELETE requests
   - Validates CSRF token from form data
   - If invalid, returns 403 Forbidden
   - If valid, proceeds

4. **Route Handler**
   - Extracts route parameters and query strings
   - Calls LiveKitClient service methods
   - Fetches data directly from LiveKit server

5. **LiveKit SDK Call**
   - LiveKitClient makes API call to LiveKit server
   - Returns data (rooms, participants, etc.)
   - No caching or persistence

6. **Template Rendering**
   - Route handler passes data to Jinja2 template
   - Template renders HTML with data
   - CSRF token generated and embedded

7. **Response**
   - HTML response sent to browser
   - Security headers added by middleware
   - Browser displays page

### HTMX Auto-Refresh Flow

For pages with auto-refresh (using HTMX):

1. **Initial Page Load**
   - Full HTML page rendered and sent

2. **HTMX Polling**
   - Every N seconds, HTMX sends GET request with `?partial=1`
   - Request includes `hx-trigger="every 5s"`

3. **Partial Update**
   - Route handler detects `partial=1` parameter
   - Returns only the updated section HTML
   - HTMX swaps the content in place

4. **Benefits**
   - Real-time updates without WebSockets
   - Reduced bandwidth (only partial HTML)
   - Progressive enhancement (works without JS)

## Component Responsibilities

### FastAPI Application (`app/main.py`)

- Application initialization and configuration
- Middleware setup (sessions, CORS, security headers)
- Router registration
- Global error handlers
- Lifespan events (startup/shutdown)

### Routes (`app/routes/`)

- **overview.py**: Dashboard with server stats and recent activity
- **rooms.py**: CRUD operations for rooms, participant management
- **egress.py**: Start/stop recordings, list egress jobs
- **sip.py**: SIP trunk and call management (optional)
- **settings.py**: Display configuration and server info
- **sandbox.py**: Token generation and testing tools
- **auth.py**: Logout page

**Responsibilities:**
- Request parameter parsing
- Authentication/authorization enforcement
- Business logic orchestration
- Response formatting
- Error handling

### Services (`app/services/`)

- **livekit.py**: Wrapper around LiveKit SDK
  - Room operations (list, create, delete)
  - Participant operations (list, kick, mute)
  - Token generation
  - Egress management
  - SIP operations (if enabled)
  - Health checks

**Responsibilities:**
- Abstract LiveKit SDK complexity
- Provide clean interface for routes
- Handle SDK errors
- Measure SDK latency

### Security (`app/security/`)

- **basic_auth.py**: Reads the session user that AuthMiddleware recorded
  - Credential verification with constant-time comparison
  - FastAPI dependency for route protection
  
- **csrf.py**: CSRF protection
  - Token generation using URLSafeTimedSerializer
  - Token validation
  - Integration with forms

**Responsibilities:**
- Enforce authentication on protected routes
- Prevent CSRF attacks
- Secure credential handling

### Templates (`app/templates/`)

- **base.html.j2**: Base layout with navigation, header, footer
- **Page templates**: Individual pages extending base
- **Partial templates**: Fragments for HTMX updates

**Responsibilities:**
- HTML structure and layout
- Data presentation
- HTMX integration
- Form generation with CSRF tokens

### Static Assets (`app/static/`)

- **CSS**: Custom styles extending Bootstrap
- **JavaScript**: Utility functions and HTMX helpers

**Responsibilities:**
- Visual styling
- Client-side interactivity
- Progressive enhancement

## Data Flow

### Read Operations (GET)

```
User Request → Auth Check → Route Handler → LiveKitClient
                                                  ↓
                                          LiveKit Server
                                                  ↓
User Response ← Template Render ← Route Handler ← SDK Response
```

### Write Operations (POST)

```
User Form Submit → Auth Check → CSRF Validation → Route Handler
                                                        ↓
                                                  LiveKitClient
                                                        ↓
                                                 LiveKit Server
                                                        ↓
User Redirect/Response ← Route Handler ← SDK Response
```

## Persistence

Live state is still read from LiveKit on every request and never cached into
staleness. MongoDB is optional and holds only what the LiveKit API cannot
answer.

### What lives in MongoDB

| Collection | Contents |
|---|---|
| `users` | The admin account and its argon2 password hash |
| `projects` | Per-project LiveKit URL, API key, and Fernet-encrypted secret |
| `usage_events` | Raw webhook deliveries, deduplicated by `(project_id, event_id)` |
| `usage_sessions` | Open/closed intervals pairing a start event with its end |
| `usage_rollups` | Per-project, per-UTC-day totals — the billing record |
| `bandwidth_samples` | Prometheus counter readings, for computing deltas |
| `rates` | Versioned rate cards |
| `alert_rules`, `saved_views`, `room_annotations`, `audit_log`, `notification_config` | Operator state, previously JSON files under `/tmp` |

### Degradation

`MONGODB_URI` unset ⇒ the app runs exactly as it did before: the `LIVEKIT_*`
environment variables form a single implicit project, authentication falls back
to the environment credentials, and the operator stores write JSON files again.

Set but unreachable ⇒ the app still boots. `get_db()` returns 503 and the
affected pages show a banner. Set `MONGODB_REQUIRED=true` to fail fast instead.

### Background workers

Two, both started only when MongoDB is available:

- **Bandwidth collector** (60 s) — samples each project's Prometheus endpoint.
- **Session sweeper** (15 min) — closes usage sessions whose end event never
  arrived, capping the duration and flagging it estimated. Without it a crashed
  LiveKit node would leave sessions open forever and their minutes uncounted.

Both are idempotent.

### Trade-offs

- **SDK Latency**: Every page still hits the LiveKit API for live state.
- **Backups**: MongoDB now holds data worth backing up, and losing
  `APP_ENCRYPTION_KEY` makes stored project secrets unrecoverable.
- **Single process**: The login rate limiter, LiveKit client pool, dispatch
  cache and project-list cache are per-process. The Dockerfile runs one uvicorn
  process; adding `--workers` would require moving the rate limiter to shared
  storage.

## Security Model

### Authentication

- **Login form + signed session cookie**, `SameSite=Lax`, with an absolute
  server-side lifetime (`SESSION_MAX_AGE`).
- **Single admin account**, argon2id hash in MongoDB, bootstrapped from
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` on first run.
- **Environment fallback** when MongoDB is unavailable, so a database outage
  cannot lock the operator out of the tool they would use to diagnose it.
- **Login throttling**: 10 attempts per 5 minutes per IP+username. The map is
  bounded in both key count and key length, because the key embeds a
  client-supplied username. Behind a reverse proxy the peer address is the
  proxy, which collapses every caller onto one key — set `TRUST_PROXY_HEADERS=true`
  (only when a proxy actually overwrites `X-Forwarded-For`) to restore
  per-client throttling. Without it, an attacker can lock the `admin` account
  out for 5 minutes at a time.
- **No username-enumeration timing oracle**: a dummy argon2 verification runs
  on the account-not-found path so both outcomes take comparable time.
- Failed logins return a generic message — no user enumeration.
- The session is cleared and re-issued on login (session-fixation defence).

### Authorization

Enforced by `AuthMiddleware` as **deny-by-default**: every path requires a
session unless explicitly allowlisted. Per-route `Depends(requires_admin)`
declarations remain as a second check. A route added without one is still
protected, and `tests/test_auth.py` asserts that property across every
registered route.

Public paths: `/health`, `/health/deep`, `/login`, `/logout`, `/static/*`, and
`/webhooks/*` (which authenticates with LiveKit's own signed JWT).
`/health/deep` reports *whether* the app is degraded to anyone, but withholds
error strings and collection names unless the caller has a session — PyMongo
errors embed the internal topology.

Signed sessions are stateless, so logging out replaces the cookie in that
browser but cannot revoke a copy captured beforehand; such a copy remains valid
until `auth_at + SESSION_MAX_AGE`.

### CSRF Protection

Tokens are signed **and bound to the session**. Signature alone was not enough:
an attacker could fetch any public page, receive a validly-signed token, and
embed it in a form on their own site. The session binding makes tokens
non-transferable between browsers.

### Security Headers

- **HSTS**: Force HTTPS in production
- **CSP**: Content Security Policy (restrictive)
- **X-Frame-Options**: Prevent clickjacking
- **X-Content-Type-Options**: Prevent MIME sniffing
- **Referrer-Policy**: Control referrer information

### Secrets Management

- **Environment Variables**: All secrets from env
- **Never Logged**: API secrets never in logs
- **Masked in UI**: Only show first/last chars

## Deployment Options

### Standalone

```
Python + Uvicorn → LiveKit Server
```

### Docker

```
Docker Container → LiveKit Server
```

### Docker Compose

```
Docker Compose → Multiple Containers → LiveKit Server
```

### With Reverse Proxy (Recommended)

```
nginx/Caddy (TLS) → FastAPI Container → LiveKit Server
```

## Performance Considerations

### Request Latency

- **SDK Calls**: Directly impacts response time
- **No Caching**: Every request hits LiveKit
- **Mitigation**: Use fast network, minimize SDK calls per request

### Scalability

- **Shared state in MongoDB**: horizontal scaling needs the caveats noted under Persistence
- **LiveKit as Bottleneck**: SDK rate limits may apply
- **Mitigation**: Use reverse proxy caching for static assets

### Resource Usage

- **Memory**: Minimal (no caching, no persistence)
- **CPU**: Template rendering + SDK calls
- **Network**: Bandwidth to LiveKit server

## Technology Stack

### Backend

- **FastAPI**: Modern Python web framework
- **Jinja2**: Template engine
- **LiveKit SDK**: Official Python SDK
- **Uvicorn**: ASGI server

### Frontend

- **Bootstrap 5**: UI framework
- **HTMX**: Progressive enhancement
- **Bootstrap Icons**: Icon set
- **Vanilla JS**: Minimal custom JavaScript

### Development

- **Poetry**: Dependency management
- **pytest**: Testing framework
- **Black**: Code formatting
- **Ruff**: Fast linting
- **mypy**: Type checking

### Deployment

- **Docker**: Containerization
- **Docker Compose**: Local orchestration
- **Make**: Build automation

## Future Architecture Considerations

### Planned Enhancements

1. **WebSocket Support**
   - Real-time updates without polling
   - Push notifications from LiveKit

2. **Caching Layer** (Optional)
   - Redis for frequently accessed data
   - TTL-based invalidation
   - Trade stateless design for performance

3. **Background Workers** (Optional)
   - Async egress processing
   - Scheduled cleanup tasks
   - Requires job queue (Celery, RQ)

4. **Multi-User Support**
   - Database for user accounts
   - Role-based access control
   - Per-user preferences

5. **Audit Logging** (Optional)
   - Database for action logs
   - Compliance and security tracking
   - Who did what, when

### Architecture Evolution

The current stateless design is intentional for simplicity. Future versions may optionally add:

- **PostgreSQL**: User accounts, audit logs
- **Redis**: Caching, sessions
- **Celery**: Background tasks

While maintaining backward compatibility for stateless deployments.

## Conclusion

LiveKit Dashboard prioritizes simplicity and reliability through its stateless architecture. All data is sourced directly from LiveKit on each request, eliminating the complexity of data synchronization and persistence while ensuring data accuracy.

The SSR approach with progressive enhancement via HTMX provides a responsive user experience without heavy client-side frameworks, making the application lightweight and easy to maintain.

