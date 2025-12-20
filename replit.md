# Media Scrubber

## Overview
A web-based media cleanup tool for Plex, Sonarr, Radarr, and Ombi libraries. This tool features a setup wizard, user authentication, and separate dashboards for TV shows and movies to intelligently identify and remove unwatched media while respecting exclusions and notifying original requesters via email.

## Features
- Web-based setup wizard for easy configuration
- Username/password authentication for secure access
- **Separate dashboards for TV Shows and Movies**
- Scans Sonarr library for all TV series
- **Scans Radarr library for all movies**
- Checks watch history across ALL Plex users (not just admin)
- Filters media based on configurable criteria (added date, watch history, exclusion list)
- Interactive exclusion list management (separate lists for TV and Movies)
- Rich data display: size, episode count/runtime, view count, monitored status
- Sortable columns (by status, size, title, dates, etc.)
- Warnings for large media (>50GB)
- CSV export of candidates for offline review
- Quarantine mode: move files to trash folder instead of deleting
- Deletes from both Plex and Sonarr/Radarr with file cleanup
- Email notifications to Ombi requesters
- Slow, deliberate deletion with delays to prevent mistakes
- Deletion history tracking with search, filter by type, and re-add links

## Project Structure
```
.
├── app.py              # Flask web application
├── cleanup_web.py      # TV show cleanup logic for web integration
├── cleanup_movies.py   # Movie cleanup logic for Radarr integration
├── cleanup.py          # CLI cleanup script (legacy)
├── templates/          # HTML templates
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html  # TV Shows dashboard
│   ├── movies.html     # Movies dashboard
│   ├── settings.html
│   ├── exclusions.html # Tabbed TV/Movies exclusions
│   ├── history.html    # Deletion history with type filter
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
- **Radarr URL** - Your Radarr server URL (e.g., http://192.168.1.100:7878)
- **Radarr API Key** - Found in Radarr: Settings > General > Security
- **Plex URL** - Your Plex server URL (e.g., http://192.168.1.100:32400)
- **Plex Token** - Your Plex authentication token

### Optional Configuration
- **Ombi URL** - Ombi server URL for requester tracking
- **Ombi API Key** - Ombi API key
- **SMTP Settings** - For email notifications to requesters

## Usage

### Dashboard (Two-Phase Workflow)
1. **Scan**: Click "Scan for Candidates" to analyze your library
2. **Review**: Candidates appear in an interactive table showing:
   - Show title and requester info
   - Show status (Continuing/Ended) - sortable, Ended shows first by default
   - Size on disk, episode count, season count
   - View count (total plays across all users)
   - Reason for inclusion (never watched, not watched recently)
   - Added date and last watched date
   - Warnings for large shows (>50GB) and continuing series
3. **Sort & Filter**: Click any column header to sort ascending/descending
4. **Export CSV**: Download candidates list for offline review
5. **Choose Actions**: For each show, select:
   - **Delete** (red trash icon) - Remove from Plex and Sonarr
   - **Exclude** (yellow shield icon) - Add to exclusion list
   - **Ignore** (gray dash) - Take no action
6. **Bulk Actions**: Use "Select All Delete", "Select All Exclude", or "Clear All"
7. **Options**:
   - **Delete from Sonarr DB**: Prevents shows from being re-added
   - **Quarantine mode**: Move files to trash folder instead of permanent deletion
8. **Execute**: Click "Execute Selected Actions" to process your selections

### Exclusion List
Add show titles to protect them from deletion. Managed via the web interface.

### Settings
All configuration can be updated via the Settings page after initial setup.
- **Test Mode Limit**: Set to a number (e.g., 25) to limit scans for testing, or 0 for full library

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
- Added priority scoring system for deletion recommendations (Dec 2025)
  - High priority: Ended + never watched + no requester + large size
  - Visual badges (High/Medium/Low) with point breakdown
  - "Why Delete?" column explains reasoning
- Added rich data display: size, episode count, view count, monitored status
- Added sortable columns (click headers to sort by any field)
- Added CSV export for offline candidate review
- Added quarantine mode (move files instead of delete)
- Added warnings for large shows (>50GB) and continuing series
- Added double confirmation for risky deletions
- Optimized Plex scan performance using server-level history
- Plex now checks ALL users' watch history (not just admin)
- Added two-phase workflow with interactive approval UI
- Added per-show action selection (delete/exclude/ignore)
- Added "Delete from Sonarr DB" option to prevent re-addition
- Added web interface with setup wizard
- Added user authentication
- Added dashboard for running and monitoring cleanup jobs
- Added settings page for configuration management
- Added exclusion list management UI
- Improved Plex matching using TVDB IDs instead of titles only
