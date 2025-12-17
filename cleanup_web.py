"""
Cleanup logic for web interface integration.
This module provides the cleanup functionality that can be called from the Flask app.
"""

import time
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional, Callable
import requests
from plexapi.server import PlexServer


def run_cleanup_with_settings(get_setting: Callable, dry_run: bool = True, log_callback: Optional[Callable] = None) -> dict:
    def log(message: str):
        if log_callback:
            log_callback(message)
        print(message)
    
    config = {
        'SONARR_URL': get_setting('SONARR_URL', ''),
        'SONARR_API_KEY': get_setting('SONARR_API_KEY', ''),
        'PLEX_URL': get_setting('PLEX_URL', ''),
        'PLEX_TOKEN': get_setting('PLEX_TOKEN', ''),
        'OMBI_URL': get_setting('OMBI_URL', ''),
        'OMBI_API_KEY': get_setting('OMBI_API_KEY', ''),
        'SMTP_HOST': get_setting('SMTP_HOST', ''),
        'SMTP_PORT': int(get_setting('SMTP_PORT', '587') or '587'),
        'SMTP_USER': get_setting('SMTP_USER', ''),
        'SMTP_PASSWORD': get_setting('SMTP_PASSWORD', ''),
        'SMTP_FROM': get_setting('SMTP_FROM', ''),
        'SKIP_IF_ADDED_WITHIN_DAYS': int(get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90') or '90'),
        'SKIP_IF_WATCHED_WITHIN_DAYS': int(get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180') or '180'),
        'DELETION_DELAY_SECONDS': float(get_setting('DELETION_DELAY_SECONDS', '2.0') or '2.0'),
    }
    
    mode = "DRY RUN" if dry_run else "LIVE MODE"
    log(f"[INFO] Starting cleanup in {mode}")
    
    cutoff_added = datetime.now() - timedelta(days=config['SKIP_IF_ADDED_WITHIN_DAYS'])
    cutoff_watched = datetime.now() - timedelta(days=config['SKIP_IF_WATCHED_WITHIN_DAYS'])
    
    exclusions = load_exclusions()
    log(f"[INFO] Loaded {len(exclusions)} shows from exclusion list")
    
    series_list = get_sonarr_series(config, log)
    if not series_list:
        log("[ERROR] No series found in Sonarr")
        return {'error': 'No series found in Sonarr'}
    
    watch_history = get_plex_watch_history(config, log)
    ombi_requesters = get_ombi_requests(config, log)
    
    log("[INFO] Analyzing shows...")
    
    candidates = []
    skipped = []
    
    for series in series_list:
        title = series.get("title", "Unknown")
        series_id = series.get("id")
        tvdb_id = series.get("tvdbId")
        status = get_show_status(series)
        
        skip, reason = should_skip_show(
            series, exclusions, watch_history, cutoff_added, cutoff_watched, config
        )
        
        if skip:
            skipped.append({"title": title, "reason": reason})
        else:
            requester = ombi_requesters.get(tvdb_id, {})
            candidates.append({
                "id": series_id,
                "title": title,
                "tvdb_id": tvdb_id,
                "status": status,
                "requester_email": requester.get("email", ""),
                "requester_name": requester.get("name", "")
            })
    
    log(f"[INFO] Total shows: {len(series_list)}")
    log(f"[SUCCESS] Skipped (protected): {len(skipped)}")
    log(f"[WARNING] Candidates for deletion: {len(candidates)}")
    
    if not candidates:
        log("[SUCCESS] No shows to delete. Your library is well-maintained!")
        return {'deleted': 0, 'candidates': 0, 'skipped': len(skipped)}
    
    for show in candidates:
        log(f"[INFO] Candidate: {show['title']} ({show['status']})")
    
    if dry_run:
        log("[INFO] DRY RUN complete - no changes were made")
        return {'deleted': 0, 'candidates': len(candidates), 'skipped': len(skipped)}
    
    log("[WARNING] Starting deletions...")
    
    deleted_count = 0
    for show in candidates:
        log(f"[INFO] Processing: {show['title']}")
        
        delete_from_plex(show["title"], config, log)
        
        if delete_from_sonarr(show["id"], show["title"], config, log):
            deleted_count += 1
            
            if show["requester_email"]:
                send_notification_email(
                    show["requester_email"],
                    show["title"],
                    show["requester_name"],
                    config,
                    log
                )
        
        log(f"[INFO] Waiting {config['DELETION_DELAY_SECONDS']}s...")
        time.sleep(config["DELETION_DELAY_SECONDS"])
    
    log(f"[SUCCESS] Deleted {deleted_count} of {len(candidates)} shows")
    return {'deleted': deleted_count, 'candidates': len(candidates), 'skipped': len(skipped)}


def load_exclusions() -> set:
    import os
    exclusions = set()
    exclusion_file = "excluded_shows.txt"
    if os.path.exists(exclusion_file):
        with open(exclusion_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    exclusions.add(line.lower())
    return exclusions


def get_sonarr_series(config: dict, log: Callable) -> list:
    log("[INFO] Fetching series from Sonarr...")
    try:
        response = requests.get(
            f"{config['SONARR_URL']}/api/v3/series",
            headers={"X-Api-Key": config["SONARR_API_KEY"]},
            timeout=30
        )
        response.raise_for_status()
        series = response.json()
        log(f"[SUCCESS] Found {len(series)} series in Sonarr")
        return series
    except requests.RequestException as e:
        log(f"[ERROR] Failed to fetch Sonarr series: {e}")
        return []


def extract_tvdb_id_from_guid(guid: str) -> Optional[int]:
    if not guid:
        return None
    patterns = [
        r'thetvdb://(\d+)',
        r'tvdb://(\d+)',
        r'com\.plexapp\.agents\.thetvdb://(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, guid)
        if match:
            return int(match.group(1))
    return None


def get_plex_watch_history(config: dict, log: Callable) -> dict:
    log("[INFO] Fetching watch history from Plex...")
    watch_history = {}
    
    try:
        plex = PlexServer(config["PLEX_URL"], config["PLEX_TOKEN"], timeout=60)
        
        for section in plex.library.sections():
            if section.type == "show":
                shows = section.all()
                total_shows = len(shows)
                log(f"[INFO] Processing {total_shows} shows from Plex library '{section.title}'...")
                
                for i, show in enumerate(shows):
                    if (i + 1) % 25 == 0:
                        log(f"[INFO] Progress: {i + 1}/{total_shows} shows processed...")
                    
                    last_watched = None
                    tvdb_id = None
                    
                    try:
                        for guid in show.guids:
                            extracted_id = extract_tvdb_id_from_guid(guid.id)
                            if extracted_id:
                                tvdb_id = extracted_id
                                break
                        
                        if not tvdb_id and hasattr(show, 'guid'):
                            tvdb_id = extract_tvdb_id_from_guid(show.guid)
                    except Exception:
                        pass
                    
                    try:
                        for episode in show.episodes():
                            if episode.lastViewedAt:
                                if last_watched is None or episode.lastViewedAt > last_watched:
                                    last_watched = episode.lastViewedAt
                    except Exception:
                        pass
                    
                    if last_watched:
                        if tvdb_id:
                            watch_history[tvdb_id] = {
                                "last_watched": last_watched,
                                "title": show.title
                            }
                        watch_history[f"title:{show.title.lower()}"] = {
                            "last_watched": last_watched,
                            "title": show.title
                        }
        
        tvdb_count = sum(1 for k in watch_history if isinstance(k, int))
        log(f"[SUCCESS] Retrieved watch history ({tvdb_count} with TVDB ID)")
        return watch_history
    except Exception as e:
        log(f"[ERROR] Failed to connect to Plex: {e}")
        return {}


def get_ombi_requests(config: dict, log: Callable) -> dict:
    if not config.get("OMBI_URL") or not config.get("OMBI_API_KEY"):
        log("[WARNING] Ombi not configured")
        return {}
    
    log("[INFO] Fetching TV requests from Ombi...")
    try:
        response = requests.get(
            f"{config['OMBI_URL']}/api/v1/Request/tv",
            headers={"ApiKey": config["OMBI_API_KEY"]},
            timeout=30
        )
        response.raise_for_status()
        requests_data = response.json()
        
        requesters = {}
        for req in requests_data:
            tvdb_id = req.get("tvDbId")
            requester_email = req.get("requestedUser", {}).get("email", "")
            requester_name = req.get("requestedUser", {}).get("userName", "Unknown")
            if tvdb_id and requester_email:
                requesters[tvdb_id] = {
                    "email": requester_email,
                    "name": requester_name
                }
        
        log(f"[SUCCESS] Found {len(requesters)} TV requests in Ombi")
        return requesters
    except requests.RequestException as e:
        log(f"[WARNING] Failed to fetch Ombi requests: {e}")
        return {}


def get_show_status(series: dict) -> str:
    status = series.get("status", "Unknown")
    if status == "continuing":
        return "Continuing"
    elif status == "ended":
        return "Ended"
    else:
        return status.capitalize() if status else "Unknown"


def should_skip_show(
    series: dict,
    exclusions: set,
    watch_history: dict,
    cutoff_added: datetime,
    cutoff_watched: datetime,
    config: dict
) -> tuple:
    title = series.get("title", "Unknown")
    title_lower = title.lower()
    tvdb_id = series.get("tvdbId")
    
    if title_lower in exclusions:
        return True, "In exclusion list"
    
    added_str = series.get("added", "")
    if added_str:
        try:
            added_date = datetime.fromisoformat(added_str.replace("Z", "+00:00"))
            if added_date.replace(tzinfo=None) > cutoff_added:
                days_ago = (datetime.now() - added_date.replace(tzinfo=None)).days
                return True, f"Added {days_ago} days ago"
        except ValueError:
            pass
    
    watch_entry = None
    if tvdb_id and tvdb_id in watch_history:
        watch_entry = watch_history[tvdb_id]
    elif f"title:{title_lower}" in watch_history:
        watch_entry = watch_history[f"title:{title_lower}"]
    
    if watch_entry:
        last_watched = watch_entry["last_watched"]
        if isinstance(last_watched, datetime):
            if last_watched.replace(tzinfo=None) > cutoff_watched:
                days_ago = (datetime.now() - last_watched.replace(tzinfo=None)).days
                return True, f"Watched {days_ago} days ago"
    
    return False, ""


def delete_from_plex(show_title: str, config: dict, log: Callable) -> bool:
    try:
        plex = PlexServer(config["PLEX_URL"], config["PLEX_TOKEN"])
        
        for section in plex.library.sections():
            if section.type == "show":
                try:
                    show = section.get(show_title)
                    show.delete()
                    log(f"[SUCCESS] Deleted '{show_title}' from Plex")
                    return True
                except Exception:
                    continue
        
        log(f"[WARNING] Show '{show_title}' not found in Plex")
        return False
    except Exception as e:
        log(f"[ERROR] Failed to delete from Plex: {e}")
        return False


def delete_from_sonarr(series_id: int, show_title: str, config: dict, log: Callable, delete_files: bool = True) -> bool:
    try:
        response = requests.delete(
            f"{config['SONARR_URL']}/api/v3/series/{series_id}",
            headers={"X-Api-Key": config["SONARR_API_KEY"]},
            params={"deleteFiles": str(delete_files).lower()},
            timeout=30
        )
        response.raise_for_status()
        log(f"[SUCCESS] Deleted '{show_title}' from Sonarr")
        return True
    except requests.RequestException as e:
        log(f"[ERROR] Failed to delete from Sonarr: {e}")
        return False


def send_notification_email(email: str, show_title: str, requester_name: str, config: dict, log: Callable) -> bool:
    if not all([config.get("SMTP_HOST"), config.get("SMTP_USER"), 
                config.get("SMTP_PASSWORD"), config.get("SMTP_FROM")]):
        log(f"[WARNING] SMTP not configured, skipping email")
        return False
    
    try:
        msg = MIMEMultipart()
        msg["From"] = config["SMTP_FROM"]
        msg["To"] = email
        msg["Subject"] = f"TV Show Removed: {show_title}"
        
        body = f"""Hi {requester_name},

This is an automated notification to let you know that the TV show you requested, "{show_title}", has been removed from our media library.

This removal was done as part of our regular library maintenance to free up storage space for new content.

If you would like this show to be added again in the future, please feel free to submit a new request through Ombi.

Best regards,
Media Library Cleanup Bot
"""
        msg.attach(MIMEText(body, "plain"))
        
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
            server.starttls()
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
        
        log(f"[SUCCESS] Sent notification to {email}")
        return True
    except Exception as e:
        log(f"[ERROR] Failed to send email: {e}")
        return False
