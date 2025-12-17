# TV Show Cleanup Tool

## Overview
An automated TV show cleanup tool for Plex, Sonarr, and Ombi libraries. This tool intelligently identifies shows that can be safely deleted based on watch history, age, and exclusion lists, while notifying original requesters via email.

## Features
- Scans Sonarr library for all TV series
- Filters shows based on configurable criteria (added date, watch history, exclusion list)
- Interactive review mode with manual exclusion prompts
- Dry-run mode for safe testing before actual deletion
- Deletes from both Plex and Sonarr with file cleanup
- Email notifications to Ombi requesters
- Slow, deliberate deletion with delays to prevent mistakes

## Project Structure
```
.
├── cleanup.py          # Main cleanup script
├── excluded_shows.txt  # Persistent exclusion list
├── .env               # Environment configuration (create this)
└── replit.md          # This documentation
```

## Configuration

### Required Environment Variables
- `SONARR_URL` - Sonarr server URL (e.g., http://localhost:8989)
- `SONARR_API_KEY` - Sonarr API key (Settings > General > Security)
- `PLEX_URL` - Plex server URL (e.g., http://localhost:32400)
- `PLEX_TOKEN` - Plex authentication token

### Optional Environment Variables
- `OMBI_URL` - Ombi server URL
- `OMBI_API_KEY` - Ombi API key
- `SMTP_HOST` - SMTP server for email notifications
- `SMTP_PORT` - SMTP port (default: 587)
- `SMTP_USER` - SMTP username
- `SMTP_PASSWORD` - SMTP password
- `SMTP_FROM` - From email address

### Configurable Parameters
- `SKIP_IF_ADDED_WITHIN_DAYS` - Skip recently added shows (default: 90)
- `SKIP_IF_WATCHED_WITHIN_DAYS` - Skip recently watched shows (default: 180)
- `DELETION_DELAY_SECONDS` - Delay between deletions (default: 2.0)

## Usage

### Dry Run (Safe Test Mode)
```bash
python cleanup.py
```

### Execute Deletions
```bash
python cleanup.py --execute
```

## Exclusion List
Add show titles to `excluded_shows.txt` (one per line) to permanently protect them from deletion. Lines starting with `#` are comments.

## Recent Changes
- Initial creation (Dec 2025)
