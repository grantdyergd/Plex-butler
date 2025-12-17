# TV Show Cleanup Tool

## Overview
A web-based TV show cleanup tool for Plex, Sonarr, and Ombi libraries. This tool features a setup wizard, user authentication, and a dashboard to intelligently identify and remove unwatched shows while respecting exclusions and notifying original requesters via email.

## Features
- Web-based setup wizard for easy configuration
- Username/password authentication for secure access
- Dashboard to run and monitor cleanup jobs
- Scans Sonarr library for all TV series
- Filters shows based on configurable criteria (added date, watch history, exclusion list)
- Interactive exclusion list management
- Dry-run mode for safe testing before actual deletion
- Deletes from both Plex and Sonarr with file cleanup
- Email notifications to Ombi requesters
- Slow, deliberate deletion with delays to prevent mistakes

## Project Structure
```
.
├── app.py              # Flask web application
├── cleanup_web.py      # Cleanup logic for web integration
├── cleanup.py          # CLI cleanup script (legacy)
├── templates/          # HTML templates
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html
│   ├── settings.html
│   ├── exclusions.html
│   └── setup/          # Setup wizard templates
├── excluded_shows.txt  # Persistent exclusion list
└── replit.md           # This documentation
```

## Getting Started

### First Run
1. Access the web interface
2. Complete the setup wizard:
   - Create your admin username and password
   - Enter your Sonarr URL and API key
   - Enter your Plex URL and token
   - Optionally configure Ombi for requester lookup
   - Optionally configure email notifications
   - Set cleanup parameters (days thresholds)
3. Log in with your credentials
4. Use the dashboard to run cleanup jobs

### Configuration Values Needed
- **Sonarr URL** - Your Sonarr server URL (e.g., http://192.168.1.100:8989)
- **Sonarr API Key** - Found in Sonarr: Settings > General > Security
- **Plex URL** - Your Plex server URL (e.g., http://192.168.1.100:32400)
- **Plex Token** - Your Plex authentication token

### Optional Configuration
- **Ombi URL** - Ombi server URL for requester tracking
- **Ombi API Key** - Ombi API key
- **SMTP Settings** - For email notifications to requesters

## Usage

### Dashboard
- **Dry Run**: Scans library and shows what would be deleted without making changes
- **Execute**: Actually deletes shows after confirmation

### Exclusion List
Add show titles to protect them from deletion. Managed via the web interface.

### Settings
All configuration can be updated via the Settings page after initial setup.

## CLI Usage (Legacy)
The command-line interface is still available for automated/scheduled use:

```bash
python cleanup.py                    # Interactive dry run
python cleanup.py --execute          # Interactive with actual deletions
python cleanup.py --auto             # Automated dry run (for scheduled jobs)
python cleanup.py --auto --execute   # Automated with actual deletions
```

## Environment Variables
For deployment, the following environment variables should be set:
- `DATABASE_URL` - PostgreSQL database connection (auto-configured on Replit)
- `SESSION_SECRET` - Session encryption key

## How It Works
1. User completes setup wizard with credentials
2. Dashboard connects to Sonarr to fetch all TV series
3. Gets watch history from Plex (uses TVDB IDs for reliable matching)
4. Filters out protected shows (recently added, recently watched, in exclusion list)
5. Shows deletion candidates in dashboard
6. On execute: Deletes from both Plex and Sonarr, notifies requesters via email

## Recent Changes
- Added web interface with setup wizard (Dec 2025)
- Added user authentication
- Added dashboard for running and monitoring cleanup jobs
- Added settings page for configuration management
- Added exclusion list management UI
- Improved Plex matching using TVDB IDs instead of titles only
