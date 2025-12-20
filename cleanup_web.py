"""
Cleanup logic for web interface integration.
This module provides the cleanup functionality that can be called from the Flask app.
"""

import time
import smtplib
import re
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional, Callable, List, Dict
import requests
from plexapi.server import PlexServer


def load_exclusions() -> set:
    exclusions = set()
    exclusion_file = "excluded_shows.txt"
    if os.path.exists(exclusion_file):
        with open(exclusion_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    exclusions.add(line.lower())
    return exclusions


def add_to_exclusions(title: str) -> bool:
    exclusion_file = "excluded_shows.txt"
    try:
        with open(exclusion_file, "a") as f:
            f.write(f"\n{title}")
        return True
    except Exception:
        return False


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


def get_plex_watch_history(config: dict, log: Callable, limit: int = 0) -> dict:
    log("[INFO] Fetching watch history from Plex...")
    watch_history = {}
    
    try:
        plex = PlexServer(config["PLEX_URL"], config["PLEX_TOKEN"], timeout=60)
        
        for section in plex.library.sections():
            if section.type == "show":
                shows = section.all()
                total_shows = len(shows)
                if limit > 0:
                    shows = shows[:limit]
                    log(f"[INFO] Processing {limit} shows from Plex library '{section.title}' (test mode)...")
                else:
                    log(f"[INFO] Processing {total_shows} shows from Plex library '{section.title}'...")
                
                for i, show in enumerate(shows):
                    if (i + 1) % 25 == 0:
                        log(f"[INFO] Progress: {i + 1}/{len(shows)} shows processed...")
                    
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
    ombi_url = config.get("OMBI_URL", "").strip().rstrip('/')
    ombi_key = config.get("OMBI_API_KEY", "").strip()
    
    if not ombi_url or not ombi_key:
        log("[INFO] Ombi not configured - skipping requester lookup")
        return {}
    
    log("[INFO] Fetching TV requests from Ombi...")
    requesters = {}
    
    try:
        response = requests.get(
            f"{ombi_url}/api/v1/Request/tv",
            headers={"ApiKey": ombi_key},
            timeout=30
        )
        
        if response.status_code == 401:
            log("[ERROR] Ombi API key is invalid (401 Unauthorized)")
            return {}
        elif response.status_code == 404:
            log("[ERROR] Ombi API endpoint not found - check your URL")
            return {}
        
        response.raise_for_status()
        requests_data = response.json()
        
        log(f"[INFO] Ombi returned {len(requests_data)} TV requests")
        
        for req in requests_data:
            tvdb_id = None
            for field in ['tvDbId', 'tvdbId', 'thetvdbid', 'externalProviderId', 'theMovieDbId']:
                val = req.get(field)
                if val and isinstance(val, int) and val > 0:
                    tvdb_id = val
                    break
            
            title = req.get("title", "Unknown")
            
            requester_email = ""
            requester_name = "Unknown"
            
            requester_user = req.get("requestedUser") or {}
            if requester_user:
                requester_email = requester_user.get("email", "") or requester_user.get("Email", "")
                requester_name = (requester_user.get("userName") or requester_user.get("username") 
                                  or requester_user.get("alias") or "Unknown")
            
            if not requester_email:
                child_requests = req.get("childRequests") or []
                for child in child_requests:
                    child_user = child.get("requestedUser") or {}
                    email = child_user.get("email", "") or child_user.get("Email", "")
                    if email:
                        requester_email = email
                        requester_name = (child_user.get("userName") or child_user.get("username") 
                                          or child_user.get("alias") or "Unknown")
                        break
            
            if tvdb_id:
                requesters[tvdb_id] = {
                    "email": requester_email,
                    "name": requester_name,
                    "title": title
                }
                log(f"[DEBUG] Ombi request: {title} (TVDB: {tvdb_id}, Email: {requester_email or 'none'})")
        
        with_email = sum(1 for r in requesters.values() if r.get("email"))
        log(f"[SUCCESS] Found {len(requesters)} TV requests in Ombi ({with_email} with email addresses)")
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


def analyze_show(
    series: dict,
    exclusions: set,
    watch_history: dict,
    cutoff_added: datetime,
    cutoff_watched: datetime
) -> Dict:
    """Analyze a single show and return its status with reason."""
    title = series.get("title", "Unknown")
    title_lower = title.lower()
    tvdb_id = series.get("tvdbId")
    series_id = series.get("id")
    
    result = {
        "id": series_id,
        "title": title,
        "tvdb_id": tvdb_id,
        "status": get_show_status(series),
        "is_candidate": True,
        "skip_reason": None,
        "added_date": None,
        "last_watched": None
    }
    
    added_str = series.get("added", "")
    if added_str:
        try:
            added_date = datetime.fromisoformat(added_str.replace("Z", "+00:00"))
            result["added_date"] = added_date.replace(tzinfo=None).strftime("%Y-%m-%d")
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
            result["last_watched"] = last_watched.replace(tzinfo=None).strftime("%Y-%m-%d")
    
    if title_lower in exclusions:
        result["is_candidate"] = False
        result["skip_reason"] = "In exclusion list"
        return result
    
    if added_str:
        try:
            added_date = datetime.fromisoformat(added_str.replace("Z", "+00:00"))
            if added_date.replace(tzinfo=None) > cutoff_added:
                days_ago = (datetime.now() - added_date.replace(tzinfo=None)).days
                result["is_candidate"] = False
                result["skip_reason"] = f"Added {days_ago} days ago (protected)"
                return result
        except ValueError:
            pass
    
    if watch_entry:
        last_watched = watch_entry["last_watched"]
        if isinstance(last_watched, datetime):
            if last_watched.replace(tzinfo=None) > cutoff_watched:
                days_ago = (datetime.now() - last_watched.replace(tzinfo=None)).days
                result["is_candidate"] = False
                result["skip_reason"] = f"Watched {days_ago} days ago (protected)"
                return result
    
    if not result["last_watched"]:
        result["skip_reason"] = "Never watched"
    else:
        result["skip_reason"] = "Not watched recently"
    
    return result


def scan_for_candidates(get_setting: Callable, log_callback: Optional[Callable] = None) -> dict:
    """Scan library and return candidates for user approval."""
    def log(message: str):
        if log_callback:
            log_callback(message)
        print(message)
    
    test_mode_limit = int(get_setting('TEST_MODE_LIMIT', '0') or '0')
    
    config = {
        'SONARR_URL': get_setting('SONARR_URL', '').strip().rstrip('/'),
        'SONARR_API_KEY': get_setting('SONARR_API_KEY', '').strip(),
        'PLEX_URL': get_setting('PLEX_URL', '').strip().rstrip('/'),
        'PLEX_TOKEN': get_setting('PLEX_TOKEN', '').strip(),
        'OMBI_URL': get_setting('OMBI_URL', '').strip().rstrip('/'),
        'OMBI_API_KEY': get_setting('OMBI_API_KEY', '').strip(),
        'SKIP_IF_ADDED_WITHIN_DAYS': int(get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90') or '90'),
        'SKIP_IF_WATCHED_WITHIN_DAYS': int(get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180') or '180'),
    }
    
    if test_mode_limit > 0:
        log(f"[INFO] Starting scan (TEST MODE: limited to {test_mode_limit} shows)")
    else:
        log("[INFO] Starting library scan...")
    
    cutoff_added = datetime.now() - timedelta(days=config['SKIP_IF_ADDED_WITHIN_DAYS'])
    cutoff_watched = datetime.now() - timedelta(days=config['SKIP_IF_WATCHED_WITHIN_DAYS'])
    
    exclusions = load_exclusions()
    log(f"[INFO] Loaded {len(exclusions)} shows from exclusion list")
    
    series_list = get_sonarr_series(config, log)
    if not series_list:
        log("[ERROR] No series found in Sonarr")
        return {'error': 'No series found in Sonarr', 'candidates': [], 'skipped': []}
    
    if test_mode_limit > 0:
        series_list = series_list[:test_mode_limit]
        log(f"[INFO] Test mode: limiting to first {test_mode_limit} shows")
    
    watch_history = get_plex_watch_history(config, log, test_mode_limit)
    ombi_requesters = get_ombi_requests(config, log)
    
    log("[INFO] Analyzing shows...")
    
    candidates = []
    skipped = []
    
    for series in series_list:
        analysis = analyze_show(series, exclusions, watch_history, cutoff_added, cutoff_watched)
        
        tvdb_id = analysis.get("tvdb_id")
        requester = ombi_requesters.get(tvdb_id, {})
        analysis["requester_email"] = requester.get("email", "")
        analysis["requester_name"] = requester.get("name", "")
        
        if analysis["is_candidate"]:
            candidates.append(analysis)
        else:
            skipped.append(analysis)
    
    log(f"[INFO] Total shows analyzed: {len(series_list)}")
    log(f"[SUCCESS] Protected shows: {len(skipped)}")
    log(f"[WARNING] Deletion candidates: {len(candidates)}")
    
    if not candidates:
        log("[SUCCESS] No shows eligible for deletion. Your library is well-maintained!")
    
    return {
        'candidates': candidates,
        'skipped': skipped,
        'total': len(series_list)
    }


def execute_actions(
    actions: List[Dict],
    get_setting: Callable,
    log_callback: Optional[Callable] = None
) -> dict:
    """Execute approved deletion/exclusion actions."""
    def log(message: str):
        if log_callback:
            log_callback(message)
        print(message)
    
    config = {
        'SONARR_URL': get_setting('SONARR_URL', '').strip().rstrip('/'),
        'SONARR_API_KEY': get_setting('SONARR_API_KEY', '').strip(),
        'PLEX_URL': get_setting('PLEX_URL', '').strip().rstrip('/'),
        'PLEX_TOKEN': get_setting('PLEX_TOKEN', '').strip(),
        'SMTP_HOST': get_setting('SMTP_HOST', ''),
        'SMTP_PORT': int(get_setting('SMTP_PORT', '587') or '587'),
        'SMTP_USER': get_setting('SMTP_USER', ''),
        'SMTP_PASSWORD': get_setting('SMTP_PASSWORD', ''),
        'SMTP_FROM': get_setting('SMTP_FROM', ''),
        'DELETION_DELAY_SECONDS': float(get_setting('DELETION_DELAY_SECONDS', '2.0') or '2.0'),
    }
    
    deleted_count = 0
    excluded_count = 0
    errors = []
    
    delete_actions = [a for a in actions if a.get('action') == 'delete']
    exclude_actions = [a for a in actions if a.get('action') == 'exclude']
    
    log(f"[INFO] Processing {len(delete_actions)} deletions and {len(exclude_actions)} exclusions...")
    
    for action in exclude_actions:
        title = action.get('title', 'Unknown')
        if add_to_exclusions(title):
            log(f"[SUCCESS] Added '{title}' to exclusion list")
            excluded_count += 1
        else:
            log(f"[ERROR] Failed to add '{title}' to exclusion list")
            errors.append(f"Failed to exclude: {title}")
    
    for action in delete_actions:
        series_id = action.get('id')
        title = action.get('title', 'Unknown')
        delete_from_db = action.get('deleteFromSonarr', False)
        requester_email = action.get('requester_email', '')
        requester_name = action.get('requester_name', '')
        
        if not series_id:
            log(f"[ERROR] Missing series ID for '{title}'")
            errors.append(f"Missing ID: {title}")
            continue
        
        log(f"[INFO] Processing deletion: {title}")
        
        plex_deleted = delete_from_plex(title, config, log)
        
        sonarr_deleted = delete_from_sonarr(
            int(series_id), title, config, log,
            delete_files=True,
            delete_from_database=delete_from_db
        )
        
        if sonarr_deleted:
            deleted_count += 1
            
            if requester_email:
                send_notification_email(
                    requester_email,
                    title,
                    requester_name,
                    config,
                    log
                )
        else:
            errors.append(f"Failed to delete: {title}")
        
        if len(delete_actions) > 1:
            log(f"[INFO] Waiting {config['DELETION_DELAY_SECONDS']}s before next deletion...")
            time.sleep(config["DELETION_DELAY_SECONDS"])
    
    log(f"[SUCCESS] Completed: {deleted_count} deleted, {excluded_count} excluded")
    
    return {
        'deleted': deleted_count,
        'excluded': excluded_count,
        'errors': errors
    }


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


def delete_from_sonarr(
    series_id: int,
    show_title: str,
    config: dict,
    log: Callable,
    delete_files: bool = True,
    delete_from_database: bool = False
) -> bool:
    """Delete show from Sonarr. If delete_from_database is True, removes from Sonarr DB entirely."""
    try:
        params = {"deleteFiles": str(delete_files).lower()}
        if delete_from_database:
            params["addImportListExclusion"] = "true"
            log(f"[INFO] Will also add '{show_title}' to Sonarr import exclusion list")
        
        response = requests.delete(
            f"{config['SONARR_URL']}/api/v3/series/{series_id}",
            headers={"X-Api-Key": config["SONARR_API_KEY"]},
            params=params,
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


def run_cleanup_with_settings(get_setting: Callable, dry_run: bool = True, log_callback: Optional[Callable] = None) -> dict:
    """Legacy function - redirects to new scan function."""
    return scan_for_candidates(get_setting, log_callback)
