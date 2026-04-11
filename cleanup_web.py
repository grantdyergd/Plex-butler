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


def load_exclusions_from_db() -> set:
    """Load exclusions from database. Returns set of lowercase titles."""
    try:
        from app import Exclusion
        exclusions = set()
        for e in Exclusion.query.all():
            exclusions.add(e.title.lower())
        return exclusions
    except Exception:
        return set()


def add_to_exclusions_db(title: str) -> bool:
    """Add a title to the database exclusion list."""
    try:
        from app import Exclusion, db
        existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == title.lower()).first()
        if not existing:
            new_exclusion = Exclusion(title=title)
            db.session.add(new_exclusion)
            db.session.commit()
        return True
    except Exception:
        return False


def record_deletion_history(
    title: str,
    sonarr_id: int = None,
    tvdb_id: int = None,
    size_bytes: int = None,
    season_count: int = None,
    episode_count: int = None,
    requester_name: str = None,
    requester_email: str = None,
    priority_score: int = None,
    priority_label: str = None,
    was_quarantined: bool = False,
    deleted_from_sonarr_db: bool = False
) -> bool:
    """Record a deletion in the history database."""
    try:
        from app import DeletionHistory, db
        record = DeletionHistory(
            title=title,
            sonarr_id=sonarr_id,
            tvdb_id=tvdb_id,
            size_bytes=size_bytes,
            season_count=season_count,
            episode_count=episode_count,
            requester_name=requester_name,
            requester_email=requester_email,
            priority_score=priority_score,
            priority_label=priority_label,
            was_quarantined=was_quarantined,
            deleted_from_sonarr_db=deleted_from_sonarr_db
        )
        db.session.add(record)
        db.session.commit()
        return True
    except Exception:
        return False


def quarantine_files(source_path: str, quarantine_path: str, title: str, log: Callable) -> bool:
    """Move show files to quarantine folder instead of deleting them."""
    import shutil
    
    if not os.path.exists(source_path):
        log(f"[WARNING] Source path does not exist: {source_path}")
        return False
    
    try:
        os.makedirs(quarantine_path, exist_ok=True)
        
        folder_name = os.path.basename(source_path.rstrip('/\\'))
        dest_path = os.path.join(quarantine_path, folder_name)
        
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(quarantine_path, f"{folder_name}_{counter}")
            counter += 1
        
        shutil.move(source_path, dest_path)
        log(f"[SUCCESS] Quarantined '{title}' to: {dest_path}")
        return True
    except Exception as e:
        log(f"[ERROR] Failed to quarantine '{title}': {e}")
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


def get_plex_watch_history(config: dict, log: Callable, limit: int = 0, force_refresh: bool = False) -> dict:
    """Get watch history from Plex using server-level history for ALL users.
    
    Uses plex.history() at server level which properly includes all users' watch history.
    Returns view counts in addition to last watched dates.
    Caches results for 7 days to avoid re-scanning.
    """
    watch_history = {}
    
    if not config.get("PLEX_URL") or not config.get("PLEX_TOKEN"):
        log("[WARNING] Plex not configured - using title matching only")
        return {}
    
    if not force_refresh:
        try:
            from app import load_watch_history_cache, db
            # Ensure clean session state before querying cache
            try:
                db.session.rollback()
            except:
                pass
            cached = load_watch_history_cache('tv')
            if cached:
                log(f"[CACHE HIT] Using cached TV watch history - {cached['age_days']} day(s) old (cached on {cached['scanned_at'][:10]})")
                log(f"[CACHE HIT] Skipping Plex scan - cache valid for {7 - cached['age_days']} more day(s)")
                return cached['history']
            else:
                log("[CACHE MISS] No valid TV cache found - will fetch fresh data from Plex")
        except Exception as e:
            import traceback
            log(f"[WARNING] Could not check cache: {e}")
            log(f"[DEBUG] Traceback: {traceback.format_exc()}")
    
    log("[FRESH SCAN] Fetching watch history from Plex (all users) - this may take a minute...")
    
    try:
        plex = PlexServer(config["PLEX_URL"], config["PLEX_TOKEN"], timeout=120)
        
        show_last_watched = {}
        show_view_counts = {}
        show_tvdb_ids = {}
        show_titles = {}
        
        log("[INFO] Fetching watch history for ALL server accounts (admin + shared users)...")
        try:
            all_accounts = plex.systemAccounts()
            log(f"[INFO] Found {len(all_accounts)} accounts on server — fetching history for each...")
        except Exception as e:
            log(f"[WARNING] Could not enumerate accounts ({e}), falling back to server-level history")
            all_accounts = [None]

        total_entries = 0
        for account in all_accounts:
            try:
                if account is None:
                    account_history = plex.history(maxresults=50000)
                    label = "server"
                else:
                    account_id = getattr(account, 'accountID', getattr(account, 'id', None))
                    account_name = getattr(account, 'name', str(account_id))
                    if account_id is None:
                        continue
                    account_history = plex.history(maxresults=50000, accountID=account_id)
                    label = account_name

                user_entries = 0
                for item in account_history:
                    try:
                        if item.type != 'episode':
                            continue

                        if hasattr(item, 'grandparentTitle') and item.grandparentTitle:
                            show_title = item.grandparentTitle
                            show_key = item.grandparentRatingKey if hasattr(item, 'grandparentRatingKey') else show_title
                        else:
                            continue

                        viewed_at = item.viewedAt if hasattr(item, 'viewedAt') else None
                        if not viewed_at:
                            continue

                        show_view_counts[show_key] = show_view_counts.get(show_key, 0) + 1
                        user_entries += 1

                        if show_key not in show_last_watched or viewed_at > show_last_watched[show_key]:
                            show_last_watched[show_key] = viewed_at
                            show_titles[show_key] = show_title
                    except Exception:
                        continue

                total_entries += user_entries
                if user_entries > 0:
                    log(f"[INFO] {label}: {user_entries} TV episode plays")
            except Exception as e:
                label = getattr(account, 'name', str(account)) if account is not None else 'server'
                log(f"[WARNING] Could not get history for {label}: {e}")
                continue

        log(f"[SUCCESS] Merged {total_entries} total TV plays across all users — {len(show_last_watched)} unique shows watched")
        
        for section in plex.library.sections():
            if section.type == "show":
                log(f"[INFO] Scanning '{section.title}' for show details...")
                shows = section.all()
                if limit > 0:
                    shows = shows[:limit]
                
                for show in shows:
                    show_key = show.ratingKey
                    show_titles[show_key] = show.title
                    
                    if show_key not in show_last_watched:
                        if hasattr(show, 'lastViewedAt') and show.lastViewedAt:
                            show_last_watched[show_key] = show.lastViewedAt
                        if hasattr(show, 'viewCount') and show.viewCount:
                            show_view_counts[show_key] = max(
                                show_view_counts.get(show_key, 0),
                                show.viewCount
                            )
                    
                    try:
                        if hasattr(show, 'guids'):
                            for guid in show.guids:
                                extracted_id = extract_tvdb_id_from_guid(guid.id)
                                if extracted_id:
                                    show_tvdb_ids[show_key] = extracted_id
                                    break
                        if show_key not in show_tvdb_ids and hasattr(show, 'guid'):
                            tvdb_id = extract_tvdb_id_from_guid(show.guid)
                            if tvdb_id:
                                show_tvdb_ids[show_key] = tvdb_id
                    except Exception:
                        pass
        
        for show_key in set(list(show_last_watched.keys()) + list(show_titles.keys())):
            title = show_titles.get(show_key, "Unknown")
            tvdb_id = show_tvdb_ids.get(show_key)
            last_watched = show_last_watched.get(show_key)
            view_count = show_view_counts.get(show_key, 0)
            
            last_watched_str = last_watched.isoformat() if last_watched else None
            entry = {
                "last_watched": last_watched_str,
                "view_count": view_count,
                "title": title
            }
            
            if tvdb_id:
                watch_history[str(tvdb_id)] = entry
            watch_history[f"title:{title.lower()}"] = entry
        
        watched_count = sum(1 for k, v in watch_history.items() if v.get('last_watched'))
        tvdb_count = sum(1 for k in watch_history if k.isdigit())
        log(f"[SUCCESS] Retrieved watch history: {watched_count} shows watched, {tvdb_count} with TVDB ID")
        
        try:
            from app import save_watch_history_cache
            save_watch_history_cache('tv', watch_history)
            log("[CACHE SAVED] TV watch history cached - next scan within 7 days will use this cache")
        except Exception as e:
            log(f"[WARNING] Could not save cache: {e}")
        
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


def format_size(size_bytes: int) -> str:
    """Format bytes to human readable string."""
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"


def calculate_priority_score(show: dict, has_requester: bool) -> dict:
    """Calculate deletion priority score (0-100). Higher = better candidate for deletion.
    
    Scoring factors:
    - Show ended: +30 points (no new episodes coming)
    - Never watched: +25 points (no interest from anyone)
    - Not requested: +20 points (no one specifically asked for it)
    - Large size (>50GB): +15 points, (>100GB): +20 points
    - Not monitored: +10 points (already disabled in Sonarr)
    """
    score = 0
    reasons = []
    
    if show.get("status") == "Ended":
        score += 30
        reasons.append("Ended series")
    
    if show.get("view_count", 0) == 0 and not show.get("last_watched"):
        score += 25
        reasons.append("Never watched")
    elif show.get("skip_reason") == "Not watched recently":
        score += 10
        reasons.append("Stale")
    
    if not has_requester:
        score += 20
        reasons.append("No requester")
    
    size_bytes = show.get("size_bytes", 0)
    size_gb = size_bytes / (1024 ** 3)
    if size_gb > 100:
        score += 20
        reasons.append(f"Very large ({size_gb:.0f}GB)")
    elif size_gb > 50:
        score += 15
        reasons.append(f"Large ({size_gb:.0f}GB)")
    elif size_gb > 20:
        score += 5
    
    if not show.get("monitored", True):
        score += 10
        reasons.append("Unmonitored")
    
    if score >= 70:
        priority_label = "High"
    elif score >= 40:
        priority_label = "Medium"
    else:
        priority_label = "Low"
    
    return {
        "score": score,
        "label": priority_label,
        "reasons": reasons
    }


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
    
    statistics = series.get("statistics", {})
    size_bytes = statistics.get("sizeOnDisk", 0)
    episode_count = statistics.get("episodeFileCount", 0)
    season_count = statistics.get("seasonCount", 0)
    
    # Extract Sonarr rating (TVdb score 0-10, convert to 0-100 pct)
    ratings_data = series.get('ratings', {}) or {}
    raw_rating = ratings_data.get('value')
    rating_pct = int(round(raw_rating * 10)) if raw_rating and raw_rating > 0 else None

    result = {
        "id": series_id,
        "title": title,
        "tvdb_id": tvdb_id,
        "status": get_show_status(series),
        "is_candidate": True,
        "skip_reason": None,
        "added_date": None,
        "last_watched": None,
        "view_count": 0,
        "size_bytes": size_bytes,
        "size_display": format_size(size_bytes),
        "episode_count": episode_count,
        "season_count": season_count,
        "monitored": series.get("monitored", False),
        "quality_profile": series.get("qualityProfileId", 0),
        "path": series.get("path", ""),
        "rating_pct": rating_pct,
        "rating_source": "tvdb" if rating_pct is not None else None,
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
        last_watched = watch_entry.get("last_watched")
        if isinstance(last_watched, datetime):
            result["last_watched"] = last_watched.replace(tzinfo=None).strftime("%Y-%m-%d")
        result["view_count"] = watch_entry.get("view_count", 0)
    
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
        last_watched = watch_entry.get("last_watched")
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
    
    exclusions = load_exclusions_from_db()
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
            has_requester = bool(analysis["requester_email"] or analysis["requester_name"])
            priority = calculate_priority_score(analysis, has_requester)
            analysis["priority_score"] = priority["score"]
            analysis["priority_label"] = priority["label"]
            analysis["priority_reasons"] = priority["reasons"]
            candidates.append(analysis)
        else:
            skipped.append(analysis)
    
    candidates.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    
    high_priority = sum(1 for c in candidates if c.get("priority_label") == "High")
    log(f"[INFO] Total shows analyzed: {len(series_list)}")
    log(f"[SUCCESS] Protected shows: {len(skipped)}")
    log(f"[WARNING] Deletion candidates: {len(candidates)} ({high_priority} high priority)")
    
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
        'QUARANTINE_PATH': get_setting('QUARANTINE_PATH', '').strip(),
    }
    
    deleted_count = 0
    excluded_count = 0
    quarantined_count = 0
    errors = []
    
    delete_actions = [a for a in actions if a.get('action') == 'delete']
    exclude_actions = [a for a in actions if a.get('action') == 'exclude']
    
    quarantine_mode = any(a.get('quarantine') for a in delete_actions)
    if quarantine_mode:
        log(f"[INFO] Quarantine mode enabled - files will be moved instead of deleted")
    
    log(f"[INFO] Processing {len(delete_actions)} deletions and {len(exclude_actions)} exclusions...")
    
    for action in exclude_actions:
        title = action.get('title', 'Unknown')
        if add_to_exclusions_db(title):
            log(f"[SUCCESS] Added '{title}' to exclusion list")
            excluded_count += 1
        else:
            log(f"[ERROR] Failed to add '{title}' to exclusion list")
            errors.append(f"Failed to exclude: {title}")
    
    for action in delete_actions:
        series_id = action.get('id')
        title = action.get('title', 'Unknown')
        delete_from_db = action.get('deleteFromSonarr', False)
        quarantine = action.get('quarantine', False)
        show_path = action.get('path', '')
        requester_email = action.get('requester_email', '')
        requester_name = action.get('requester_name', '')
        
        if not series_id:
            log(f"[ERROR] Missing series ID for '{title}'")
            errors.append(f"Missing ID: {title}")
            continue
        
        log(f"[INFO] Processing {'quarantine' if quarantine else 'deletion'}: {title}")
        
        plex_deleted = delete_from_plex(title, config, log)
        
        if quarantine and show_path:
            quarantine_path = config.get('QUARANTINE_PATH', '')
            if quarantine_path:
                if quarantine_files(show_path, quarantine_path, title, log):
                    quarantined_count += 1
                    sonarr_deleted = delete_from_sonarr(
                        int(series_id), title, config, log,
                        delete_files=False,
                        delete_from_database=delete_from_db
                    )
                else:
                    errors.append(f"Failed to quarantine: {title}")
                    continue
            else:
                log(f"[WARNING] Quarantine path not configured, falling back to delete for '{title}'")
                sonarr_deleted = delete_from_sonarr(
                    int(series_id), title, config, log,
                    delete_files=True,
                    delete_from_database=delete_from_db
                )
        else:
            sonarr_deleted = delete_from_sonarr(
                int(series_id), title, config, log,
                delete_files=True,
                delete_from_database=delete_from_db
            )
        
        if sonarr_deleted:
            deleted_count += 1
            
            record_deletion_history(
                title=title,
                sonarr_id=series_id,
                tvdb_id=action.get('tvdb_id'),
                size_bytes=action.get('size_bytes'),
                season_count=action.get('season_count'),
                episode_count=action.get('episode_count'),
                requester_name=requester_name,
                requester_email=requester_email,
                priority_score=action.get('priority_score'),
                priority_label=action.get('priority_label'),
                was_quarantined=quarantine and bool(config.get('QUARANTINE_PATH')),
                deleted_from_sonarr_db=delete_from_db
            )
            
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
    
    if quarantined_count > 0:
        log(f"[SUCCESS] Completed: {deleted_count} deleted ({quarantined_count} quarantined), {excluded_count} excluded")
    else:
        log(f"[SUCCESS] Completed: {deleted_count} deleted, {excluded_count} excluded")
    
    return {
        'deleted': deleted_count,
        'excluded': excluded_count,
        'quarantined': quarantined_count,
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
    smtp_password = config.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
    
    if not all([config.get("SMTP_HOST"), config.get("SMTP_USER"), 
                smtp_password, config.get("SMTP_FROM")]):
        log(f"[WARNING] SMTP not configured, skipping email")
        return False
    
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Grant's Plex Library <{config['SMTP_FROM']}>"
        msg["To"] = email
        msg["Subject"] = f"TV Show Removed: {show_title}"
        
        plain_body = f"""Hi {requester_name},

The TV show "{show_title}" has been removed from Grant's Plex library as part of regular maintenance.

You can request it again anytime through Ombi if you'd like it back!

If you want this show protected from future cleanup, just let Grant know and he'll add it to the exclusion list.

- Grant's Media Library"""

        html_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #1a1a2e;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #1a1a2e; padding: 40px 20px;">
        <tr>
            <td align="center">
                <table width="600" cellpadding="0" cellspacing="0" style="background: linear-gradient(135deg, #16213e 0%, #1a1a2e 100%); border-radius: 16px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.3);">
                    <!-- Header -->
                    <tr>
                        <td style="background: linear-gradient(135deg, #e94560 0%, #ff6b6b 100%); padding: 30px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">
                                📺 Show Removed
                            </h1>
                        </td>
                    </tr>
                    
                    <!-- Content -->
                    <tr>
                        <td style="padding: 40px 30px;">
                            <p style="color: #a0a0a0; font-size: 16px; margin: 0 0 20px 0;">
                                Hi <strong style="color: #ffffff;">{requester_name}</strong>,
                            </p>
                            
                            <div style="background: rgba(233, 69, 96, 0.1); border-left: 4px solid #e94560; padding: 20px; border-radius: 8px; margin: 25px 0;">
                                <p style="color: #ffffff; font-size: 18px; margin: 0; font-weight: 500;">
                                    "{show_title}"
                                </p>
                                <p style="color: #a0a0a0; font-size: 14px; margin: 8px 0 0 0;">
                                    has been removed from the library
                                </p>
                            </div>
                            
                            <p style="color: #c0c0c0; font-size: 15px; line-height: 1.6; margin: 25px 0;">
                                This was part of our regular library maintenance to free up space for new content. Don't worry though - you have options!
                            </p>
                            
                            <!-- Options Box -->
                            <table width="100%" cellpadding="0" cellspacing="0" style="margin: 30px 0;">
                                <tr>
                                    <td style="background: rgba(255,255,255,0.05); border-radius: 12px; padding: 25px;">
                                        <p style="color: #4ecca3; font-size: 14px; font-weight: 600; margin: 0 0 15px 0; text-transform: uppercase; letter-spacing: 1px;">
                                            🎬 Want it back?
                                        </p>
                                        <p style="color: #c0c0c0; font-size: 14px; line-height: 1.6; margin: 0 0 20px 0;">
                                            Simply request it again through Ombi and it'll be re-added to the library.
                                        </p>
                                        
                                        <p style="color: #f9d423; font-size: 14px; font-weight: 600; margin: 20px 0 15px 0; text-transform: uppercase; letter-spacing: 1px;">
                                            🛡️ Protect from future removal?
                                        </p>
                                        <p style="color: #c0c0c0; font-size: 14px; line-height: 1.6; margin: 0;">
                                            Let Grant know and he'll add it to the exclusion list so it won't be removed in future cleanups.
                                        </p>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Footer -->
                    <tr>
                        <td style="background: rgba(0,0,0,0.2); padding: 25px 30px; text-align: center;">
                            <p style="color: #666; font-size: 13px; margin: 0;">
                                Grant's Plex Library • Automated Notification
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
        
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
            server.starttls()
            server.login(config["SMTP_USER"], smtp_password)
            server.send_message(msg)
        
        log(f"[SUCCESS] Sent notification to {email}")
        return True
    except Exception as e:
        log(f"[ERROR] Failed to send email: {e}")
        return False


def send_test_email(config: dict) -> tuple:
    """Send a test email to verify SMTP configuration."""
    smtp_password = config.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
    
    if not all([config.get("SMTP_HOST"), config.get("SMTP_USER"), 
                smtp_password, config.get("SMTP_FROM")]):
        return False, "SMTP not fully configured"
    
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Grant's Plex Library <{config['SMTP_FROM']}>"
        msg["To"] = config["SMTP_FROM"]
        msg["Subject"] = "Test Email - Plex Cleanup Tool"
        
        plain_body = "This is a test email from your Plex cleanup tool. If you received this, email notifications are working!"
        
        html_body = """<!DOCTYPE html>
<html>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; background-color: #1a1a2e;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #1a1a2e; padding: 40px 20px;">
        <tr>
            <td align="center">
                <table width="500" cellpadding="0" cellspacing="0" style="background: linear-gradient(135deg, #16213e 0%, #1a1a2e 100%); border-radius: 16px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.3);">
                    <tr>
                        <td style="background: linear-gradient(135deg, #4ecca3 0%, #45b7aa 100%); padding: 30px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 24px;">✅ Test Successful!</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 40px 30px; text-align: center;">
                            <p style="color: #c0c0c0; font-size: 16px; line-height: 1.6; margin: 0;">
                                Email notifications are working correctly. When shows are removed, requesters will receive beautiful notifications like this.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td style="background: rgba(0,0,0,0.2); padding: 20px; text-align: center;">
                            <p style="color: #666; font-size: 13px; margin: 0;">Grant's Plex Library</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
        
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
            server.starttls()
            server.login(config["SMTP_USER"], smtp_password)
            server.send_message(msg)
        
        return True, f"Test email sent to {config['SMTP_FROM']}"
    except Exception as e:
        return False, str(e)


def run_cleanup_with_settings(get_setting: Callable, dry_run: bool = True, log_callback: Optional[Callable] = None) -> dict:
    """Legacy function - redirects to new scan function."""
    return scan_for_candidates(get_setting, log_callback)
