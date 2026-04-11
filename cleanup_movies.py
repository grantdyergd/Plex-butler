"""
Cleanup logic for movies using Radarr.
This module provides movie cleanup functionality that can be called from the Flask app.
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


def load_movie_exclusions_from_db() -> set:
    """Load movie exclusions from database. Returns set of (lowercase title, year) tuples."""
    try:
        from app import MovieExclusion
        exclusions = set()
        for e in MovieExclusion.query.all():
            exclusions.add((e.title.lower(), e.year))
        return exclusions
    except Exception:
        return set()


def add_movie_to_exclusions_db(title: str, year: int = None, tmdb_id: int = None) -> bool:
    """Add a movie to the database exclusion list."""
    try:
        from app import MovieExclusion, db
        existing = MovieExclusion.query.filter(
            db.func.lower(MovieExclusion.title) == title.lower(),
            MovieExclusion.year == year
        ).first()
        if not existing:
            new_exclusion = MovieExclusion(title=title, year=year, tmdb_id=tmdb_id)
            db.session.add(new_exclusion)
            db.session.commit()
        return True
    except Exception:
        return False


def record_movie_deletion_history(
    title: str,
    radarr_id: int = None,
    tmdb_id: int = None,
    imdb_id: str = None,
    year: int = None,
    size_bytes: int = None,
    runtime_minutes: int = None,
    requester_name: str = None,
    requester_email: str = None,
    priority_score: int = None,
    priority_label: str = None,
    was_quarantined: bool = False,
    deleted_from_radarr_db: bool = False
) -> bool:
    """Record a movie deletion in the history database."""
    try:
        from app import DeletionHistory, db
        record = DeletionHistory(
            media_type='movie',
            title=title,
            radarr_id=radarr_id,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            year=year,
            size_bytes=size_bytes,
            runtime_minutes=runtime_minutes,
            requester_name=requester_name,
            requester_email=requester_email,
            priority_score=priority_score,
            priority_label=priority_label,
            was_quarantined=was_quarantined,
            deleted_from_radarr_db=deleted_from_radarr_db
        )
        db.session.add(record)
        db.session.commit()
        return True
    except Exception:
        return False


def quarantine_movie_files(source_path: str, quarantine_path: str, title: str, log: Callable) -> bool:
    """Move movie files to quarantine folder instead of deleting them."""
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


def get_radarr_movies(config: dict, log: Callable) -> list:
    """Fetch all movies from Radarr."""
    log("[INFO] Fetching movies from Radarr...")
    try:
        response = requests.get(
            f"{config['RADARR_URL']}/api/v3/movie",
            headers={"X-Api-Key": config["RADARR_API_KEY"]},
            timeout=30
        )
        response.raise_for_status()
        movies = response.json()
        log(f"[SUCCESS] Found {len(movies)} movies in Radarr")
        return movies
    except requests.RequestException as e:
        log(f"[ERROR] Failed to fetch Radarr movies: {e}")
        return []


def extract_tmdb_id_from_guid(guid: str) -> Optional[int]:
    """Extract TMDB ID from Plex GUID."""
    if not guid:
        return None
    patterns = [
        r'themoviedb://(\d+)',
        r'tmdb://(\d+)',
        r'com\.plexapp\.agents\.themoviedb://(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, guid)
        if match:
            return int(match.group(1))
    return None


def extract_imdb_id_from_guid(guid: str) -> Optional[str]:
    """Extract IMDB ID from Plex GUID."""
    if not guid:
        return None
    patterns = [
        r'imdb://([t]{2}\d+)',
        r'com\.plexapp\.agents\.imdb://([t]{2}\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, guid)
        if match:
            return match.group(1)
    return None


def get_plex_movie_watch_history(config: dict, log: Callable, limit: int = 0, force_refresh: bool = False) -> dict:
    """Get movie watch history from Plex using server-level history for ALL users.
    
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
            cached = load_watch_history_cache('movie')
            if cached:
                log(f"[CACHE HIT] Using cached movie watch history - {cached['age_days']} day(s) old (cached on {cached['scanned_at'][:10]})")
                log(f"[CACHE HIT] Skipping Plex scan - cache valid for {7 - cached['age_days']} more day(s)")
                return cached['history']
            else:
                log("[CACHE MISS] No valid movie cache found - will fetch fresh data from Plex")
        except Exception as e:
            log(f"[WARNING] Could not check cache: {e}")
    
    log("[FRESH SCAN] Fetching movie watch history from Plex (all users) - this may take a minute...")
    
    try:
        plex = PlexServer(config["PLEX_URL"], config["PLEX_TOKEN"], timeout=120)
        
        movie_last_watched = {}
        movie_view_counts = {}
        movie_tmdb_ids = {}
        movie_titles = {}
        
        log("[INFO] Fetching movie watch history for ALL server accounts (admin + shared users)...")
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
                        if item.type != 'movie':
                            continue

                        movie_key = item.ratingKey
                        movie_title = item.title
                        watched_at = item.viewedAt if hasattr(item, 'viewedAt') else None

                        movie_titles[movie_key] = movie_title
                        movie_view_counts[movie_key] = movie_view_counts.get(movie_key, 0) + 1
                        user_entries += 1

                        if watched_at:
                            if movie_key not in movie_last_watched or watched_at > movie_last_watched[movie_key]:
                                movie_last_watched[movie_key] = watched_at

                        if hasattr(item, 'guids'):
                            for guid in item.guids:
                                guid_str = str(guid.id) if hasattr(guid, 'id') else str(guid)
                                tmdb_id = extract_tmdb_id_from_guid(guid_str)
                                if tmdb_id:
                                    movie_tmdb_ids[movie_key] = tmdb_id
                                    break
                        elif hasattr(item, 'guid'):
                            tmdb_id = extract_tmdb_id_from_guid(str(item.guid))
                            if tmdb_id:
                                movie_tmdb_ids[movie_key] = tmdb_id

                    except Exception:
                        continue

                total_entries += user_entries
                if user_entries > 0:
                    log(f"[INFO] {label}: {user_entries} movie plays")
            except Exception as e:
                label = getattr(account, 'name', str(account)) if account is not None else 'server'
                log(f"[WARNING] Could not get history for {label}: {e}")
                continue

        log(f"[INFO] Merged {total_entries} total movie plays across all users — {len(movie_view_counts)} unique movies watched")
        
        for movie_key in movie_view_counts:
            tmdb_id = movie_tmdb_ids.get(movie_key)
            title = movie_titles.get(movie_key, "Unknown")
            last_watched = movie_last_watched.get(movie_key)
            view_count = movie_view_counts.get(movie_key, 0)
            
            last_watched_str = last_watched.isoformat() if last_watched else None
            if tmdb_id:
                watch_history[f"tmdb:{tmdb_id}"] = {
                    'last_watched': last_watched_str,
                    'view_count': view_count,
                    'title': title
                }
            
            watch_history[f"title:{title.lower()}"] = {
                'last_watched': last_watched_str,
                'view_count': view_count,
                'title': title
            }
        
        log(f"[SUCCESS] Loaded watch history for {len(movie_view_counts)} movies")
        
        try:
            from app import save_watch_history_cache
            save_watch_history_cache('movie', watch_history)
            log("[CACHE SAVED] Movie watch history cached - next scan within 7 days will use this cache")
        except Exception as e:
            log(f"[WARNING] Could not save cache: {e}")
        
        return watch_history
        
    except Exception as e:
        log(f"[ERROR] Failed to connect to Plex: {e}")
        return {}


def get_ombi_movie_requesters(config: dict, log: Callable) -> dict:
    """Get movie requesters from Ombi."""
    requesters = {}
    
    if not config.get("OMBI_URL") or not config.get("OMBI_API_KEY"):
        log("[INFO] Ombi not configured - requester info won't be available")
        return {}
    
    log("[INFO] Fetching movie requests from Ombi...")
    
    try:
        response = requests.get(
            f"{config['OMBI_URL']}/api/v1/Request/movie",
            headers={
                "ApiKey": config["OMBI_API_KEY"],
                "Content-Type": "application/json"
            },
            timeout=30
        )
        response.raise_for_status()
        requests_data = response.json()
        
        for req in requests_data:
            tmdb_id = req.get('theMovieDbId')
            imdb_id = req.get('imdbId')
            title = req.get('title', '')
            
            requester_name = req.get('requestedUser', {}).get('userName', '')
            requester_email = req.get('requestedUser', {}).get('email', '')
            
            if not requester_name:
                requester_name = req.get('requestedByAlias', '')
            
            if tmdb_id:
                requesters[f"tmdb:{tmdb_id}"] = {
                    'name': requester_name,
                    'email': requester_email,
                    'title': title
                }
            if imdb_id:
                requesters[f"imdb:{imdb_id}"] = {
                    'name': requester_name,
                    'email': requester_email,
                    'title': title
                }
            if title:
                requesters[f"title:{title.lower()}"] = {
                    'name': requester_name,
                    'email': requester_email,
                    'title': title
                }
        
        log(f"[SUCCESS] Found {len(requests_data)} movie requests in Ombi")
        return requesters
        
    except requests.RequestException as e:
        log(f"[WARNING] Failed to fetch Ombi movie requests: {e}")
        return {}


def calculate_movie_priority(movie: dict, watch_info: dict, requester: dict, config: dict) -> tuple:
    """Calculate priority score (0-100) for a movie deletion candidate.
    
    Higher score = higher priority for deletion
    """
    score = 0
    reasons = []
    
    size_bytes = movie.get('sizeOnDisk', 0)
    size_gb = size_bytes / (1024**3)
    if size_gb >= 50:
        score += 25
        reasons.append(f"Very large ({size_gb:.1f}GB)")
    elif size_gb >= 20:
        score += 15
        reasons.append(f"Large ({size_gb:.1f}GB)")
    elif size_gb >= 10:
        score += 10
        reasons.append(f"Medium ({size_gb:.1f}GB)")
    else:
        score += 5
        reasons.append(f"Small ({size_gb:.1f}GB)")
    
    view_count = watch_info.get('view_count', 0)
    if view_count == 0:
        score += 30
        reasons.append("Never watched")
    elif view_count == 1:
        score += 15
        reasons.append("Watched once")
    else:
        score += 5
        reasons.append(f"Watched {view_count}x")
    
    if not requester.get('name') and not requester.get('email'):
        score += 20
        reasons.append("No requester")
    else:
        score += 5
        reasons.append(f"Requested by {requester.get('name', 'someone')}")
    
    added_date = movie.get('added')
    if added_date:
        try:
            if isinstance(added_date, str):
                added_dt = datetime.fromisoformat(added_date.replace('Z', '+00:00'))
            else:
                added_dt = added_date
            
            days_old = (datetime.now(added_dt.tzinfo) - added_dt).days
            if days_old > 365:
                score += 15
                reasons.append(f"Added {days_old // 30} months ago")
            elif days_old > 180:
                score += 10
                reasons.append(f"Added {days_old} days ago")
            else:
                score += 5
                reasons.append(f"Added {days_old} days ago")
        except Exception:
            pass
    
    last_watched = watch_info.get('last_watched')
    if last_watched:
        try:
            if isinstance(last_watched, datetime):
                days_since = (datetime.now(last_watched.tzinfo) - last_watched).days
            else:
                last_dt = datetime.fromisoformat(str(last_watched).replace('Z', '+00:00'))
                days_since = (datetime.now(last_dt.tzinfo) - last_dt).days
            
            if days_since > 365:
                score += 10
                reasons.append(f"Last watched {days_since // 30} months ago")
        except Exception:
            pass
    
    score = min(100, max(0, score))
    
    if score >= 70:
        label = "High"
    elif score >= 40:
        label = "Medium"
    else:
        label = "Low"
    
    return score, label, reasons


def scan_movies_for_cleanup(config: dict, log: Callable) -> list:
    """Scan Radarr library and return deletion candidates."""
    
    movies = get_radarr_movies(config, log)
    if not movies:
        log("[ERROR] No movies found in Radarr")
        return []
    
    test_limit_val = config.get('TEST_MODE_LIMIT', '0') or '0'
    test_limit = int(test_limit_val) if str(test_limit_val).isdigit() else 0
    if test_limit > 0:
        movies = movies[:test_limit]
        log(f"[INFO] Test mode: limiting to {test_limit} movies")
    
    watch_history = get_plex_movie_watch_history(config, log)
    requesters = get_ombi_movie_requesters(config, log)
    exclusions = load_movie_exclusions_from_db()
    
    skip_added_val = config.get('SKIP_IF_ADDED_WITHIN_DAYS', '90') or '90'
    skip_watched_val = config.get('SKIP_IF_WATCHED_WITHIN_DAYS', '180') or '180'
    skip_added_days = int(skip_added_val) if str(skip_added_val).isdigit() else 90
    skip_watched_days = int(skip_watched_val) if str(skip_watched_val).isdigit() else 180
    
    candidates = []
    now = datetime.utcnow()
    
    log(f"[INFO] Analyzing {len(movies)} movies...")
    
    for movie in movies:
        title = movie.get('title', 'Unknown')
        year = movie.get('year')
        tmdb_id = movie.get('tmdbId')
        imdb_id = movie.get('imdbId')
        
        if (title.lower(), year) in exclusions:
            continue
        
        for exc_title, exc_year in exclusions:
            if title.lower() == exc_title and (exc_year is None or year == exc_year):
                continue
        
        added_date = movie.get('added')
        if added_date:
            try:
                if isinstance(added_date, str):
                    added_dt = datetime.fromisoformat(added_date.replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    added_dt = added_date.replace(tzinfo=None)
                
                if (now - added_dt).days < skip_added_days:
                    continue
            except Exception:
                pass
        
        watch_info = {}
        if tmdb_id and f"tmdb:{tmdb_id}" in watch_history:
            watch_info = watch_history[f"tmdb:{tmdb_id}"]
        elif f"title:{title.lower()}" in watch_history:
            watch_info = watch_history[f"title:{title.lower()}"]
        
        last_watched = watch_info.get('last_watched')
        view_count = watch_info.get('view_count', 0)
        
        is_candidate = False
        reason = ""
        
        if view_count == 0:
            is_candidate = True
            reason = "Never watched"
        elif last_watched:
            try:
                if isinstance(last_watched, datetime):
                    last_watched_dt = last_watched.replace(tzinfo=None)
                else:
                    last_watched_dt = datetime.fromisoformat(str(last_watched).replace('Z', '+00:00')).replace(tzinfo=None)
                
                if (now - last_watched_dt).days > skip_watched_days:
                    is_candidate = True
                    reason = f"Not watched in {(now - last_watched_dt).days} days"
            except Exception:
                pass
        
        if not is_candidate:
            continue
        
        requester = {}
        if tmdb_id and f"tmdb:{tmdb_id}" in requesters:
            requester = requesters[f"tmdb:{tmdb_id}"]
        elif imdb_id and f"imdb:{imdb_id}" in requesters:
            requester = requesters[f"imdb:{imdb_id}"]
        elif f"title:{title.lower()}" in requesters:
            requester = requesters[f"title:{title.lower()}"]
        
        priority_score, priority_label, priority_reasons = calculate_movie_priority(
            movie, watch_info, requester, config
        )
        
        candidates.append({
            'id': movie.get('id'),
            'title': title,
            'year': year,
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id,
            'path': movie.get('path', ''),
            'size_bytes': movie.get('sizeOnDisk', 0),
            'runtime_minutes': movie.get('runtime', 0),
            'has_file': movie.get('hasFile', False),
            'monitored': movie.get('monitored', True),
            'added': added_date,
            'last_watched': last_watched.isoformat() if isinstance(last_watched, datetime) else last_watched,
            'view_count': view_count,
            'reason': reason,
            'requester_name': requester.get('name', ''),
            'requester_email': requester.get('email', ''),
            'priority_score': priority_score,
            'priority_label': priority_label,
            'priority_reasons': priority_reasons
        })
    
    candidates.sort(key=lambda x: (-x['priority_score'], x['title']))
    
    log(f"[SUCCESS] Found {len(candidates)} movie deletion candidates")
    return candidates


def delete_movie_from_radarr(config: dict, movie_id: int, delete_files: bool = True, delete_from_database: bool = False, log: Callable = print) -> bool:
    """Delete a movie from Radarr."""
    try:
        params = {
            'deleteFiles': str(delete_files).lower(),
            'addImportExclusion': str(delete_from_database).lower()
        }
        
        response = requests.delete(
            f"{config['RADARR_URL']}/api/v3/movie/{movie_id}",
            headers={"X-Api-Key": config["RADARR_API_KEY"]},
            params=params,
            timeout=30
        )
        response.raise_for_status()
        log(f"[SUCCESS] Deleted movie ID {movie_id} from Radarr")
        return True
    except requests.RequestException as e:
        log(f"[ERROR] Failed to delete movie from Radarr: {e}")
        return False


def send_movie_notification_email(
    recipient_email: str,
    movie_title: str,
    requester_name: str,
    config: dict,
    log: Callable = print
) -> bool:
    """Send notification email to movie requester about deletion."""
    smtp_host = config.get('SMTP_HOST')
    smtp_port_val = config.get('SMTP_PORT', '587') or '587'
    smtp_port = int(smtp_port_val) if str(smtp_port_val).isdigit() else 587
    smtp_user = config.get('SMTP_USER')
    smtp_password = config.get('SMTP_PASSWORD')
    smtp_from = config.get('SMTP_FROM')
    
    if not all([smtp_host, smtp_user, smtp_password, smtp_from, recipient_email]):
        log("[INFO] Email not configured or no recipient - skipping notification")
        return False
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Movie Removed: {movie_title}"
        msg['From'] = smtp_from
        msg['To'] = recipient_email
        
        greeting = f"Hi {requester_name}," if requester_name else "Hi,"
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; margin: 0; padding: 0; background-color: #0f1729; color: #e2e8f0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 12px 12px 0 0; text-align: center; }}
                .header h1 {{ margin: 0; color: white; font-size: 24px; }}
                .content {{ background-color: #1e293b; padding: 30px; border-radius: 0 0 12px 12px; }}
                .movie-title {{ color: #f472b6; font-size: 20px; font-weight: bold; margin: 15px 0; }}
                .info-box {{ background-color: #334155; border-radius: 8px; padding: 15px; margin: 20px 0; }}
                .footer {{ text-align: center; margin-top: 20px; color: #64748b; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Movie Removed from Library</h1>
                </div>
                <div class="content">
                    <p>{greeting}</p>
                    <p>The following movie has been removed from our Plex library as part of routine cleanup:</p>
                    <div class="movie-title">{movie_title}</div>
                    <div class="info-box">
                        <p><strong>Want to watch it again?</strong></p>
                        <p>You can re-request this movie through Ombi at any time.</p>
                        <p>If you'd like this movie to be permanently protected from cleanup, please contact Grant.</p>
                    </div>
                    <p>Thanks for using the Plex server!</p>
                </div>
                <div class="footer">
                    <p>This is an automated message from the Plex cleanup system.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_content = f"""
{greeting}

The following movie has been removed from our Plex library:

{movie_title}

You can re-request this movie through Ombi at any time.

Thanks for using the Plex server!
        """
        
        msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))
        
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        log(f"[SUCCESS] Notification email sent to {recipient_email}")
        return True
        
    except Exception as e:
        log(f"[ERROR] Failed to send email: {e}")
        return False


def execute_movie_actions(
    actions: list,
    config: dict,
    log: Callable = print,
    quarantine: bool = False,
    delete_from_db: bool = False
) -> dict:
    """Execute selected movie deletion actions."""
    
    results = {
        'deleted': 0,
        'excluded': 0,
        'errors': []
    }
    
    delete_actions = [a for a in actions if a.get('action') == 'delete']
    exclude_actions = [a for a in actions if a.get('action') == 'exclude']
    
    for action in exclude_actions:
        title = action.get('title', '')
        year = action.get('year')
        tmdb_id = action.get('tmdb_id')
        if add_movie_to_exclusions_db(title, year, tmdb_id):
            results['excluded'] += 1
            log(f"[SUCCESS] Added '{title}' ({year}) to movie exclusion list")
        else:
            results['errors'].append(f"Failed to exclude: {title}")
    
    for i, action in enumerate(delete_actions):
        movie_id = action.get('id')
        title = action.get('title', 'Unknown')
        year = action.get('year')
        path = action.get('path', '')
        requester_email = action.get('requester_email', '')
        requester_name = action.get('requester_name', '')
        
        log(f"[INFO] Processing deletion {i+1}/{len(delete_actions)}: {title} ({year})")
        
        if quarantine and config.get('QUARANTINE_PATH'):
            quarantine_movie_files(path, config['QUARANTINE_PATH'], title, log)
        
        radarr_deleted = delete_movie_from_radarr(
            config,
            movie_id,
            delete_files=True,
            delete_from_database=delete_from_db,
            log=log
        )
        
        if radarr_deleted:
            results['deleted'] += 1
            
            record_movie_deletion_history(
                title=title,
                radarr_id=movie_id,
                tmdb_id=action.get('tmdb_id'),
                imdb_id=action.get('imdb_id'),
                year=year,
                size_bytes=action.get('size_bytes'),
                runtime_minutes=action.get('runtime_minutes'),
                requester_name=requester_name,
                requester_email=requester_email,
                priority_score=action.get('priority_score'),
                priority_label=action.get('priority_label'),
                was_quarantined=quarantine and bool(config.get('QUARANTINE_PATH')),
                deleted_from_radarr_db=delete_from_db
            )
            
            if requester_email:
                send_movie_notification_email(
                    requester_email,
                    title,
                    requester_name,
                    config,
                    log
                )
        else:
            results['errors'].append(f"Failed to delete: {title}")
        
        if len(delete_actions) > 1 and i < len(delete_actions) - 1:
            delay = float(config.get('DELETION_DELAY_SECONDS', 2.0))
            log(f"[INFO] Waiting {delay}s before next deletion...")
            time.sleep(delay)
    
    return results
