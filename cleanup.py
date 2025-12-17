#!/usr/bin/env python3
"""
TV Show Cleanup Tool for Plex, Sonarr, and Ombi
Intelligently removes unwatched shows while respecting exclusions and notifying requesters.
"""

import os
import sys
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional
import requests
from plexapi.server import PlexServer
from dotenv import load_dotenv

load_dotenv()

SONARR_URL = os.getenv("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
OMBI_URL = os.getenv("OMBI_URL", "").rstrip("/")
OMBI_API_KEY = os.getenv("OMBI_API_KEY", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")

SKIP_IF_ADDED_WITHIN_DAYS = int(os.getenv("SKIP_IF_ADDED_WITHIN_DAYS", "90"))
SKIP_IF_WATCHED_WITHIN_DAYS = int(os.getenv("SKIP_IF_WATCHED_WITHIN_DAYS", "180"))
DELETION_DELAY_SECONDS = float(os.getenv("DELETION_DELAY_SECONDS", "2.0"))

EXCLUSION_FILE = "excluded_shows.txt"


class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text.center(60)}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'='*60}{Colors.ENDC}\n")


def print_info(text: str):
    print(f"{Colors.CYAN}[INFO]{Colors.ENDC} {text}")


def print_success(text: str):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.ENDC} {text}")


def print_warning(text: str):
    print(f"{Colors.YELLOW}[WARNING]{Colors.ENDC} {text}")


def print_error(text: str):
    print(f"{Colors.RED}[ERROR]{Colors.ENDC} {text}")


def load_exclusions() -> set:
    exclusions = set()
    if os.path.exists(EXCLUSION_FILE):
        with open(EXCLUSION_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    exclusions.add(line.lower())
    return exclusions


def save_exclusion(show_title: str):
    with open(EXCLUSION_FILE, "a") as f:
        f.write(f"{show_title}\n")
    print_success(f"Added '{show_title}' to exclusion list")


def check_config() -> bool:
    missing = []
    if not SONARR_URL:
        missing.append("SONARR_URL")
    if not SONARR_API_KEY:
        missing.append("SONARR_API_KEY")
    if not PLEX_URL:
        missing.append("PLEX_TOKEN")
    if not PLEX_TOKEN:
        missing.append("PLEX_TOKEN")
    
    if missing:
        print_error("Missing required configuration:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set these environment variables or add them to a .env file")
        return False
    return True


def get_sonarr_series() -> list:
    print_info("Fetching series from Sonarr...")
    try:
        response = requests.get(
            f"{SONARR_URL}/api/v3/series",
            headers={"X-Api-Key": SONARR_API_KEY},
            timeout=30
        )
        response.raise_for_status()
        series = response.json()
        print_success(f"Found {len(series)} series in Sonarr")
        return series
    except requests.RequestException as e:
        print_error(f"Failed to fetch Sonarr series: {e}")
        return []


def get_plex_watch_history() -> dict:
    print_info("Fetching watch history from Plex...")
    watch_history = {}
    
    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        
        for section in plex.library.sections():
            if section.type == "show":
                for show in section.all():
                    last_watched = None
                    try:
                        for episode in show.episodes():
                            if episode.lastViewedAt:
                                if last_watched is None or episode.lastViewedAt > last_watched:
                                    last_watched = episode.lastViewedAt
                    except Exception:
                        pass
                    
                    if last_watched:
                        watch_history[show.title.lower()] = last_watched
        
        print_success(f"Retrieved watch history for {len(watch_history)} shows")
        return watch_history
    except Exception as e:
        print_error(f"Failed to connect to Plex: {e}")
        return {}


def get_ombi_requests() -> dict:
    if not OMBI_URL or not OMBI_API_KEY:
        print_warning("Ombi not configured, skipping requester lookup")
        return {}
    
    print_info("Fetching TV requests from Ombi...")
    try:
        response = requests.get(
            f"{OMBI_URL}/api/v1/Request/tv",
            headers={"ApiKey": OMBI_API_KEY},
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
        
        print_success(f"Found {len(requesters)} TV requests in Ombi")
        return requesters
    except requests.RequestException as e:
        print_warning(f"Failed to fetch Ombi requests: {e}")
        return {}


def get_show_status(series: dict) -> str:
    status = series.get("status", "Unknown")
    if status == "continuing":
        return "Continuing (Still Airing)"
    elif status == "ended":
        return "Ended"
    else:
        return status.capitalize() if status else "Unknown"


def should_skip_show(
    series: dict,
    exclusions: set,
    watch_history: dict,
    cutoff_added: datetime,
    cutoff_watched: datetime
) -> tuple[bool, str]:
    title = series.get("title", "Unknown")
    title_lower = title.lower()
    
    if title_lower in exclusions:
        return True, "In exclusion list"
    
    added_str = series.get("added", "")
    if added_str:
        try:
            added_date = datetime.fromisoformat(added_str.replace("Z", "+00:00"))
            if added_date.replace(tzinfo=None) > cutoff_added:
                days_ago = (datetime.now() - added_date.replace(tzinfo=None)).days
                return True, f"Added {days_ago} days ago (within {SKIP_IF_ADDED_WITHIN_DAYS} days)"
        except ValueError:
            pass
    
    if title_lower in watch_history:
        last_watched = watch_history[title_lower]
        if isinstance(last_watched, datetime):
            if last_watched.replace(tzinfo=None) > cutoff_watched:
                days_ago = (datetime.now() - last_watched.replace(tzinfo=None)).days
                return True, f"Watched {days_ago} days ago (within {SKIP_IF_WATCHED_WITHIN_DAYS} days)"
    
    return False, ""


def delete_from_plex(show_title: str) -> bool:
    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        
        for section in plex.library.sections():
            if section.type == "show":
                try:
                    show = section.get(show_title)
                    show.delete()
                    print_success(f"Deleted '{show_title}' from Plex")
                    return True
                except Exception:
                    continue
        
        print_warning(f"Show '{show_title}' not found in Plex")
        return False
    except Exception as e:
        print_error(f"Failed to delete '{show_title}' from Plex: {e}")
        return False


def delete_from_sonarr(series_id: int, show_title: str, delete_files: bool = True) -> bool:
    try:
        response = requests.delete(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers={"X-Api-Key": SONARR_API_KEY},
            params={"deleteFiles": str(delete_files).lower()},
            timeout=30
        )
        response.raise_for_status()
        print_success(f"Deleted '{show_title}' from Sonarr (files {'removed' if delete_files else 'kept'})")
        return True
    except requests.RequestException as e:
        print_error(f"Failed to delete '{show_title}' from Sonarr: {e}")
        return False


def send_notification_email(email: str, show_title: str, requester_name: str) -> bool:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM]):
        print_warning(f"SMTP not configured, skipping email to {email}")
        return False
    
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_FROM
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
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        
        print_success(f"Sent notification email to {email}")
        return True
    except Exception as e:
        print_error(f"Failed to send email to {email}: {e}")
        return False


def run_cleanup(dry_run: bool = True):
    print_header("TV Show Cleanup Tool")
    
    if not check_config():
        return
    
    mode = "DRY RUN" if dry_run else "LIVE MODE"
    print(f"{Colors.BOLD}Mode: {Colors.YELLOW if dry_run else Colors.RED}{mode}{Colors.ENDC}\n")
    
    cutoff_added = datetime.now() - timedelta(days=SKIP_IF_ADDED_WITHIN_DAYS)
    cutoff_watched = datetime.now() - timedelta(days=SKIP_IF_WATCHED_WITHIN_DAYS)
    
    exclusions = load_exclusions()
    print_info(f"Loaded {len(exclusions)} shows from exclusion list")
    
    series_list = get_sonarr_series()
    if not series_list:
        print_error("No series found in Sonarr. Exiting.")
        return
    
    watch_history = get_plex_watch_history()
    ombi_requesters = get_ombi_requests()
    
    print_header("Analyzing Shows")
    
    candidates = []
    skipped = []
    
    for series in series_list:
        title = series.get("title", "Unknown")
        series_id = series.get("id")
        tvdb_id = series.get("tvdbId")
        status = get_show_status(series)
        
        skip, reason = should_skip_show(
            series, exclusions, watch_history, cutoff_added, cutoff_watched
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
    
    print(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
    print(f"  Total shows: {len(series_list)}")
    print(f"  {Colors.GREEN}Skipped (protected): {len(skipped)}{Colors.ENDC}")
    print(f"  {Colors.RED}Candidates for deletion: {len(candidates)}{Colors.ENDC}")
    
    if not candidates:
        print_success("\nNo shows to delete. Your library is well-maintained!")
        return
    
    print_header("Deletion Candidates")
    
    for i, show in enumerate(candidates, 1):
        status_color = Colors.GREEN if "Continuing" in show["status"] else Colors.YELLOW
        print(f"\n{Colors.BOLD}{i}. {show['title']}{Colors.ENDC}")
        print(f"   Status: {status_color}{show['status']}{Colors.ENDC}")
        if show["requester_email"]:
            print(f"   Requested by: {show['requester_name']} ({show['requester_email']})")
        else:
            print(f"   Requested by: Unknown/Not via Ombi")
    
    print_header("Manual Review")
    
    final_candidates = []
    for show in candidates:
        while True:
            response = input(f"Exclude '{show['title']}'? (yes/no/skip): ").strip().lower()
            if response in ["yes", "y"]:
                save_exclusion(show["title"])
                exclusions.add(show["title"].lower())
                break
            elif response in ["no", "n"]:
                final_candidates.append(show)
                break
            elif response in ["skip", "s"]:
                print_info(f"Skipping '{show['title']}' for this run only")
                break
            else:
                print("Please enter 'yes', 'no', or 'skip'")
    
    if not final_candidates:
        print_success("\nNo shows selected for deletion!")
        return
    
    print_header(f"{'Test Results' if dry_run else 'Deletion Phase'}")
    
    print(f"\n{Colors.BOLD}Shows to be deleted ({len(final_candidates)}):{Colors.ENDC}")
    for show in final_candidates:
        print(f"  - {show['title']}")
    
    if dry_run:
        print(f"\n{Colors.YELLOW}{Colors.BOLD}This was a DRY RUN - no changes were made.{Colors.ENDC}")
        print("To perform actual deletion, run with --execute flag")
        return
    
    confirm = input(f"\n{Colors.RED}Are you SURE you want to delete these {len(final_candidates)} shows? (type 'DELETE' to confirm): {Colors.ENDC}")
    if confirm != "DELETE":
        print_info("Deletion cancelled")
        return
    
    print_header("Executing Deletions")
    
    deleted_count = 0
    for show in final_candidates:
        print(f"\n{Colors.BOLD}Processing: {show['title']}{Colors.ENDC}")
        
        delete_from_plex(show["title"])
        
        if delete_from_sonarr(show["id"], show["title"]):
            deleted_count += 1
            
            if show["requester_email"]:
                send_notification_email(
                    show["requester_email"],
                    show["title"],
                    show["requester_name"]
                )
        
        print_info(f"Waiting {DELETION_DELAY_SECONDS}s before next deletion...")
        time.sleep(DELETION_DELAY_SECONDS)
    
    print_header("Cleanup Complete")
    print_success(f"Successfully deleted {deleted_count} of {len(final_candidates)} shows")


def main():
    print_header("TV Show Cleanup Tool - Setup")
    
    print(f"""
{Colors.BOLD}Current Configuration:{Colors.ENDC}
  - Skip shows added within: {SKIP_IF_ADDED_WITHIN_DAYS} days
  - Skip shows watched within: {SKIP_IF_WATCHED_WITHIN_DAYS} days
  - Deletion delay: {DELETION_DELAY_SECONDS} seconds

{Colors.BOLD}Required Environment Variables:{Colors.ENDC}
  - SONARR_URL: {'Configured' if SONARR_URL else 'Not set'}
  - SONARR_API_KEY: {'Configured' if SONARR_API_KEY else 'Not set'}
  - PLEX_URL: {'Configured' if PLEX_URL else 'Not set'}
  - PLEX_TOKEN: {'Configured' if PLEX_TOKEN else 'Not set'}
  
{Colors.BOLD}Optional Environment Variables:{Colors.ENDC}
  - OMBI_URL: {'Configured' if OMBI_URL else 'Not set'}
  - OMBI_API_KEY: {'Configured' if OMBI_API_KEY else 'Not set'}
  - SMTP_HOST: {'Configured' if SMTP_HOST else 'Not set'}
  - SMTP_PORT: {SMTP_PORT}
  - SMTP_USER: {'Configured' if SMTP_USER else 'Not set'}
  - SMTP_PASSWORD: {'Configured' if SMTP_PASSWORD else 'Not set'}
  - SMTP_FROM: {'Configured' if SMTP_FROM else 'Not set'}
""")
    
    if "--execute" in sys.argv:
        run_cleanup(dry_run=False)
    else:
        print(f"{Colors.CYAN}Running in DRY RUN mode (safe - no changes will be made){Colors.ENDC}")
        print(f"To execute deletions, run with: python cleanup.py --execute\n")
        run_cleanup(dry_run=True)


if __name__ == "__main__":
    main()
