# Plex Butler

A self-hosted web app for managing your Plex media library. Connects to Sonarr, Radarr, Ombi, and Plex to help you find and clean up unwatched media — with smart expiration policies, email notifications to requesters, and an AI-powered media chat assistant.

![Plex Butler login screen](docs/screenshot-login.png)

---

## Features

- **Setup wizard** — walks you through every setting on first run, no config files needed
- **TV & Movie dashboards** — scan your Sonarr/Radarr library and see exactly what hasn't been watched
- **Smart Expirations** — automatically schedule content for deletion after a configurable period, with warning emails to the person who requested it
- **Media Chat** — talk naturally to search, add, and delete shows/movies, check your Plex watchlist, see what's trending, and more
- **Requester notifications** — integrates with Ombi to find who requested content and email them before anything is deleted
- **Deletion history** — full log of everything removed, with re-add links
- **Quarantine mode** — move files to a trash folder instead of permanently deleting
- **Exclusion lists** — protect specific titles from ever being cleaned up
- **Bulk actions** — select hundreds of items and delete/extend/keep in one click, with a background progress bar you can track from any page

---

## Requirements

- Python 3.11+
- PostgreSQL (recommended) or SQLite
- A running [Sonarr](https://sonarr.tv) instance
- A running [Radarr](https://radarr.video) instance
- A [Plex Media Server](https://www.plex.tv) with an auth token
- (Optional) [Ombi](https://ombi.io) for requester lookup
- (Optional) OpenAI API key for Media Chat AI features
- (Optional) TMDb API key for recommendations and trending content

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/plex-butler.git
cd plex-butler
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

Create a `.env` file (or set these in your environment):

```env
# Required
DATABASE_URL=postgresql://user:password@localhost/plexbutler
SESSION_SECRET=a-long-random-string-change-this

# Optional — enables AI features in Media Chat
TMDB_API_KEY=your_tmdb_api_key
```

> **Note:** Your Sonarr, Radarr, Plex, Ombi, and SMTP settings are configured through the in-app setup wizard — you do **not** need to put them in environment variables.

### 4. Run the app

```bash
python app.py
```

Then open `http://localhost:5000` in your browser. The setup wizard will guide you through the rest.

### Running on Replit

1. Fork this project on Replit
2. Add `DATABASE_URL` and `SESSION_SECRET` as Secrets (under Tools → Secrets)
3. Optionally add `TMDB_API_KEY`
4. Click Run — the setup wizard opens automatically

---

## Configuration (via Setup Wizard)

On first run, the wizard collects:

| Setting | Where to find it |
|---|---|
| Admin username & password | You choose these |
| Sonarr URL + API key | Sonarr → Settings → General → Security |
| Radarr URL + API key | Radarr → Settings → General → Security |
| Plex URL + token | [How to find your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| Ombi URL + API key | Ombi → Settings → Ombi → API Key *(optional)* |
| SMTP email settings | Your email provider's SMTP details *(optional)* |

All settings can be changed later under **Settings** in the app.

---

## Architecture

```
app.py                  — Flask application, all routes and business logic
cleanup_web.py          — TV show cleanup logic (Sonarr integration)
cleanup_movies.py       — Movie cleanup logic (Radarr integration)
templates/              — Jinja2 HTML templates
  base.html             — Shared layout and navigation
  login.html            — Sign-in page
  landing.html          — Dashboard home
  home.html             — TV & Movie cleanup UI
  expirations.html      — Expiration policy management
  settings.html         — Configuration
  exclusions.html       — Exclusion list management
  history.html          — Deletion history
  email_history.html    — Email log
  setup/                — Setup wizard steps (step1–step6)
```

The app uses a PostgreSQL database (auto-created on first run) to store:
- Configuration settings
- Expiration schedules per media item
- Deletion history and archive
- Email log
- Bulk delete job state

---

## Updating

Pull the latest code and restart — the app runs database migrations automatically on startup, so no manual SQL is needed.

```bash
git pull
python app.py
```

---

## Optional Features

### AI Media Chat (`/media-chat`)
Requires `TMDB_API_KEY` in your environment. OpenAI is accessed via Replit's AI Integrations — if you're running outside Replit, you'll need to adapt the OpenAI calls in `app.py` to use a standard `OPENAI_API_KEY`.

### Email Notifications
Configure SMTP settings in the app under **Settings → Email**. Works with Gmail (use an App Password), SendGrid, Mailgun, or any standard SMTP provider.

### Watchlist Sync
Automatically adds items from your Plex watchlist to Sonarr/Radarr. Enable and configure under **Settings → Watchlist Sync**.

---

## Security Notes

- The app is designed as a **single-admin, self-hosted tool** — one person runs it for their Plex server
- All API keys are stored in the database, never in code or environment variables (except `SESSION_SECRET` and `DATABASE_URL`)
- Requester-facing pages (`/expire/<token>`, `/review/<token>`) use single-use tokens that expire after 30 days
- Run behind a reverse proxy (nginx, Caddy, Traefik) with HTTPS if exposing to the internet

---

## License

MIT — do whatever you want with it. Attribution appreciated but not required.
