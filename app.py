import os
import re
import json
import secrets
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import threading
from datetime import datetime, timedelta
from openai import OpenAI

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SESSION_SECRET', os.urandom(24).hex())
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 1800,
}

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

cleanup_lock = threading.Lock()
cleanup_status = {
    'running': False,
    'phase': None,
    'last_run': None,
    'last_result': None,
    'candidates': [],
    'skipped': [],
    'log': [],
    'started_at': None
}

CLEANUP_TIMEOUT_SECONDS = 600  # 10 minute timeout for stale operations

openai_client = OpenAI(
    api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"),
    base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    is_secret = db.Column(db.Boolean, default=False)


class Exclusion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), unique=True, nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    excluded_by = db.Column(db.String(50), default='admin')
    excluded_by_name = db.Column(db.String(200), nullable=True)
    excluded_by_email = db.Column(db.String(200), nullable=True)
    original_requester_name = db.Column(db.String(200), nullable=True)
    original_requester_email = db.Column(db.String(200), nullable=True)


class DeletionHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    media_type = db.Column(db.String(20), default='tv')  # 'tv' or 'movie'
    title = db.Column(db.String(500), nullable=False)
    sonarr_id = db.Column(db.Integer, nullable=True)
    radarr_id = db.Column(db.Integer, nullable=True)
    tvdb_id = db.Column(db.Integer, nullable=True)
    tmdb_id = db.Column(db.Integer, nullable=True)
    imdb_id = db.Column(db.String(20), nullable=True)
    year = db.Column(db.Integer, nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    season_count = db.Column(db.Integer, nullable=True)
    episode_count = db.Column(db.Integer, nullable=True)
    runtime_minutes = db.Column(db.Integer, nullable=True)
    requester_name = db.Column(db.String(200), nullable=True)
    requester_email = db.Column(db.String(200), nullable=True)
    priority_score = db.Column(db.Integer, nullable=True)
    priority_label = db.Column(db.String(20), nullable=True)
    was_quarantined = db.Column(db.Boolean, default=False)
    deleted_from_sonarr_db = db.Column(db.Boolean, default=False)
    deleted_from_radarr_db = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow)


class MovieExclusion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    year = db.Column(db.Integer, nullable=True)
    tmdb_id = db.Column(db.Integer, nullable=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    excluded_by = db.Column(db.String(50), default='admin')
    excluded_by_name = db.Column(db.String(200), nullable=True)
    excluded_by_email = db.Column(db.String(200), nullable=True)
    original_requester_name = db.Column(db.String(200), nullable=True)
    original_requester_email = db.Column(db.String(200), nullable=True)


class EmailHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    media_type = db.Column(db.String(20), default='tv')
    media_title = db.Column(db.String(500), nullable=False)
    action_type = db.Column(db.String(50), default='exclusion')
    recipient_name = db.Column(db.String(200), nullable=True)
    recipient_email = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(500), nullable=False)
    body_html = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    was_successful = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text, nullable=True)


class RequesterReviewToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    requester_email = db.Column(db.String(200), nullable=False)
    requester_name = db.Column(db.String(200), nullable=True)
    items_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False)


class MediaExpiration(db.Model):
    """Tracks expiration date for each media item; auto-deletion target."""
    id = db.Column(db.Integer, primary_key=True)
    media_type = db.Column(db.String(10), nullable=False)  # 'show' | 'movie'
    service_id = db.Column(db.Integer, nullable=False)     # Sonarr series id or Radarr movie id
    tmdb_id = db.Column(db.Integer, nullable=True, index=True)
    tvdb_id = db.Column(db.Integer, nullable=True, index=True)
    imdb_id = db.Column(db.String(20), nullable=True)
    title = db.Column(db.String(500), nullable=False)
    year = db.Column(db.Integer, nullable=True)
    requester_email = db.Column(db.String(200), nullable=True)
    requester_name = db.Column(db.String(200), nullable=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_warning_sent_at = db.Column(db.DateTime, nullable=True)
    warning_count = db.Column(db.Integer, default=0)
    permanent = db.Column(db.Boolean, default=False)         # never auto-delete
    extension_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='active', index=True)  # active|extended|permanent|deleted|missing
    deleted_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    additional_requester_emails = db.Column(db.Text, nullable=True)  # comma-separated extra emails
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_warning_status = db.Column(db.String(20), nullable=True)    # sent|failed|skipped|no_email
    last_warning_error = db.Column(db.Text, nullable=True)
    requester_lookup_attempts = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('media_type', 'service_id', name='uq_expiration_type_id'),)


class ExpirationActionToken(db.Model):
    """One-time token sent in warning email; lets requester decide what to do."""
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expiration_id = db.Column(db.Integer, db.ForeignKey('media_expiration.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    action_taken = db.Column(db.String(20), nullable=True)   # extend|keep|delete


class OmbiIntroEmailLog(db.Model):
    """Tracks Ombi requesters we've already sent the intro/rules email to."""
    id = db.Column(db.Integer, primary_key=True)
    requester_email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    requester_name = db.Column(db.String(200), nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    ombi_user_id = db.Column(db.String(100), nullable=True, index=True)


class DeletedMediaArchive(db.Model):
    """Breadcrumb archive of every expiration-driven deletion (real or dry-run) so admin can re-request later."""
    id = db.Column(db.Integer, primary_key=True)
    media_type = db.Column(db.String(10), nullable=False, index=True)
    service_id = db.Column(db.Integer, nullable=True)
    tvdb_id = db.Column(db.Integer, nullable=True)
    tmdb_id = db.Column(db.Integer, nullable=True)
    imdb_id = db.Column(db.String(20), nullable=True)
    title = db.Column(db.String(500), nullable=False)
    year = db.Column(db.Integer, nullable=True)
    requester_email = db.Column(db.String(200), nullable=True)
    requester_name = db.Column(db.String(200), nullable=True)
    original_added_at = db.Column(db.DateTime, nullable=True)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    deleted_by = db.Column(db.String(50), nullable=False)  # auto-expiration | dry-run | admin-delete-now | bulk-delete
    dry_run = db.Column(db.Boolean, default=False, index=True)
    re_requested_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)


class ScanCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_type = db.Column(db.String(20), nullable=False)  # 'tv' or 'movie'
    candidates_json = db.Column(db.Text, nullable=True)
    skipped_json = db.Column(db.Text, nullable=True)
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)


class WatchHistoryCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    media_type = db.Column(db.String(20), nullable=False)  # 'tv' or 'movie'
    history_json = db.Column(db.Text, nullable=True)
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)


WATCH_HISTORY_CACHE_DAYS = 7


class WatchlistSyncItem(db.Model):
    """Tracks every Plex watchlist item we have seen and attempted to sync to Sonarr/Radarr."""
    id = db.Column(db.Integer, primary_key=True)
    plex_guid = db.Column(db.String(300), unique=True, nullable=False, index=True)
    plex_rating_key = db.Column(db.String(100), nullable=True)
    title = db.Column(db.String(500), nullable=False)
    year = db.Column(db.Integer, nullable=True)
    media_type = db.Column(db.String(10), nullable=False)  # movie | show
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=True, index=True)  # added|already_exists|failed|skipped
    status_message = db.Column(db.Text, nullable=True)
    removed_watched_at = db.Column(db.DateTime, nullable=True)  # set when removed from watchlist after being watched


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def is_setup_complete():
    return User.query.first() is not None


def get_setting(key, default=''):
    setting = Settings.query.filter_by(key=key).first()
    return setting.value if setting else default


def set_setting(key, value, is_secret=False, preserve_if_empty=False):
    if preserve_if_empty and not value:
        return
    setting = Settings.query.filter_by(key=key).first()
    if setting:
        setting.value = value
        setting.is_secret = is_secret
    else:
        setting = Settings(key=key, value=value, is_secret=is_secret)
        db.session.add(setting)
    db.session.commit()


def save_scan_cache(scan_type, candidates, skipped):
    """Save scan results to database for persistence."""
    try:
        cache = ScanCache.query.filter_by(scan_type=scan_type).first()
        if cache:
            cache.candidates_json = json.dumps(candidates)
            cache.skipped_json = json.dumps(skipped)
            cache.scanned_at = datetime.utcnow()
        else:
            cache = ScanCache(
                scan_type=scan_type,
                candidates_json=json.dumps(candidates),
                skipped_json=json.dumps(skipped)
            )
            db.session.add(cache)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error saving scan cache: {e}")


def load_scan_cache(scan_type):
    """Load cached scan results from database."""
    try:
        cache = ScanCache.query.filter_by(scan_type=scan_type).first()
        if cache:
            return {
                'candidates': json.loads(cache.candidates_json or '[]'),
                'skipped': json.loads(cache.skipped_json or '[]'),
                'scanned_at': cache.scanned_at.isoformat() if cache.scanned_at else None
            }
    except Exception as e:
        print(f"Error loading scan cache: {e}")
    return None


def save_watch_history_cache(media_type: str, history_data: dict):
    """Save watch history to database for 7-day caching."""
    try:
        cache = WatchHistoryCache.query.filter_by(media_type=media_type).first()
        if cache:
            cache.history_json = json.dumps(history_data)
            cache.scanned_at = datetime.utcnow()
        else:
            cache = WatchHistoryCache(
                media_type=media_type,
                history_json=json.dumps(history_data)
            )
            db.session.add(cache)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error saving watch history cache: {e}")


def load_watch_history_cache(media_type: str):
    """Load cached watch history if less than 7 days old."""
    try:
        cache = WatchHistoryCache.query.filter_by(media_type=media_type).first()
        if cache and cache.scanned_at:
            age = datetime.utcnow() - cache.scanned_at
            print(f"[DEBUG] Found {media_type} cache from {cache.scanned_at}, age: {age.days} days")
            if age.days < WATCH_HISTORY_CACHE_DAYS:
                history_data = json.loads(cache.history_json or '{}')
                print(f"[DEBUG] Returning cached {media_type} history with {len(history_data)} entries")
                return {
                    'history': history_data,
                    'scanned_at': cache.scanned_at.isoformat(),
                    'age_days': age.days
                }
            else:
                print(f"[DEBUG] Cache too old: {age.days} days >= {WATCH_HISTORY_CACHE_DAYS}")
        else:
            print(f"[DEBUG] No cache found for {media_type}")
    except Exception as e:
        print(f"Error loading watch history cache: {e}")
        import traceback
        traceback.print_exc()
    return None


def clear_watch_history_cache(media_type=None):
    """Clear watch history cache (optionally for specific type)."""
    try:
        if media_type:
            WatchHistoryCache.query.filter_by(media_type=media_type).delete()
        else:
            WatchHistoryCache.query.delete()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error clearing watch history cache: {e}")


@app.route('/')
def index():
    if not is_setup_complete():
        return redirect(url_for('setup_step1'))
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return redirect(url_for('home'))


@app.route('/home')
@login_required
def home():
    settings = {
        'sonarr_url': get_setting('SONARR_URL'),
        'radarr_url': get_setting('RADARR_URL'),
        'plex_url': get_setting('PLEX_URL'),
        'ombi_url': get_setting('OMBI_URL'),
        'skip_added_days': get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
        'skip_watched_days': get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
    }
    return render_template('home.html', settings=settings)


@app.route('/setup/step1', methods=['GET', 'POST'])
def setup_step1():
    if is_setup_complete():
        return redirect(url_for('login'))
    
    existing_user = User.query.first()
    if existing_user:
        login_user(existing_user)
        flash('Welcome back! Continuing setup...', 'info')
        return redirect(url_for('setup_step2'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters')
        if len(password) < 6:
            errors.append('Password must be at least 6 characters')
        if password != confirm_password:
            errors.append('Passwords do not match')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('setup/step1.html', username=username)
        
        try:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            
            login_user(user)
            flash('Account created successfully!', 'success')
            return redirect(url_for('setup_step2'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {str(e)}', 'error')
            return render_template('setup/step1.html', username=username)
    
    return render_template('setup/step1.html')


@app.route('/setup/step2', methods=['GET', 'POST'])
@login_required
def setup_step2():
    if request.method == 'POST':
        sonarr_url = request.form.get('sonarr_url', '').strip().rstrip('/')
        sonarr_api_key = request.form.get('sonarr_api_key', '').strip()
        
        errors = []
        if not sonarr_url:
            errors.append('Sonarr URL is required')
        if not sonarr_api_key:
            errors.append('Sonarr API Key is required')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('setup/step2.html', 
                                   sonarr_url=sonarr_url, 
                                   sonarr_api_key=sonarr_api_key)
        
        set_setting('SONARR_URL', sonarr_url)
        set_setting('SONARR_API_KEY', sonarr_api_key, is_secret=True)
        
        flash('Sonarr configuration saved!', 'success')
        return redirect(url_for('setup_step3'))
    
    return render_template('setup/step2.html',
                           sonarr_url=get_setting('SONARR_URL'),
                           sonarr_api_key=get_setting('SONARR_API_KEY'))


@app.route('/setup/step3', methods=['GET', 'POST'])
@login_required
def setup_step3():
    if request.method == 'POST':
        plex_url = request.form.get('plex_url', '').strip().rstrip('/')
        plex_token = request.form.get('plex_token', '').strip()
        
        errors = []
        if not plex_url:
            errors.append('Plex URL is required')
        if not plex_token:
            errors.append('Plex Token is required')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('setup/step3.html',
                                   plex_url=plex_url,
                                   plex_token=plex_token)
        
        set_setting('PLEX_URL', plex_url)
        set_setting('PLEX_TOKEN', plex_token, is_secret=True)
        
        flash('Plex configuration saved!', 'success')
        return redirect(url_for('setup_step4'))
    
    return render_template('setup/step3.html',
                           plex_url=get_setting('PLEX_URL'),
                           plex_token=get_setting('PLEX_TOKEN'))


@app.route('/setup/step4', methods=['GET', 'POST'])
@login_required
def setup_step4():
    if request.method == 'POST':
        ombi_url = request.form.get('ombi_url', '').strip().rstrip('/')
        ombi_api_key = request.form.get('ombi_api_key', '').strip()
        
        if ombi_url:
            set_setting('OMBI_URL', ombi_url)
        if ombi_api_key:
            set_setting('OMBI_API_KEY', ombi_api_key, is_secret=True)
        
        flash('Ombi configuration saved!', 'success')
        return redirect(url_for('setup_step5'))
    
    return render_template('setup/step4.html',
                           ombi_url=get_setting('OMBI_URL'),
                           ombi_api_key=get_setting('OMBI_API_KEY'))


@app.route('/setup/step5', methods=['GET', 'POST'])
@login_required
def setup_step5():
    if request.method == 'POST':
        smtp_host = request.form.get('smtp_host', '').strip()
        smtp_port = request.form.get('smtp_port', '587').strip()
        smtp_user = request.form.get('smtp_user', '').strip()
        smtp_password = request.form.get('smtp_password', '').strip()
        smtp_from = request.form.get('smtp_from', '').strip()
        
        if smtp_host:
            set_setting('SMTP_HOST', smtp_host)
            set_setting('SMTP_PORT', smtp_port)
            set_setting('SMTP_USER', smtp_user)
            set_setting('SMTP_PASSWORD', smtp_password, is_secret=True)
            set_setting('SMTP_FROM', smtp_from)
        
        flash('Email configuration saved!', 'success')
        return redirect(url_for('setup_step6'))
    
    return render_template('setup/step5.html',
                           smtp_host=get_setting('SMTP_HOST'),
                           smtp_port=get_setting('SMTP_PORT', '587'),
                           smtp_user=get_setting('SMTP_USER'),
                           smtp_from=get_setting('SMTP_FROM'))


@app.route('/setup/step6', methods=['GET', 'POST'])
@login_required
def setup_step6():
    if request.method == 'POST':
        skip_added_days = request.form.get('skip_added_days', '90').strip()
        skip_watched_days = request.form.get('skip_watched_days', '180').strip()
        deletion_delay = request.form.get('deletion_delay', '2.0').strip()
        
        set_setting('SKIP_IF_ADDED_WITHIN_DAYS', skip_added_days)
        set_setting('SKIP_IF_WATCHED_WITHIN_DAYS', skip_watched_days)
        set_setting('DELETION_DELAY_SECONDS', deletion_delay)
        
        flash('Setup complete! You can now use the cleanup tool.', 'success')
        return redirect(url_for('home'))
    
    return render_template('setup/step6.html',
                           skip_added_days=get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
                           skip_watched_days=get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
                           deletion_delay=get_setting('DELETION_DELAY_SECONDS', '2.0'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_setup_complete():
        return redirect(url_for('setup_step1'))
    
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            if next_page:
                from urllib.parse import urlparse
                parsed = urlparse(next_page)
                if parsed.netloc and parsed.netloc != request.host:
                    next_page = None
            return redirect(next_page or url_for('home'))
        
        flash('Invalid username or password', 'error')
    
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    # Redirect old dashboard to new home page
    return redirect(url_for('home'))


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    if request.method == 'POST':
        set_setting('SONARR_URL', request.form.get('sonarr_url', '').strip().rstrip('/'))
        set_setting('SONARR_API_KEY', request.form.get('sonarr_api_key', '').strip(), is_secret=True, preserve_if_empty=True)
        set_setting('RADARR_URL', request.form.get('radarr_url', '').strip().rstrip('/'))
        set_setting('RADARR_API_KEY', request.form.get('radarr_api_key', '').strip(), is_secret=True, preserve_if_empty=True)
        set_setting('PLEX_URL', request.form.get('plex_url', '').strip().rstrip('/'))
        set_setting('PLEX_TOKEN', request.form.get('plex_token', '').strip(), is_secret=True, preserve_if_empty=True)
        set_setting('OMBI_URL', request.form.get('ombi_url', '').strip().rstrip('/'))
        set_setting('OMBI_API_KEY', request.form.get('ombi_api_key', '').strip(), is_secret=True, preserve_if_empty=True)
        set_setting('SMTP_HOST', request.form.get('smtp_host', '').strip())
        set_setting('SMTP_PORT', request.form.get('smtp_port', '587').strip())
        set_setting('SMTP_USER', request.form.get('smtp_user', '').strip())
        set_setting('SMTP_PASSWORD', request.form.get('smtp_password', '').strip(), is_secret=True, preserve_if_empty=True)
        set_setting('SMTP_FROM', request.form.get('smtp_from', '').strip())
        set_setting('CUSTOM_DOMAIN', request.form.get('custom_domain', '').strip())
        set_setting('SKIP_IF_ADDED_WITHIN_DAYS', request.form.get('skip_added_days', '90').strip())
        set_setting('SKIP_IF_WATCHED_WITHIN_DAYS', request.form.get('skip_watched_days', '180').strip())
        set_setting('DELETION_DELAY_SECONDS', request.form.get('deletion_delay', '2.0').strip())
        set_setting('TEST_MODE_LIMIT', request.form.get('test_mode_limit', '0').strip())
        set_setting('QUARANTINE_PATH', request.form.get('quarantine_path', '').strip())
        
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings_page'))
    
    settings = {
        'sonarr_url': get_setting('SONARR_URL'),
        'sonarr_api_key': get_setting('SONARR_API_KEY'),
        'radarr_url': get_setting('RADARR_URL'),
        'radarr_api_key': get_setting('RADARR_API_KEY'),
        'plex_url': get_setting('PLEX_URL'),
        'plex_token': get_setting('PLEX_TOKEN'),
        'ombi_url': get_setting('OMBI_URL'),
        'ombi_api_key': get_setting('OMBI_API_KEY'),
        'smtp_host': get_setting('SMTP_HOST'),
        'smtp_port': get_setting('SMTP_PORT', '587'),
        'smtp_user': get_setting('SMTP_USER'),
        'smtp_from': get_setting('SMTP_FROM'),
        'custom_domain': get_setting('CUSTOM_DOMAIN'),
        'skip_added_days': get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
        'skip_watched_days': get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
        'deletion_delay': get_setting('DELETION_DELAY_SECONDS', '2.0'),
        'test_mode_limit': get_setting('TEST_MODE_LIMIT', '0'),
        'quarantine_path': get_setting('QUARANTINE_PATH', ''),
    }
    return render_template('settings.html', settings=settings)


@app.route('/exclusions', methods=['GET', 'POST'])
@login_required
def exclusions():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add_show':
            show_title = request.form.get('show_title', '').strip()
            if show_title:
                existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == show_title.lower()).first()
                if not existing:
                    ombi_tv_requesters = get_ombi_tv_requesters()
                    original_email = ombi_tv_requesters.get(show_title.lower(), '')
                    original_name = None
                    if original_email:
                        ombi_tv_names = get_ombi_tv_requester_names()
                        original_name = ombi_tv_names.get(show_title.lower(), '')
                    new_exclusion = Exclusion(
                        title=show_title,
                        excluded_by='admin',
                        original_requester_email=original_email if original_email else None,
                        original_requester_name=original_name if original_name else None
                    )
                    db.session.add(new_exclusion)
                    db.session.commit()
                flash(f"Added '{show_title}' to TV show exclusions", 'success')
        
        elif action == 'remove_show':
            show_to_remove = request.form.get('show_to_remove', '').strip().lower()
            if show_to_remove:
                exclusion = Exclusion.query.filter(db.func.lower(Exclusion.title) == show_to_remove).first()
                if exclusion:
                    db.session.delete(exclusion)
                    db.session.commit()
                flash(f"Removed show from exclusion list", 'success')
        
        elif action == 'add_movie':
            movie_title = request.form.get('movie_title', '').strip()
            movie_year = request.form.get('movie_year', '').strip()
            if movie_title:
                try:
                    year_val = int(movie_year) if movie_year else None
                except ValueError:
                    year_val = None
                existing = MovieExclusion.query.filter(
                    db.func.lower(MovieExclusion.title) == movie_title.lower(),
                    MovieExclusion.year == year_val
                ).first()
                if not existing:
                    ombi_movie_requesters = get_ombi_movie_requesters()
                    original_email = ombi_movie_requesters.get(movie_title.lower(), '')
                    original_name = None
                    if original_email:
                        ombi_movie_names = get_ombi_movie_requester_names()
                        original_name = ombi_movie_names.get(movie_title.lower(), '')
                    new_exclusion = MovieExclusion(
                        title=movie_title,
                        year=year_val,
                        excluded_by='admin',
                        original_requester_email=original_email if original_email else None,
                        original_requester_name=original_name if original_name else None
                    )
                    db.session.add(new_exclusion)
                    db.session.commit()
                year_str = f" ({year_val})" if year_val else ""
                flash(f"Added '{movie_title}{year_str}' to movie exclusions", 'success')
        
        elif action == 'remove_movie':
            movie_id = request.form.get('movie_id', '').strip()
            if movie_id and movie_id.isdigit():
                exclusion = MovieExclusion.query.get(int(movie_id))
                if exclusion:
                    db.session.delete(exclusion)
                    db.session.commit()
                flash(f"Removed movie from exclusion list", 'success')
        
        return redirect(url_for('exclusions'))
    
    excluded_shows = Exclusion.query.order_by(Exclusion.title).all()
    excluded_movies = MovieExclusion.query.order_by(MovieExclusion.title).all()
    return render_template('exclusions.html', excluded_shows=excluded_shows, excluded_movies=excluded_movies)


@app.route('/history')
@login_required
def history():
    """Show deletion history."""
    deletions = DeletionHistory.query.order_by(DeletionHistory.deleted_at.desc()).all()
    sonarr_url = get_setting('SONARR_URL', '').rstrip('/')
    radarr_url = get_setting('RADARR_URL', '').rstrip('/')
    return render_template('history.html', deletions=deletions, sonarr_url=sonarr_url, radarr_url=radarr_url)


@app.route('/user-analytics')
@login_required
def user_analytics_page():
    """User analytics dashboard."""
    return render_template('user_analytics.html')


@app.route('/api/user-analytics')
@login_required
def get_user_analytics_data():
    """Return per-user watch + request analytics from Plex and Ombi."""
    from plexapi.server import PlexServer
    plex_url = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()

    users = {}
    errors = []

    if not plex_url or not plex_token:
        return jsonify({'users': [], 'errors': ['Plex not configured']})

    try:
        plex = PlexServer(plex_url, plex_token, timeout=60)

        for acc in plex.systemAccounts():
            aid = str(getattr(acc, 'accountID', '') or '')
            if not aid or aid == '0':
                continue
            users[aid] = {
                'account_id': aid,
                'name': getattr(acc, 'name', '') or f'User {aid}',
                'thumb': getattr(acc, 'thumb', '') or '',
                'type': 'Admin' if aid == '1' else 'User',
                'tv_plays': 0,
                'movie_plays': 0,
                'last_active': None,
                'tv_requested': 0,
                'movies_requested': 0,
            }
    except Exception as e:
        errors.append(f'Plex accounts error: {str(e)}')

    if users:
        try:
            plex = PlexServer(plex_url, plex_token, timeout=120)
            history = plex.history(maxresults=100000)
            for item in history:
                aid = str(getattr(item, 'accountID', '') or '')
                if aid not in users:
                    continue
                viewed_at = getattr(item, 'viewedAt', None)
                if item.type == 'episode':
                    users[aid]['tv_plays'] += 1
                elif item.type == 'movie':
                    users[aid]['movie_plays'] += 1
                if viewed_at:
                    cur = users[aid]['last_active']
                    if cur is None or viewed_at > cur:
                        users[aid]['last_active'] = viewed_at
        except Exception as e:
            errors.append(f'Plex history error: {str(e)}')

    for user in users.values():
        la = user['last_active']
        if la and hasattr(la, 'isoformat'):
            user['last_active'] = la.isoformat()

    if ombi_url and ombi_key:
        headers = {'ApiKey': ombi_key}
        name_index = {u['name'].lower(): uid for uid, u in users.items()}
        for media_type, field in [('tv', 'tv_requested'), ('movie', 'movies_requested')]:
            try:
                resp = requests.get(f'{ombi_url}/api/v1/Request/{media_type}', headers=headers, timeout=15)
                if resp.ok:
                    for req in resp.json():
                        ru = req.get('requestedUser') or {}
                        uname = (ru.get('userName') or ru.get('alias') or '').lower().strip()
                        uid = name_index.get(uname)
                        if uid:
                            users[uid][field] += 1
            except Exception as e:
                errors.append(f'Ombi {media_type} error: {str(e)}')

    user_list = sorted(users.values(), key=lambda u: u['tv_plays'] + u['movie_plays'], reverse=True)
    return jsonify({'users': user_list, 'errors': errors})


movie_cleanup_status = {
    'running': False,
    'candidates': [],
    'log': [],
    'started_at': None
}
movie_cleanup_lock = threading.Lock()


@app.route('/movies')
@login_required
def movies():
    """Movies dashboard for Radarr cleanup."""
    radarr_url = get_setting('RADARR_URL')
    if not radarr_url:
        flash('Please configure Radarr in Settings first.', 'warning')
        return redirect(url_for('settings_page'))
    
    settings = {
        'radarr_url': radarr_url,
        'plex_url': get_setting('PLEX_URL'),
        'ombi_url': get_setting('OMBI_URL'),
        'skip_added_days': get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
        'skip_watched_days': get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
    }
    return render_template('movies.html', settings=settings, cleanup_status=movie_cleanup_status)


movie_cleanup_lock = threading.Lock()


@app.route('/api/movies/scan', methods=['POST'])
@login_required
def scan_movies_api():
    global movie_cleanup_status
    
    with movie_cleanup_lock:
        check_stale_movie_cleanup()
        if movie_cleanup_status['running']:
            return jsonify({'error': 'A movie scan is already running'}), 400
        movie_cleanup_status['running'] = True
        movie_cleanup_status['log'] = []
        movie_cleanup_status['candidates'] = []
        movie_cleanup_status['started_at'] = datetime.now()
    
    def run_scan_thread():
        global movie_cleanup_status
        with app.app_context():
            try:
                from cleanup_movies import scan_movies_for_cleanup
                config = {
                    'RADARR_URL': get_setting('RADARR_URL'),
                    'RADARR_API_KEY': get_setting('RADARR_API_KEY'),
                    'PLEX_URL': get_setting('PLEX_URL'),
                    'PLEX_TOKEN': get_setting('PLEX_TOKEN'),
                    'OMBI_URL': get_setting('OMBI_URL'),
                    'OMBI_API_KEY': get_setting('OMBI_API_KEY'),
                    'SKIP_IF_ADDED_WITHIN_DAYS': get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
                    'SKIP_IF_WATCHED_WITHIN_DAYS': get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
                    'TEST_MODE_LIMIT': get_setting('TEST_MODE_LIMIT', '0'),
                }
                candidates = scan_movies_for_cleanup(
                    config,
                    log=lambda msg: movie_cleanup_status['log'].append(msg)
                )
                movie_cleanup_status['candidates'] = candidates
                save_scan_cache('movie', candidates, [])
            except Exception as e:
                movie_cleanup_status['log'].append(f"[ERROR] {str(e)}")
            finally:
                with movie_cleanup_lock:
                    movie_cleanup_status['running'] = False
    
    thread = threading.Thread(target=run_scan_thread)
    thread.start()
    
    return jsonify({'message': 'Movie scan started'})


@app.route('/api/scan-cache/<scan_type>', methods=['GET'])
@login_required
def get_scan_cache_api(scan_type):
    """Get cached scan results, filtering out excluded and deleted items."""
    if scan_type not in ['tv', 'movie']:
        return jsonify({'error': 'Invalid scan type'}), 400
    
    cache = load_scan_cache(scan_type)
    if cache:
        candidates = cache['candidates']
        
        if scan_type == 'tv':
            exclusions = {e.title.lower() for e in Exclusion.query.all()}
            deleted_titles = {d.title.lower() for d in DeletionHistory.query.filter_by(media_type='tv').all()}
            deleted_tvdb_ids = {d.tvdb_id for d in DeletionHistory.query.filter_by(media_type='tv').all() if d.tvdb_id}
            candidates = [c for c in candidates 
                         if c.get('title', '').lower() not in exclusions
                         and c.get('title', '').lower() not in deleted_titles
                         and c.get('tvdb_id') not in deleted_tvdb_ids]
        elif scan_type == 'movie':
            movie_exclusions = {(e.title.lower(), e.year) for e in MovieExclusion.query.all()}
            deleted_movies = {(d.title.lower(), d.year) for d in DeletionHistory.query.filter_by(media_type='movie').all()}
            deleted_tmdb_ids = {d.tmdb_id for d in DeletionHistory.query.filter_by(media_type='movie').all() if d.tmdb_id}
            candidates = [c for c in candidates 
                         if (c.get('title', '').lower(), c.get('year')) not in movie_exclusions
                         and (c.get('title', '').lower(), c.get('year')) not in deleted_movies
                         and c.get('tmdb_id') not in deleted_tmdb_ids]
        
        return jsonify({
            'cached': True,
            'candidates': candidates,
            'skipped': cache.get('skipped', []),
            'scanned_at': cache['scanned_at']
        })
    return jsonify({'cached': False, 'candidates': [], 'skipped': [], 'scanned_at': None})


@app.route('/api/movies/execute', methods=['POST'])
@login_required
def execute_movies_api():
    global movie_cleanup_status
    
    with movie_cleanup_lock:
        check_stale_movie_cleanup()
        if movie_cleanup_status['running']:
            return jsonify({'error': 'A movie operation is already running'}), 400
        movie_cleanup_status['running'] = True
        movie_cleanup_status['started_at'] = datetime.now()
    
    actions = request.json.get('actions', [])
    
    if not actions:
        with movie_cleanup_lock:
            movie_cleanup_status['running'] = False
        return jsonify({'error': 'No actions provided'}), 400
    
    def run_execute_thread():
        global movie_cleanup_status
        with app.app_context():
            try:
                from cleanup_movies import execute_movie_actions
                config = {
                    'RADARR_URL': get_setting('RADARR_URL'),
                    'RADARR_API_KEY': get_setting('RADARR_API_KEY'),
                    'SMTP_HOST': get_setting('SMTP_HOST'),
                    'SMTP_PORT': get_setting('SMTP_PORT', '587'),
                    'SMTP_USER': get_setting('SMTP_USER'),
                    'SMTP_PASSWORD': get_setting('SMTP_PASSWORD') or os.environ.get('SMTP_PASSWORD', ''),
                    'SMTP_FROM': get_setting('SMTP_FROM'),
                    'QUARANTINE_PATH': get_setting('QUARANTINE_PATH'),
                    'DELETION_DELAY_SECONDS': get_setting('DELETION_DELAY_SECONDS', '2.0'),
                }
                movie_cleanup_status['log'].append("[INFO] Executing movie actions...")
                result = execute_movie_actions(
                    actions,
                    config,
                    log=lambda msg: movie_cleanup_status['log'].append(msg),
                    quarantine=any(a.get('quarantine') for a in actions),
                    delete_from_db=any(a.get('deleteFromRadarr') for a in actions)
                )
                movie_cleanup_status['candidates'] = []
            except Exception as e:
                movie_cleanup_status['log'].append(f"[ERROR] {str(e)}")
            finally:
                with movie_cleanup_lock:
                    movie_cleanup_status['running'] = False
    
    thread = threading.Thread(target=run_execute_thread)
    thread.start()
    
    return jsonify({'message': 'Movie execution started', 'action_count': len(actions)})


@app.route('/api/movies/status')
@login_required
def movie_cleanup_status_api():
    return jsonify(movie_cleanup_status)


def send_exclusion_email(media_type, title, recipient_name, recipient_email, year=None):
    """Send exclusion notification email and record in history."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import os
    
    smtp_host = get_setting('SMTP_HOST')
    smtp_port_val = get_setting('SMTP_PORT', '587') or '587'
    smtp_port = int(smtp_port_val) if smtp_port_val.isdigit() else 587
    smtp_user = get_setting('SMTP_USER')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    smtp_from = get_setting('SMTP_FROM')
    
    if not all([smtp_host, smtp_user, smtp_password, smtp_from, recipient_email]):
        return False, 'Email not configured or no recipient'
    
    media_label = f"{title} ({year})" if year else title
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"
    media_type_display = "TV show" if media_type == "tv" else "movie"
    
    subject = f"ACTION REQUIRED: {media_label} Has Been Protected From Deletion"
    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background-color: #0f1729; color: #e2e8f0; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 12px 12px 0 0; text-align: center; }}
            .header h1 {{ margin: 0; color: white; font-size: 24px; }}
            .content {{ background-color: #1e293b; padding: 30px; border-radius: 0 0 12px 12px; }}
            .media-title {{ color: #60a5fa; font-size: 20px; font-weight: bold; margin: 15px 0; }}
            .info-box {{ background-color: #334155; padding: 15px; border-radius: 8px; margin: 15px 0; }}
            .footer {{ text-align: center; margin-top: 20px; color: #94a3b8; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Media Scrubber</h1>
            </div>
            <div class="content">
                <p>{greeting}</p>
                <p>We wanted to let you know that a {media_type_display} you requested has been added to our permanent exclusion list:</p>
                <div class="media-title">{media_label}</div>
                <div class="info-box">
                    <p><strong>What this means:</strong></p>
                    <p>This {media_type_display} will be protected from future cleanup operations and will remain in our library indefinitely.</p>
                </div>
                <p>If you have any questions, please reach out to your media server administrator.</p>
                <p>Best regards,<br>Media Scrubber</p>
            </div>
            <div class="footer">
                <p>This is an automated message from Media Scrubber</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = smtp_from
        msg['To'] = recipient_email
        msg.attach(MIMEText(body_html, 'html'))
        
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        email_record = EmailHistory(
            media_type=media_type,
            media_title=title,
            action_type='exclusion',
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            was_successful=True
        )
        db.session.add(email_record)
        db.session.commit()
        return True, 'Email sent successfully'
    except Exception as e:
        email_record = EmailHistory(
            media_type=media_type,
            media_title=title,
            action_type='exclusion',
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            subject=subject,
            body_html=body_html,
            was_successful=False,
            error_message=str(e)
        )
        db.session.add(email_record)
        db.session.commit()
        return False, str(e)


@app.route('/api/movies/exclude', methods=['POST'])
@login_required
def exclude_movie_api():
    """Add a movie to the exclusion list immediately."""
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'success': False, 'error': 'No movie title provided'}), 400
    
    title = data.get('title')
    year = data.get('year')
    tmdb_id = data.get('tmdb_id')
    requester_email = data.get('requester_email')
    requester_name = data.get('requester_name')
    
    existing = MovieExclusion.query.filter(
        db.func.lower(MovieExclusion.title) == title.lower(),
        MovieExclusion.year == year
    ).first()
    
    if existing:
        return jsonify({'success': True, 'message': 'Movie already in exclusion list'})
    
    new_exclusion = MovieExclusion(
        title=title, 
        year=year, 
        tmdb_id=tmdb_id,
        excluded_by='admin',
        original_requester_name=requester_name,
        original_requester_email=requester_email
    )
    db.session.add(new_exclusion)
    db.session.commit()
    
    email_sent = False
    if requester_email:
        email_sent, _ = send_exclusion_email('movie', title, requester_name, requester_email, year)
    
    return jsonify({'success': True, 'message': f"Added '{title}' to exclusion list", 'email_sent': email_sent})


@app.route('/api/history', methods=['GET'])
@login_required
def get_history_api():
    """Get deletion history as JSON."""
    deletions = DeletionHistory.query.order_by(DeletionHistory.deleted_at.desc()).all()
    sonarr_url = get_setting('SONARR_URL', '').rstrip('/')
    return jsonify([{
        'id': d.id,
        'title': d.title,
        'sonarr_id': d.sonarr_id,
        'tvdb_id': d.tvdb_id,
        'size_bytes': d.size_bytes,
        'season_count': d.season_count,
        'episode_count': d.episode_count,
        'requester_name': d.requester_name,
        'requester_email': d.requester_email,
        'priority_score': d.priority_score,
        'priority_label': d.priority_label,
        'was_quarantined': d.was_quarantined,
        'deleted_from_sonarr_db': d.deleted_from_sonarr_db,
        'deleted_at': d.deleted_at.isoformat() if d.deleted_at else None,
        'sonarr_url': sonarr_url
    } for d in deletions])


@app.route('/api/history/record', methods=['POST'])
@login_required
def record_deletion_api():
    """Record a deletion in history."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    try:
        record = DeletionHistory(
            title=data.get('title', 'Unknown'),
            sonarr_id=data.get('sonarr_id'),
            tvdb_id=data.get('tvdb_id'),
            size_bytes=data.get('size_bytes'),
            season_count=data.get('season_count'),
            episode_count=data.get('episode_count'),
            requester_name=data.get('requester_name'),
            requester_email=data.get('requester_email'),
            priority_score=data.get('priority_score'),
            priority_label=data.get('priority_label'),
            was_quarantined=data.get('was_quarantined', False),
            deleted_from_sonarr_db=data.get('deleted_from_sonarr_db', False)
        )
        db.session.add(record)
        db.session.commit()
        return jsonify({'success': True, 'id': record.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/email-history', methods=['GET'])
@login_required
def get_email_history_api():
    """Get email history as JSON."""
    emails = EmailHistory.query.order_by(EmailHistory.sent_at.desc()).all()
    return jsonify([{
        'id': e.id,
        'media_type': e.media_type,
        'media_title': e.media_title,
        'action_type': e.action_type,
        'recipient_name': e.recipient_name,
        'recipient_email': e.recipient_email,
        'subject': e.subject,
        'sent_at': e.sent_at.isoformat() if e.sent_at else None,
        'was_successful': e.was_successful,
        'error_message': e.error_message
    } for e in emails])


@app.route('/api/email-history/<int:email_id>', methods=['GET'])
@login_required
def get_email_detail_api(email_id):
    """Get a single email with full body content."""
    email = EmailHistory.query.get_or_404(email_id)
    return jsonify({
        'id': email.id,
        'media_type': email.media_type,
        'media_title': email.media_title,
        'action_type': email.action_type,
        'recipient_name': email.recipient_name,
        'recipient_email': email.recipient_email,
        'subject': email.subject,
        'body_html': email.body_html,
        'sent_at': email.sent_at.isoformat() if email.sent_at else None,
        'was_successful': email.was_successful,
        'error_message': email.error_message
    })


@app.route('/email-history')
@login_required
def email_history_page():
    """Email history page."""
    return render_template('email_history.html')


@app.route('/api/ai-analyze', methods=['POST'])
@login_required
def ai_analyze_api():
    """AI-powered analysis of media candidates for smart deletion recommendations."""
    data = request.get_json()
    media_type = data.get('type', 'tv') if data else 'tv'
    candidates = data.get('candidates', []) if data else []
    
    if not candidates:
        return jsonify({'error': 'No candidates to analyze'}), 400
    
    try:
        summary_data = []
        total_size = 0
        for c in candidates[:50]:
            size_gb = round(c.get('size_bytes', 0) / (1024**3), 1)
            total_size += size_gb
            summary_data.append({
                'title': c.get('title', 'Unknown'),
                'size_gb': size_gb,
                'priority': c.get('priority_label', 'Unknown'),
                'priority_score': c.get('priority_score', 0),
                'views': c.get('view_count', 0),
                'reason': c.get('reason', ''),
                'status': c.get('status', ''),
                'has_requester': bool(c.get('requester_name') or c.get('requester_email')),
                'year': c.get('year', ''),
                'episode_count': c.get('episode_count', 0) if media_type == 'tv' else None
            })
        
        type_label = 'TV shows' if media_type == 'tv' else 'movies'
        
        prompt = f"""You are a media library cleanup advisor. Analyze these {len(summary_data)} {type_label} candidates for deletion and give practical, actionable recommendations.

Total potential space savings: {round(total_size, 1)} GB

Here are the candidates (sorted by priority score, highest first):
{json.dumps(sorted(summary_data, key=lambda x: -x['priority_score']), indent=2)}

Provide a brief, helpful analysis:
1. **Quick Wins** - Name 3-5 specific titles that are the safest and most beneficial to delete (high priority, large size, no requester, never watched)
2. **Space Savers** - Name any particularly large items (10+ GB) that would free significant space
3. **Think Twice** - Name any items you'd recommend keeping or investigating before deleting (had views, has requester, etc.)
4. **Bottom Line** - One sentence summary of your recommendation

Keep it concise and actionable. Use the actual titles from the data. Focus on helping the user make quick decisions.

IMPORTANT: At the very end, include a line starting with "RECOMMENDED_TITLES:" followed by a comma-separated list of the exact titles you recommend deleting (from Quick Wins and Space Savers sections). Use exact titles as they appear in the data."""

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful media library cleanup advisor. Be concise, practical, and use the actual titles provided."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1000,
            temperature=0.5
        )
        
        analysis = response.choices[0].message.content or ''
        
        recommended_titles = []
        if analysis and 'RECOMMENDED_TITLES:' in analysis:
            parts = analysis.split('RECOMMENDED_TITLES:')
            titles_part = parts[1].strip()
            recommended_titles = [t.strip() for t in titles_part.split(',') if t.strip()]
            analysis = parts[0].strip()
        
        return jsonify({'analysis': analysis, 'recommended_titles': recommended_titles})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shows', methods=['GET'])
@login_required
def get_all_shows():
    """Get all shows from last scan for autocomplete."""
    all_shows = []
    
    candidates = cleanup_status.get('candidates', [])
    skipped = cleanup_status.get('skipped', [])
    
    for show in candidates + skipped:
        all_shows.append({
            'title': show.get('title', ''),
            'status': show.get('status', ''),
            'id': show.get('id')
        })
    
    all_shows.sort(key=lambda x: x['title'].lower())
    return jsonify(all_shows)


@app.route('/api/exclusions/add', methods=['POST'])
@login_required
def add_exclusion_api():
    """Add a show to exclusions immediately via API."""
    data = request.get_json()
    title = data.get('title', '').strip() if data else ''
    requester_email = data.get('requester_email') if data else None
    requester_name = data.get('requester_name') if data else None
    
    if not title:
        return jsonify({'success': False, 'error': 'No title provided'}), 400
    
    existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == title.lower()).first()
    if existing:
        return jsonify({'success': True, 'message': 'Already excluded'})
    
    try:
        new_exclusion = Exclusion(
            title=title,
            excluded_by='admin',
            original_requester_name=requester_name,
            original_requester_email=requester_email
        )
        db.session.add(new_exclusion)
        db.session.commit()
        
        email_sent = False
        if requester_email:
            email_sent, _ = send_exclusion_email('tv', title, requester_name, requester_email)
        
        return jsonify({'success': True, 'message': f'Added "{title}" to exclusion list', 'email_sent': email_sent})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/exclusions/backfill-requesters', methods=['POST'])
@login_required
def backfill_exclusion_requesters():
    """Backfill original_requester_email for all exclusions using Ombi data."""
    try:
        ombi_tv_requesters = get_ombi_tv_requesters()
        ombi_tv_names = get_ombi_tv_requester_names()
        ombi_movie_requesters = get_ombi_movie_requesters()
        ombi_movie_names = get_ombi_movie_requester_names()
        
        tv_updated = 0
        tv_skipped = 0
        movie_updated = 0
        movie_skipped = 0
        details = []
        
        tv_exclusions = Exclusion.query.all()
        for exc in tv_exclusions:
            if exc.original_requester_email:
                tv_skipped += 1
                continue
            
            ombi_email = ombi_tv_requesters.get(exc.title.lower(), '')
            if ombi_email:
                ombi_name = ombi_tv_names.get(exc.title.lower(), '')
                exc.original_requester_email = ombi_email
                exc.original_requester_name = ombi_name if ombi_name else None
                tv_updated += 1
                details.append(f"TV: {exc.title} -> {ombi_email}")
            else:
                tv_skipped += 1
        
        movie_exclusions = MovieExclusion.query.all()
        for exc in movie_exclusions:
            if exc.original_requester_email:
                movie_skipped += 1
                continue
            
            ombi_email = ombi_movie_requesters.get(exc.title.lower(), '')
            if ombi_email:
                ombi_name = ombi_movie_names.get(exc.title.lower(), '')
                exc.original_requester_email = ombi_email
                exc.original_requester_name = ombi_name if ombi_name else None
                movie_updated += 1
                details.append(f"Movie: {exc.title} -> {ombi_email}")
            else:
                movie_skipped += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'tv_updated': tv_updated,
            'tv_skipped': tv_skipped,
            'movie_updated': movie_updated,
            'movie_skipped': movie_skipped,
            'details': details,
            'message': f"Updated {tv_updated} TV shows and {movie_updated} movies with original requester info"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


def check_stale_cleanup():
    """Check and reset stale cleanup operations that exceed timeout."""
    global cleanup_status
    if cleanup_status['running'] and cleanup_status.get('started_at'):
        elapsed = (datetime.now() - cleanup_status['started_at']).total_seconds()
        if elapsed > CLEANUP_TIMEOUT_SECONDS:
            cleanup_status['running'] = False
            cleanup_status['phase'] = 'timeout'
            cleanup_status['log'].append(f"[WARNING] Operation timed out after {int(elapsed)}s - state reset")
            return True
    return False

def check_stale_movie_cleanup():
    """Check and reset stale movie cleanup operations that exceed timeout."""
    global movie_cleanup_status
    if movie_cleanup_status['running'] and movie_cleanup_status.get('started_at'):
        elapsed = (datetime.now() - movie_cleanup_status['started_at']).total_seconds()
        if elapsed > CLEANUP_TIMEOUT_SECONDS:
            movie_cleanup_status['running'] = False
            movie_cleanup_status['log'].append(f"[WARNING] Operation timed out after {int(elapsed)}s - state reset")
            return True
    return False


@app.route('/api/cleanup/scan', methods=['POST'])
@login_required
def scan_cleanup_api():
    global cleanup_status
    
    with cleanup_lock:
        check_stale_cleanup()
        if cleanup_status['running']:
            return jsonify({'error': 'A scan is already running'}), 400
        cleanup_status['running'] = True
        cleanup_status['phase'] = 'scanning'
        cleanup_status['log'] = []
        cleanup_status['candidates'] = []
        cleanup_status['skipped'] = []
        cleanup_status['started_at'] = datetime.now()
    
    def run_scan_thread():
        global cleanup_status
        with app.app_context():
            try:
                from cleanup_web import scan_for_candidates
                result = scan_for_candidates(
                    get_setting,
                    log_callback=lambda msg: cleanup_status['log'].append(msg)
                )
                cleanup_status['candidates'] = result.get('candidates', [])
                cleanup_status['skipped'] = result.get('skipped', [])
                cleanup_status['last_result'] = result
                cleanup_status['phase'] = 'ready'
                save_scan_cache('tv', cleanup_status['candidates'], cleanup_status['skipped'])
            except Exception as e:
                cleanup_status['last_result'] = {'error': str(e)}
                cleanup_status['log'].append(f"[ERROR] {str(e)}")
                cleanup_status['phase'] = 'error'
            finally:
                with cleanup_lock:
                    cleanup_status['running'] = False
                    cleanup_status['last_run'] = datetime.now().isoformat()
    
    thread = threading.Thread(target=run_scan_thread)
    thread.start()
    
    return jsonify({'message': 'Scan started'})


@app.route('/api/email/test', methods=['POST'])
@login_required
def test_email_api():
    """Send a test email to verify SMTP configuration."""
    from cleanup_web import send_test_email
    import os
    
    config = {
        'SMTP_HOST': get_setting('SMTP_HOST', ''),
        'SMTP_PORT': int(get_setting('SMTP_PORT', '587') or '587') if get_setting('SMTP_PORT', '587') else 587,
        'SMTP_USER': get_setting('SMTP_USER', ''),
        'SMTP_PASSWORD': os.environ.get('SMTP_PASSWORD', ''),
        'SMTP_FROM': get_setting('SMTP_FROM', ''),
    }
    
    success, message = send_test_email(config)
    return jsonify({'success': success, 'message': message})


@app.route('/api/cleanup/execute', methods=['POST'])
@login_required
def execute_cleanup_api():
    global cleanup_status
    
    with cleanup_lock:
        check_stale_cleanup()
        if cleanup_status['running']:
            return jsonify({'error': 'A cleanup operation is already running'}), 400
        cleanup_status['running'] = True
        cleanup_status['phase'] = 'executing'
        cleanup_status['started_at'] = datetime.now()
    
    actions = request.json.get('actions', [])
    
    if not actions:
        with cleanup_lock:
            cleanup_status['running'] = False
            cleanup_status['phase'] = 'ready'
        return jsonify({'error': 'No actions provided'}), 400
    
    def run_execute_thread():
        global cleanup_status
        with app.app_context():
            try:
                from cleanup_web import execute_actions
                cleanup_status['log'].append("[INFO] Executing approved actions...")
                result = execute_actions(
                    actions,
                    get_setting,
                    log_callback=lambda msg: cleanup_status['log'].append(msg)
                )
                cleanup_status['last_result'] = result
                cleanup_status['phase'] = 'completed'
                cleanup_status['candidates'] = []
            except Exception as e:
                cleanup_status['last_result'] = {'error': str(e)}
                cleanup_status['log'].append(f"[ERROR] {str(e)}")
                cleanup_status['phase'] = 'error'
            finally:
                with cleanup_lock:
                    cleanup_status['running'] = False
                    cleanup_status['last_run'] = datetime.now().isoformat()
    
    thread = threading.Thread(target=run_execute_thread)
    thread.start()
    
    return jsonify({'message': 'Execution started', 'action_count': len(actions)})


@app.route('/api/cleanup/status')
@login_required
def cleanup_status_api():
    return jsonify(cleanup_status)


@app.route('/api/cleanup/reset', methods=['POST'])
@login_required
def reset_cleanup_api():
    """Force reset cleanup state if stuck."""
    global cleanup_status
    with cleanup_lock:
        cleanup_status['running'] = False
        cleanup_status['phase'] = 'reset'
        cleanup_status['started_at'] = None
        cleanup_status['log'].append("[INFO] Cleanup state manually reset")
    return jsonify({'success': True, 'message': 'Cleanup state reset'})


@app.route('/api/movies/reset', methods=['POST'])
@login_required
def reset_movies_api():
    """Force reset movie cleanup state if stuck."""
    global movie_cleanup_status
    with movie_cleanup_lock:
        movie_cleanup_status['running'] = False
        movie_cleanup_status['started_at'] = None
        movie_cleanup_status['log'].append("[INFO] Movie cleanup state manually reset")
    return jsonify({'success': True, 'message': 'Movie cleanup state reset'})


@app.route('/api/watch-history-cache/clear', methods=['POST'])
@login_required
def clear_watch_history_cache_api():
    """Clear the watch history cache to force a fresh scan."""
    media_type = request.json.get('media_type') if request.json else None
    clear_watch_history_cache(media_type)
    msg = f"Cleared {media_type or 'all'} watch history cache"
    return jsonify({'success': True, 'message': msg})


@app.route('/api/requester-review/send', methods=['POST'])
@login_required
def send_requester_review_emails():
    """Generate and send review emails to all requesters with candidates."""
    req_data = request.get_json() or {}
    test_mode = req_data.get('test_mode', False)
    test_requester_email = req_data.get('test_requester_email')
    override_email = req_data.get('override_email')
    
    tv_candidates = cleanup_status.get('candidates', [])
    movie_candidates = movie_cleanup_status.get('candidates', [])
    
    requester_items = {}
    
    for c in tv_candidates:
        email = c.get('requester_email')
        if email:
            if email not in requester_items:
                requester_items[email] = {'name': c.get('requester_name', ''), 'tv': [], 'movies': []}
            requester_items[email]['tv'].append({
                'title': c.get('title'),
                'tvdb_id': c.get('tvdb_id'),
                'size_gb': round(c.get('size_bytes', 0) / (1024**3), 2),
                'last_watched': c.get('last_watched'),
                'view_count': c.get('view_count', 0),
                'status': c.get('status')
            })
    
    for c in movie_candidates:
        email = c.get('requester_email')
        if email:
            if email not in requester_items:
                requester_items[email] = {'name': c.get('requester_name', ''), 'tv': [], 'movies': []}
            requester_items[email]['movies'].append({
                'title': c.get('title'),
                'year': c.get('year'),
                'tmdb_id': c.get('tmdb_id'),
                'size_gb': round(c.get('size_bytes', 0) / (1024**3), 2),
                'last_watched': c.get('last_watched'),
                'view_count': c.get('view_count', 0)
            })
    
    if not requester_items:
        return jsonify({'success': False, 'error': 'No requesters with candidates found. Run a scan first.'}), 400
    
    if test_mode:
        if not test_requester_email or test_requester_email not in requester_items:
            return jsonify({'success': False, 'error': 'Invalid test requester email'}), 400
        if not override_email:
            return jsonify({'success': False, 'error': 'Override email required for test mode'}), 400
        requester_items = {test_requester_email: requester_items[test_requester_email]}
    
    sent_count = 0
    errors = []
    
    custom_domain = get_setting('CUSTOM_DOMAIN', '')
    if custom_domain:
        base_url = f"https://{custom_domain.lstrip('https://').lstrip('http://').rstrip('/')}"
    else:
        base_url = request.host_url.rstrip('/')
    
    for email, data in requester_items.items():
        try:
            token = secrets.token_urlsafe(32)
            review_token = RequesterReviewToken(
                token=token,
                requester_email=email,
                requester_name=data['name'],
                items_json=json.dumps({'tv': data['tv'], 'movies': data['movies']}),
                expires_at=datetime.utcnow() + timedelta(days=14)
            )
            db.session.add(review_token)
            db.session.commit()
            
            review_url = f"{base_url}/review/{token}"
            tv_count = len(data['tv'])
            movie_count = len(data['movies'])
            
            subject = "ACTION REQUIRED: Your Media May Be Deleted - Review Now"
            html_body = f"""
            <html><body style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #333;">Media Library Review Request</h2>
            <p>Hi{' ' + data['name'] if data['name'] else ''},</p>
            <p>To help keep our media library clean and save storage space, we're reviewing content that hasn't been watched recently.</p>
            <p>You have <strong>{tv_count} TV show{'s' if tv_count != 1 else ''}</strong> and <strong>{movie_count} movie{'s' if movie_count != 1 else ''}</strong> that you requested which are being considered for removal.</p>
            <p style="margin: 25px 0;">
                <a href="{review_url}" style="background: #4f46e5; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500;">Review Your Content</a>
            </p>
            <p style="color: #666; font-size: 14px;">Click the button above to see your content and select any items you'd like to keep. Items you don't select may be removed in a future cleanup.</p>
            <p style="color: #999; font-size: 12px;">This link expires in 14 days.</p>
            </body></html>
            """
            
            smtp_host = get_setting('SMTP_HOST')
            smtp_port = int(get_setting('SMTP_PORT', '587'))
            smtp_user = get_setting('SMTP_USER')
            smtp_password = get_setting('SMTP_PASSWORD') or os.environ.get('SMTP_PASSWORD', '')
            smtp_from = get_setting('SMTP_FROM')
            
            if smtp_host and smtp_user and smtp_password:
                send_to = override_email if test_mode else email
                
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"[TEST] {subject}" if test_mode else subject
                msg['From'] = smtp_from or smtp_user
                msg['To'] = send_to
                msg.attach(MIMEText(html_body, 'html'))
                
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
                
                if not test_mode:
                    email_record = EmailHistory(
                        media_type='review',
                        media_title=f"{tv_count} shows, {movie_count} movies",
                        action_type='requester_review',
                        recipient_name=data['name'],
                        recipient_email=email,
                        subject=subject,
                        body_html=html_body,
                        was_successful=True
                    )
                    db.session.add(email_record)
                    db.session.commit()
                sent_count += 1
            else:
                errors.append(f"SMTP not configured")
                break
                
        except Exception as e:
            errors.append(f"{email}: {str(e)}")
    
    return jsonify({
        'success': True,
        'sent_count': sent_count,
        'total_requesters': len(requester_items),
        'errors': errors
    })


def get_ombi_tv_requesters():
    """Fetch TV request data from Ombi and return dict mapping titles to requester emails."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    
    print(f"[DEBUG OMBI] TV - URL: {ombi_url[:30] if ombi_url else 'NOT SET'}...")
    print(f"[DEBUG OMBI] TV - API Key set: {bool(ombi_key)}")
    
    if not ombi_url or not ombi_key:
        print("[DEBUG OMBI] TV - Missing URL or API key, returning empty")
        return {}
    
    try:
        response = requests.get(
            f"{ombi_url}/api/v1/Request/tv",
            headers={"ApiKey": ombi_key},
            timeout=30
        )
        print(f"[DEBUG OMBI] TV - Response status: {response.status_code}")
        response.raise_for_status()
        requests_data = response.json()
        print(f"[DEBUG OMBI] TV - Got {len(requests_data)} requests from Ombi")
        
        requesters = {}
        for req in requests_data:
            title = req.get("title", "").strip()
            requester_user = req.get("requestedUser") or {}
            email = requester_user.get("email", "") or requester_user.get("Email", "")
            
            if not email:
                child_requests = req.get("childRequests") or []
                for child in child_requests:
                    child_user = child.get("requestedUser") or {}
                    email = child_user.get("email", "") or child_user.get("Email", "")
                    if email:
                        break
            
            if title and email:
                requesters[title.lower()] = email.lower()
            elif title:
                print(f"[DEBUG OMBI] TV - No email for: {title}")
        
        print(f"[DEBUG OMBI] TV - Final mapping has {len(requesters)} titles with emails")
        return requesters
    except Exception as e:
        print(f"[DEBUG OMBI] TV - Error: {str(e)}")
        return {}


def get_ombi_movie_requesters():
    """Fetch movie request data from Ombi and return dict mapping titles to requester emails."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    
    if not ombi_url or not ombi_key:
        return {}
    
    try:
        response = requests.get(
            f"{ombi_url}/api/v1/Request/movie",
            headers={"ApiKey": ombi_key},
            timeout=30
        )
        response.raise_for_status()
        requests_data = response.json()
        
        requesters = {}
        for req in requests_data:
            title = req.get("title", "").strip()
            requester_user = req.get("requestedUser") or {}
            email = requester_user.get("email", "") or requester_user.get("Email", "")
            
            if title and email:
                requesters[title.lower()] = email.lower()
        
        return requesters
    except:
        return {}


def get_ombi_tv_requester_names():
    """Fetch TV request data from Ombi and return dict mapping titles to requester names."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    
    if not ombi_url or not ombi_key:
        return {}
    
    try:
        response = requests.get(
            f"{ombi_url}/api/v1/Request/tv",
            headers={"ApiKey": ombi_key},
            timeout=30
        )
        response.raise_for_status()
        requests_data = response.json()
        
        requesters = {}
        for req in requests_data:
            title = req.get("title", "").strip()
            requester_user = req.get("requestedUser") or {}
            name = requester_user.get("userName", "") or requester_user.get("alias", "")
            
            if not name:
                child_requests = req.get("childRequests") or []
                for child in child_requests:
                    child_user = child.get("requestedUser") or {}
                    name = child_user.get("userName", "") or child_user.get("alias", "")
                    if name:
                        break
            
            if title and name:
                requesters[title.lower()] = name
        
        return requesters
    except:
        return {}


def get_ombi_movie_requester_names():
    """Fetch movie request data from Ombi and return dict mapping titles to requester names."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    
    if not ombi_url or not ombi_key:
        return {}
    
    try:
        response = requests.get(
            f"{ombi_url}/api/v1/Request/movie",
            headers={"ApiKey": ombi_key},
            timeout=30
        )
        response.raise_for_status()
        requests_data = response.json()
        
        requesters = {}
        for req in requests_data:
            title = req.get("title", "").strip()
            requester_user = req.get("requestedUser") or {}
            name = requester_user.get("userName", "") or requester_user.get("alias", "")
            
            if title and name:
                requesters[title.lower()] = name
        
        return requesters
    except:
        return {}


@app.route('/review/<token>')
def requester_review_page(token):
    """Public page for requesters to review and exclude their content."""
    try:
        db.session.rollback()
        
        review = RequesterReviewToken.query.filter_by(token=token).first()
        
        if not review:
            return render_template('review_error.html', error="Invalid or expired review link."), 404
        
        if review.expires_at and datetime.utcnow() > review.expires_at:
            return render_template('review_error.html', error="This review link has expired."), 410
        
        items = json.loads(review.items_json or '{}')
        requester_email_lower = review.requester_email.lower() if review.requester_email else ''
        
        print(f"[DEBUG] Review page for: {requester_email_lower}")
        
        # Get Ombi requester data to find admin-excluded items that this requester originally requested
        ombi_tv_requesters = get_ombi_tv_requesters()
        ombi_movie_requesters = get_ombi_movie_requesters()
        
        print(f"[DEBUG] Ombi TV requesters found: {len(ombi_tv_requesters)}")
        print(f"[DEBUG] Ombi Movie requesters found: {len(ombi_movie_requesters)}")
        
        # Log which titles this requester has in Ombi
        requester_ombi_tv = [title for title, email in ombi_tv_requesters.items() if email == requester_email_lower]
        requester_ombi_movies = [title for title, email in ombi_movie_requesters.items() if email == requester_email_lower]
        print(f"[DEBUG] Requester's Ombi TV titles: {requester_ombi_tv}")
        print(f"[DEBUG] Requester's Ombi Movie titles: {requester_ombi_movies}")
        
        # Get all exclusions and filter to find ones relevant to this requester
        all_tv_exclusions = Exclusion.query.all()
        all_movie_exclusions = MovieExclusion.query.all()
        
        print(f"[DEBUG] Total TV exclusions: {len(all_tv_exclusions)}")
        
        # Log exclusion titles (lowercase) for matching comparison
        exclusion_titles_lower = [exc.title.lower() for exc in all_tv_exclusions]
        print(f"[DEBUG] First 20 exclusion titles: {exclusion_titles_lower[:20]}")
        
        # Check for matches between requester's Ombi titles and exclusion titles
        matching_titles = set(requester_ombi_tv) & set(exclusion_titles_lower)
        print(f"[DEBUG] Matching titles (Ombi & Exclusions): {matching_titles}")
        print(f"[DEBUG] Total Movie exclusions: {len(all_movie_exclusions)}")
        
        existing_tv_exclusions = []
        updates_made = False
        for exc in all_tv_exclusions:
            # Include if: requester excluded it themselves, OR original_requester matches, OR Ombi says they requested it
            if exc.excluded_by_email and exc.excluded_by_email.lower() == requester_email_lower:
                print(f"[DEBUG] TV match (excluded_by_email): {exc.title}")
                existing_tv_exclusions.append(exc)
            elif exc.original_requester_email and exc.original_requester_email.lower() == requester_email_lower:
                print(f"[DEBUG] TV match (original_requester): {exc.title}")
                existing_tv_exclusions.append(exc)
            elif exc.excluded_by == 'admin' or exc.excluded_by is None or exc.excluded_by == '':
                # Admin exclusions (including legacy ones with NULL excluded_by)
                ombi_email = ombi_tv_requesters.get(exc.title.lower(), '')
                if ombi_email == requester_email_lower:
                    print(f"[DEBUG] TV match (ombi lookup): {exc.title}")
                    existing_tv_exclusions.append(exc)
                    # Auto-populate original_requester_email if not set
                    if not exc.original_requester_email:
                        exc.original_requester_email = requester_email_lower
                        exc.original_requester_name = review.requester_name
                        updates_made = True
                        print(f"[DEBUG] Auto-populated original_requester_email for: {exc.title}")
                else:
                    print(f"[DEBUG] TV admin exclusion '{exc.title}' - Ombi email: '{ombi_email}' vs requester: '{requester_email_lower}'")
        
        existing_movie_exclusions = []
        for exc in all_movie_exclusions:
            if exc.excluded_by_email and exc.excluded_by_email.lower() == requester_email_lower:
                print(f"[DEBUG] Movie match (excluded_by_email): {exc.title}")
                existing_movie_exclusions.append(exc)
            elif exc.original_requester_email and exc.original_requester_email.lower() == requester_email_lower:
                print(f"[DEBUG] Movie match (original_requester): {exc.title}")
                existing_movie_exclusions.append(exc)
            elif exc.excluded_by == 'admin' or exc.excluded_by is None or exc.excluded_by == '':
                # Admin exclusions (including legacy ones with NULL excluded_by)
                ombi_email = ombi_movie_requesters.get(exc.title.lower(), '')
                if ombi_email == requester_email_lower:
                    print(f"[DEBUG] Movie match (ombi lookup): {exc.title}")
                    existing_movie_exclusions.append(exc)
                    # Auto-populate original_requester_email if not set
                    if not exc.original_requester_email:
                        exc.original_requester_email = requester_email_lower
                        exc.original_requester_name = review.requester_name
                        updates_made = True
                        print(f"[DEBUG] Auto-populated original_requester_email for movie: {exc.title}")
        
        # Commit any auto-populated requester info
        if updates_made:
            try:
                db.session.commit()
                print(f"[DEBUG] Committed auto-populated original_requester data")
            except Exception as commit_err:
                db.session.rollback()
                print(f"[DEBUG] Failed to commit auto-populated data: {commit_err}")
        
        print(f"[DEBUG] Final TV exclusions for requester: {len(existing_tv_exclusions)}")
        print(f"[DEBUG] Final Movie exclusions for requester: {len(existing_movie_exclusions)}")
        
        ombi_url = get_setting('OMBI_URL', '')
        
        return render_template('requester_review.html',
            token=token,
            requester_name=review.requester_name,
            tv_items=items.get('tv', []),
            movie_items=items.get('movies', []),
            existing_tv_exclusions=existing_tv_exclusions,
            existing_movie_exclusions=existing_movie_exclusions,
            is_completed=review.is_used,
            ombi_url=ombi_url
        )
    except Exception as e:
        print(f"Error in review page: {str(e)}")
        return render_template('review_error.html', error=f"An error occurred loading the review page."), 500


@app.route('/api/review/<token>/submit', methods=['POST'])
def submit_requester_exclusions(token):
    """Process requester's exclusion selections."""
    db.session.rollback()
    review = RequesterReviewToken.query.filter_by(token=token).first()
    
    if not review:
        return jsonify({'success': False, 'error': 'Invalid token'}), 404
    
    if review.expires_at and datetime.utcnow() > review.expires_at:
        return jsonify({'success': False, 'error': 'Link expired'}), 410
    
    data = request.get_json()
    tv_exclusions = data.get('tv_exclusions', [])
    movie_exclusions = data.get('movie_exclusions', [])
    
    added_tv = 0
    added_movies = 0
    
    for title in tv_exclusions:
        existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == title.lower()).first()
        if not existing:
            exclusion = Exclusion(
                title=title,
                excluded_by='requester',
                excluded_by_name=review.requester_name,
                excluded_by_email=review.requester_email
            )
            db.session.add(exclusion)
            added_tv += 1
    
    for movie in movie_exclusions:
        title = movie.get('title')
        year = movie.get('year')
        tmdb_id = movie.get('tmdb_id')
        
        existing = MovieExclusion.query.filter(
            db.func.lower(MovieExclusion.title) == title.lower(),
            MovieExclusion.year == year
        ).first()
        if not existing:
            exclusion = MovieExclusion(
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                excluded_by='requester',
                excluded_by_name=review.requester_name,
                excluded_by_email=review.requester_email
            )
            db.session.add(exclusion)
            added_movies += 1
    
    review.is_used = True
    review.completed_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({
        'success': True,
        'added_tv': added_tv,
        'added_movies': added_movies,
        'message': f"Added {added_tv} TV shows and {added_movies} movies to exclusion list."
    })


@app.route('/api/review/<token>/remove', methods=['POST'])
def remove_requester_exclusion(token):
    """Allow requester to remove protection from their content."""
    db.session.rollback()
    review = RequesterReviewToken.query.filter_by(token=token).first()
    
    if not review:
        return jsonify({'success': False, 'error': 'Invalid token'}), 404
    
    if review.expires_at and datetime.utcnow() > review.expires_at:
        return jsonify({'success': False, 'error': 'Link expired'}), 410
    
    data = request.get_json()
    item_type = data.get('type')  # 'tv' or 'movie'
    title = data.get('title')
    exclusion_id = data.get('id')
    
    if not item_type or not exclusion_id:
        return jsonify({'success': False, 'error': 'Missing required fields'}), 400
    
    if item_type not in ('tv', 'movie'):
        return jsonify({'success': False, 'error': 'Invalid type. Must be "tv" or "movie"'}), 400
    
    try:
        if item_type == 'tv':
            exclusion = Exclusion.query.get(exclusion_id)
            if exclusion:
                # Only allow removal of exclusions the requester created themselves (not admin-excluded)
                if exclusion.excluded_by_email == review.requester_email and exclusion.excluded_by == 'requester':
                    db.session.delete(exclusion)
                    db.session.commit()
                    return jsonify({'success': True, 'message': f'Removed protection from "{title}"'})
                else:
                    return jsonify({'success': False, 'error': 'Only items you protected can be removed'}), 403
        else:
            exclusion = MovieExclusion.query.get(exclusion_id)
            if exclusion:
                if exclusion.excluded_by_email == review.requester_email and exclusion.excluded_by == 'requester':
                    db.session.delete(exclusion)
                    db.session.commit()
                    return jsonify({'success': True, 'message': f'Removed protection from "{title}"'})
                else:
                    return jsonify({'success': False, 'error': 'Only items you protected can be removed'}), 403
        
        return jsonify({'success': False, 'error': 'Exclusion not found'}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/run-cleanup', methods=['POST'])
@login_required
def run_cleanup_api():
    return redirect(url_for('scan_cleanup_api'))


def init_db():
    with app.app_context():
        db.create_all()

init_db()


@app.route('/health')
def health_check():
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'database': str(e)}), 500


@app.route('/media-chat')
@login_required
def media_chat():
    return render_template('media_chat.html')


@app.route('/api/media-chat/status')
@login_required
def media_chat_status():
    status = {'sonarr': None, 'radarr': None, 'plex': None}
    
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    plex_url = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    
    if sonarr_url and sonarr_key:
        try:
            r = requests.get(f"{sonarr_url}/api/v3/system/status", params={'apikey': sonarr_key}, timeout=5)
            status['sonarr'] = r.ok
        except:
            status['sonarr'] = False
    
    if radarr_url and radarr_key:
        try:
            r = requests.get(f"{radarr_url}/api/v3/system/status", params={'apikey': radarr_key}, timeout=5)
            status['radarr'] = r.ok
        except:
            status['radarr'] = False
    
    if plex_url and plex_token:
        try:
            r = requests.get(f"{plex_url}/identity", params={'X-Plex-Token': plex_token}, headers={'Accept': 'application/json'}, timeout=5)
            status['plex'] = r.ok
        except:
            status['plex'] = False
    
    return jsonify(status)


@app.route('/api/media-chat/clear-history', methods=['POST'])
@login_required
def media_chat_clear_history():
    session.pop('chat_history', None)
    return jsonify({'ok': True})

@app.route('/api/media-chat/send', methods=['POST'])
@login_required
def media_chat_send():
    data = request.get_json()
    user_message = data.get('message', '').strip()
    
    if not user_message:
        return jsonify({'reply': 'Please type a message.'})
    
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    plex_url = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    
    # Load conversation history from session (last 6 turns = 12 messages)
    chat_history = session.get('chat_history', [])
    
    try:
        system_prompt = """You control Sonarr (TV shows), Radarr (movies), Plex, and Ombi (requests). Return ONLY valid JSON, no markdown fences:
{"intent":"<intent>","query":"title or search term","reply":"one sentence saying what you are doing","filter":{}}

The "filter" field is optional. Use it for bulk/conditional operations:
- {"before_year": 2025} — items released before 2025
- {"after_year": 2020} — items released after 2020
- {"type": "movie"} or {"type": "show"} — filter by media type
- {"status": "ended"} — for ended/canceled shows
- {"has_file": false} — items without downloaded files
- Combine filters: {"before_year": 2020, "type": "movie"}
- {"all": true} — user wants ALL items (e.g. "delete everything", "remove all")

Intent guide:
- search_show: user wants to find/add a NEW TV show (search Sonarr's online database)
- search_movie: user wants to find/add a NEW movie (search Radarr's online database)
- check_lib_show: user wants to check if a TV show is ALREADY in their library
- check_lib_movie: user wants to check if a movie is ALREADY in their library
- delete_show: user wants to remove/delete TV show(s) FROM their library. Use filter for bulk (e.g. "delete all ended shows")
- delete_movie: user wants to remove/delete movie(s) FROM their library. Use filter for bulk (e.g. "delete all movies before 2010")
- queue_sonarr: user wants to see what's downloading in Sonarr
- queue_radarr: user wants to see what's downloading in Radarr
- plex_library: user wants to see their Plex libraries
- plex_watchlist_add: user wants to add a movie or show to their Plex watchlist
- plex_watchlist_show: user wants to see what's on their Plex watchlist
- plex_watchlist_remove: user wants to remove item(s) from their Plex watchlist. Use query for specific title, or filter for bulk (e.g. "remove all movies before 2020 from watchlist")
- plex_recently_added: user wants to see what was recently added to Plex (what's new)
- missing_episodes: user wants to check for missing/wanted episodes (optionally for a specific show)
- disk_space: user wants to check storage, disk space, or library size
- ombi_requests: user wants to see pending/recent media requests from Ombi
- calendar: user wants to see upcoming episodes or movie releases this week
- recommend: user wants recommendations for popular/trending/upcoming shows or movies. Use query for specifics like "comedy", "sci-fi", "upcoming movies", etc.
- chitchat: general conversation, greetings, or questions you can answer directly

CONTEXT AWARENESS — use conversation history to handle follow-ups:
- If the user previously searched for a movie and now says "it's a show" or "it's a TV show" → use search_show with the SAME title from the prior query
- If the user says "the TV show" or "the movie" without a title → reuse the most recent title from history
- If the user says "add it" or "add that" → figure out what "it" refers to from history and search for it
- If the user corrects a previous search (e.g. "no, the 2019 version") → search again with that context
- If the user just says a title with no other context → infer add intent (search_show or search_movie) based on context

IMPORTANT: Understand complex requests. Examples:
- "Delete everything on the watchlist before 2025" → intent: plex_watchlist_remove, filter: {"before_year": 2025}
- "Remove all movies from my watchlist" → intent: plex_watchlist_remove, filter: {"type": "movie"}
- "Delete all ended shows from Sonarr" → intent: delete_show, filter: {"status": "ended"}
- "Remove The Bear from my watchlist" → intent: plex_watchlist_remove, query: "The Bear"
- "Remove Wayward, Together, Alien Earth from my watchlist" → intent: plex_watchlist_remove, query: "Wayward, Together, Alien Earth" (comma-separated list of titles)
- "Delete Severance and The Bear from Sonarr" → intent: delete_show, query: "Severance, The Bear"

When the user lists multiple titles, put them ALL in the query field as a comma-separated list. Do NOT paraphrase or reword titles.
"""
        
        messages = [{"role": "system", "content": system_prompt}]
        # Add last 6 turns of history for context
        messages.extend(chat_history[-12:])
        messages.append({"role": "user", "content": user_message})
        
        intent_response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.1,
            max_tokens=400
        )
        
        raw = intent_response.choices[0].message.content.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return jsonify({'reply': raw})
        
        intent = parsed.get('intent', 'chitchat')
        query = parsed.get('query', '')
        reply = parsed.get('reply', '')
        filters = parsed.get('filter', {})
        
        # Save conversation turn to session history
        try:
            history_entry_user = {"role": "user", "content": user_message}
            history_entry_asst = {"role": "assistant", "content": reply or f"Performing {intent} for: {query}"}
            chat_history = chat_history + [history_entry_user, history_entry_asst]
            session['chat_history'] = chat_history[-12:]  # keep last 6 turns
            session.modified = True
        except Exception:
            pass
        
        def multi_title_match(items, query_str, title_key='title'):
            if not query_str:
                return items
            titles = [t.strip().lower() for t in query_str.replace(' and ', ',').split(',') if t.strip()]
            if len(titles) <= 1:
                return [i for i in items if query_str.lower() in i.get(title_key, '').lower()]
            matched = []
            for i in items:
                item_title = i.get(title_key, '').lower()
                for t in titles:
                    if t in item_title:
                        matched.append(i)
                        break
            return matched
        
        if intent == 'search_show':
            if not sonarr_url or not sonarr_key:
                return jsonify({'reply': 'Sonarr is not configured. Go to Settings to add your Sonarr URL and API key.'})
            
            results = requests.get(f"{sonarr_url}/api/v3/series/lookup", params={'term': query, 'apikey': sonarr_key}, timeout=15).json()
            profiles = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={'apikey': sonarr_key}, timeout=10).json()
            
            results = results[:8] if isinstance(results, list) else []
            profiles = [{'id': p['id'], 'name': p['name']} for p in profiles] if isinstance(profiles, list) else []
            
            if not results:
                return jsonify({'reply': f'No TV shows found for "{query}".'})
            
            return jsonify({
                'reply': reply,
                'cards': {
                    'mediaType': 'show',
                    'results': results,
                    'profiles': profiles
                }
            })
        
        elif intent == 'search_movie':
            if not radarr_url or not radarr_key:
                return jsonify({'reply': 'Radarr is not configured. Go to Settings to add your Radarr URL and API key.'})
            
            results = requests.get(f"{radarr_url}/api/v3/movie/lookup", params={'term': query, 'apikey': radarr_key}, timeout=15).json()
            profiles = requests.get(f"{radarr_url}/api/v3/qualityprofile", params={'apikey': radarr_key}, timeout=10).json()
            
            results = results[:8] if isinstance(results, list) else []
            profiles = [{'id': p['id'], 'name': p['name']} for p in profiles] if isinstance(profiles, list) else []
            
            if not results:
                return jsonify({'reply': f'No movies found for "{query}".'})
            
            return jsonify({
                'reply': reply,
                'cards': {
                    'mediaType': 'movie',
                    'results': results,
                    'profiles': profiles
                }
            })
        
        elif intent == 'check_lib_show':
            if not sonarr_url or not sonarr_key:
                return jsonify({'reply': 'Sonarr is not configured.'})
            
            try:
                resp = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15)
                resp.raise_for_status()
                all_series = resp.json()
                if not isinstance(all_series, list):
                    return jsonify({'reply': f'Unexpected response from Sonarr. Please check your Sonarr settings.'})
            except Exception as e:
                return jsonify({'reply': f'Could not reach Sonarr: {str(e)}'})
            
            matches = multi_title_match(all_series, query)
            
            if not matches:
                return jsonify({'reply': f'**{query}** isn\'t in your Sonarr library. Want me to search for it to add?'})
            
            lines = []
            for s in matches:
                monitored = "Monitored" if s.get('monitored') else "Not monitored"
                eps = s.get('statistics', {}).get('episodeFileCount', 0)
                total = s.get('statistics', {}).get('totalEpisodeCount', 0)
                size_bytes = s.get('statistics', {}).get('sizeOnDisk', 0)
                size_gb = round(size_bytes / (1024**3), 1) if size_bytes else 0
                lines.append(f"**{s['title']}** ({s.get('year', '')}) — {monitored} — {eps}/{total} episodes — {size_gb} GB")
            
            return jsonify({'reply': reply, 'data': '\n'.join(lines)})
        
        elif intent == 'check_lib_movie':
            if not radarr_url or not radarr_key:
                return jsonify({'reply': 'Radarr is not configured.'})
            
            try:
                resp = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15)
                resp.raise_for_status()
                all_movies = resp.json()
                if not isinstance(all_movies, list):
                    return jsonify({'reply': f'Unexpected response from Radarr. Please check your Radarr settings.'})
            except Exception as e:
                return jsonify({'reply': f'Could not reach Radarr: {str(e)}'})
            
            matches = multi_title_match(all_movies, query)
            
            if not matches:
                return jsonify({'reply': f'**{query}** isn\'t in your Radarr library. Want me to search for it to add?'})
            
            lines = []
            for m in matches:
                has_file = "Downloaded" if m.get('hasFile') else "Not downloaded"
                size_bytes = m.get('sizeOnDisk', 0)
                size_gb = round(size_bytes / (1024**3), 1) if size_bytes else 0
                lines.append(f"**{m['title']}** ({m.get('year', '')}) — {has_file} — {size_gb} GB")
            
            return jsonify({'reply': reply, 'data': '\n'.join(lines)})
        
        elif intent == 'queue_sonarr':
            if not sonarr_url or not sonarr_key:
                return jsonify({'reply': 'Sonarr is not configured.'})
            
            queue_data = requests.get(f"{sonarr_url}/api/v3/queue", params={'apikey': sonarr_key}, timeout=10).json()
            records = queue_data.get('records', [])
            
            if not records:
                return jsonify({'reply': 'Sonarr download queue is empty. Nothing downloading right now.'})
            
            lines = [f"📺 **Sonarr Queue ({len(records)})**:"]
            for item in records:
                title = item.get('series', {}).get('title', 'Unknown')
                ep_title = item.get('episode', {}).get('title', '')
                status = item.get('status', '')
                size = item.get('size', 0)
                sizeleft = item.get('sizeleft', 0)
                pct = round((1 - sizeleft/size) * 100) if size else 0
                lines.append(f"**{title}** — {ep_title} — {status} {pct}%")
            
            return jsonify({'reply': reply, 'data': '\n'.join(lines)})
        
        elif intent == 'queue_radarr':
            if not radarr_url or not radarr_key:
                return jsonify({'reply': 'Radarr is not configured.'})
            
            queue_data = requests.get(f"{radarr_url}/api/v3/queue", params={'apikey': radarr_key}, timeout=10).json()
            records = queue_data.get('records', [])
            
            if not records:
                return jsonify({'reply': 'Radarr download queue is empty. Nothing downloading right now.'})
            
            lines = [f"🎬 **Radarr Queue ({len(records)})**:"]
            for item in records:
                title = item.get('movie', {}).get('title', 'Unknown')
                status = item.get('status', '')
                size = item.get('size', 0)
                sizeleft = item.get('sizeleft', 0)
                pct = round((1 - sizeleft/size) * 100) if size else 0
                lines.append(f"**{title}** — {status} {pct}%")
            
            return jsonify({'reply': reply, 'data': '\n'.join(lines)})
        
        elif intent == 'plex_library':
            if not plex_url or not plex_token:
                return jsonify({'reply': 'Plex is not configured.'})
            
            sections = requests.get(f"{plex_url}/library/sections", params={'X-Plex-Token': plex_token}, headers={'Accept': 'application/json'}, timeout=10).json()
            dirs = sections.get('MediaContainer', {}).get('Directory', [])
            
            if not dirs:
                return jsonify({'reply': 'No Plex libraries found.'})
            
            lines = ["🟡 **Plex Libraries**:"]
            for d in dirs:
                icon = "🎬" if d.get('type') == 'movie' else "📺"
                lines.append(f"{icon} **{d.get('title', '')}** ({d.get('type', '')})")
            
            return jsonify({'reply': reply, 'data': '\n'.join(lines)})
        
        elif intent == 'plex_watchlist_add':
            if not plex_token:
                return jsonify({'reply': 'Plex is not configured. Add your Plex token in Settings.'})
            
            try:
                plex_headers = {
                    'X-Plex-Token': plex_token,
                    'X-Plex-Client-Identifier': 'media-scrubber-chat',
                    'X-Plex-Product': 'Media Scrubber',
                    'X-Plex-Version': '1.0',
                    'Accept': 'application/json'
                }
                search_resp = requests.get(
                    "https://discover.provider.plex.tv/library/search",
                    params={
                        'query': query,
                        'limit': 8,
                        'searchTypes': 'tv',
                        'searchProviders': 'discover'
                    },
                    headers=plex_headers,
                    timeout=15
                )
                search_resp.raise_for_status()
                tv_data = search_resp.json()
                
                movie_resp = requests.get(
                    "https://discover.provider.plex.tv/library/search",
                    params={
                        'query': query,
                        'limit': 8,
                        'searchTypes': 'movie',
                        'searchProviders': 'discover'
                    },
                    headers=plex_headers,
                    timeout=15
                )
                movie_resp.raise_for_status()
                movie_data = movie_resp.json()
                
                all_metadata = []
                for data in [tv_data, movie_data]:
                    container = data.get('MediaContainer', {})
                    all_metadata.extend(_parse_plex_discover_results(container))
                
                if not all_metadata:
                    return jsonify({'reply': f'No results found on Plex for "{query}". Try a different search term.'})
                
                watchlist_items = []
                for metadata in all_metadata[:8]:
                    title = metadata.get('title', '')
                    year = metadata.get('year', '')
                    media_type = metadata.get('type', '')
                    rating_key = metadata.get('ratingKey', '')
                    guid = metadata.get('guid', '')
                    thumb = metadata.get('thumb', '')
                    
                    poster_url = thumb if thumb and thumb.startswith('http') else None
                    
                    watchlist_items.append({
                        'title': title,
                        'year': year,
                        'type': media_type,
                        'ratingKey': rating_key,
                        'guid': guid,
                        'posterUrl': poster_url
                    })
                
                if not watchlist_items:
                    return jsonify({'reply': f'No results found on Plex for "{query}".'})
                
                return jsonify({
                    'reply': reply,
                    'cards': {
                        'mediaType': 'watchlist',
                        'results': watchlist_items,
                        'profiles': []
                    }
                })
            except Exception as e:
                print(f"[Plex Watchlist Search Error] {str(e)}")
                return jsonify({'reply': 'Could not search Plex. Please check your Plex token in Settings.'})
        
        elif intent == 'plex_watchlist_show':
            if not plex_token:
                return jsonify({'reply': 'Plex is not configured. Add your Plex token in Settings.'})
            
            plex_headers = {
                'X-Plex-Token': plex_token,
                'X-Plex-Client-Identifier': 'media-scrubber-chat',
                'X-Plex-Product': 'Media Scrubber',
                'X-Plex-Version': '1.0',
                'Accept': 'application/json'
            }
            
            try:
                all_items = []
                offset = 0
                page_size = 50
                total_size = None
                
                while True:
                    wl_resp = requests.get(
                        "https://discover.provider.plex.tv/library/sections/watchlist/all",
                        params={'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': page_size},
                        headers=plex_headers,
                        timeout=15
                    )
                    
                    if wl_resp.status_code == 401:
                        return jsonify({'reply': 'Plex authentication failed. Your Plex token may have expired. Please update it in Settings.'})
                    
                    if wl_resp.status_code != 200:
                        print(f"[Plex Watchlist] Status {wl_resp.status_code}: {wl_resp.text[:300]}")
                        return jsonify({'reply': f'Plex returned an unexpected response (status {wl_resp.status_code}). Try again or check your Plex token.'})
                    
                    wl_data = wl_resp.json()
                    container = wl_data.get('MediaContainer', {})
                    items = container.get('Metadata', [])
                    if total_size is None:
                        total_size = container.get('totalSize', len(items))
                    
                    if not items:
                        break
                    
                    all_items.extend(items)
                    offset += len(items)
                    
                    if offset >= total_size:
                        break
                
                if not all_items:
                    return jsonify({'reply': 'Your Plex watchlist is empty.'})
                
                lines = [f"🟡 **Plex Watchlist ({total_size} items)**:"]
                for item in all_items:
                    icon = "🎬" if item.get('type') == 'movie' else "📺"
                    year = f" ({item.get('year')})" if item.get('year') else ""
                    lines.append(f"{icon} **{item.get('title', '')}**{year}")
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except requests.exceptions.ConnectionError:
                print(f"[Plex Watchlist] Connection error to metadata.provider.plex.tv")
                return jsonify({'reply': 'Could not connect to Plex servers. Please try again in a moment.'})
            except Exception as e:
                print(f"[Plex Watchlist Show Error] {str(e)}")
                return jsonify({'reply': 'Could not fetch your Plex watchlist. Please check your Plex token in Settings.'})
        
        elif intent == 'delete_show':
            if not sonarr_url or not sonarr_key:
                return jsonify({'reply': 'Sonarr is not configured.'})
            
            all_series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json()
            
            if filters:
                matches = list(all_series) if isinstance(all_series, list) else []
                if filters.get('before_year'):
                    matches = [s for s in matches if s.get('year', 9999) < filters['before_year']]
                if filters.get('after_year'):
                    matches = [s for s in matches if s.get('year', 0) > filters['after_year']]
                if filters.get('status') == 'ended':
                    matches = [s for s in matches if s.get('status', '').lower() in ('ended', 'deleted')]
                if filters.get('has_file') is False:
                    matches = [s for s in matches if s.get('statistics', {}).get('episodeFileCount', 0) == 0]
                if query:
                    matches = multi_title_match(matches, query)
            else:
                matches = multi_title_match(all_series, query)
            
            if not matches:
                filter_desc = query or 'your filters'
                return jsonify({'reply': f'No shows matching {filter_desc} found in your Sonarr library.'})
            
            delete_items = []
            for s in matches:
                size_gb = round(s.get('statistics', {}).get('sizeOnDisk', 0) / (1024**3), 1)
                eps = s.get('statistics', {}).get('episodeFileCount', 0)
                poster = None
                for img in s.get('images', []):
                    if img.get('coverType') == 'poster' and img.get('remoteUrl'):
                        poster = img['remoteUrl']
                        break
                delete_items.append({
                    'id': s['id'],
                    'title': s['title'],
                    'year': s.get('year', ''),
                    'remotePoster': poster,
                    'sizeGb': size_gb,
                    'episodeCount': eps
                })
            
            return jsonify({
                'reply': reply,
                'cards': {
                    'mediaType': 'delete_show',
                    'results': delete_items,
                    'profiles': []
                }
            })
        
        elif intent == 'delete_movie':
            if not radarr_url or not radarr_key:
                return jsonify({'reply': 'Radarr is not configured.'})
            
            all_movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json()
            
            if filters:
                matches = list(all_movies) if isinstance(all_movies, list) else []
                if filters.get('before_year'):
                    matches = [m for m in matches if m.get('year', 9999) < filters['before_year']]
                if filters.get('after_year'):
                    matches = [m for m in matches if m.get('year', 0) > filters['after_year']]
                if filters.get('has_file') is False:
                    matches = [m for m in matches if not m.get('hasFile')]
                if query:
                    matches = multi_title_match(matches, query)
            else:
                matches = multi_title_match(all_movies, query)
            
            if not matches:
                filter_desc = query or 'your filters'
                return jsonify({'reply': f'No movies matching {filter_desc} found in your Radarr library.'})
            
            delete_items = []
            for m in matches:
                size_gb = round(m.get('sizeOnDisk', 0) / (1024**3), 1)
                has_file = m.get('hasFile', False)
                poster = None
                for img in m.get('images', []):
                    if img.get('coverType') == 'poster' and img.get('remoteUrl'):
                        poster = img['remoteUrl']
                        break
                delete_items.append({
                    'id': m['id'],
                    'title': m['title'],
                    'year': m.get('year', ''),
                    'remotePoster': poster,
                    'sizeGb': size_gb,
                    'hasFile': has_file
                })
            
            return jsonify({
                'reply': reply,
                'cards': {
                    'mediaType': 'delete_movie',
                    'results': delete_items,
                    'profiles': []
                }
            })
        
        elif intent == 'plex_watchlist_remove':
            if not plex_token:
                return jsonify({'reply': 'Plex is not configured. Add your Plex token in Settings.'})
            
            plex_headers = {
                'X-Plex-Token': plex_token,
                'X-Plex-Client-Identifier': 'media-scrubber-chat',
                'X-Plex-Product': 'Media Scrubber',
                'X-Plex-Version': '1.0',
                'Accept': 'application/json'
            }
            
            try:
                all_items = []
                offset = 0
                page_size = 50
                total_size = None
                while True:
                    wl_resp = requests.get(
                        "https://discover.provider.plex.tv/library/sections/watchlist/all",
                        params={'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': page_size},
                        headers=plex_headers,
                        timeout=15
                    )
                    if wl_resp.status_code != 200:
                        return jsonify({'reply': 'Could not fetch your Plex watchlist. Please check your token.'})
                    wl_data = wl_resp.json()
                    container = wl_data.get('MediaContainer', {})
                    items = container.get('Metadata', [])
                    if total_size is None:
                        total_size = container.get('totalSize', len(items))
                    if not items:
                        break
                    all_items.extend(items)
                    offset += len(items)
                    if offset >= total_size:
                        break
                
                if filters:
                    matches = list(all_items)
                    if filters.get('before_year'):
                        matches = [i for i in matches if i.get('year', 9999) < filters['before_year']]
                    if filters.get('after_year'):
                        matches = [i for i in matches if i.get('year', 0) > filters['after_year']]
                    if filters.get('type'):
                        type_map = {'movie': 'movie', 'show': 'show', 'tv': 'show'}
                        target_type = type_map.get(filters['type'].lower(), filters['type'].lower())
                        matches = [i for i in matches if i.get('type', '').lower() == target_type]
                    if filters.get('all'):
                        pass
                    if query and not filters.get('all'):
                        matches = multi_title_match(matches, query)
                elif query:
                    matches = multi_title_match(all_items, query)
                else:
                    return jsonify({'reply': 'Please specify what to remove from your watchlist — a title or a filter like "before 2020".'})
                
                if not matches:
                    filter_desc = query or 'your filters'
                    return jsonify({'reply': f'No watchlist items matching {filter_desc} found.'})
                
                remove_items = []
                for item in matches:
                    thumb = item.get('thumb', '')
                    poster_url = thumb if thumb and thumb.startswith('http') else None
                    remove_items.append({
                        'title': item.get('title', ''),
                        'year': item.get('year', ''),
                        'type': item.get('type', ''),
                        'ratingKey': item.get('ratingKey', ''),
                        'posterUrl': poster_url
                    })
                
                return jsonify({
                    'reply': reply,
                    'cards': {
                        'mediaType': 'watchlist_remove',
                        'results': remove_items,
                        'profiles': []
                    }
                })
            except Exception as e:
                print(f"[Plex Watchlist Remove Error] {str(e)}")
                return jsonify({'reply': 'Could not search your Plex watchlist. Please try again.'})
        
        elif intent == 'plex_recently_added':
            if not plex_url or not plex_token:
                return jsonify({'reply': 'Plex is not configured.'})
            
            try:
                recent_resp = requests.get(
                    f"{plex_url}/library/recentlyAdded",
                    params={'X-Plex-Token': plex_token, 'X-Plex-Container-Size': 20},
                    headers={'Accept': 'application/json'},
                    timeout=15
                )
                recent_resp.raise_for_status()
                recent_data = recent_resp.json()
                items = recent_data.get('MediaContainer', {}).get('Metadata', [])
                
                if not items:
                    return jsonify({'reply': 'No recently added items found in Plex.'})
                
                lines = [f"🆕 **Recently Added to Plex ({len(items)} items)**:"]
                for item in items:
                    media_type = item.get('type', '')
                    if media_type == 'movie':
                        icon = "🎬"
                        year = f" ({item.get('year', '')})" if item.get('year') else ""
                        lines.append(f"{icon} **{item.get('title', '')}**{year}")
                    elif media_type == 'season':
                        icon = "📺"
                        show_title = item.get('parentTitle', item.get('title', ''))
                        season = item.get('title', '')
                        lines.append(f"{icon} **{show_title}** — {season}")
                    elif media_type == 'episode':
                        icon = "📺"
                        show_title = item.get('grandparentTitle', '')
                        ep_title = item.get('title', '')
                        s = item.get('parentIndex', '')
                        e = item.get('index', '')
                        lines.append(f"{icon} **{show_title}** — S{s:02d}E{e:02d} {ep_title}" if isinstance(s, int) and isinstance(e, int) else f"{icon} **{show_title}** — {ep_title}")
                    else:
                        lines.append(f"**{item.get('title', '')}**")
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except Exception as e:
                print(f"[Plex Recently Added Error] {str(e)}")
                return jsonify({'reply': 'Could not fetch recently added items from Plex.'})
        
        elif intent == 'missing_episodes':
            if not sonarr_url or not sonarr_key:
                return jsonify({'reply': 'Sonarr is not configured.'})
            
            try:
                if query:
                    all_series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json()
                    matches = [s for s in all_series if query.lower() in s.get('title', '').lower()]
                    
                    if matches:
                        lines = []
                        for s in matches:
                            stats = s.get('statistics', {})
                            total = stats.get('totalEpisodeCount', 0)
                            have = stats.get('episodeFileCount', 0)
                            missing = total - have
                            pct = round(have / total * 100) if total else 0
                            if missing > 0:
                                lines.append(f"📺 **{s['title']}** — {have}/{total} episodes ({pct}%) — **{missing} missing**")
                            else:
                                lines.append(f"✅ **{s['title']}** — All {total} episodes downloaded!")
                        return jsonify({'reply': reply, 'data': '\n'.join(lines)})
                
                wanted = requests.get(
                    f"{sonarr_url}/api/v3/wanted/missing",
                    params={'apikey': sonarr_key, 'pageSize': 30, 'sortKey': 'airDateUtc', 'sortDirection': 'descending'},
                    timeout=15
                ).json()
                records = wanted.get('records', [])
                total_missing = wanted.get('totalRecords', 0)
                
                if not records:
                    return jsonify({'reply': 'No missing episodes found. Your library is fully up to date!'})
                
                lines = [f"⚠️ **Missing Episodes ({total_missing} total)**:"]
                for ep in records[:20]:
                    show = ep.get('series', {}).get('title', 'Unknown')
                    s_num = ep.get('seasonNumber', 0)
                    e_num = ep.get('episodeNumber', 0)
                    ep_title = ep.get('title', '')
                    air_date = ep.get('airDate', '')
                    lines.append(f"📺 **{show}** — S{s_num:02d}E{e_num:02d} {ep_title} ({air_date})")
                if total_missing > 20:
                    lines.append(f"\n*Showing 20 of {total_missing} missing episodes.*")
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except Exception as e:
                print(f"[Missing Episodes Error] {str(e)}")
                return jsonify({'reply': 'Could not check for missing episodes. Please try again.'})
        
        elif intent == 'disk_space':
            try:
                lines = ["💾 **Storage Overview**:"]
                
                if sonarr_url and sonarr_key:
                    try:
                        roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=10).json()
                        for r in roots:
                            free_gb = round(r.get('freeSpace', 0) / (1024**3), 1)
                            path = r.get('path', 'Unknown')
                            lines.append(f"📺 **Sonarr** ({path}) — {free_gb} GB free")
                    except Exception:
                        lines.append("📺 Sonarr — Could not fetch")
                
                if radarr_url and radarr_key:
                    try:
                        roots = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=10).json()
                        for r in roots:
                            free_gb = round(r.get('freeSpace', 0) / (1024**3), 1)
                            path = r.get('path', 'Unknown')
                            lines.append(f"🎬 **Radarr** ({path}) — {free_gb} GB free")
                    except Exception:
                        lines.append("🎬 Radarr — Could not fetch")
                
                if sonarr_url and sonarr_key:
                    try:
                        all_series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json()
                        total_shows = len(all_series)
                        total_size = sum(s.get('statistics', {}).get('sizeOnDisk', 0) for s in all_series)
                        total_eps = sum(s.get('statistics', {}).get('episodeFileCount', 0) for s in all_series)
                        lines.append(f"\n📺 **TV Library**: {total_shows} shows, {total_eps} episodes, {round(total_size / (1024**3), 1)} GB")
                    except Exception:
                        pass
                
                if radarr_url and radarr_key:
                    try:
                        all_movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json()
                        total_movies = len(all_movies)
                        downloaded = sum(1 for m in all_movies if m.get('hasFile'))
                        total_size = sum(m.get('sizeOnDisk', 0) for m in all_movies)
                        lines.append(f"🎬 **Movie Library**: {total_movies} movies ({downloaded} downloaded), {round(total_size / (1024**3), 1)} GB")
                    except Exception:
                        pass
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except Exception as e:
                print(f"[Disk Space Error] {str(e)}")
                return jsonify({'reply': 'Could not retrieve storage information.'})
        
        elif intent == 'ombi_requests':
            ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
            ombi_key = get_setting('OMBI_API_KEY', '').strip()
            
            if not ombi_url or not ombi_key:
                return jsonify({'reply': 'Ombi is not configured. Add your Ombi URL and API key in Settings.'})
            
            try:
                ombi_headers = {'ApiKey': ombi_key, 'Accept': 'application/json'}
                
                tv_resp = requests.get(f"{ombi_url}/api/v1/Request/tv", headers=ombi_headers, timeout=15)
                movie_resp = requests.get(f"{ombi_url}/api/v1/Request/movie", headers=ombi_headers, timeout=15)
                
                if tv_resp.status_code == 401 or movie_resp.status_code == 401:
                    return jsonify({'reply': 'Ombi authentication failed. Please check your Ombi API key in Settings.'})
                
                tv_requests = tv_resp.json() if tv_resp.ok else []
                movie_requests = movie_resp.json() if movie_resp.ok else []
                
                lines = []
                
                pending_tv = []
                if isinstance(tv_requests, list):
                    for r in tv_requests:
                        child_reqs = r.get('childRequests', [])
                        if child_reqs and not child_reqs[0].get('approved', True):
                            pending_tv.append(r)
                pending_movies = [r for r in movie_requests if not r.get('approved', True)] if isinstance(movie_requests, list) else []
                
                if pending_movies or pending_tv:
                    lines.append(f"⏳ **Pending Requests ({len(pending_movies) + len(pending_tv)})**:")
                    for m in pending_movies[:10]:
                        lines.append(f"🎬 **{m.get('title', '')}** ({m.get('releaseDate', '')[:4]}) — requested by {m.get('requestedUser', {}).get('userAlias', m.get('requestedUser', {}).get('userName', 'Unknown'))}")
                    for t in pending_tv[:10]:
                        requester = 'Unknown'
                        if t.get('childRequests'):
                            req_user = t['childRequests'][0].get('requestedUser', {})
                            requester = req_user.get('userAlias', req_user.get('userName', 'Unknown'))
                        lines.append(f"📺 **{t.get('title', '')}** — requested by {requester}")
                
                recent_approved_movies = [r for r in movie_requests if r.get('approved') and r.get('available')] if isinstance(movie_requests, list) else []
                recent_approved_tv = []
                if isinstance(tv_requests, list):
                    for t in tv_requests:
                        for cr in t.get('childRequests', []):
                            if cr.get('approved') and cr.get('available'):
                                recent_approved_tv.append(t)
                                break
                
                if recent_approved_movies or recent_approved_tv:
                    lines.append(f"\n✅ **Fulfilled Requests ({len(recent_approved_movies) + len(recent_approved_tv)})**:")
                    for m in recent_approved_movies[:10]:
                        lines.append(f"🎬 **{m.get('title', '')}** — Available")
                    for t in recent_approved_tv[:10]:
                        lines.append(f"📺 **{t.get('title', '')}** — Available")
                
                if not lines:
                    return jsonify({'reply': 'No media requests found in Ombi.'})
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except Exception as e:
                print(f"[Ombi Requests Error] {str(e)}")
                return jsonify({'reply': 'Could not fetch requests from Ombi. Please check your Ombi settings.'})
        
        elif intent == 'calendar':
            try:
                from datetime import datetime, timedelta
                today = datetime.now().strftime('%Y-%m-%d')
                week_later = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
                
                lines = [f"📅 **Upcoming This Week** ({today} to {week_later}):"]
                
                if sonarr_url and sonarr_key:
                    try:
                        cal = requests.get(
                            f"{sonarr_url}/api/v3/calendar",
                            params={'apikey': sonarr_key, 'start': today, 'end': week_later},
                            timeout=15
                        ).json()
                        
                        if cal and isinstance(cal, list):
                            lines.append(f"\n📺 **TV Episodes ({len(cal)})**:")
                            for ep in cal[:15]:
                                show = ep.get('series', {}).get('title', 'Unknown')
                                s_num = ep.get('seasonNumber', 0)
                                e_num = ep.get('episodeNumber', 0)
                                ep_title = ep.get('title', '')
                                air_date = ep.get('airDate', '')
                                has_file = "✅" if ep.get('hasFile') else "⏳"
                                lines.append(f"{has_file} **{show}** — S{s_num:02d}E{e_num:02d} {ep_title} ({air_date})")
                            if len(cal) > 15:
                                lines.append(f"*...and {len(cal) - 15} more*")
                        else:
                            lines.append("\n📺 No upcoming TV episodes this week.")
                    except Exception:
                        lines.append("\n📺 Could not fetch Sonarr calendar.")
                
                if radarr_url and radarr_key:
                    try:
                        cal = requests.get(
                            f"{radarr_url}/api/v3/calendar",
                            params={'apikey': radarr_key, 'start': today, 'end': week_later},
                            timeout=15
                        ).json()
                        
                        if cal and isinstance(cal, list):
                            lines.append(f"\n🎬 **Movies ({len(cal)})**:")
                            for m in cal[:10]:
                                title = m.get('title', 'Unknown')
                                year = m.get('year', '')
                                has_file = "✅" if m.get('hasFile') else "⏳"
                                in_cinemas = m.get('inCinemas', '')[:10] if m.get('inCinemas') else ''
                                digital = m.get('digitalRelease', '')[:10] if m.get('digitalRelease') else ''
                                release = digital or in_cinemas
                                lines.append(f"{has_file} **{title}** ({year}) — {release}")
                        else:
                            lines.append("\n🎬 No upcoming movies this week.")
                    except Exception:
                        lines.append("\n🎬 Could not fetch Radarr calendar.")
                
                return jsonify({'reply': reply, 'data': '\n'.join(lines)})
            except Exception as e:
                print(f"[Calendar Error] {str(e)}")
                return jsonify({'reply': 'Could not fetch calendar data.'})
        
        elif intent == 'recommend':
            try:
                import re as _re
                
                existing_shows = set()
                existing_movies = set()
                
                if sonarr_url and sonarr_key:
                    try:
                        all_series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=10).json()
                        existing_shows = {s.get('title', '').lower().strip() for s in all_series}
                    except Exception:
                        pass
                
                if radarr_url and radarr_key:
                    try:
                        all_movies_list = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=10).json()
                        existing_movies = {m.get('title', '').lower().strip() for m in all_movies_list}
                    except Exception:
                        pass
                
                def is_in_library(title, lib_set):
                    clean = _re.sub(r'\s*[:]\s*season\s+\d+', '', title, flags=_re.IGNORECASE).strip().lower()
                    clean = _re.sub(r'\s*\(?\d{4}\)?$', '', clean).strip()
                    if not clean:
                        return False
                    if clean in lib_set:
                        return True
                    for existing in lib_set:
                        if existing and (clean in existing or existing in clean):
                            return True
                    return False
                
                tmdb_key = os.environ.get('TMDB_API_KEY', '')
                tmdb_base = 'https://api.themoviedb.org/3'
                
                tmdb_anticipated_shows = []
                tmdb_anticipated_movies = []
                tmdb_trending_shows = []
                tmdb_trending_movies = []
                tmdb_upcoming_movies = []
                tmdb_upcoming_shows = []
                seen_show_names = set()
                seen_movie_names = set()
                today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
                one_year = (__import__('datetime').datetime.now() + __import__('datetime').timedelta(days=365)).strftime('%Y-%m-%d')
                
                tv_genres = {10759:'Action & Adventure',16:'Animation',35:'Comedy',80:'Crime',99:'Documentary',18:'Drama',10751:'Family',10762:'Kids',9648:'Mystery',10763:'News',10764:'Reality',10765:'Sci-Fi & Fantasy',10766:'Soap',10767:'Talk',10768:'War & Politics',37:'Western'}
                movie_genres = {28:'Action',12:'Adventure',16:'Animation',35:'Comedy',80:'Crime',99:'Documentary',18:'Drama',10751:'Family',14:'Fantasy',36:'History',27:'Horror',10402:'Music',9648:'Mystery',10749:'Romance',878:'Sci-Fi',10770:'TV Movie',53:'Thriller',10752:'War',37:'Western'}
                
                def add_show(item, target_list):
                    name = item.get('name', '')
                    if not name or name in seen_show_names or is_in_library(name, existing_shows):
                        return
                    lang = item.get('original_language', '')
                    if lang and lang != 'en':
                        return
                    seen_show_names.add(name)
                    year = (item.get('first_air_date', '') or '')[:4]
                    rating = item.get('vote_average', 0)
                    popularity = item.get('popularity', 0)
                    overview = (item.get('overview', '') or '')[:120]
                    first_air = item.get('first_air_date', '')
                    genre_names = [tv_genres.get(gid, '') for gid in item.get('genre_ids', [])]
                    genre_str = ', '.join([g for g in genre_names if g][:3])
                    target_list.append({'title': name, 'year': year, 'rating': round(rating, 1), 'popularity': round(popularity, 1), 'overview': overview, 'air_date': first_air, 'genres': genre_str, 'tmdb_id': item.get('id'), 'media_type': 'tv'})
                
                def add_movie(item, target_list):
                    title = item.get('title', '')
                    if not title or title in seen_movie_names or is_in_library(title, existing_movies):
                        return
                    lang = item.get('original_language', '')
                    if lang and lang != 'en':
                        return
                    seen_movie_names.add(title)
                    year = (item.get('release_date', '') or '')[:4]
                    rating = item.get('vote_average', 0)
                    popularity = item.get('popularity', 0)
                    overview = (item.get('overview', '') or '')[:120]
                    release_date = item.get('release_date', '')
                    genre_names = [movie_genres.get(gid, '') for gid in item.get('genre_ids', [])]
                    genre_str = ', '.join([g for g in genre_names if g][:3])
                    target_list.append({'title': title, 'year': year, 'rating': round(rating, 1), 'popularity': round(popularity, 1), 'overview': overview, 'release_date': release_date, 'genres': genre_str, 'tmdb_id': item.get('id'), 'media_type': 'movie'})
                
                if tmdb_key:
                    try:
                        for page in [1, 2, 3]:
                            resp = requests.get(f"{tmdb_base}/discover/tv", params={
                                'api_key': tmdb_key, 'language': 'en-US', 'sort_by': 'popularity.desc',
                                'first_air_date.gte': today, 'first_air_date.lte': one_year,
                                'with_original_language': 'en', 'page': page,
                                'vote_count.gte': 0
                            }, timeout=10).json()
                            for item in resp.get('results', [])[:20]:
                                add_show(item, tmdb_anticipated_shows)
                    except Exception as e:
                        print(f"[TMDb Anticipated TV Error] {e}")
                    
                    try:
                        for page in [1, 2, 3]:
                            resp = requests.get(f"{tmdb_base}/discover/movie", params={
                                'api_key': tmdb_key, 'language': 'en-US', 'sort_by': 'popularity.desc',
                                'primary_release_date.gte': today, 'primary_release_date.lte': one_year,
                                'with_original_language': 'en', 'page': page,
                                'vote_count.gte': 0
                            }, timeout=10).json()
                            for item in resp.get('results', [])[:20]:
                                add_movie(item, tmdb_anticipated_movies)
                    except Exception as e:
                        print(f"[TMDb Anticipated Movie Error] {e}")
                    
                    try:
                        resp = requests.get(f"{tmdb_base}/trending/tv/week", params={'api_key': tmdb_key, 'language': 'en-US'}, timeout=10).json()
                        for item in resp.get('results', [])[:20]:
                            add_show(item, tmdb_trending_shows)
                    except Exception as e:
                        print(f"[TMDb Trending TV Error] {e}")
                    
                    try:
                        resp = requests.get(f"{tmdb_base}/tv/on_the_air", params={'api_key': tmdb_key, 'language': 'en-US'}, timeout=10).json()
                        for item in resp.get('results', [])[:20]:
                            add_show(item, tmdb_trending_shows)
                    except Exception as e:
                        print(f"[TMDb On Air Error] {e}")
                    
                    try:
                        resp = requests.get(f"{tmdb_base}/trending/movie/week", params={'api_key': tmdb_key, 'language': 'en-US'}, timeout=10).json()
                        for item in resp.get('results', [])[:20]:
                            add_movie(item, tmdb_trending_movies)
                    except Exception as e:
                        print(f"[TMDb Trending Movie Error] {e}")
                    
                    try:
                        resp = requests.get(f"{tmdb_base}/movie/upcoming", params={'api_key': tmdb_key, 'language': 'en-US', 'region': 'US'}, timeout=10).json()
                        for item in resp.get('results', [])[:20]:
                            add_movie(item, tmdb_upcoming_movies)
                    except Exception as e:
                        print(f"[TMDb Upcoming Movie Error] {e}")
                
                def enrich_with_cast(items, max_items=10):
                    from concurrent.futures import ThreadPoolExecutor
                    display_items = items[:max_items]
                    
                    def fetch_credits(item):
                        tmdb_id = item.get('tmdb_id')
                        media_type = item.get('media_type', 'movie')
                        if not tmdb_id or not tmdb_key:
                            return
                        try:
                            url = f"{tmdb_base}/{media_type}/{tmdb_id}"
                            resp = requests.get(url, params={'api_key': tmdb_key, 'language': 'en-US', 'append_to_response': 'credits'}, timeout=8).json()
                            cast = resp.get('credits', {}).get('cast', [])
                            top_cast = [c.get('name', '') for c in cast[:4] if c.get('name')]
                            item['cast'] = ', '.join(top_cast)
                            if media_type == 'tv':
                                networks = resp.get('networks', [])
                                if networks:
                                    item['network'] = networks[0].get('name', '')
                                item['status'] = resp.get('status', '')
                            else:
                                runtime = resp.get('runtime')
                                if runtime:
                                    item['runtime'] = f"{runtime}m"
                        except Exception:
                            pass
                    
                    try:
                        with ThreadPoolExecutor(max_workers=5) as executor:
                            executor.map(fetch_credits, display_items)
                    except Exception:
                        pass
                    
                    return display_items
                
                display_anticipated_shows = enrich_with_cast(tmdb_anticipated_shows, 10)
                display_anticipated_movies = enrich_with_cast(tmdb_anticipated_movies, 10)
                display_trending_shows = enrich_with_cast(tmdb_trending_shows, 8)
                display_trending_movies = enrich_with_cast(tmdb_trending_movies, 8)
                
                def build_item(item, date_key='air_date'):
                    result = {
                        'title': item['title'],
                        'year': item['year'],
                        'rating': item['rating'],
                        'date': item.get(date_key, ''),
                        'overview': item['overview'],
                        'genres': item.get('genres', ''),
                        'cast': item.get('cast', ''),
                    }
                    if item.get('network'):
                        result['network'] = item['network']
                    if item.get('status'):
                        result['status'] = item['status']
                    if item.get('runtime'):
                        result['runtime'] = item['runtime']
                    return result
                
                sections = []
                
                if display_anticipated_shows:
                    sections.append({
                        'label': '🔥 Most Anticipated TV Shows',
                        'addType': 'show',
                        'items': [build_item(s, 'air_date') for s in display_anticipated_shows]
                    })
                
                if display_anticipated_movies:
                    sections.append({
                        'label': '🔥 Most Anticipated Movies',
                        'addType': 'movie',
                        'items': [build_item(m, 'release_date') for m in display_anticipated_movies]
                    })
                
                if display_trending_shows:
                    sections.append({
                        'label': '📺 Trending TV Right Now',
                        'addType': 'show',
                        'items': [build_item(s, 'air_date') for s in display_trending_shows]
                    })
                
                if display_trending_movies:
                    sections.append({
                        'label': '🎬 Trending Movies Right Now',
                        'addType': 'movie',
                        'items': [build_item(m, 'release_date') for m in display_trending_movies]
                    })
                
                if tmdb_upcoming_movies:
                    anticipated_titles = {am['title'] for am in tmdb_anticipated_movies}
                    trending_titles = {tm['title'] for tm in tmdb_trending_movies}
                    new_upcoming = [m for m in tmdb_upcoming_movies if m['title'] not in anticipated_titles and m['title'] not in trending_titles]
                    if new_upcoming:
                        display_upcoming = enrich_with_cast(new_upcoming, 6)
                        sections.append({
                            'label': '🗓️ More Upcoming Movies',
                            'addType': 'movie',
                            'items': [build_item(m, 'release_date') for m in display_upcoming]
                        })
                
                total_items = sum(len(s['items']) for s in sections)
                if total_items == 0:
                    if not tmdb_key:
                        return jsonify({'reply': reply, 'data': '⚠️ TMDb API key not configured. Add TMDB_API_KEY to get real trending data.'})
                    else:
                        return jsonify({'reply': reply, 'data': 'No new recommendations found — your library is very comprehensive!'})
                
                return jsonify({'reply': reply, 'rec_sections': sections})
            except Exception as e:
                print(f"[Recommend Error] {str(e)}")
                import traceback
                traceback.print_exc()
                return jsonify({'reply': 'Could not generate recommendations. Please try again.'})
        
        else:
            # Chitchat — make a dedicated conversational call with full context
            services = []
            if sonarr_url and sonarr_key: services.append("Sonarr (TV shows)")
            if radarr_url and radarr_key: services.append("Radarr (movies)")
            if plex_url and plex_token: services.append("Plex (media server)")
            services_str = ", ".join(services) if services else "no media services configured yet"
            
            conv_messages = [
                {"role": "system", "content": f"""You are a knowledgeable, friendly media assistant helping manage a personal media library. You have access to {services_str}.

You can help with TV shows, movies, recommendations, media management, and general conversation. Be natural, warm, and genuinely helpful — like a knowledgeable friend who loves media. Give thorough, thoughtful answers. If asked about a show or movie, share real details. If asked for an opinion or recommendation, give one confidently.

Keep responses conversational and appropriately concise — not too short, not a wall of text."""}
            ]
            # Include conversation history for context
            conv_messages.extend(chat_history[-12:])
            conv_messages.append({"role": "user", "content": user_message})
            
            conv_response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=conv_messages,
                temperature=0.7,
                max_tokens=800
            )
            conv_reply = conv_response.choices[0].message.content.strip()
            
            # Update history with the actual conversational reply
            try:
                session['chat_history'][-1] = {"role": "assistant", "content": conv_reply}
                session.modified = True
            except Exception:
                pass
            
            return jsonify({'reply': conv_reply})
    
    except Exception as e:
        print(f"[Media Chat Error] {str(e)}")
        return jsonify({'reply': 'Sorry, something went wrong processing your request. Please try again.'})


@app.route('/api/media-chat/add', methods=['POST'])
@login_required
def media_chat_add():
    data = request.get_json()
    media_type = data.get('type')
    item = data.get('item', {})
    profile_id = data.get('profileId')
    custom_root = data.get('rootFolderPath', '').strip()
    
    if media_type == 'show':
        sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
        sonarr_key = get_setting('SONARR_API_KEY', '').strip()
        
        if not sonarr_url or not sonarr_key:
            return jsonify({'success': False, 'error': 'Sonarr not configured'})
        
        try:
            roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=10).json()
            root_path = custom_root if custom_root else (roots[0]['path'] if roots else '/tv')
            
            tvdb_id = item.get('tvdbId')
            try:
                tvdb_id = int(tvdb_id) if tvdb_id else 0
            except (ValueError, TypeError):
                tvdb_id = 0

            # If we have no TVDB ID (e.g. item came from Discover which only has TMDb ID),
            # look up the series in Sonarr to get full metadata including tvdbId.
            lookup_item = None
            if tvdb_id == 0:
                title = item.get('title', '')
                tmdb_id = item.get('tmdbId')
                # Try tmdb: prefix first (Sonarr v3 supports it), fall back to title search
                for term in ([f'tmdb:{tmdb_id}'] if tmdb_id else []) + [title]:
                    try:
                        lk = requests.get(
                            f"{sonarr_url}/api/v3/series/lookup",
                            params={'term': term, 'apikey': sonarr_key},
                            timeout=12
                        ).json()
                        if isinstance(lk, list) and lk:
                            # Pick the best match by title similarity
                            title_lower = title.lower()
                            for candidate in lk:
                                if (candidate.get('title') or '').lower() == title_lower:
                                    lookup_item = candidate
                                    break
                            if not lookup_item:
                                lookup_item = lk[0]
                            tvdb_id = lookup_item.get('tvdbId', 0)
                            break
                    except Exception as le:
                        print(f"[Add Show Lookup] {term}: {le}")

            if tvdb_id == 0:
                return jsonify({'success': False, 'error': f'Could not find "{item.get("title")}" in Sonarr. Try searching for it manually.'})

            qual_id = profile_id
            try:
                qual_id = int(qual_id) if qual_id else None
            except (ValueError, TypeError):
                qual_id = None
            
            if not qual_id:
                profiles = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={'apikey': sonarr_key}, timeout=10).json()
                qual_id = profiles[0]['id'] if profiles else 1

            # Use lookup_item metadata if available (has titleSlug, images, seasons)
            src = lookup_item or item
            payload = {
                'title': src.get('title') or item.get('title'),
                'tvdbId': tvdb_id,
                'qualityProfileId': int(qual_id),
                'titleSlug': src.get('titleSlug') or item.get('titleSlug') or item.get('title', '').lower().replace(' ', '-'),
                'images': src.get('images', []),
                'seasons': src.get('seasons', []),
                'rootFolderPath': root_path,
                'monitored': True,
                'addOptions': {'searchForMissingEpisodes': True}
            }
            
            r = requests.post(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key},
                            json=payload, timeout=15)
            
            if r.ok:
                return jsonify({'success': True, 'message': f'**{item.get("title")}** added to Sonarr and searching for episodes.'})
            
            error_data = r.json()
            error_msg = error_data[0].get('errorMessage', '') if isinstance(error_data, list) and error_data else str(error_data)
            
            if 'already' in error_msg.lower() or 'exists' in error_msg.lower():
                return jsonify({'success': True, 'already_exists': True, 'message': f'**{item.get("title")}** is already in Sonarr.'})
            
            return jsonify({'success': False, 'message': f'Sonarr error: {error_msg}'})
        except Exception as e:
            print(f"[Media Chat Add Error] Sonarr: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to add show to Sonarr. Please try again.'})
    
    elif media_type == 'movie':
        radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
        radarr_key = get_setting('RADARR_API_KEY', '').strip()
        
        if not radarr_url or not radarr_key:
            return jsonify({'success': False, 'error': 'Radarr not configured'})
        
        try:
            roots = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=10).json()
            root_path = custom_root if custom_root else (roots[0]['path'] if roots else '/movies')
            
            tmdb_id = item.get('tmdbId')
            try:
                tmdb_id = int(tmdb_id) if tmdb_id else 0
            except (ValueError, TypeError):
                tmdb_id = 0
            
            qual_id = profile_id
            try:
                qual_id = int(qual_id) if qual_id else None
            except (ValueError, TypeError):
                qual_id = None
            
            if not qual_id:
                profiles = requests.get(f"{radarr_url}/api/v3/qualityprofile", params={'apikey': radarr_key}, timeout=10).json()
                qual_id = profiles[0]['id'] if profiles else 1
            
            movie_year = item.get('year')
            try:
                movie_year = int(movie_year) if movie_year else 0
            except (ValueError, TypeError):
                movie_year = 0
            
            payload = {
                'title': item.get('title'),
                'tmdbId': tmdb_id,
                'qualityProfileId': int(qual_id),
                'titleSlug': item.get('titleSlug') or item.get('title', '').lower().replace(' ', '-'),
                'images': item.get('images', []),
                'year': movie_year,
                'rootFolderPath': root_path,
                'monitored': True,
                'addOptions': {'searchForMovie': True}
            }
            
            r = requests.post(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key},
                            json=payload, timeout=15)
            
            if r.ok:
                return jsonify({'success': True, 'message': f'**{item.get("title")}** added to Radarr and searching for download.'})
            
            error_data = r.json()
            error_msg = error_data[0].get('errorMessage', '') if isinstance(error_data, list) and error_data else str(error_data)
            
            if 'already' in error_msg.lower() or 'exists' in error_msg.lower():
                return jsonify({'success': True, 'already_exists': True, 'message': f'**{item.get("title")}** is already in Radarr.'})
            
            return jsonify({'success': False, 'message': f'Radarr error: {error_msg}'})
        except Exception as e:
            print(f"[Media Chat Add Error] Radarr: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to add movie to Radarr. Please try again.'})
    
    elif media_type == 'watchlist':
        plex_token = get_setting('PLEX_TOKEN', '').strip()
        
        if not plex_token:
            return jsonify({'success': False, 'error': 'Plex not configured'})
        
        rating_key = item.get('ratingKey', '')
        guid = item.get('guid', '')
        title = item.get('title', 'Unknown')
        
        if not rating_key and not guid:
            return jsonify({'success': False, 'error': 'Missing item identifier'})
        
        try:
            plex_headers = {
                'X-Plex-Token': plex_token,
                'X-Plex-Client-Identifier': 'media-scrubber-chat',
                'X-Plex-Product': 'Media Scrubber',
                'X-Plex-Version': '1.0',
                'Accept': 'application/json'
            }
            add_resp = requests.put(
                f"https://discover.provider.plex.tv/actions/addToWatchlist",
                params={'ratingKey': rating_key},
                headers=plex_headers,
                timeout=15
            )
            
            if add_resp.status_code in (200, 201, 204):
                return jsonify({'success': True, 'message': f'**{title}** added to your Plex watchlist!'})
            elif add_resp.status_code == 409:
                return jsonify({'success': True, 'already_exists': True, 'message': f'**{title}** is already on your Plex watchlist.'})
            else:
                print(f"[Plex Watchlist Add] Status {add_resp.status_code}: {add_resp.text[:200]}")
                return jsonify({'success': False, 'message': f'Plex returned status {add_resp.status_code}. The item may not be available for watchlist.'})
        except Exception as e:
            print(f"[Media Chat Add Error] Plex watchlist: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to add to Plex watchlist. Please try again.'})
    
    return jsonify({'success': False, 'error': 'Invalid media type'})


@app.route('/api/media-chat/delete', methods=['POST'])
@login_required
def media_chat_delete():
    data = request.get_json()
    media_type = data.get('type')
    item = data.get('item', {})
    
    if media_type == 'delete_show':
        sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
        sonarr_key = get_setting('SONARR_API_KEY', '').strip()
        
        if not sonarr_url or not sonarr_key:
            return jsonify({'success': False, 'error': 'Sonarr not configured'})
        
        series_id = item.get('id')
        title = item.get('title', 'Unknown')
        
        if not series_id:
            return jsonify({'success': False, 'error': 'Missing show ID'})
        
        try:
            r = requests.delete(
                f"{sonarr_url}/api/v3/series/{series_id}",
                params={'apikey': sonarr_key, 'deleteFiles': 'true'},
                timeout=15
            )
            
            if r.ok:
                return jsonify({'success': True, 'message': f'**{title}** has been deleted from Sonarr and files removed.'})
            else:
                print(f"[Media Chat Delete] Sonarr {r.status_code}: {r.text[:200]}")
                return jsonify({'success': False, 'message': f'Sonarr returned status {r.status_code}.'})
        except Exception as e:
            print(f"[Media Chat Delete Error] Sonarr: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to delete show. Please try again.'})
    
    elif media_type == 'delete_movie':
        radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
        radarr_key = get_setting('RADARR_API_KEY', '').strip()
        
        if not radarr_url or not radarr_key:
            return jsonify({'success': False, 'error': 'Radarr not configured'})
        
        movie_id = item.get('id')
        title = item.get('title', 'Unknown')
        
        if not movie_id:
            return jsonify({'success': False, 'error': 'Missing movie ID'})
        
        try:
            r = requests.delete(
                f"{radarr_url}/api/v3/movie/{movie_id}",
                params={'apikey': radarr_key, 'deleteFiles': 'true'},
                timeout=15
            )
            
            if r.ok:
                return jsonify({'success': True, 'message': f'**{title}** has been deleted from Radarr and files removed.'})
            else:
                print(f"[Media Chat Delete] Radarr {r.status_code}: {r.text[:200]}")
                return jsonify({'success': False, 'message': f'Radarr returned status {r.status_code}.'})
        except Exception as e:
            print(f"[Media Chat Delete Error] Radarr: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to delete movie. Please try again.'})
    
    elif media_type == 'watchlist_remove':
        plex_token = get_setting('PLEX_TOKEN', '').strip()
        
        if not plex_token:
            return jsonify({'success': False, 'error': 'Plex not configured'})
        
        rating_key = item.get('ratingKey', '')
        title = item.get('title', 'Unknown')
        
        if not rating_key:
            return jsonify({'success': False, 'error': 'Missing item identifier'})
        
        try:
            plex_headers = {
                'X-Plex-Token': plex_token,
                'X-Plex-Client-Identifier': 'media-scrubber-chat',
                'X-Plex-Product': 'Media Scrubber',
                'X-Plex-Version': '1.0',
                'Accept': 'application/json'
            }
            remove_resp = requests.put(
                "https://discover.provider.plex.tv/actions/removeFromWatchlist",
                params={'ratingKey': rating_key},
                headers=plex_headers,
                timeout=15
            )
            
            if remove_resp.status_code in (200, 201, 204):
                return jsonify({'success': True, 'message': f'**{title}** removed from your Plex watchlist.'})
            else:
                print(f"[Plex Watchlist Remove] Status {remove_resp.status_code}: {remove_resp.text[:200]}")
                return jsonify({'success': False, 'message': f'Plex returned status {remove_resp.status_code}.'})
        except Exception as e:
            print(f"[Media Chat Delete Error] Plex watchlist: {str(e)}")
            return jsonify({'success': False, 'error': 'Failed to remove from Plex watchlist. Please try again.'})
    
    return jsonify({'success': False, 'error': 'Invalid delete type'})



# ─── Direct Media Browser API (no AI) ────────────────────────────────────────

def _plex_headers(token):
    return {
        'X-Plex-Token': token,
        'X-Plex-Client-Identifier': 'media-scrubber-browser',
        'X-Plex-Product': 'Media Scrubber',
        'X-Plex-Version': '1.0',
        'Accept': 'application/json'
    }

def _normalize_title(title):
    """Normalize a title for fuzzy library matching: lowercase, strip year suffixes, strip punctuation."""
    t = (title or '').lower().strip()
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)   # remove trailing (year)
    t = re.sub(r'\s*:\s*', ' ', t)             # normalize colons
    t = re.sub(r"[''`]", "'", t)              # normalize apostrophes
    t = re.sub(r'[^a-z0-9\' ]', ' ', t)       # strip remaining punctuation
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _library_sets():
    """Return (sonarr_normalized, radarr_normalized) sets for library status checks."""
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    sonarr_set, radarr_set = set(), set()
    if sonarr_url and sonarr_key:
        try:
            data = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=10).json()
            if isinstance(data, list):
                sonarr_set = {_normalize_title(s.get('title', '')) for s in data}
        except Exception:
            pass
    if radarr_url and radarr_key:
        try:
            data = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=10).json()
            if isinstance(data, list):
                radarr_set = {_normalize_title(m.get('title', '')) for m in data}
        except Exception:
            pass
    return sonarr_set, radarr_set


def _in_lib(title, sonarr_set, radarr_set):
    t = _normalize_title(title)
    return t in sonarr_set or t in radarr_set


@app.route('/api/media/search')
@login_required
def media_search():
    q = request.args.get('q', '').strip()
    media_type = request.args.get('type', 'all')  # movie | show | all
    if not q:
        return jsonify({'results': []})

    tmdb_key = os.environ.get('TMDB_API_KEY', '')
    if not tmdb_key:
        return jsonify({'error': 'TMDb API key not configured.', 'results': []})

    sonarr_set, radarr_set = _library_sets()
    results = []

    def fetch_tmdb(endpoint, params):
        try:
            r = requests.get(f"https://api.themoviedb.org/3{endpoint}",
                             params={**params, 'api_key': tmdb_key, 'language': 'en-US'},
                             timeout=10)
            return r.json().get('results', [])
        except Exception:
            return []

    def enrich(items, mtype):
        out = []
        for item in items[:6]:
            title = item.get('title') or item.get('name', '')
            year = (item.get('release_date') or item.get('first_air_date') or '')[:4]
            in_library = _in_lib(title, sonarr_set, radarr_set)
            out.append({
                'tmdbId': item.get('id'),
                'title': title,
                'year': year,
                'overview': item.get('overview', ''),
                'poster': f"https://image.tmdb.org/t/p/w185{item['poster_path']}" if item.get('poster_path') else None,
                'rating': round(item.get('vote_average', 0), 1),
                'mediaType': mtype,
                'inLibrary': in_library,
            })
        return out

    if media_type in ('movie', 'all'):
        items = fetch_tmdb('/search/movie', {'query': q})
        results.extend(enrich(items, 'movie'))
    if media_type in ('show', 'all'):
        items = fetch_tmdb('/search/tv', {'query': q})
        results.extend(enrich(items, 'show'))

    # Sort: in-library last so new results are prominent
    results.sort(key=lambda x: (x['inLibrary'], -float(x['rating'] or 0)))
    return jsonify({'results': results})


@app.route('/api/media/watchlist')
@login_required
def media_watchlist():
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    if not plex_token:
        return jsonify({'error': 'Plex token not configured.', 'items': []})

    headers = _plex_headers(plex_token)
    sonarr_set, radarr_set = _library_sets()

    try:
        all_items = []
        offset = 0
        page_size = 100
        total_size = None

        while True:
            resp = requests.get(
                "https://discover.provider.plex.tv/library/sections/watchlist/all",
                params={'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': page_size},
                headers=headers, timeout=15
            )
            if resp.status_code == 401:
                return jsonify({'error': 'Plex authentication failed. Check your Plex token in Settings.', 'items': []})
            if not resp.ok:
                return jsonify({'error': f'Plex error (status {resp.status_code}).', 'items': []})

            data = resp.json()
            container = data.get('MediaContainer', {})
            items = container.get('Metadata', [])
            if total_size is None:
                total_size = container.get('totalSize', len(items))
            if not items:
                break
            all_items.extend(items)
            offset += len(items)
            if offset >= total_size:
                break

        result = []
        for item in all_items:
            title = item.get('title', '')
            thumb = item.get('thumb', '')
            poster = thumb if thumb and thumb.startswith('http') else None
            result.append({
                'title': title,
                'year': item.get('year', ''),
                'mediaType': item.get('type', ''),
                'ratingKey': item.get('ratingKey', ''),
                'guid': item.get('guid', ''),
                'poster': poster,
                'inLibrary': _in_lib(title, sonarr_set, radarr_set),
            })

        return jsonify({'items': result, 'total': total_size})
    except Exception as e:
        print(f"[Watchlist Error] {e}")
        return jsonify({'error': 'Could not fetch watchlist. Check your Plex token.', 'items': []})


@app.route('/api/media/queue')
@login_required
def media_queue():
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    sonarr_queue, radarr_queue = [], []

    if sonarr_url and sonarr_key:
        try:
            data = requests.get(f"{sonarr_url}/api/v3/queue",
                                params={'apikey': sonarr_key, 'pageSize': 50,
                                        'includeSeries': 'true', 'includeEpisode': 'true'},
                                timeout=10).json()
            for item in data.get('records', []):
                size = item.get('size', 0)
                sizeleft = item.get('sizeleft', 0)
                pct = round((1 - sizeleft / size) * 100) if size else 0
                series_title = (item.get('series') or {}).get('title') or item.get('title', 'Unknown')
                ep_obj = item.get('episode') or {}
                season_num = ep_obj.get('seasonNumber', item.get('seasonNumber', 0))
                ep_num = ep_obj.get('episodeNumber', 0)
                episode_str = f"S{season_num:02d}E{ep_num:02d}" if ep_num else ''
                sonarr_queue.append({
                    'title': series_title,
                    'episode': episode_str,
                    'status': item.get('status', ''),
                    'pct': pct,
                    'size': round((size - sizeleft) / (1024**3), 2),
                    'total': round(size / (1024**3), 2),
                })
        except Exception as e:
            print(f"[Queue Sonarr] {e}")

    if radarr_url and radarr_key:
        try:
            data = requests.get(f"{radarr_url}/api/v3/queue",
                                params={'apikey': radarr_key, 'pageSize': 50, 'includeMovie': 'true'},
                                timeout=10).json()
            for item in data.get('records', []):
                size = item.get('size', 0)
                sizeleft = item.get('sizeleft', 0)
                pct = round((1 - sizeleft / size) * 100) if size else 0
                movie_title = (item.get('movie') or {}).get('title') or item.get('title', 'Unknown')
                radarr_queue.append({
                    'title': movie_title,
                    'episode': '',
                    'status': item.get('status', ''),
                    'pct': pct,
                    'size': round((size - sizeleft) / (1024**3), 2),
                    'total': round(size / (1024**3), 2),
                })
        except Exception as e:
            print(f"[Queue Radarr] {e}")

    return jsonify({'sonarr': sonarr_queue, 'radarr': radarr_queue})


@app.route('/api/media/recent')
@login_required
def media_recent():
    plex_url = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    if not plex_url or not plex_token:
        return jsonify({'error': 'Plex not configured.', 'items': []})

    try:
        resp = requests.get(
            f"{plex_url}/library/recentlyAdded",
            params={'X-Plex-Token': plex_token, 'X-Plex-Container-Size': 40},
            headers={'Accept': 'application/json'}, timeout=10
        )
        resp.raise_for_status()
        items_raw = resp.json().get('MediaContainer', {}).get('Metadata', [])
        items = []
        for item in items_raw:
            mtype = item.get('type', '')
            if mtype == 'movie':
                label = item.get('title', '')
                sub = str(item.get('year', ''))
            elif mtype == 'season':
                label = item.get('parentTitle', item.get('title', ''))
                sub = item.get('title', '')
            elif mtype == 'episode':
                label = item.get('grandparentTitle', '')
                s, e = item.get('parentIndex', ''), item.get('index', '')
                sub = f"S{s:02d}E{e:02d} {item.get('title','')}" if isinstance(s, int) and isinstance(e, int) else item.get('title', '')
            else:
                label = item.get('title', '')
                sub = ''
            thumb = item.get('thumb') or item.get('parentThumb') or item.get('grandparentThumb') or ''
            poster = f"{plex_url}{thumb}?X-Plex-Token={plex_token}" if thumb and not thumb.startswith('http') else thumb
            items.append({'label': label, 'sub': sub, 'type': mtype, 'poster': poster})
        return jsonify({'items': items})
    except Exception as e:
        print(f"[Recent Error] {e}")
        return jsonify({'error': 'Could not fetch recently added.', 'items': []})


@app.route('/api/media/calendar')
@login_required
def media_calendar():
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    today = datetime.now().strftime('%Y-%m-%d')
    end = (datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d')
    episodes, movies = [], []

    if sonarr_url and sonarr_key:
        try:
            cal = requests.get(f"{sonarr_url}/api/v3/calendar",
                               params={'apikey': sonarr_key, 'start': today, 'end': end,
                                       'includeSeries': 'true'}, timeout=10).json()
            for ep in (cal if isinstance(cal, list) else []):
                show_title = (ep.get('series') or {}).get('title') or ep.get('seriesTitle', 'Unknown')
                episodes.append({
                    'show': show_title,
                    'episode': f"S{ep.get('seasonNumber',0):02d}E{ep.get('episodeNumber',0):02d}",
                    'title': ep.get('title', ''),
                    'airDate': ep.get('airDate', ''),
                    'hasFile': ep.get('hasFile', False),
                })
        except Exception as e:
            print(f"[Calendar Sonarr] {e}")

    if radarr_url and radarr_key:
        try:
            cal = requests.get(f"{radarr_url}/api/v3/calendar",
                               params={'apikey': radarr_key, 'start': today, 'end': end}, timeout=10).json()
            for m in (cal if isinstance(cal, list) else []):
                movies.append({
                    'title': m.get('title', 'Unknown'),
                    'year': m.get('year', ''),
                    'date': (m.get('digitalRelease') or m.get('inCinemas') or '')[:10],
                    'hasFile': m.get('hasFile', False),
                })
        except Exception as e:
            print(f"[Calendar Radarr] {e}")

    return jsonify({'episodes': episodes, 'movies': movies, 'from': today, 'to': end})


@app.route('/api/media/discover')
@login_required
def media_discover():
    """Return trending, upcoming, and streaming content from TMDb (English only)."""
    tmdb_key = os.environ.get('TMDB_API_KEY', '').strip()
    if not tmdb_key:
        return jsonify({'error': 'TMDb API key not configured.'})

    tmdb_base = 'https://api.themoviedb.org/3'
    img_base = 'https://image.tmdb.org/t/p/w342'

    # Build a set of known-library titles from Sonarr + Radarr (most reliable)
    # and also Plex as a supplement.
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    plex_url   = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    lib_titles = set()

    # Radarr — movies in library
    if radarr_url and radarr_key:
        try:
            movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=10).json()
            if isinstance(movies, list):
                for m in movies:
                    t = (m.get('title') or '').lower().strip()
                    if t:
                        lib_titles.add(t)
        except Exception as e:
            print(f"[Discover] Radarr lib fetch: {e}")

    # Sonarr — TV shows in library
    if sonarr_url and sonarr_key:
        try:
            series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=10).json()
            if isinstance(series, list):
                for s in series:
                    t = (s.get('title') or '').lower().strip()
                    if t:
                        lib_titles.add(t)
        except Exception as e:
            print(f"[Discover] Sonarr lib fetch: {e}")

    # Plex — supplement with any items Sonarr/Radarr may not know about
    # type=1 (movies), type=2 (shows) — NOT type=4 which is episodes
    if plex_url and plex_token:
        try:
            for ptype in [1, 2]:
                r = requests.get(
                    f"{plex_url}/library/all",
                    params={'X-Plex-Token': plex_token, 'type': ptype},
                    headers={'Accept': 'application/json'}, timeout=8
                )
                if r.ok:
                    for m in r.json().get('MediaContainer', {}).get('Metadata', []):
                        t = (m.get('title') or '').lower().strip()
                        if t:
                            lib_titles.add(t)
        except Exception as e:
            print(f"[Discover] Plex lib fetch: {e}")

    def make_item(raw, media_type):
        title = raw.get('title') or raw.get('name', '')
        date_str = raw.get('release_date') or raw.get('first_air_date') or ''
        year = int(date_str[:4]) if date_str[:4].isdigit() else None
        poster = (img_base + raw['poster_path']) if raw.get('poster_path') else None
        tmdb_id = raw.get('id')
        url_type = 'movie' if media_type == 'movie' else 'tv'
        return {
            'title': title,
            'year': year,
            'tmdbId': tmdb_id,
            'tmdbUrl': f'https://www.themoviedb.org/{url_type}/{tmdb_id}' if tmdb_id else None,
            'mediaType': media_type,
            'rating': round(raw.get('vote_average') or 0, 1),
            'voteCount': raw.get('vote_count', 0),
            'popularity': round(raw.get('popularity') or 0, 1),
            'overview': (raw.get('overview') or '')[:160],
            'poster': poster,
            'releaseDate': date_str or None,
            'inLibrary': title.lower().strip() in lib_titles,
        }

    def fetch(url, params, media_type, limit=20):
        try:
            resp = requests.get(url, params=params, timeout=10).json()
            out = []
            seen = set()
            for item in resp.get('results', []):
                if item.get('original_language') != 'en':
                    continue
                t = (item.get('title') or item.get('name') or '').strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                out.append(make_item(item, media_type))
                if len(out) >= limit:
                    break
            return out
        except Exception as e:
            print(f"[Discover] {url}: {e}")
            return []

    base_params = {'api_key': tmdb_key, 'language': 'en-US'}
    today = datetime.now().strftime('%Y-%m-%d')
    future_90 = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')
    # Major English streaming providers: Netflix, Prime, Disney+, Apple TV+, Hulu, Max, Peacock, Paramount+
    streaming_providers = '8|9|15|337|350|386|531|1899'

    trending_movies = fetch(
        f"{tmdb_base}/trending/movie/week", {**base_params}, 'movie', 20)
    trending_tv = fetch(
        f"{tmdb_base}/trending/tv/week", {**base_params}, 'tv', 20)
    coming_soon = fetch(
        f"{tmdb_base}/discover/movie",
        {**base_params, 'sort_by': 'popularity.desc', 'with_original_language': 'en',
         'primary_release_date.gte': today, 'primary_release_date.lte': future_90,
         'region': 'US'}, 'movie', 20)
    upcoming_tv = fetch(
        f"{tmdb_base}/discover/tv",
        {**base_params, 'sort_by': 'popularity.desc', 'with_original_language': 'en',
         'first_air_date.gte': today, 'first_air_date.lte': future_90}, 'tv', 20)
    streaming_now = fetch(
        f"{tmdb_base}/discover/movie",
        {**base_params, 'sort_by': 'popularity.desc', 'with_original_language': 'en',
         'with_watch_providers': streaming_providers, 'watch_region': 'US'}, 'movie', 20)
    new_on_tv = fetch(
        f"{tmdb_base}/discover/tv",
        {**base_params, 'sort_by': 'popularity.desc', 'with_original_language': 'en',
         'with_watch_providers': streaming_providers, 'watch_region': 'US',
         'air_date.gte': (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')}, 'tv', 20)

    return jsonify({
        'trending_movies': trending_movies,
        'trending_tv': trending_tv,
        'coming_soon': coming_soon,
        'upcoming_tv': upcoming_tv,
        'streaming_now': streaming_now,
        'new_on_tv': new_on_tv,
    })


@app.route('/api/radarr/profiles')
@login_required
def radarr_profiles():
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    if not radarr_url or not radarr_key:
        return jsonify({'profiles': [], 'folders': [], 'error': 'Radarr not configured'})
    try:
        profs = requests.get(f"{radarr_url}/api/v3/qualityprofile", params={'apikey': radarr_key}, timeout=10).json()
        profiles = [{'id': p['id'], 'name': p['name']} for p in profs] if isinstance(profs, list) else []
        roots = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=10).json()
        folders = [{'path': r['path'], 'freeGB': round(r.get('freeSpace', 0) / 1e9, 1)} for r in roots] if isinstance(roots, list) else []
        return jsonify({'profiles': profiles, 'folders': folders})
    except Exception as e:
        return jsonify({'profiles': [], 'folders': [], 'error': str(e)})


@app.route('/api/sonarr/profiles')
@login_required
def sonarr_profiles():
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    if not sonarr_url or not sonarr_key:
        return jsonify({'profiles': [], 'folders': [], 'error': 'Sonarr not configured'})
    try:
        profs = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={'apikey': sonarr_key}, timeout=10).json()
        profiles = [{'id': p['id'], 'name': p['name']} for p in profs] if isinstance(profs, list) else []
        roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=10).json()
        folders = [{'path': r['path'], 'freeGB': round(r.get('freeSpace', 0) / 1e9, 1)} for r in roots] if isinstance(roots, list) else []
        return jsonify({'profiles': profiles, 'folders': folders})
    except Exception as e:
        return jsonify({'profiles': [], 'folders': [], 'error': str(e)})


@app.route('/api/media/disk')
@login_required
def media_disk():
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    info = {}

    if sonarr_url and sonarr_key:
        try:
            roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=8).json()
            series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=10).json()
            free = sum(r.get('freeSpace', 0) for r in roots) if isinstance(roots, list) else 0
            used = sum(s.get('statistics', {}).get('sizeOnDisk', 0) for s in series) if isinstance(series, list) else 0
            info['sonarr'] = {
                'freeGB': round(free / 1024**3, 1),
                'usedGB': round(used / 1024**3, 1),
                'shows': len(series) if isinstance(series, list) else 0,
                'episodes': sum(s.get('statistics', {}).get('episodeFileCount', 0) for s in series) if isinstance(series, list) else 0,
            }
        except Exception as e:
            print(f"[Disk Sonarr] {e}")

    if radarr_url and radarr_key:
        try:
            roots = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=8).json()
            movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=10).json()
            free = sum(r.get('freeSpace', 0) for r in roots) if isinstance(roots, list) else 0
            used = sum(m.get('sizeOnDisk', 0) for m in movies) if isinstance(movies, list) else 0
            info['radarr'] = {
                'freeGB': round(free / 1024**3, 1),
                'usedGB': round(used / 1024**3, 1),
                'movies': len(movies) if isinstance(movies, list) else 0,
                'downloaded': sum(1 for m in movies if m.get('hasFile')) if isinstance(movies, list) else 0,
            }
        except Exception as e:
            print(f"[Disk Radarr] {e}")

    return jsonify(info)


@app.route('/api/storage/drives')
@login_required
def storage_drives():
    """Return unique drives (root folders) from Sonarr+Radarr with capacity info."""
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    drives = {}  # path -> drive info

    def add_root(r, source):
        path = r.get('path', '').rstrip('/') or '/'
        free = r.get('freeSpace', 0) or 0
        total = r.get('totalSpace', 0) or 0
        if path not in drives:
            drives[path] = {
                'path': path,
                'freeBytes': free,
                'totalBytes': total,
                'sources': set(),
            }
        # Use the largest reported total/free in case of mismatch
        drives[path]['freeBytes'] = max(drives[path]['freeBytes'], free)
        drives[path]['totalBytes'] = max(drives[path]['totalBytes'], total)
        drives[path]['sources'].add(source)

    if sonarr_url and sonarr_key:
        try:
            roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=8).json()
            if isinstance(roots, list):
                for r in roots:
                    add_root(r, 'sonarr')
        except Exception as e:
            print(f"[Storage] Sonarr roots: {e}")

    if radarr_url and radarr_key:
        try:
            roots = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=8).json()
            if isinstance(roots, list):
                for r in roots:
                    add_root(r, 'radarr')
        except Exception as e:
            print(f"[Storage] Radarr roots: {e}")

    # For each drive, sum used space from Sonarr/Radarr items rooted there
    series_list = []
    movies_list = []
    if sonarr_url and sonarr_key:
        try:
            series_list = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json() or []
        except Exception as e:
            print(f"[Storage] Sonarr series: {e}")
    if radarr_url and radarr_key:
        try:
            movies_list = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json() or []
        except Exception as e:
            print(f"[Storage] Radarr movies: {e}")

    out = []
    for path, d in drives.items():
        used_by_lib = 0
        item_count = 0
        for s in series_list if isinstance(series_list, list) else []:
            sp = (s.get('path') or '').rstrip('/')
            if sp == path or sp.startswith(path + '/'):
                size = (s.get('statistics') or {}).get('sizeOnDisk', 0) or 0
                used_by_lib += size
                item_count += 1
        for m in movies_list if isinstance(movies_list, list) else []:
            mp = (m.get('path') or '').rstrip('/')
            if mp == path or mp.startswith(path + '/'):
                used_by_lib += m.get('sizeOnDisk', 0) or 0
                if m.get('hasFile'):
                    item_count += 1
        used_total = max(d['totalBytes'] - d['freeBytes'], 0)
        other_used = max(used_total - used_by_lib, 0)
        out.append({
            'path': path,
            'totalGB': round(d['totalBytes'] / 1024**3, 1),
            'freeGB': round(d['freeBytes'] / 1024**3, 1),
            'usedGB': round(used_total / 1024**3, 1),
            'libraryGB': round(used_by_lib / 1024**3, 1),
            'otherGB': round(other_used / 1024**3, 1),
            'percentUsed': round((used_total / d['totalBytes']) * 100, 1) if d['totalBytes'] else 0,
            'itemCount': item_count,
            'sources': sorted(list(d['sources'])),
        })

    out.sort(key=lambda x: -x['percentUsed'])
    return jsonify({'drives': out})


@app.route('/api/storage/analyze')
@login_required
def storage_analyze():
    """Analyze what's on a given drive: largest items + smart recommendations."""
    path = (request.args.get('path') or '').strip().rstrip('/')
    if not path:
        return jsonify({'error': 'Missing path parameter'})

    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    items = []
    now = datetime.now()
    one_year_ago = now - timedelta(days=365)
    six_mo_ago = now - timedelta(days=180)

    # Pull requester data from Ombi (best-effort; cheap network calls)
    try:
        tv_requesters = get_ombi_tv_requester_names() or {}
    except Exception as e:
        print(f"[Storage Analyze] Ombi TV: {e}")
        tv_requesters = {}
    try:
        movie_requesters = get_ombi_movie_requester_names() or {}
    except Exception as e:
        print(f"[Storage Analyze] Ombi movies: {e}")
        movie_requesters = {}

    # Pull watch-history aggregates from Plex (single history call, group by title)
    plex_url = get_setting('PLEX_URL', '').strip().rstrip('/')
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    show_watch = {}   # title.lower() -> {last, count}
    movie_watch = {}  # title.lower() -> {last, count}
    if plex_url and plex_token:
        try:
            from plexapi.server import PlexServer
            plex = PlexServer(plex_url, plex_token, timeout=60)
            history = plex.history(maxresults=100000)
            for h in history:
                t = getattr(h, 'type', None)
                viewed_at = getattr(h, 'viewedAt', None)
                if t == 'episode':
                    title = (getattr(h, 'grandparentTitle', '') or '').strip().lower()
                    if not title:
                        continue
                    e = show_watch.setdefault(title, {'last': None, 'count': 0})
                elif t == 'movie':
                    title = (getattr(h, 'title', '') or '').strip().lower()
                    if not title:
                        continue
                    e = movie_watch.setdefault(title, {'last': None, 'count': 0})
                else:
                    continue
                e['count'] += 1
                if viewed_at and (e['last'] is None or viewed_at > e['last']):
                    e['last'] = viewed_at
        except Exception as e:
            print(f"[Storage Analyze] Plex history: {e}")

    def parse_iso(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace('Z', '+00:00').split('.')[0].replace('+00:00', ''))
        except Exception:
            return None

    if sonarr_url and sonarr_key:
        try:
            series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json() or []
            for s in series if isinstance(series, list) else []:
                sp = (s.get('path') or '').rstrip('/')
                if not (sp == path or sp.startswith(path + '/')):
                    continue
                size = (s.get('statistics') or {}).get('sizeOnDisk', 0) or 0
                if size <= 0:
                    continue
                stats = s.get('statistics') or {}
                added = parse_iso(s.get('added'))
                last_air = parse_iso(s.get('previousAiring'))
                title_str = s.get('title') or ''
                title_lc = title_str.strip().lower()
                w = show_watch.get(title_lc) or {}
                last_watched = w.get('last')
                last_watched_days = (now - last_watched).days if last_watched else None
                items.append({
                    'id': s.get('id'),
                    'type': 'show',
                    'title': title_str,
                    'year': s.get('year'),
                    'sizeBytes': size,
                    'sizeGB': round(size / 1024**3, 2),
                    'path': sp,
                    'status': s.get('status'),  # continuing | ended | upcoming
                    'episodeFileCount': stats.get('episodeFileCount', 0),
                    'episodeCount': stats.get('episodeCount', 0),
                    'avgEpisodeMB': round((size / stats['episodeFileCount']) / 1024**2, 0) if stats.get('episodeFileCount') else 0,
                    'addedDate': s.get('added', '')[:10] if s.get('added') else '',
                    'addedDaysAgo': (now - added).days if added else None,
                    'lastAirDate': s.get('previousAiring', '')[:10] if s.get('previousAiring') else '',
                    'lastAirDaysAgo': (now - last_air).days if last_air else None,
                    'monitored': s.get('monitored', False),
                    'rating': round((s.get('ratings') or {}).get('value', 0), 1),
                    'requester': tv_requesters.get(title_lc, ''),
                    'lastWatchedDate': last_watched.strftime('%Y-%m-%d') if last_watched else '',
                    'lastWatchedDaysAgo': last_watched_days,
                    'viewCount': w.get('count', 0),
                })
        except Exception as e:
            print(f"[Storage Analyze] Sonarr: {e}")

    if radarr_url and radarr_key:
        try:
            movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json() or []
            for m in movies if isinstance(movies, list) else []:
                mp = (m.get('path') or '').rstrip('/')
                if not (mp == path or mp.startswith(path + '/')):
                    continue
                size = m.get('sizeOnDisk', 0) or 0
                if size <= 0:
                    continue
                added = parse_iso(m.get('added'))
                title_str = m.get('title') or ''
                title_lc = title_str.strip().lower()
                w = movie_watch.get(title_lc) or {}
                last_watched = w.get('last')
                last_watched_days = (now - last_watched).days if last_watched else None
                items.append({
                    'id': m.get('id'),
                    'type': 'movie',
                    'title': title_str,
                    'year': m.get('year'),
                    'sizeBytes': size,
                    'sizeGB': round(size / 1024**3, 2),
                    'path': mp,
                    'status': m.get('status'),  # released | inCinemas | announced
                    'addedDate': m.get('added', '')[:10] if m.get('added') else '',
                    'addedDaysAgo': (now - added).days if added else None,
                    'monitored': m.get('monitored', False),
                    'rating': round((m.get('ratings') or {}).get('imdb', {}).get('value', 0), 1),
                    'qualityName': ((m.get('movieFile') or {}).get('quality') or {}).get('quality', {}).get('name', ''),
                    'requester': movie_requesters.get(title_lc, ''),
                    'lastWatchedDate': last_watched.strftime('%Y-%m-%d') if last_watched else '',
                    'lastWatchedDaysAgo': last_watched_days,
                    'viewCount': w.get('count', 0),
                })
        except Exception as e:
            print(f"[Storage Analyze] Radarr: {e}")

    items.sort(key=lambda x: -x['sizeBytes'])
    total_bytes = sum(i['sizeBytes'] for i in items)

    # Build recommendation buckets
    largest = items[:20]

    ended_or_released_old = [
        i for i in items
        if i['sizeGB'] >= 5
        and ((i['type'] == 'show' and (i.get('status') == 'ended' or (i.get('lastAirDaysAgo') or 0) > 365))
             or (i['type'] == 'movie' and (i.get('addedDaysAgo') or 0) > 365))
    ][:20]

    inactive_shows = [
        i for i in items
        if i['type'] == 'show'
        and (i.get('lastAirDaysAgo') or 0) > 180
        and i.get('status') != 'ended'
        and i['sizeGB'] >= 3
    ][:20]

    high_avg = [
        i for i in items
        if (i['type'] == 'show' and i.get('avgEpisodeMB', 0) >= 2500)
           or (i['type'] == 'movie' and i['sizeGB'] >= 15)
    ][:20]

    old_unused = [
        i for i in items
        if (i.get('addedDaysAgo') or 0) > 365
        and i['sizeGB'] >= 2
    ][:30]

    # Top-level summary
    summary = []
    if largest:
        top10_gb = sum(i['sizeGB'] for i in items[:10])
        summary.append(f"📊 Your top 10 largest items use **{top10_gb:.1f} GB** ({(top10_gb*1024**3/total_bytes*100):.0f}% of this drive's library content).")
    if ended_or_released_old:
        gb = sum(i['sizeGB'] for i in ended_or_released_old)
        summary.append(f"🏁 **{len(ended_or_released_old)}** ended/old items totaling **{gb:.1f} GB** could likely be cleaned up.")
    if inactive_shows:
        gb = sum(i['sizeGB'] for i in inactive_shows)
        summary.append(f"💤 **{len(inactive_shows)}** TV shows haven't aired new episodes in 6+ months ({gb:.1f} GB).")
    if high_avg:
        gb = sum(i['sizeGB'] for i in high_avg)
        summary.append(f"📦 **{len(high_avg)}** items have unusually large file sizes ({gb:.1f} GB) — possibly higher quality than needed.")
    if old_unused:
        gb = sum(i['sizeGB'] for i in old_unused)
        summary.append(f"🕰️ **{len(old_unused)}** items were added over a year ago ({gb:.1f} GB) — review for relevance.")

    return jsonify({
        'path': path,
        'totalLibraryGB': round(total_bytes / 1024**3, 1),
        'itemCount': len(items),
        'summary': summary,
        'largest': largest,
        'endedOrOld': ended_or_released_old,
        'inactiveShows': inactive_shows,
        'highAverage': high_avg,
        'oldUnused': old_unused,
    })


@app.route('/api/storage/show-seasons')
@login_required
def storage_show_seasons():
    """Return per-season breakdown for a Sonarr series."""
    series_id = request.args.get('id', type=int)
    if not series_id:
        return jsonify({'error': 'Missing series id'}), 400

    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    if not sonarr_url or not sonarr_key:
        return jsonify({'error': 'Sonarr not configured'}), 400

    try:
        sr = requests.get(f"{sonarr_url}/api/v3/series/{series_id}",
                          params={'apikey': sonarr_key}, timeout=10)
        if not sr.ok:
            return jsonify({'error': f'Sonarr returned {sr.status_code}'}), 502
        series = sr.json()

        ef = requests.get(f"{sonarr_url}/api/v3/episodefile",
                          params={'apikey': sonarr_key, 'seriesId': series_id}, timeout=15)
        episode_files = ef.json() if ef.ok else []
    except Exception as e:
        print(f"[Storage Seasons] {e}")
        return jsonify({'error': 'Failed to fetch season info'}), 502

    # Group episode files by season
    files_by_season = {}
    for f in episode_files if isinstance(episode_files, list) else []:
        sn = f.get('seasonNumber')
        if sn is None:
            continue
        files_by_season.setdefault(sn, []).append(f.get('id'))

    seasons_out = []
    for s in series.get('seasons', []) or []:
        sn = s.get('seasonNumber')
        stats = s.get('statistics') or {}
        size = stats.get('sizeOnDisk', 0) or 0
        seasons_out.append({
            'seasonNumber': sn,
            'monitored': s.get('monitored', False),
            'episodeFileCount': stats.get('episodeFileCount', 0),
            'episodeCount': stats.get('episodeCount', 0),
            'totalEpisodeCount': stats.get('totalEpisodeCount', 0),
            'sizeBytes': size,
            'sizeGB': round(size / 1024**3, 2),
            'episodeFileIds': files_by_season.get(sn, []),
        })

    seasons_out.sort(key=lambda x: x['seasonNumber'] if x['seasonNumber'] is not None else -1)

    return jsonify({
        'seriesId': series_id,
        'title': series.get('title'),
        'totalSizeGB': round(sum(s['sizeBytes'] for s in seasons_out) / 1024**3, 2),
        'seasons': seasons_out,
    })


@app.route('/api/storage/delete-seasons', methods=['POST'])
@login_required
def storage_delete_seasons():
    """Delete files for selected seasons of a show, optionally unmonitor those seasons."""
    data = request.get_json() or {}
    series_id = data.get('seriesId')
    season_numbers = data.get('seasons') or []
    unmonitor = bool(data.get('unmonitor', True))

    if not series_id or not season_numbers:
        return jsonify({'success': False, 'error': 'Missing seriesId or seasons'}), 400

    try:
        season_numbers = [int(s) for s in season_numbers]
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid season numbers'}), 400

    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    if not sonarr_url or not sonarr_key:
        return jsonify({'success': False, 'error': 'Sonarr not configured'}), 400

    try:
        ef = requests.get(f"{sonarr_url}/api/v3/episodefile",
                          params={'apikey': sonarr_key, 'seriesId': series_id}, timeout=15)
        if not ef.ok:
            return jsonify({'success': False, 'error': f'Failed to list episode files ({ef.status_code})'}), 502
        episode_files = ef.json() or []
    except Exception as e:
        print(f"[Storage Delete Seasons] list files: {e}")
        return jsonify({'success': False, 'error': 'Failed to list episode files'}), 502

    file_ids_to_delete = [
        f.get('id') for f in episode_files
        if f.get('seasonNumber') in season_numbers and f.get('id')
    ]

    deleted_count = 0
    failed_count = 0
    delete_warning = None
    if file_ids_to_delete:
        # Try Sonarr's bulk delete first (fast path; not supported on all versions)
        bulk_ok = False
        try:
            r = requests.delete(
                f"{sonarr_url}/api/v3/episodefile/bulk",
                params={'apikey': sonarr_key},
                json={'episodeFileIds': file_ids_to_delete},
                timeout=60,
            )
            if r.ok:
                deleted_count = len(file_ids_to_delete)
                bulk_ok = True
            else:
                print(f"[Storage Delete Seasons] bulk failed {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[Storage Delete Seasons] bulk exception: {e}")

        if not bulk_ok:
            # Fallback: delete files in parallel for speed
            from concurrent.futures import ThreadPoolExecutor

            def _del_one(fid):
                try:
                    rr = requests.delete(
                        f"{sonarr_url}/api/v3/episodefile/{fid}",
                        params={'apikey': sonarr_key},
                        timeout=20,
                    )
                    return rr.ok
                except Exception as ex:
                    print(f"[Storage Delete Seasons] file {fid} error: {ex}")
                    return False

            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(_del_one, file_ids_to_delete))
            deleted_count = sum(1 for ok in results if ok)
            failed_count = len(results) - deleted_count
            if failed_count > 0:
                delete_warning = f"{failed_count} of {len(file_ids_to_delete)} files could not be deleted by Sonarr."

    # Optionally unmonitor those seasons
    unmonitored = []
    unmonitor_warning = None
    if unmonitor:
        try:
            sr = requests.get(f"{sonarr_url}/api/v3/series/{series_id}",
                              params={'apikey': sonarr_key}, timeout=10)
            if sr.ok:
                series = sr.json()
                pending = []
                for s in series.get('seasons', []) or []:
                    if s.get('seasonNumber') in season_numbers and s.get('monitored'):
                        s['monitored'] = False
                        pending.append(s.get('seasonNumber'))
                if pending:
                    pr = requests.put(f"{sonarr_url}/api/v3/series/{series_id}",
                                      params={'apikey': sonarr_key}, json=series, timeout=15)
                    if pr.ok:
                        unmonitored = pending
                    else:
                        print(f"[Storage Delete Seasons] unmonitor failed {pr.status_code}: {pr.text[:200]}")
                        unmonitor_warning = f"Could not unmonitor seasons (Sonarr returned {pr.status_code})."
            else:
                unmonitor_warning = f"Could not fetch series to unmonitor (Sonarr returned {sr.status_code})."
        except Exception as e:
            print(f"[Storage Delete Seasons] unmonitor: {e}")
            unmonitor_warning = "Could not unmonitor seasons (network error)."

    parts = []
    if deleted_count:
        parts.append(f"Deleted **{deleted_count}** episode file(s) from season(s) {', '.join(str(s) for s in sorted(set(season_numbers)))}")
    elif file_ids_to_delete:
        parts.append("Could not delete any episode files")
    else:
        parts.append("No episode files found to delete for the selected season(s)")
    if delete_warning:
        parts.append(delete_warning)
    if unmonitored:
        parts.append(f"Unmonitored season(s) {', '.join(str(s) for s in sorted(unmonitored))}")
    if unmonitor_warning:
        parts.append(unmonitor_warning)
    return jsonify({
        'success': True,
        'deletedFiles': deleted_count,
        'failedFiles': failed_count,
        'deleteWarning': delete_warning,
        'unmonitored': unmonitored,
        'unmonitorWarning': unmonitor_warning,
        'message': '. '.join(parts) + '.',
    })


@app.route('/api/media/sonarr-search')
@login_required
def media_sonarr_search():
    q = request.args.get('q', '').strip()
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    if not sonarr_url or not sonarr_key:
        return jsonify({'error': 'Sonarr not configured.', 'results': [], 'profiles': [], 'rootFolders': []})
    try:
        results = requests.get(f"{sonarr_url}/api/v3/series/lookup", params={'term': q, 'apikey': sonarr_key}, timeout=15).json()
        profiles = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={'apikey': sonarr_key}, timeout=10).json()
        roots_raw = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=10).json()
        results = results[:8] if isinstance(results, list) else []
        profiles = [{'id': p['id'], 'name': p['name']} for p in profiles] if isinstance(profiles, list) else []
        roots = [{'path': r['path'], 'freeGB': round(r.get('freeSpace', 0) / 1024**3, 1)} for r in roots_raw] if isinstance(roots_raw, list) else []
        return jsonify({'results': results, 'profiles': profiles, 'rootFolders': roots})
    except Exception as e:
        return jsonify({'error': str(e), 'results': [], 'profiles': [], 'rootFolders': []})


@app.route('/api/media/radarr-search')
@login_required
def media_radarr_search():
    q = request.args.get('q', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()
    if not radarr_url or not radarr_key:
        return jsonify({'error': 'Radarr not configured.', 'results': [], 'profiles': [], 'rootFolders': []})
    try:
        results = requests.get(f"{radarr_url}/api/v3/movie/lookup", params={'term': q, 'apikey': radarr_key}, timeout=15).json()
        profiles = requests.get(f"{radarr_url}/api/v3/qualityprofile", params={'apikey': radarr_key}, timeout=10).json()
        roots_raw = requests.get(f"{radarr_url}/api/v3/rootfolder", params={'apikey': radarr_key}, timeout=10).json()
        results = results[:8] if isinstance(results, list) else []
        profiles = [{'id': p['id'], 'name': p['name']} for p in profiles] if isinstance(profiles, list) else []
        roots = [{'path': r['path'], 'freeGB': round(r.get('freeSpace', 0) / 1024**3, 1)} for r in roots_raw] if isinstance(roots_raw, list) else []
        return jsonify({'results': results, 'profiles': profiles, 'rootFolders': roots})
    except Exception as e:
        return jsonify({'error': str(e), 'results': [], 'profiles': [], 'rootFolders': []})


def _parse_plex_discover_results(container):
    """Flatten Plex Discover search results regardless of format.
    Handles both 'SearchResult' (singular) and 'SearchResults' (plural) keys,
    flat lists, and nested groups."""
    items = []
    raw = container.get('SearchResult') or container.get('SearchResults') or []
    if isinstance(raw, dict):
        raw = [raw]
    for entry in raw:
        # Flat format: entry = {"Metadata": {...}, "score": ...}
        meta = entry.get('Metadata')
        if meta and meta.get('title'):
            items.append(meta)
            continue
        # Nested format: entry = {"SearchResult": [...], "hub": ...}
        nested = entry.get('SearchResult') or entry.get('SearchResults') or []
        if isinstance(nested, dict):
            nested = [nested]
        for sr in nested:
            m = sr.get('Metadata', sr)
            if m and m.get('title'):
                items.append(m)
    return items


def _find_plex_match(candidates, title, tmdb_id=None, year=None):
    """Return the best matching Plex metadata item. Priority: TMDb GUID > exact title+year > substantial partial title+year."""
    title_lower = title.lower()

    def _substantial_overlap(a, b):
        """True only if the shorter string is a substring of the longer AND covers
        at least 60% of the longer string's length — prevents 'Mary' matching
        inside 'Project Hail Mary'."""
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return shorter in longer and len(shorter) >= len(longer) * 0.6

    # 1. GUID match via TMDb ID (most reliable)
    if tmdb_id:
        tmdb_str = str(tmdb_id)
        for m in candidates:
            for g in (m.get('Guid') or []):
                gid = g.get('id', '') if isinstance(g, dict) else str(g)
                if tmdb_str in gid and ('themoviedb' in gid or 'tmdb' in gid):
                    return m

    # 2. Exact title + year
    for m in candidates:
        m_title = m.get('title', '').lower()
        m_year = m.get('year')
        if m_title == title_lower:
            if not year or not m_year or abs(int(m_year) - int(year)) <= 1:
                return m

    # 3. Exact title (any year) — catches year-off-by-one edge cases
    for m in candidates:
        m_title = m.get('title', '').lower()
        if m_title == title_lower:
            return m

    # 4. Substantial partial title + year — the shorter must cover ≥60% of the longer
    for m in candidates:
        m_title = m.get('title', '').lower()
        m_year = m.get('year')
        if _substantial_overlap(title_lower, m_title):
            if not year or not m_year or abs(int(m_year) - int(year)) <= 1:
                return m

    # 5. Substantial partial title (no year constraint) — still requires ≥60% coverage
    for m in candidates:
        m_title = m.get('title', '').lower()
        if _substantial_overlap(title_lower, m_title):
            return m

    return None


@app.route('/api/media/plex-watchlist-add', methods=['POST'])
@login_required
def media_plex_watchlist_add():
    """Search Plex Discover and add a title to the Plex watchlist."""
    data = request.get_json()
    title = data.get('title', '').strip()
    tmdb_id = data.get('tmdb_id')
    year = data.get('year')
    media_type = data.get('media_type', '')  # 'movie' or 'tv'
    plex_token = get_setting('PLEX_TOKEN', '').strip()

    if not plex_token:
        return jsonify({'success': False, 'error': 'Plex token not configured.'})
    if not title:
        return jsonify({'success': False, 'error': 'No title provided.'})

    headers = {
        'X-Plex-Token': plex_token,
        'X-Plex-Client-Identifier': 'media-scrubber-browser',
        'X-Plex-Product': 'Media Scrubber',
        'X-Plex-Version': '1.0',
        'Accept': 'application/json'
    }

    diag_info = []

    def _plex_discover_search(query, search_types, headers):
        """Search Plex Discover for a query across given types. Returns flat list of candidates."""
        results = []
        for search_type in search_types:
            try:
                resp = requests.get(
                    "https://discover.provider.plex.tv/library/search",
                    params={
                        'query': query,
                        'limit': 30,
                        'searchTypes': search_type,
                        'searchProviders': 'discover',
                        'includeGuids': 1,
                    },
                    headers=headers, timeout=12
                )
                print(f"[Plex Discover] type={search_type} status={resp.status_code} query={query!r}")
                if not resp.ok:
                    body_snip = resp.text[:200]
                    print(f"[Plex Discover] Error body: {body_snip}")
                    diag_info.append(f"API {resp.status_code} for q={query!r} t={search_type}")
                    continue
                raw_json = resp.json()
                container = raw_json.get('MediaContainer', {})
                parsed = _parse_plex_discover_results(container)
                print(f"[Plex Discover] type={search_type} found {len(parsed)}: {[m.get('title') for m in parsed[:5]]}")
                if not parsed:
                    keys = list(container.keys())[:6]
                    diag_info.append(f"q={query!r} t={search_type}: 200 OK but 0 parsed (keys={keys}, size={container.get('size', '?')})")
                results.extend(parsed)
            except Exception as e:
                print(f"[Plex Discover] Exception on type={search_type}: {e}")
                diag_info.append(f"Exception for q={query!r}: {str(e)[:100]}")
        return results

    def _title_variants(t):
        """Generate title search variants: original, without article, first word."""
        seen = []
        def add(v):
            v = v.strip()
            if v and v not in seen:
                seen.append(v)
        add(t)
        for article in ('The ', 'A ', 'An '):
            if t.startswith(article):
                add(t[len(article):])
                break
        words = t.split()
        if len(words) >= 3:
            add(' '.join(words[:2]))
        if len(words) >= 2 and len(words[0]) > 3:
            add(words[0])
        return seen

    try:
        # Determine search order: try known type first if provided
        # NOTE: Plex Discover uses 'movies' (plural) for films and 'tv' for shows
        if media_type == 'movie':
            search_types = ('movies', 'tv')
        elif media_type in ('tv', 'show'):
            search_types = ('tv', 'movies')
        else:
            search_types = ('movies', 'tv')

        found = None
        all_candidates = []
        used_query = title

        # ── Step 0: Direct TMDb-ID lookup on Plex Discover (bypasses text search) ──
        if tmdb_id and not found:
            plex_type = 1 if media_type == 'movie' else 2  # 1=movie, 2=show
            plex_agent = 'tv.plex.agents.movie' if media_type == 'movie' else 'tv.plex.agents.series'

            # 0a. metadata/matches endpoint
            for agent_fmt in (f"tmdb://{tmdb_id}", f"com.plexapp.agents.themoviedb://{tmdb_id}"):
                try:
                    r = requests.get(
                        "https://discover.provider.plex.tv/library/metadata/matches",
                        params={'guid': agent_fmt, 'agent': plex_agent,
                                'language': 'en', 'type': plex_type},
                        headers=headers, timeout=10
                    )
                    print(f"[Plex Discover] GUID lookup {agent_fmt} → {r.status_code}")
                    if r.ok:
                        container = r.json().get('MediaContainer', {})
                        candidates = _parse_plex_discover_results(container)
                        if not candidates:
                            meta_list = container.get('Metadata', [])
                            if isinstance(meta_list, list):
                                candidates = meta_list
                        print(f"[Plex Discover] GUID match returned {len(candidates)} result(s)")
                        if candidates:
                            found = candidates[0]
                            print(f"[Plex Discover] GUID direct match: {found.get('title')!r}")
                            break
                except Exception as e:
                    print(f"[Plex Discover] GUID lookup exception: {e}")
                if found:
                    break

            # 0b. Search with guid as query string (alternate Plex Discover approach)
            if not found:
                for search_type in search_types[:1]:
                    try:
                        guid_query = f"tmdb://{tmdb_id}"
                        r = requests.get(
                            "https://discover.provider.plex.tv/library/search",
                            params={'query': guid_query, 'limit': 5, 'searchTypes': search_type,
                                    'searchProviders': 'discover', 'includeGuids': 1},
                            headers=headers, timeout=10
                        )
                        print(f"[Plex Discover] GUID-query search {guid_query} → {r.status_code}")
                        if r.ok:
                            container = r.json().get('MediaContainer', {})
                            candidates = _parse_plex_discover_results(container)
                            if candidates:
                                found = candidates[0]
                                print(f"[Plex Discover] GUID-query match: {found.get('title')!r}")
                                break
                    except Exception as e:
                        print(f"[Plex Discover] GUID-query exception: {e}")

        # ── Step 1: Text-search each title variant; keep going until we get a MATCH ──
        if not found:
            all_seen_candidates = []
            for variant in _title_variants(title):
                candidates = _plex_discover_search(variant, search_types, headers)
                if candidates:
                    all_seen_candidates.extend(candidates)
                    used_query = variant
                    match = _find_plex_match(candidates, title, tmdb_id=tmdb_id, year=year)
                    if match:
                        found = match
                        break
            # If no per-variant match, try matching across everything we found
            if not found and all_seen_candidates:
                all_candidates = all_seen_candidates
                found = _find_plex_match(all_candidates, title, tmdb_id=tmdb_id, year=year)

        # ── Step 2: Single-result fallback (year must match) ──
        if not found and len(all_candidates) == 1:
            candidate = all_candidates[0]
            candidate_year = str(candidate.get('year', ''))
            if not year or not candidate_year or str(year)[:4] == candidate_year[:4]:
                print(f"[Plex Discover] Single-result fallback: using {candidate.get('title')!r}")
                found = candidate

        if not found:
            candidate_titles = [m.get('title', '?') for m in all_candidates[:8]]
            print(f"[Plex Discover] No match for {title!r} (query={used_query!r}, tmdb={tmdb_id}, year={year}). Candidates: {candidate_titles}. Diag: {diag_info}")
            if candidate_titles:
                hint = f' Plex returned: {", ".join(candidate_titles)}'
            elif diag_info:
                hint = ' Debug: ' + '; '.join(diag_info[:3])
            else:
                hint = ' Plex returned no results — the title may not be indexed on Plex Discover.'
            return jsonify({'success': False, 'error': f'Could not find "{title}" on Plex Discover.{hint}'})

        rating_key = found.get('ratingKey', '')
        if not rating_key:
            return jsonify({'success': False, 'error': 'Could not get Plex ID for this title.'})

        # Add to watchlist
        add_resp = requests.put(
            "https://discover.provider.plex.tv/actions/addToWatchlist",
            params={'ratingKey': rating_key},
            headers=headers, timeout=12
        )
        if add_resp.status_code in (200, 201, 204):
            return jsonify({'success': True, 'message': f'"{title}" added to your Plex watchlist.'})
        elif add_resp.status_code == 409:
            return jsonify({'success': True, 'already_exists': True, 'message': f'"{title}" is already on your Plex watchlist.'})
        else:
            return jsonify({'success': False, 'error': f'Plex returned status {add_resp.status_code}.'})
    except Exception as e:
        print(f"[Plex Watchlist Add] {e}")
        return jsonify({'success': False, 'error': 'Could not add to Plex watchlist. Check your Plex token.'})


# ============================================================
# MEDIA EXPIRATION SYSTEM
# ============================================================

EXPIRATION_DEFAULTS = {
    'EXPIRATION_ENABLED': 'false',
    'EXPIRATION_MONTHS': '6',
    'EXPIRATION_WARN_DAYS_BEFORE': '14',
    'EXPIRATION_GRACE_DAYS': '7',
    'EXPIRATION_EXTEND_MONTHS': '6',
    'EXPIRATION_INTRO_EMAIL_ENABLED': 'true',
    'EXPIRATION_USE_OLDEST_FILE_DATE': 'false',
    'DISPLAY_TIMEZONE': 'UTC',
    'EXPIRATION_DRY_RUN': 'true',                  # safe default: don't actually delete on first install
    'EXPIRATION_MAX_DELETIONS_PER_RUN': '10',      # blast-radius cap
    'EXPIRATION_ADMIN_FALLBACK_EMAIL': '',         # if set, no-requester items warn this address
    'EXPIRATION_PUBLIC_URL': '',                   # public base URL used in warning emails (auto-detects from REPLIT_DOMAINS if blank)
    'EXPIRATION_LAST_RUN_AT': '',
}

# In-process lock to keep manual scan-now and scheduled tick from racing each other inside one worker
_expiration_run_lock = threading.Lock()


def _run_expiration_migrations():
    """Idempotently add new columns to existing tables (safe: ADD COLUMN IF NOT EXISTS)."""
    statements = [
        "ALTER TABLE media_expiration ADD COLUMN IF NOT EXISTS additional_requester_emails TEXT",
        "ALTER TABLE media_expiration ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
        "ALTER TABLE media_expiration ADD COLUMN IF NOT EXISTS last_warning_status VARCHAR(20)",
        "ALTER TABLE media_expiration ADD COLUMN IF NOT EXISTS last_warning_error TEXT",
        "ALTER TABLE media_expiration ADD COLUMN IF NOT EXISTS requester_lookup_attempts INTEGER DEFAULT 0",
        "ALTER TABLE ombi_intro_email_log ADD COLUMN IF NOT EXISTS ombi_user_id VARCHAR(100)",
        "ALTER TABLE watchlist_sync_item ADD COLUMN IF NOT EXISTS removed_watched_at TIMESTAMP",
    ]
    with app.app_context():
        for sql in statements:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"[Expiration Migration] {sql[:80]}... -> {e}")


def _format_date_in_tz(dt, fmt='%b %d, %Y'):
    """Format a UTC datetime in the configured display timezone."""
    if not dt:
        return ''
    try:
        from zoneinfo import ZoneInfo
        tz_name = get_setting('DISPLAY_TIMEZONE', 'UTC') or 'UTC'
        utc_dt = dt.replace(tzinfo=ZoneInfo('UTC')) if dt.tzinfo is None else dt
        return utc_dt.astimezone(ZoneInfo(tz_name)).strftime(fmt)
    except Exception:
        return dt.strftime(fmt)


def get_ombi_requesters_full(media_type):
    """Return {title_lc: [{email, name, user_id}, ...]} for all requesters of this media_type."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    out = {}
    if not ombi_url or not ombi_key:
        return out
    endpoint = 'tv' if media_type == 'show' else 'movie'
    try:
        r = requests.get(f"{ombi_url}/api/v1/Request/{endpoint}",
                         headers={"ApiKey": ombi_key}, timeout=30)
        if not r.ok:
            return out
        for req in r.json() or []:
            title = (req.get('title') or '').strip().lower()
            if not title:
                continue
            users = []

            def _add(u):
                if not u: return
                em = (u.get('email') or u.get('Email') or '').strip().lower()
                nm = u.get('userName') or u.get('alias') or ''
                uid = u.get('id') or u.get('userId') or ''
                if em:
                    users.append({'email': em, 'name': nm, 'user_id': str(uid) if uid else None})

            _add(req.get('requestedUser'))
            for child in (req.get('childRequests') or []):
                _add(child.get('requestedUser'))

            if users:
                # de-dup by email
                by_email = {u['email']: u for u in users}
                out.setdefault(title, [])
                for u in by_email.values():
                    if u['email'] not in {x['email'] for x in out[title]}:
                        out[title].append(u)
    except Exception as e:
        print(f"[Ombi Full] {endpoint} error: {e}")
    return out


def _get_arr_ids_present(media_type):
    """Return set of currently-present service_ids in *arr, or None if *arr unreachable."""
    if media_type == 'show':
        url = get_setting('SONARR_URL', '').strip().rstrip('/')
        key = get_setting('SONARR_API_KEY', '').strip()
        path = '/api/v3/series'
    else:
        url = get_setting('RADARR_URL', '').strip().rstrip('/')
        key = get_setting('RADARR_API_KEY', '').strip()
        path = '/api/v3/movie'
    if not url or not key:
        return None
    try:
        r = requests.get(f"{url}{path}", params={'apikey': key}, timeout=30)
        if not r.ok:
            return None
        return {x.get('id') for x in (r.json() or []) if x.get('id')}
    except Exception:
        return None


def _get_arr_oldest_file_date(media_type, service_id):
    """Look up the oldest file date in *arr for an item; returns datetime or None."""
    try:
        if media_type == 'show':
            url = get_setting('SONARR_URL', '').strip().rstrip('/')
            key = get_setting('SONARR_API_KEY', '').strip()
            r = requests.get(f"{url}/api/v3/episodefile",
                             params={'apikey': key, 'seriesId': service_id}, timeout=20)
            files = r.json() if r.ok else []
            dates = [f.get('dateAdded') for f in files if f.get('dateAdded')]
        else:
            url = get_setting('RADARR_URL', '').strip().rstrip('/')
            key = get_setting('RADARR_API_KEY', '').strip()
            r = requests.get(f"{url}/api/v3/moviefile",
                             params={'apikey': key, 'movieId': service_id}, timeout=20)
            files = r.json() if r.ok else []
            dates = [f.get('dateAdded') for f in files if f.get('dateAdded')]
        if not dates:
            return None
        parsed = []
        for d in dates:
            try:
                parsed.append(datetime.fromisoformat(d.replace('Z', '+00:00').split('.')[0].replace('+00:00', '')))
            except Exception:
                pass
        return min(parsed) if parsed else None
    except Exception:
        return None


def get_expiration_policy():
    return {
        'enabled': get_setting('EXPIRATION_ENABLED', 'false').lower() == 'true',
        'months': int(get_setting('EXPIRATION_MONTHS', '6') or '6'),
        'warn_days': int(get_setting('EXPIRATION_WARN_DAYS_BEFORE', '14') or '14'),
        'grace_days': int(get_setting('EXPIRATION_GRACE_DAYS', '7') or '7'),
        'extend_months': int(get_setting('EXPIRATION_EXTEND_MONTHS', '6') or '6'),
        'intro_email_enabled': get_setting('EXPIRATION_INTRO_EMAIL_ENABLED', 'true').lower() == 'true',
    }


def _base_url_for_email():
    """Build the public URL the requester will visit when they click 'Manage This Item'.

    Resolution order (first non-empty wins):
      1. EXPIRATION_PUBLIC_URL setting (admin override on the Expirations page)
      2. CUSTOM_DOMAIN setting (legacy)
      3. REPLIT_DOMAINS env var (auto-set on Replit deploys & dev)
      4. Flask's request.host_url (works inside a request, but the daily job has no request)
      5. localhost (last resort — only useful if you're testing locally)
    """
    def _normalize(host):
        host = host.strip().rstrip('/')
        if host.startswith('http://') or host.startswith('https://'):
            return host
        return f"https://{host}"

    explicit = get_setting('EXPIRATION_PUBLIC_URL', '').strip()
    if explicit:
        return _normalize(explicit)
    custom_domain = get_setting('CUSTOM_DOMAIN', '').strip()
    if custom_domain:
        return _normalize(custom_domain)
    replit_domains = os.environ.get('REPLIT_DOMAINS', '').strip()
    if replit_domains:
        # comma-separated; first one is the canonical domain
        return _normalize(replit_domains.split(',')[0].strip())
    try:
        return request.host_url.rstrip('/')
    except Exception:
        return 'http://localhost:5000'


def _send_smtp_email(to_email, subject, html_body, log_meta=None):
    """Send a single HTML email via configured SMTP. Logs to EmailHistory."""
    smtp_host = get_setting('SMTP_HOST')
    smtp_port = int(get_setting('SMTP_PORT', '587') or '587')
    smtp_user = get_setting('SMTP_USER')
    smtp_password = get_setting('SMTP_PASSWORD') or os.environ.get('SMTP_PASSWORD', '')
    smtp_from = get_setting('SMTP_FROM') or smtp_user
    if not (smtp_host and smtp_user and smtp_password and to_email):
        return False, 'SMTP not configured or missing recipient'

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = smtp_from
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        success, err = True, None
    except Exception as e:
        success, err = False, str(e)

    try:
        meta = log_meta or {}
        db.session.add(EmailHistory(
            media_type=meta.get('media_type', 'system'),
            media_title=meta.get('media_title', subject[:200]),
            action_type=meta.get('action_type', 'expiration'),
            recipient_name=meta.get('recipient_name'),
            recipient_email=to_email,
            subject=subject,
            body_html=html_body,
            sent_at=datetime.utcnow(),
            was_successful=success,
            error_message=err,
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return success, err


def _is_title_excluded(media_type, title, year=None):
    """Check whether this title is on the user's exclusion list."""
    if not title:
        return False
    t = title.strip().lower()
    if media_type == 'show':
        return Exclusion.query.filter(db.func.lower(Exclusion.title) == t).first() is not None
    return MovieExclusion.query.filter(db.func.lower(MovieExclusion.title) == t).first() is not None


def _add_to_exclusion_list(rec, by_name=None, by_email=None):
    """Add a MediaExpiration record's title to the appropriate exclusion list."""
    try:
        if rec.media_type == 'show':
            existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == rec.title.lower()).first()
            if not existing:
                db.session.add(Exclusion(
                    title=rec.title,
                    excluded_by='requester' if by_email else 'admin',
                    excluded_by_name=by_name or rec.requester_name,
                    excluded_by_email=by_email or rec.requester_email,
                    original_requester_name=rec.requester_name,
                    original_requester_email=rec.requester_email,
                ))
        else:
            existing = MovieExclusion.query.filter(
                db.func.lower(MovieExclusion.title) == rec.title.lower()
            ).first()
            if not existing:
                db.session.add(MovieExclusion(
                    title=rec.title,
                    year=rec.year,
                    tmdb_id=rec.tmdb_id,
                    excluded_by='requester' if by_email else 'admin',
                    excluded_by_name=by_name or rec.requester_name,
                    excluded_by_email=by_email or rec.requester_email,
                    original_requester_name=rec.requester_name,
                    original_requester_email=rec.requester_email,
                ))
    except Exception as e:
        print(f"[Exclusion Add] {e}")


def _add_months(dt, months):
    """Add N months to a datetime (approximate, calendar-aware enough)."""
    # Simple approach: add 30.44 days per month
    return dt + timedelta(days=int(round(months * 30.44)))


def _resolve_requester_for_item(media_type, title, tv_requesters_email, tv_requesters_name,
                                  movie_requesters_email, movie_requesters_name):
    title_lc = (title or '').strip().lower()
    if media_type == 'show':
        return (tv_requesters_email.get(title_lc), tv_requesters_name.get(title_lc))
    return (movie_requesters_email.get(title_lc), movie_requesters_name.get(title_lc))


def expiration_reconcile_with_exclusions():
    """Make the global exclusion lists the single source of truth for `permanent`."""
    policy = get_expiration_policy()
    excl_shows = {e.title.lower() for e in Exclusion.query.all()}
    excl_movies = {e.title.lower() for e in MovieExclusion.query.all()}
    promoted = demoted = 0
    for rec in MediaExpiration.query.filter(MediaExpiration.status != 'deleted').all():
        title_lc = (rec.title or '').lower()
        is_excluded = (title_lc in excl_shows) if rec.media_type == 'show' else (title_lc in excl_movies)
        if is_excluded and not rec.permanent:
            rec.permanent = True
            rec.status = 'permanent'
            rec.notes = 'on-exclusion-list'
            promoted += 1
        elif (not is_excluded) and rec.permanent:
            # Removed from exclusion list — bring back into rotation with a fresh grace window
            rec.permanent = False
            rec.status = 'active'
            rec.notes = None
            floor = datetime.utcnow() + timedelta(days=policy['warn_days'] + policy['grace_days'] + 1)
            rec.expires_at = max(_add_months(rec.added_at or datetime.utcnow(), policy['months']), floor)
            demoted += 1
    if promoted or demoted:
        try:
            db.session.commit()
            print(f"[Expiration Reconcile] Promoted {promoted} / Demoted {demoted}")
        except Exception:
            db.session.rollback()


def _parse_iso(s):
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00').split('.')[0].replace('+00:00', ''))
    except Exception:
        return None


def _select_primary_requester(users, current_email=None):
    """From a list of {email,name,user_id}, pick one as primary (prefer existing)."""
    if not users:
        return None, None, [], []
    if current_email:
        for u in users:
            if u['email'] == current_email.lower():
                others = [x for x in users if x['email'] != u['email']]
                return u['email'], u['name'], [x['email'] for x in others], [x.get('user_id') for x in others]
    primary = users[0]
    others = users[1:]
    return primary['email'], primary['name'], [x['email'] for x in others], [x.get('user_id') for x in others]


def _process_arr_item(media_type, item, requesters_by_title, policy, use_oldest_file):
    """Upsert one *arr item into MediaExpiration. Returns ('added'|'revived'|'updated'|'skipped')."""
    sid = item.get('id')
    if not sid:
        return 'skipped'
    title = item.get('title') or ''
    title_lc = title.strip().lower()
    users = requesters_by_title.get(title_lc, [])

    if media_type == 'show':
        ids = {'tvdb_id': item.get('tvdbId'), 'tmdb_id': item.get('tmdbId'), 'imdb_id': item.get('imdbId')}
    else:
        ids = {'tmdb_id': item.get('tmdbId'), 'imdb_id': item.get('imdbId')}

    added_dt = _parse_iso(item.get('added')) or datetime.utcnow()
    if use_oldest_file:
        oldest = _get_arr_oldest_file_date(media_type, sid)
        if oldest and oldest < added_dt:
            added_dt = oldest

    existing = MediaExpiration.query.filter_by(media_type=media_type, service_id=sid).first()
    if existing:
        existing.last_seen_at = datetime.utcnow()
        if not existing.title and title:
            existing.title = title
        # Re-attempt requester lookup if missing
        if not existing.requester_email and users:
            primary_email, primary_name, extra_emails, _ = _select_primary_requester(users)
            existing.requester_email = primary_email
            existing.requester_name = primary_name
            existing.additional_requester_emails = ','.join(extra_emails) if extra_emails else None
            existing.requester_lookup_attempts = (existing.requester_lookup_attempts or 0) + 1
        elif users:
            # Refresh additional list (so newly added co-requesters get picked up)
            primary_email, _, extra_emails, _ = _select_primary_requester(users, existing.requester_email)
            existing.additional_requester_emails = ','.join(extra_emails) if extra_emails else None
        elif not existing.requester_email:
            existing.requester_lookup_attempts = (existing.requester_lookup_attempts or 0) + 1
        # Revive if previously deleted/missing (id reuse or restored item)
        if existing.status in ('deleted', 'missing'):
            existing.status = 'active'
            existing.deleted_at = None
            existing.added_at = added_dt
            natural = _add_months(added_dt, policy['months'])
            floor = datetime.utcnow() + timedelta(days=policy['warn_days'] + policy['grace_days'] + 1)
            existing.expires_at = max(natural, floor)
            existing.last_warning_sent_at = None
            existing.last_warning_status = None
            existing.warning_count = 0
            existing.notes = 'revived'
        return 'revived' if (existing.status == 'active' and existing.notes == 'revived') else 'updated'

    # New record
    natural = _add_months(added_dt, policy['months'])
    floor = datetime.utcnow() + timedelta(days=policy['warn_days'] + policy['grace_days'] + 1)
    expires_at = max(natural, floor)
    excluded = _is_title_excluded(media_type, title, item.get('year'))
    primary_email, primary_name, extra_emails, _ = _select_primary_requester(users)

    rec = MediaExpiration(
        media_type=media_type,
        service_id=sid,
        title=title,
        year=item.get('year'),
        requester_email=primary_email,
        requester_name=primary_name,
        additional_requester_emails=','.join(extra_emails) if extra_emails else None,
        requester_lookup_attempts=1 if not primary_email else 0,
        added_at=added_dt,
        last_seen_at=datetime.utcnow(),
        expires_at=expires_at,
        permanent=excluded,
        status='permanent' if excluded else 'active',
        notes=('on-exclusion-list' if excluded else
               ('first-sync-grace' if expires_at != natural else None)),
        **ids,
    )
    db.session.add(rec)
    return 'added'


def expiration_sync_new_items():
    """Discover new *arr items, refresh existing ones, mark zombies as missing."""
    policy = get_expiration_policy()
    use_oldest_file = get_setting('EXPIRATION_USE_OLDEST_FILE_DATE', 'false').lower() == 'true'

    tv_users = get_ombi_requesters_full('show')
    mv_users = get_ombi_requesters_full('movie')

    counts = {'added': 0, 'revived': 0, 'updated': 0, 'missing': 0}

    for media_type, arr_path, key_setting, url_setting in [
        ('show', '/api/v3/series', 'SONARR_API_KEY', 'SONARR_URL'),
        ('movie', '/api/v3/movie', 'RADARR_API_KEY', 'RADARR_URL'),
    ]:
        url = get_setting(url_setting, '').strip().rstrip('/')
        key = get_setting(key_setting, '').strip()
        if not url or not key:
            continue
        try:
            r = requests.get(f"{url}{arr_path}", params={'apikey': key}, timeout=30)
            if not r.ok:
                print(f"[Expiration Sync] {media_type} *arr returned HTTP {r.status_code}; skipping zombie check")
                continue
            items = r.json() or []
            present_ids = set()
            requesters = tv_users if media_type == 'show' else mv_users
            for it in items:
                outcome = _process_arr_item(media_type, it, requesters, policy, use_oldest_file)
                if outcome in counts:
                    counts[outcome] += 1
                if it.get('id'):
                    present_ids.add(it['id'])
            db.session.commit()

            # Zombie sweep — only because we successfully got the list
            zombies = MediaExpiration.query.filter(
                MediaExpiration.media_type == media_type,
                MediaExpiration.status.in_(['active', 'extended', 'permanent']),
            ).all()
            for z in zombies:
                if z.service_id not in present_ids:
                    z.status = 'missing'
                    z.notes = 'no-longer-in-arr'
                    counts['missing'] += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[Expiration Sync] {media_type} error: {e}")

    print(f"[Expiration Sync] {counts}")
    return counts


def expiration_send_warnings():
    """Send warning emails for items expiring within warn_days_before. Sends to all requesters."""
    policy = get_expiration_policy()
    if not policy['enabled']:
        return 0

    threshold = datetime.utcnow() + timedelta(days=policy['warn_days'])
    # Same cap as deletions, so the warning queue and delete queue stay in lock-step.
    try:
        cap = max(0, int(get_setting('EXPIRATION_MAX_DELETIONS_PER_RUN', '10')))
    except Exception:
        cap = 10

    # Oldest-expiring first, so the items closest to deletion get warned first.
    candidates = MediaExpiration.query.filter(
        MediaExpiration.status.in_(['active', 'extended']),
        MediaExpiration.permanent == False,
        MediaExpiration.expires_at <= threshold,
        MediaExpiration.expires_at > datetime.utcnow() - timedelta(days=policy['grace_days']),
    ).order_by(MediaExpiration.expires_at.asc()).all()

    sent_count = 0
    for rec in candidates:
        # Per-run cap: only send as many warnings as we could actually act on this run.
        if cap and sent_count >= cap:
            print(f"[Expiration Warn] Per-run cap of {cap} reached; remaining candidates deferred to next run.")
            break
        # Respect the exclusion list — promote to permanent and skip
        if _is_title_excluded(rec.media_type, rec.title, rec.year):
            rec.permanent = True
            rec.status = 'permanent'
            rec.notes = 'on-exclusion-list'
            db.session.commit()
            continue
        # Skip if we sent a warning recently (within the past 7 days)
        if rec.last_warning_sent_at and (datetime.utcnow() - rec.last_warning_sent_at).days < 7:
            continue
        # Build recipient list (primary + additional)
        recipients = []
        unclaimed = False
        if rec.requester_email:
            recipients.append(rec.requester_email)
        if rec.additional_requester_emails:
            for em in rec.additional_requester_emails.split(','):
                em = em.strip().lower()
                if em and em not in recipients:
                    recipients.append(em)
        if not recipients:
            fallback = (get_setting('EXPIRATION_ADMIN_FALLBACK_EMAIL', '') or '').strip().lower()
            if fallback:
                recipients = [fallback]
                unclaimed = True
            else:
                rec.last_warning_status = 'no_email'
                rec.last_warning_error = 'No requester email on file (no admin fallback set)'
                db.session.commit()
                continue

        try:
            token_str = secrets.token_urlsafe(32)
            tok = ExpirationActionToken(
                token=token_str,
                expiration_id=rec.id,
                expires_at=datetime.utcnow() + timedelta(days=30),
            )
            db.session.add(tok)
            db.session.commit()

            base_url = _base_url_for_email()
            action_url = f"{base_url}/expire/{token_str}"
            days_left = max(0, (rec.expires_at - datetime.utcnow()).days)
            type_label = 'TV Show' if rec.media_type == 'show' else 'Movie'
            year_str = f" ({rec.year})" if rec.year else ''
            extend_months = policy['extend_months']
            expires_str = _format_date_in_tz(rec.expires_at)
            tz_name = get_setting('DISPLAY_TIMEZONE', 'UTC') or 'UTC'

            subject_prefix = ''
            if _is_dry_run():
                subject_prefix += '[TEST] '
            if unclaimed:
                subject_prefix += '[Unclaimed] '
            subject = f"{subject_prefix}Action needed: \"{rec.title}\" expires in {days_left} days"

            multi_recipient_note = ('<p style="color:#888;font-size:12px;">'
                                    'Note: this notice was also sent to other people who requested this item.</p>'
                                    if len(recipients) > 1 else '')
            unclaimed_note = ('<p style="color:#888;font-size:12px;background:#fff3cd;padding:8px;border-radius:4px;">'
                              "This item is currently <strong>unclaimed</strong> in our records (no requester email). "
                              "You are receiving this because you are configured as the admin fallback. Acting on the "
                              "link below will manage the item on the requester's behalf.</p>"
                              if unclaimed else '')
            dry_run_note = ('<p style="color:#0d6efd;font-size:12px;background:#cfe2ff;padding:8px;border-radius:4px;">'
                            '<strong>TEST MODE:</strong> The system is currently in dry-run mode. The link still works '
                            'for managing this item, but the automatic deletion step will be skipped &mdash; nothing '
                            'will actually be removed yet.</p>'
                            if _is_dry_run() else '')

            any_ok = False
            last_err = None
            for to_email in recipients:
                html = f"""
                <html><body style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">
                    <h2 style="color: #d97706;">⚠️ Your media is about to expire</h2>
                    <p>Hi{(' ' + rec.requester_name) if rec.requester_name else ''},</p>
                    <p>The {type_label.lower()} <strong>{rec.title}{year_str}</strong> that you requested
                       is scheduled for automatic deletion in <strong>{days_left} day(s)</strong>
                       ({expires_str} {tz_name}).</p>
                    <p>To keep it in the library, please choose an option:</p>
                    <p style="margin: 25px 0;">
                        <a href="{action_url}" style="background: #4f46e5; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: 600;">Manage This Item</a>
                    </p>
                    <ul style="color: #555;">
                        <li><strong>Extend</strong> — keep it for another {extend_months} month(s)</li>
                        <li><strong>Keep permanently</strong> — never auto-delete</li>
                        <li><strong>Delete now</strong> — free up space immediately</li>
                    </ul>
                    <p style="color: #c00;">If no action is taken, this item will be automatically deleted shortly after the expiration date.</p>
                    {multi_recipient_note}
                    {unclaimed_note}
                    {dry_run_note}
                    <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
                    <p style="color: #999; font-size: 12px;">This link expires in 30 days. Sent by your media server's automated cleanup system.</p>
                </body></html>
                """
                ok, err = _send_smtp_email(to_email, subject, html, log_meta={
                    'media_type': rec.media_type,
                    'media_title': rec.title,
                    'action_type': 'expiration_warning',
                    'recipient_name': rec.requester_name,
                })
                any_ok = any_ok or ok
                if not ok:
                    last_err = err
                    print(f"[Expiration Warn] Email to {to_email} failed: {err}")

            if any_ok:
                rec.last_warning_sent_at = datetime.utcnow()
                rec.warning_count = (rec.warning_count or 0) + 1
                rec.last_warning_status = 'sent'
                rec.last_warning_error = None
                sent_count += 1
            else:
                rec.last_warning_status = 'failed'
                rec.last_warning_error = last_err or 'unknown'
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[Expiration Warn] Error for {rec.title}: {e}")

    print(f"[Expiration Warn] Sent {sent_count} warning emails")
    return sent_count


def _is_dry_run():
    return get_setting('EXPIRATION_DRY_RUN', 'false').lower() == 'true'


def _delete_via_arr(rec, force_real=False):
    """Remove from Sonarr/Radarr with file deletion. In dry-run, log only and return True."""
    if _is_dry_run() and not force_real:
        print(f"[DRY RUN] Would delete {rec.media_type} {rec.title!r} (id={rec.service_id})")
        return True
    if rec.media_type == 'show':
        url = get_setting('SONARR_URL', '').strip().rstrip('/')
        key = get_setting('SONARR_API_KEY', '').strip()
        if not url or not key:
            return False
        r = requests.delete(f"{url}/api/v3/series/{rec.service_id}",
                            params={'apikey': key, 'deleteFiles': 'true'}, timeout=30)
        return r.ok
    else:
        url = get_setting('RADARR_URL', '').strip().rstrip('/')
        key = get_setting('RADARR_API_KEY', '').strip()
        if not url or not key:
            return False
        r = requests.delete(f"{url}/api/v3/movie/{rec.service_id}",
                            params={'apikey': key, 'deleteFiles': 'true'}, timeout=30)
        return r.ok


def _archive_deletion(rec, deleted_by, dry_run=False, notes=None):
    """Stage a breadcrumb row and flush it so any DB error surfaces BEFORE the destructive *arr call.

    Raises on failure — callers MUST catch and abort the deletion if archiving fails.
    """
    db.session.add(DeletedMediaArchive(
        media_type=rec.media_type,
        service_id=rec.service_id,
        tvdb_id=rec.tvdb_id,
        tmdb_id=rec.tmdb_id,
        imdb_id=rec.imdb_id,
        title=rec.title,
        year=rec.year,
        requester_email=rec.requester_email,
        requester_name=rec.requester_name,
        original_added_at=rec.added_at,
        deleted_at=datetime.utcnow(),
        deleted_by=deleted_by,
        dry_run=dry_run,
        notes=notes,
    ))
    db.session.flush()  # surface DB errors now, before we hit Sonarr/Radarr


def expiration_process_due():
    """Auto-delete items past expiration+grace.

    Safety gates (in order):
      1. Exclusion list always wins.
      2. A successful warning must have been sent at least grace_days ago.
      3. Per-run delete cap. Anything beyond the cap is left for the next run with notes='cap-exceeded'.
      4. Dry-run mode short-circuits the actual *arr delete (still archives + marks the record).
    """
    policy = get_expiration_policy()
    if not policy['enabled']:
        return 0

    dry_run = _is_dry_run()
    try:
        cap = max(0, int(get_setting('EXPIRATION_MAX_DELETIONS_PER_RUN', '10')))
    except Exception:
        cap = 10

    cutoff = datetime.utcnow() - timedelta(days=policy['grace_days'])
    due = MediaExpiration.query.filter(
        MediaExpiration.status.in_(['active', 'extended']),
        MediaExpiration.permanent == False,
        MediaExpiration.expires_at <= cutoff,
    ).order_by(MediaExpiration.expires_at.asc()).all()

    deleted = 0
    skipped_for_cap = 0
    for rec in due:
        # Final safety net — exclusion list always wins
        if _is_title_excluded(rec.media_type, rec.title, rec.year):
            rec.permanent = True
            rec.status = 'permanent'
            rec.notes = 'on-exclusion-list'
            db.session.commit()
            continue
        # SAFETY: never auto-delete without a successful warning at least grace_days old
        if rec.last_warning_status != 'sent' or not rec.last_warning_sent_at:
            rec.notes = 'awaiting-warning'
            db.session.commit()
            continue
        if (datetime.utcnow() - rec.last_warning_sent_at).days < policy['grace_days']:
            continue
        # Per-run cap — leave the rest for the next run, surface in admin
        if cap and deleted >= cap:
            rec.notes = f'cap-exceeded ({cap}/run)'
            db.session.commit()
            skipped_for_cap += 1
            continue
        try:
            # 1) Archive FIRST (flushes immediately) — if this fails we never call *arr.
            note_val = 'dry-run-deletion' if dry_run else None
            _archive_deletion(rec, deleted_by=('dry-run' if dry_run else 'auto-expiration'),
                              dry_run=dry_run, notes=note_val)
            # 2) External destructive call (no-op in dry-run).
            ok = _delete_via_arr(rec)
            if not ok:
                # Roll back the archive row too — nothing was actually deleted.
                db.session.rollback()
                print(f"[Expiration Delete] Failed to delete {rec.title}; archive rolled back")
                continue
            # 3) Mark the source record + DeletionHistory, then commit.
            rec.status = 'deleted'
            rec.deleted_at = datetime.utcnow()
            if dry_run:
                rec.notes = 'dry-run-deletion'
            try:
                db.session.add(DeletionHistory(
                    media_type=rec.media_type,
                    title=rec.title,
                    deleted_at=datetime.utcnow(),
                    deleted_by=('auto-expiration-dryrun' if dry_run else 'auto-expiration'),
                    size_bytes=0,
                ))
            except Exception:
                pass
            db.session.commit()
            deleted += 1
            tag = '[DRY RUN] ' if dry_run else ''
            print(f"[Expiration Delete] {tag}{rec.media_type} {rec.title}")
        except Exception as e:
            db.session.rollback()
            print(f"[Expiration Delete] Error for {rec.title}: {e}")

    if skipped_for_cap:
        print(f"[Expiration Delete] Per-run cap of {cap} reached; {skipped_for_cap} item(s) deferred to next run.")
    return deleted


def expiration_send_intro_emails():
    """Email new Ombi requesters about the auto-expiration policy."""
    policy = get_expiration_policy()
    if not policy['intro_email_enabled']:
        return 0

    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    if not ombi_url or not ombi_key:
        return 0

    # Collect all unique requesters across both endpoints, keyed by ombi user_id when present
    seen = {}  # key -> {email, name, user_id}
    for endpoint in ('tv', 'movie'):
        try:
            r = requests.get(f"{ombi_url}/api/v1/Request/{endpoint}",
                             headers={'ApiKey': ombi_key}, timeout=20)
            if not r.ok:
                continue
            def _collect(u):
                if not u: return
                em = (u.get('email') or u.get('Email') or '').strip().lower()
                nm = u.get('userName') or u.get('alias') or ''
                uid = u.get('id') or u.get('userId') or ''
                if not em: return
                key = f"uid:{uid}" if uid else f"em:{em}"
                if key not in seen:
                    seen[key] = {'email': em, 'name': nm, 'user_id': str(uid) if uid else None}
            for req in r.json() or []:
                _collect(req.get('requestedUser'))
                for c in req.get('childRequests') or []:
                    _collect(c.get('requestedUser'))
        except Exception as e:
            print(f"[Intro Emails] Ombi {endpoint} error: {e}")

    sent = 0
    for info in seen.values():
        email = info['email']
        name = info['name']
        uid = info['user_id']
        # Dedup by either user_id (preferred) or email
        q = OmbiIntroEmailLog.query
        if uid:
            already = q.filter(db.or_(
                OmbiIntroEmailLog.ombi_user_id == uid,
                OmbiIntroEmailLog.requester_email == email,
            )).first()
        else:
            already = q.filter_by(requester_email=email).first()
        if already:
            continue

        months = policy['months']
        warn = policy['warn_days']
        subject = "Welcome — how the media library handles expirations"
        html = f"""
        <html><body style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">
            <h2>Thanks for your media request{(', ' + name) if name else ''}!</h2>
            <p>Just a heads-up about how our library handles content over time:</p>
            <ul style="line-height: 1.8;">
                <li>Each show or movie has an <strong>expiration date</strong> set to about <strong>{months} months</strong> after it was added.</li>
                <li>About <strong>{warn} days</strong> before that date, we'll email you with a link to:
                    <ul>
                        <li>Extend the item another {policy['extend_months']} months</li>
                        <li>Keep it permanently</li>
                        <li>Delete it immediately to free up space</li>
                    </ul>
                </li>
                <li>If you don't respond, the content is <strong>automatically removed</strong> shortly after its expiration date.</li>
            </ul>
            <p>This keeps the library lean and ensures everyone has the storage they need. Just keep an eye on these emails when they arrive!</p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
            <p style="color: #999; font-size: 12px;">This is a one-time notification. You won't receive it again.</p>
        </body></html>
        """
        ok, err = _send_smtp_email(email, subject, html, log_meta={
            'media_type': 'system',
            'media_title': 'Welcome / Expiration Policy',
            'action_type': 'intro_email',
            'recipient_name': name,
        })
        if ok:
            try:
                db.session.add(OmbiIntroEmailLog(
                    requester_email=email,
                    requester_name=name,
                    ombi_user_id=uid,
                ))
                db.session.commit()
                sent += 1
            except Exception:
                db.session.rollback()

    print(f"[Intro Emails] Sent {sent} new intro emails")
    return sent


def daily_expiration_job(force=False, lock_already_held=False):
    """Master job; safe across multiple workers via atomic compare-and-set + in-process lock."""
    if not lock_already_held:
        if not _expiration_run_lock.acquire(blocking=False):
            print("[Daily Expiration Job] Already running in this process; skipping.")
            return
    try:
        with app.app_context():
            now_iso = datetime.utcnow().isoformat()
            threshold_iso = (datetime.utcnow() - timedelta(hours=23)).isoformat()
            claimed = False
            try:
                if not Settings.query.filter_by(key='EXPIRATION_LAST_RUN_AT').first():
                    try:
                        db.session.add(Settings(key='EXPIRATION_LAST_RUN_AT', value=''))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                if force:
                    db.session.execute(db.text(
                        "UPDATE settings SET value = :now WHERE key = 'EXPIRATION_LAST_RUN_AT'"
                    ), {'now': now_iso})
                    db.session.commit()
                    claimed = True
                else:
                    result = db.session.execute(db.text("""
                        UPDATE settings SET value = :now
                        WHERE key = 'EXPIRATION_LAST_RUN_AT'
                          AND (value IS NULL OR value = '' OR value < :threshold)
                    """), {'now': now_iso, 'threshold': threshold_iso})
                    db.session.commit()
                    claimed = (result.rowcount or 0) > 0
                if not claimed:
                    return
                print("[Daily Expiration Job] Running...")
                expiration_reconcile_with_exclusions()
                expiration_sync_new_items()
                expiration_send_warnings()
                expiration_process_due()
                expiration_send_intro_emails()
                print("[Daily Expiration Job] Done.")
            except Exception as e:
                print(f"[Daily Expiration Job] Fatal error: {e}")
                # Release the lock by setting timestamp 22h old, so next hourly tick can retry
                if claimed:
                    try:
                        retry_iso = (datetime.utcnow() - timedelta(hours=22)).isoformat()
                        db.session.execute(db.text(
                            "UPDATE settings SET value = :v WHERE key = 'EXPIRATION_LAST_RUN_AT'"
                        ), {'v': retry_iso})
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
    finally:
        _expiration_run_lock.release()


# ===== Watchlist Sync =====

_watchlist_sync_lock = threading.Lock()


def _pick_quality_profile(profiles):
    """Return the best quality profile id: prefer 1080p, then 720p, then first."""
    if not profiles:
        return 1
    for keyword in ('1080', '720'):
        for p in profiles:
            if keyword in (p.get('name') or ''):
                return p['id']
    return profiles[0]['id']


def _best_root_folder(roots):
    """Return the root folder path with the most free space."""
    if not roots:
        return None
    best = max(roots, key=lambda r: r.get('freeSpace', 0))
    return best.get('path')


def _sync_watchlist_item(item_row):
    """Try to add a WatchlistSyncItem to Sonarr or Radarr. Returns (status, message)."""
    title = item_row.title
    year = item_row.year
    media_type = item_row.media_type  # 'movie' | 'show'

    if media_type == 'show':
        sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
        sonarr_key = get_setting('SONARR_API_KEY', '').strip()
        if not sonarr_url or not sonarr_key:
            return 'skipped', 'Sonarr not configured'
        try:
            roots = requests.get(f"{sonarr_url}/api/v3/rootfolder",
                                 params={'apikey': sonarr_key}, timeout=10).json()
            root_path = _best_root_folder(roots) or '/tv'
            profiles = requests.get(f"{sonarr_url}/api/v3/qualityprofile",
                                    params={'apikey': sonarr_key}, timeout=10).json()
            qual_id = _pick_quality_profile(profiles)
            search_terms = [f'"{title}"']
            if year:
                search_terms.append(f'"{title} {year}"')
            lookup_item = None
            for term in search_terms:
                lk = requests.get(f"{sonarr_url}/api/v3/series/lookup",
                                  params={'term': term, 'apikey': sonarr_key}, timeout=12).json()
                if isinstance(lk, list) and lk:
                    title_lower = title.lower()
                    for candidate in lk:
                        if (candidate.get('title') or '').lower() == title_lower:
                            if not year or candidate.get('year') == year:
                                lookup_item = candidate
                                break
                    if not lookup_item:
                        lookup_item = lk[0]
                    break
            if not lookup_item:
                return 'failed', f'Could not find "{title}" in Sonarr lookup'
            tvdb_id = lookup_item.get('tvdbId', 0)
            if not tvdb_id:
                return 'failed', f'No TVDB ID found for "{title}"'
            payload = {
                'title': lookup_item.get('title', title),
                'tvdbId': tvdb_id,
                'qualityProfileId': qual_id,
                'titleSlug': lookup_item.get('titleSlug', title.lower().replace(' ', '-')),
                'images': lookup_item.get('images', []),
                'seasons': lookup_item.get('seasons', []),
                'rootFolderPath': root_path,
                'monitored': True,
                'addOptions': {'searchForMissingEpisodes': True},
            }
            r = requests.post(f"{sonarr_url}/api/v3/series",
                              params={'apikey': sonarr_key}, json=payload, timeout=15)
            if r.ok:
                return 'added', f'Added to Sonarr (profile id {qual_id}, folder {root_path})'
            err_data = r.json()
            err_msg = (err_data[0].get('errorMessage', '') if isinstance(err_data, list) and err_data
                       else str(err_data))
            if 'already' in err_msg.lower() or 'exists' in err_msg.lower():
                return 'already_exists', 'Already in Sonarr'
            return 'failed', f'Sonarr error: {err_msg}'
        except Exception as e:
            return 'failed', f'Exception: {e}'

    elif media_type == 'movie':
        radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
        radarr_key = get_setting('RADARR_API_KEY', '').strip()
        if not radarr_url or not radarr_key:
            return 'skipped', 'Radarr not configured'
        try:
            roots = requests.get(f"{radarr_url}/api/v3/rootfolder",
                                 params={'apikey': radarr_key}, timeout=10).json()
            root_path = _best_root_folder(roots) or '/movies'
            profiles = requests.get(f"{radarr_url}/api/v3/qualityprofile",
                                    params={'apikey': radarr_key}, timeout=10).json()
            qual_id = _pick_quality_profile(profiles)
            search_term = f'"{title}"'
            lk = requests.get(f"{radarr_url}/api/v3/movie/lookup",
                              params={'term': search_term, 'apikey': radarr_key}, timeout=12).json()
            lookup_item = None
            if isinstance(lk, list) and lk:
                title_lower = title.lower()
                for candidate in lk:
                    if (candidate.get('title') or '').lower() == title_lower:
                        if not year or candidate.get('year') == year:
                            lookup_item = candidate
                            break
                if not lookup_item:
                    lookup_item = lk[0]
            if not lookup_item:
                return 'failed', f'Could not find "{title}" in Radarr lookup'
            tmdb_id = lookup_item.get('tmdbId', 0)
            if not tmdb_id:
                return 'failed', f'No TMDB ID found for "{title}"'
            payload = {
                'title': lookup_item.get('title', title),
                'tmdbId': tmdb_id,
                'qualityProfileId': qual_id,
                'titleSlug': lookup_item.get('titleSlug', title.lower().replace(' ', '-')),
                'images': lookup_item.get('images', []),
                'year': lookup_item.get('year', year or 0),
                'rootFolderPath': root_path,
                'monitored': True,
                'addOptions': {'searchForMovie': True},
            }
            r = requests.post(f"{radarr_url}/api/v3/movie",
                              params={'apikey': radarr_key}, json=payload, timeout=15)
            if r.ok:
                return 'added', f'Added to Radarr (profile id {qual_id}, folder {root_path})'
            err_data = r.json()
            err_msg = (err_data[0].get('errorMessage', '') if isinstance(err_data, list) and err_data
                       else str(err_data))
            if 'already' in err_msg.lower() or 'exists' in err_msg.lower():
                return 'already_exists', 'Already in Radarr'
            return 'failed', f'Radarr error: {err_msg}'
        except Exception as e:
            return 'failed', f'Exception: {e}'

    return 'skipped', f'Unknown media type: {media_type}'


def _remove_watched_from_watchlist(watchlist_items, plex_url, plex_token, plex_headers):
    """Check each watchlist item against the local Plex library. Remove fully-watched items.

    Movies: removed if viewCount > 0.
    TV shows: removed if viewedLeafCount == leafCount (all episodes watched).
    Returns list of (title, year) tuples that were removed.
    """
    try:
        sections_resp = requests.get(
            f"{plex_url}/library/sections",
            params={'X-Plex-Token': plex_token},
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        if sections_resp.status_code != 200:
            print(f'[WatchlistSync/RemoveWatched] Could not fetch Plex sections: {sections_resp.status_code}')
            return []
        dirs = sections_resp.json().get('MediaContainer', {}).get('Directory', [])
    except Exception as e:
        print(f'[WatchlistSync/RemoveWatched] Sections error: {e}')
        return []

    # Build lookup: (title_lower, year_or_none, plex_type) -> fully_watched bool
    watched = {}
    for section in dirs:
        sec_type = section.get('type')
        sec_key = section.get('key')
        if sec_type not in ('movie', 'show'):
            continue
        try:
            lib_resp = requests.get(
                f"{plex_url}/library/sections/{sec_key}/all",
                params={'X-Plex-Token': plex_token, 'type': 1 if sec_type == 'movie' else 2},
                headers={'Accept': 'application/json'},
                timeout=20,
            )
            if lib_resp.status_code != 200:
                continue
            for m in lib_resp.json().get('MediaContainer', {}).get('Metadata', []):
                tl = (m.get('title') or '').lower()
                yr = m.get('year')
                try:
                    yr = int(yr) if yr else None
                except (ValueError, TypeError):
                    yr = None
                if sec_type == 'movie':
                    watched[(tl, yr, 'movie')] = (m.get('viewCount') or 0) > 0
                else:
                    leaf = m.get('leafCount') or 0
                    viewed = m.get('viewedLeafCount') or 0
                    watched[(tl, yr, 'show')] = leaf > 0 and viewed >= leaf
        except Exception as e:
            print(f'[WatchlistSync/RemoveWatched] Section {sec_key} error: {e}')

    removed = []
    for item in watchlist_items:
        tl = (item.get('title') or '').lower()
        raw_y = item.get('year')
        try:
            yr = int(raw_y) if raw_y else None
        except (ValueError, TypeError):
            yr = None
        ptype = item.get('type', '')
        if ptype not in ('movie', 'show'):
            continue
        rating_key = item.get('ratingKey', '')
        if not rating_key:
            continue

        # Match with year first, then title-only fallback
        is_watched = (
            watched.get((tl, yr, ptype))
            or watched.get((tl, None, ptype))
            or any(v for (tt, ty, tp), v in watched.items() if tt == tl and tp == ptype)
        )
        if not is_watched:
            continue

        try:
            r = requests.put(
                "https://discover.provider.plex.tv/actions/removeFromWatchlist",
                params={'ratingKey': rating_key},
                headers=plex_headers,
                timeout=15,
            )
            if r.status_code in (200, 201, 204):
                removed.append((item.get('title', ''), yr, item.get('guid', '')))
                print(f"[WatchlistSync/RemoveWatched] Removed '{item.get('title')}' ({yr}) — fully watched")
            else:
                print(f"[WatchlistSync/RemoveWatched] Could not remove '{item.get('title')}': HTTP {r.status_code}")
        except Exception as e:
            print(f"[WatchlistSync/RemoveWatched] Error removing '{tl}': {e}")

    return removed


def watchlist_sync_job():
    """Background job: poll Plex watchlist and auto-add new items to Sonarr/Radarr."""
    with app.app_context():
        try:
            enabled = get_setting('WATCHLIST_SYNC_ENABLED', 'false').lower() == 'true'
            if not enabled:
                return

            plex_token = get_setting('PLEX_TOKEN', '').strip()
            if not plex_token:
                print('[WatchlistSync] Skipping — Plex token not configured')
                return

            plex_headers = {
                'X-Plex-Token': plex_token,
                'X-Plex-Client-Identifier': 'media-scrubber-watchlist-sync',
                'X-Plex-Product': 'Media Scrubber',
                'X-Plex-Version': '1.0',
                'Accept': 'application/json',
            }

            all_items = []
            offset = 0
            page_size = 50
            total_size = None
            while True:
                resp = requests.get(
                    "https://discover.provider.plex.tv/library/sections/watchlist/all",
                    params={'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': page_size},
                    headers=plex_headers,
                    timeout=20,
                )
                if resp.status_code != 200:
                    print(f'[WatchlistSync] Plex returned {resp.status_code}, aborting')
                    return
                container = resp.json().get('MediaContainer', {})
                items = container.get('Metadata', [])
                if total_size is None:
                    total_size = container.get('totalSize', len(items))
                if not items:
                    break
                all_items.extend(items)
                offset += len(items)
                if offset >= total_size:
                    break

            now = datetime.utcnow()
            added_count = 0
            new_count = 0
            fail_count = 0

            for item in all_items:
                guid = item.get('guid', '')
                if not guid:
                    continue
                rating_key = item.get('ratingKey', '')
                title = item.get('title', 'Unknown')
                raw_year = item.get('year')
                try:
                    year = int(raw_year) if raw_year else None
                except (ValueError, TypeError):
                    year = None
                plex_type = item.get('type', '')  # 'movie' or 'show'
                if plex_type not in ('movie', 'show'):
                    continue

                row = WatchlistSyncItem.query.filter_by(plex_guid=guid).first()
                if row:
                    row.last_seen_at = now
                    if row.status in ('added', 'already_exists', 'skipped'):
                        db.session.commit()
                        continue
                else:
                    row = WatchlistSyncItem(
                        plex_guid=guid,
                        plex_rating_key=rating_key,
                        title=title,
                        year=year,
                        media_type=plex_type,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    db.session.add(row)
                    db.session.flush()
                    new_count += 1

                status, msg = _sync_watchlist_item(row)
                row.status = status
                row.status_message = msg
                row.processed_at = now
                db.session.commit()

                print(f'[WatchlistSync] {title} ({year}) [{plex_type}] → {status}: {msg}')
                if status == 'added':
                    added_count += 1
                elif status == 'failed':
                    fail_count += 1

            # Optional: remove fully-watched items from the Plex watchlist
            removed_count = 0
            if get_setting('WATCHLIST_SYNC_REMOVE_WATCHED', 'false').lower() == 'true':
                plex_url_local = get_setting('PLEX_URL', '').strip().rstrip('/')
                if plex_url_local:
                    removed = _remove_watched_from_watchlist(all_items, plex_url_local, plex_token, plex_headers)
                    removed_count = len(removed)
                    for (r_title, r_year, r_guid) in removed:
                        row = WatchlistSyncItem.query.filter_by(plex_guid=r_guid).first()
                        if not row:
                            row = WatchlistSyncItem.query.filter(
                                WatchlistSyncItem.title == r_title,
                                WatchlistSyncItem.year == r_year,
                            ).first()
                        if row:
                            row.removed_watched_at = now
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

            summary_parts = [
                f'{len(all_items)} on watchlist',
                f'{new_count} new',
                f'{added_count} added',
                f'{fail_count} failed',
            ]
            if removed_count:
                summary_parts.append(f'{removed_count} removed (watched)')
            set_setting('WATCHLIST_SYNC_LAST_RUN', now.strftime('%Y-%m-%d %H:%M:%S UTC'))
            set_setting('WATCHLIST_SYNC_LAST_SUMMARY', ', '.join(summary_parts))
            print(f'[WatchlistSync] Done — {", ".join(summary_parts)}')
        except Exception as e:
            print(f'[WatchlistSync] Unhandled error: {e}')


# Initialize policy defaults & start scheduler
def _init_expiration_system():
    _run_expiration_migrations()
    with app.app_context():
        for k, v in EXPIRATION_DEFAULTS.items():
            existing = Settings.query.filter_by(key=k).first()
            if not existing:
                db.session.add(Settings(key=k, value=v))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(daily_expiration_job, 'interval', hours=1, id='daily_expiration', max_instances=1, coalesce=True)
        sched.add_job(watchlist_sync_job, 'interval', minutes=5, id='watchlist_sync', max_instances=1, coalesce=True)
        sched.start()
        print("[Scheduler] Daily expiration job scheduled (hourly check, runs ~daily)")
        print("[Scheduler] Watchlist sync job scheduled (every 5 minutes)")
    except Exception as e:
        print(f"[Scheduler] Failed to start: {e}")


_init_expiration_system()


# ===== Admin & action routes =====

@app.route('/expirations')
@login_required
def expirations_page():
    policy = get_expiration_policy()
    attention_q = MediaExpiration.query.filter(db.or_(
        MediaExpiration.status == 'missing',
        MediaExpiration.last_warning_status.in_(['failed', 'no_email']),
        db.and_(
            MediaExpiration.status.in_(['active', 'extended']),
            MediaExpiration.permanent == False,
            MediaExpiration.requester_email.is_(None),
        ),
    ))
    counts = {
        'active': MediaExpiration.query.filter_by(status='active').count(),
        'extended': MediaExpiration.query.filter_by(status='extended').count(),
        'permanent': MediaExpiration.query.filter_by(permanent=True).count(),
        'deleted': MediaExpiration.query.filter_by(status='deleted').count(),
        'missing': MediaExpiration.query.filter_by(status='missing').count(),
        'attention': attention_q.count(),
    }
    return render_template('expirations.html', policy=policy, counts=counts,
                           last_run=get_setting('EXPIRATION_LAST_RUN_AT', ''),
                           display_timezone=get_setting('DISPLAY_TIMEZONE', 'UTC'),
                           use_oldest_file_date=(get_setting('EXPIRATION_USE_OLDEST_FILE_DATE', 'false').lower() == 'true'),
                           dry_run=_is_dry_run(),
                           max_per_run=int(get_setting('EXPIRATION_MAX_DELETIONS_PER_RUN', '10') or 10),
                           admin_fallback_email=get_setting('EXPIRATION_ADMIN_FALLBACK_EMAIL', ''),
                           public_url=get_setting('EXPIRATION_PUBLIC_URL', ''),
                           detected_public_url=_base_url_for_email())


@app.route('/api/expirations/list')
@login_required
def expirations_list_api():
    status = request.args.get('status', 'active')
    sort = request.args.get('sort', 'expires_asc')
    media_type = request.args.get('media_type', '')
    search = (request.args.get('search', '') or '').strip().lower()

    q = MediaExpiration.query
    if status == 'permanent':
        q = q.filter_by(permanent=True)
    elif status == 'attention':
        q = q.filter(db.or_(
            MediaExpiration.status == 'missing',
            MediaExpiration.last_warning_status.in_(['failed', 'no_email']),
            db.and_(
                MediaExpiration.status.in_(['active', 'extended']),
                MediaExpiration.permanent == False,
                MediaExpiration.requester_email.is_(None),
            ),
        ))
    elif status == 'all':
        pass
    else:
        q = q.filter_by(status=status)
    if media_type in ('show', 'movie'):
        q = q.filter_by(media_type=media_type)
    if search:
        q = q.filter(db.func.lower(MediaExpiration.title).like(f"%{search}%"))
    if sort == 'expires_asc':
        q = q.order_by(MediaExpiration.expires_at.asc())
    elif sort == 'expires_desc':
        q = q.order_by(MediaExpiration.expires_at.desc())
    elif sort == 'added_desc':
        q = q.order_by(MediaExpiration.added_at.desc())
    items = q.limit(500).all()

    now = datetime.utcnow()
    return jsonify({
        'items': [{
            'id': i.id,
            'media_type': i.media_type,
            'service_id': i.service_id,
            'title': i.title,
            'year': i.year,
            'requester_name': i.requester_name,
            'requester_email': i.requester_email,
            'additional_requester_emails': i.additional_requester_emails,
            'added_at': i.added_at.isoformat() if i.added_at else None,
            'expires_at': i.expires_at.isoformat() if i.expires_at else None,
            'days_left': (i.expires_at - now).days if i.expires_at else None,
            'status': i.status,
            'permanent': i.permanent,
            'warning_count': i.warning_count or 0,
            'last_warning_sent_at': i.last_warning_sent_at.isoformat() if i.last_warning_sent_at else None,
            'last_warning_status': i.last_warning_status,
            'last_warning_error': i.last_warning_error,
            'requester_lookup_attempts': i.requester_lookup_attempts or 0,
            'last_seen_at': i.last_seen_at.isoformat() if i.last_seen_at else None,
            'extension_count': i.extension_count or 0,
            'notes': i.notes,
        } for i in items]
    })


@app.route('/api/expirations/save-policy', methods=['POST'])
@login_required
def expirations_save_policy():
    data = request.get_json() or {}
    mapping = {
        'EXPIRATION_ENABLED': 'true' if data.get('enabled') else 'false',
        'EXPIRATION_MONTHS': str(int(data.get('months', 6))),
        'EXPIRATION_WARN_DAYS_BEFORE': str(int(data.get('warn_days', 14))),
        'EXPIRATION_GRACE_DAYS': str(int(data.get('grace_days', 7))),
        'EXPIRATION_EXTEND_MONTHS': str(int(data.get('extend_months', 6))),
        'EXPIRATION_INTRO_EMAIL_ENABLED': 'true' if data.get('intro_email_enabled') else 'false',
        'EXPIRATION_USE_OLDEST_FILE_DATE': 'true' if data.get('use_oldest_file_date') else 'false',
        'DISPLAY_TIMEZONE': (data.get('display_timezone') or 'UTC').strip() or 'UTC',
        'EXPIRATION_DRY_RUN': 'true' if data.get('dry_run') else 'false',
        'EXPIRATION_MAX_DELETIONS_PER_RUN': str(max(0, int(data.get('max_per_run', 10)))),
        'EXPIRATION_ADMIN_FALLBACK_EMAIL': (data.get('admin_fallback_email') or '').strip(),
        'EXPIRATION_PUBLIC_URL': (data.get('public_url') or '').strip(),
    }
    for k, v in mapping.items():
        set_setting(k, v)
    return jsonify({'success': True, 'policy': get_expiration_policy()})


@app.route('/api/expirations/send-test-warning', methods=['POST'])
@login_required
def expirations_send_test_warning():
    """Send a real, working warning email to a chosen address against the soonest-expiring active item.

    Useful for verifying SMTP, the public URL setting, and the full /expire/<token> click-through flow.
    The token is real, so clicking through and choosing an action will actually take effect on the item.
    """
    data = request.get_json() or {}
    to_email = (data.get('email') or '').strip()
    if not to_email or '@' not in to_email:
        return jsonify({'success': False, 'error': 'Valid recipient email required'}), 400

    rec = (MediaExpiration.query
           .filter(MediaExpiration.status.in_(['active', 'extended']),
                   MediaExpiration.permanent == False)
           .order_by(MediaExpiration.expires_at.asc())
           .first())
    if not rec:
        return jsonify({'success': False,
                        'error': 'No active expiration records to test against. Run a scan first.'}), 400

    try:
        token_str = secrets.token_urlsafe(32)
        tok = ExpirationActionToken(
            token=token_str,
            expiration_id=rec.id,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.session.add(tok)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Could not mint token: {e}'}), 500

    base_url = _base_url_for_email()
    action_url = f"{base_url}/expire/{token_str}"
    policy = get_expiration_policy()
    days_left = max(0, (rec.expires_at - datetime.utcnow()).days)
    type_label = 'TV Show' if rec.media_type == 'show' else 'Movie'
    year_str = f" ({rec.year})" if rec.year else ''
    extend_months = policy['extend_months']
    expires_str = _format_date_in_tz(rec.expires_at)
    tz_name = get_setting('DISPLAY_TIMEZONE', 'UTC') or 'UTC'

    subject = f"[TEST EMAIL] Action needed: \"{rec.title}\" expires in {days_left} days"
    test_banner = (
        '<p style="color:#0d6efd;font-size:13px;background:#cfe2ff;padding:10px;border-radius:4px;">'
        '<strong>This is a manual TEST email</strong> sent from the admin so you can verify the '
        '"Manage This Item" button works. The token is real &mdash; clicking through and choosing an '
        "action will actually take effect on the item shown below.</p>"
    )
    dry_run_note = (
        '<p style="color:#0d6efd;font-size:12px;background:#cfe2ff;padding:8px;border-radius:4px;">'
        '<strong>TEST MODE:</strong> The system is currently in dry-run mode. The link still works '
        'for managing this item, but the automatic deletion step will be skipped &mdash; nothing '
        'will actually be removed yet.</p>'
        if _is_dry_run() else ''
    )
    html = f"""
    <html><body style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">
        {test_banner}
        <h2 style="color: #d97706;">⚠️ Your media is about to expire</h2>
        <p>Hi,</p>
        <p>The {type_label.lower()} <strong>{rec.title}{year_str}</strong> that you requested
           is scheduled for automatic deletion in <strong>{days_left} day(s)</strong>
           ({expires_str} {tz_name}).</p>
        <p>To keep it in the library, please choose an option:</p>
        <p style="margin: 25px 0;">
            <a href="{action_url}" style="background: #4f46e5; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: 600;">Manage This Item</a>
        </p>
        <ul style="color: #555;">
            <li><strong>Extend</strong> &mdash; keep it for another {extend_months} month(s)</li>
            <li><strong>Keep permanently</strong> &mdash; never auto-delete</li>
            <li><strong>Delete now</strong> &mdash; free up space immediately</li>
        </ul>
        <p style="color: #c00;">If no action is taken, this item will be automatically deleted shortly after the expiration date.</p>
        {dry_run_note}
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="color: #999; font-size: 12px;">
            Test target item id: {rec.id} &middot; Token expires in 30 days &middot;
            Link host: <code>{base_url}</code>
        </p>
    </body></html>
    """

    ok, err = _send_smtp_email(to_email, subject, html, log_meta={
        'media_type': rec.media_type,
        'media_title': rec.title,
        'action_type': 'expiration_warning_test',
        'recipient_name': 'Test Recipient',
    })
    if ok:
        return jsonify({
            'success': True,
            'item': f"{rec.title}{year_str}",
            'item_id': rec.id,
            'action_url': action_url,
            'base_url': base_url,
            'recipient': to_email,
        })
    return jsonify({'success': False, 'error': err or 'SMTP send failed'}), 500


@app.route('/api/expirations/scan-now', methods=['POST'])
@login_required
def expirations_scan_now():
    """Manually trigger the daily job. Atomically acquire the lock here so simultaneous clicks can't both spawn workers."""
    if not _expiration_run_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': 'A scan is already running.'}), 409

    def _runner():
        try:
            daily_expiration_job(force=True, lock_already_held=True)
        finally:
            _expiration_run_lock.release()

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({'success': True, 'message': 'Scan started in background.'})


@app.route('/api/expirations/recompute-dates', methods=['POST'])
@login_required
def expirations_recompute_dates():
    """Re-apply the current policy to all active items (after months/warn/grace changed)."""
    policy = get_expiration_policy()
    updated = 0
    floor_days = policy['warn_days'] + policy['grace_days'] + 1
    for rec in MediaExpiration.query.filter(
        MediaExpiration.status.in_(['active', 'extended']),
        MediaExpiration.permanent == False,
    ).all():
        base = rec.added_at or datetime.utcnow()
        natural = _add_months(base, policy['months'])
        floor = datetime.utcnow() + timedelta(days=floor_days)
        rec.expires_at = max(natural, floor)
        updated += 1
    db.session.commit()
    return jsonify({'success': True, 'updated': updated})


@app.route('/api/expirations/bulk-action', methods=['POST'])
@login_required
def expirations_bulk_action():
    """Apply an action to many items at once."""
    data = request.get_json() or {}
    ids = data.get('ids') or []
    action = data.get('action')
    months = int(data.get('months') or get_expiration_policy()['extend_months'])
    if not ids or not action:
        return jsonify({'success': False, 'error': 'ids and action required'}), 400

    policy = get_expiration_policy()
    ok_count, fail_count, errors = 0, 0, []
    by = current_user.username if current_user.is_authenticated else 'admin'

    for eid in ids:
        rec = MediaExpiration.query.get(eid)
        if not rec:
            fail_count += 1
            continue
        try:
            if action == 'extend':
                rec.expires_at = _add_months(datetime.utcnow(), months)
                rec.status = 'extended'
                rec.extension_count = (rec.extension_count or 0) + 1
                rec.last_warning_sent_at = None
                rec.last_warning_status = None
            elif action == 'permanent':
                rec.permanent = True
                rec.status = 'permanent'
                _add_to_exclusion_list(rec, by_name=by)
            elif action == 'reset':
                if rec.media_type == 'show':
                    ex = Exclusion.query.filter(db.func.lower(Exclusion.title) == rec.title.lower()).first()
                    if ex: db.session.delete(ex)
                else:
                    ex = MovieExclusion.query.filter(db.func.lower(MovieExclusion.title) == rec.title.lower()).first()
                    if ex: db.session.delete(ex)
                rec.permanent = False
                rec.status = 'active'
                rec.notes = None
                floor = datetime.utcnow() + timedelta(days=policy['warn_days'] + policy['grace_days'] + 1)
                rec.expires_at = max(_add_months(rec.added_at or datetime.utcnow(), policy['months']), floor)
            elif action == 'forget':
                db.session.delete(rec)
            elif action == 'delete-now':
                dry = _is_dry_run()
                # Archive FIRST (flushes); abort cleanly if it fails.
                _archive_deletion(rec, deleted_by=('bulk-dry-run' if dry else 'bulk-delete'),
                                  dry_run=dry, notes=f'bulk action by {by}')
                if not _delete_via_arr(rec):
                    db.session.rollback()
                    fail_count += 1
                    errors.append(f"{rec.title}: delete failed (archive rolled back)")
                    continue
                rec.status = 'deleted'
                rec.deleted_at = datetime.utcnow()
                if dry:
                    rec.notes = 'dry-run-deletion'
                # Commit per-item so a later item's failure can't undo this archive.
                db.session.commit()
            else:
                fail_count += 1
                continue
            ok_count += 1
        except Exception as e:
            # Roll back any pending (un-committed) work for this item — including a flushed-but-uncommitted
            # archive row from delete-now — so it can never be smuggled into the final commit below.
            db.session.rollback()
            fail_count += 1
            errors.append(f"{rec.title if rec else eid}: {e}")
    db.session.commit()
    return jsonify({'success': True, 'ok': ok_count, 'failed': fail_count, 'errors': errors[:5]})


@app.route('/api/expirations/<int:eid>/action', methods=['POST'])
@login_required
def expirations_admin_action(eid):
    rec = MediaExpiration.query.get_or_404(eid)
    data = request.get_json() or {}
    action = data.get('action')
    policy = get_expiration_policy()

    if action == 'extend':
        months = int(data.get('months', policy['extend_months']))
        rec.expires_at = _add_months(datetime.utcnow(), months)
        rec.status = 'extended'
        rec.extension_count = (rec.extension_count or 0) + 1
        rec.last_warning_sent_at = None
    elif action == 'permanent':
        rec.permanent = True
        rec.status = 'permanent'
        # Mirror into the global exclusion list — single source of truth
        _add_to_exclusion_list(rec, by_name=current_user.username if current_user.is_authenticated else 'admin')
    elif action == 'reset':
        # Also remove from exclusion list so it's truly back in rotation
        if rec.media_type == 'show':
            ex = Exclusion.query.filter(db.func.lower(Exclusion.title) == rec.title.lower()).first()
            if ex: db.session.delete(ex)
        else:
            ex = MovieExclusion.query.filter(db.func.lower(MovieExclusion.title) == rec.title.lower()).first()
            if ex: db.session.delete(ex)
        rec.permanent = False
        rec.status = 'active'
        rec.notes = None
        floor = datetime.utcnow() + timedelta(days=policy['warn_days'] + policy['grace_days'] + 1)
        rec.expires_at = max(_add_months(rec.added_at or datetime.utcnow(), policy['months']), floor)
    elif action == 'delete-now':
        dry = _is_dry_run()
        by = current_user.username if current_user.is_authenticated else 'admin'
        # Archive FIRST so a deletion can never escape the audit log.
        try:
            _archive_deletion(rec, deleted_by=('admin-dry-run' if dry else 'admin-delete-now'),
                              dry_run=dry, notes=f'admin action by {by}')
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': f'Archive insert failed; deletion aborted: {e}'}), 500
        if not _delete_via_arr(rec):
            db.session.rollback()
            return jsonify({'success': False, 'error': 'Sonarr/Radarr delete failed (archive rolled back)'}), 502
        rec.status = 'deleted'
        rec.deleted_at = datetime.utcnow()
        if dry:
            rec.notes = 'dry-run-deletion'
    elif action == 'forget':
        db.session.delete(rec)
    else:
        return jsonify({'success': False, 'error': 'Unknown action'}), 400

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/archive/list')
@login_required
def archive_list():
    """Paginated list of archived deletions, newest first."""
    try:
        limit = max(1, min(500, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))
    except Exception:
        limit, offset = 100, 0
    show_dry = (request.args.get('include_dry', 'true').lower() == 'true')
    q = DeletedMediaArchive.query
    if not show_dry:
        q = q.filter(DeletedMediaArchive.dry_run == False)
    total = q.count()
    rows = q.order_by(DeletedMediaArchive.deleted_at.desc()).offset(offset).limit(limit).all()
    return jsonify({
        'success': True,
        'total': total,
        'items': [{
            'id': r.id,
            'media_type': r.media_type,
            'service_id': r.service_id,
            'tvdb_id': r.tvdb_id,
            'tmdb_id': r.tmdb_id,
            'imdb_id': r.imdb_id,
            'title': r.title,
            'year': r.year,
            'requester_email': r.requester_email,
            'requester_name': r.requester_name,
            'original_added_at': r.original_added_at.isoformat() if r.original_added_at else None,
            'deleted_at': r.deleted_at.isoformat() if r.deleted_at else None,
            'deleted_by': r.deleted_by,
            'dry_run': bool(r.dry_run),
            're_requested_at': r.re_requested_at.isoformat() if r.re_requested_at else None,
            'notes': r.notes,
        } for r in rows],
    })


def _ombi_re_request(rec):
    """Submit a fresh request to Ombi for an archived item. Returns (ok, message)."""
    ombi_url = get_setting('OMBI_URL', '').strip().rstrip('/')
    ombi_key = get_setting('OMBI_API_KEY', '').strip()
    if not ombi_url or not ombi_key:
        return False, 'Ombi is not configured'
    headers = {'ApiKey': ombi_key, 'Accept': 'application/json', 'Content-Type': 'application/json'}
    try:
        if rec.media_type == 'movie':
            if not rec.tmdb_id:
                return False, 'Missing TMDb id; cannot re-request movie'
            r = requests.post(f'{ombi_url}/api/v1/Request/movie', headers=headers,
                              json={'theMovieDbId': int(rec.tmdb_id)}, timeout=20)
        else:
            if not rec.tvdb_id:
                return False, 'Missing TVDb id; cannot re-request show'
            r = requests.post(f'{ombi_url}/api/v1/Request/tv', headers=headers,
                              json={'tvDbId': int(rec.tvdb_id), 'requestAll': True}, timeout=20)
        if r.ok:
            return True, 'Re-requested via Ombi'
        return False, f'Ombi returned HTTP {r.status_code}: {r.text[:200]}'
    except Exception as e:
        return False, str(e)


@app.route('/api/archive/<int:aid>/re-request', methods=['POST'])
@login_required
def archive_re_request(aid):
    rec = DeletedMediaArchive.query.get_or_404(aid)
    ok, msg = _ombi_re_request(rec)
    if ok:
        rec.re_requested_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': ok, 'message': msg})


@app.route('/expire/<token>')
def expire_action_page(token):
    tok = ExpirationActionToken.query.filter_by(token=token).first()
    if not tok:
        return render_template('expire_error.html', message='This link is invalid.'), 404
    if tok.used_at:
        return render_template('expire_error.html', message=f'You already chose to {tok.action_taken} this item.')
    if tok.expires_at < datetime.utcnow():
        return render_template('expire_error.html', message='This link has expired. Please contact your library admin.')
    rec = MediaExpiration.query.get(tok.expiration_id)
    if not rec:
        return render_template('expire_error.html', message='This item is no longer tracked.'), 404
    return render_template('expire_action.html', rec=rec, token=token,
                           extend_months=get_expiration_policy()['extend_months'])


@app.route('/expire/<token>/action', methods=['POST'])
def expire_action_submit(token):
    action = (request.get_json() or {}).get('action')
    if action not in ('extend', 'keep', 'delete'):
        return jsonify({'success': False, 'error': 'Unknown action'}), 400

    # Atomically claim the token so concurrent requests can't double-act
    now = datetime.utcnow()
    claim = db.session.execute(db.text("""
        UPDATE expiration_action_token
        SET used_at = :now, action_taken = :action
        WHERE token = :token AND used_at IS NULL AND expires_at > :now
    """), {'now': now, 'action': action, 'token': token})
    if (claim.rowcount or 0) == 0:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Token invalid, expired, or already used'}), 400

    tok = ExpirationActionToken.query.filter_by(token=token).first()
    rec = MediaExpiration.query.get(tok.expiration_id) if tok else None
    if not rec:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Item no longer tracked'}), 404

    policy = get_expiration_policy()
    try:
        if action == 'extend':
            rec.expires_at = _add_months(now, policy['extend_months'])
            rec.status = 'extended'
            rec.extension_count = (rec.extension_count or 0) + 1
            msg = f'Kept "{rec.title}" for another {policy["extend_months"]} months.'
        elif action == 'keep':
            rec.permanent = True
            rec.status = 'permanent'
            _add_to_exclusion_list(rec, by_name=rec.requester_name, by_email=rec.requester_email)
            msg = f'"{rec.title}" will be kept permanently.'
        else:  # delete
            dry = _is_dry_run()
            # Archive FIRST so a deletion can never escape the audit log.
            _archive_deletion(rec, deleted_by=('requester-dry-run' if dry else 'requester-delete'),
                              dry_run=dry, notes='requester chose delete via token')
            if not _delete_via_arr(rec):
                db.session.rollback()  # releases the token claim AND the archive row
                return jsonify({'success': False, 'error': 'Could not delete from server'}), 502
            rec.status = 'deleted'
            rec.deleted_at = now
            if dry:
                rec.notes = 'dry-run-deletion'
            msg = f'"{rec.title}" has been deleted.'
        db.session.commit()
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== Watchlist Sync routes =====

@app.route('/watchlist-sync')
@login_required
def watchlist_sync_page():
    enabled = get_setting('WATCHLIST_SYNC_ENABLED', 'false').lower() == 'true'
    remove_watched = get_setting('WATCHLIST_SYNC_REMOVE_WATCHED', 'false').lower() == 'true'
    last_run = get_setting('WATCHLIST_SYNC_LAST_RUN', '')
    last_summary = get_setting('WATCHLIST_SYNC_LAST_SUMMARY', '')
    recent = (WatchlistSyncItem.query
              .order_by(WatchlistSyncItem.last_seen_at.desc())
              .limit(200).all())
    return render_template('watchlist_sync.html',
                           enabled=enabled,
                           remove_watched=remove_watched,
                           last_run=last_run,
                           last_summary=last_summary,
                           recent=recent)


@app.route('/api/watchlist-sync/toggle', methods=['POST'])
@login_required
def watchlist_sync_toggle():
    data = request.get_json() or {}
    if 'enabled' in data:
        set_setting('WATCHLIST_SYNC_ENABLED', 'true' if data.get('enabled') else 'false')
    if 'remove_watched' in data:
        set_setting('WATCHLIST_SYNC_REMOVE_WATCHED', 'true' if data.get('remove_watched') else 'false')
    return jsonify({'success': True})


@app.route('/api/watchlist-sync/run-now', methods=['POST'])
@login_required
def watchlist_sync_run_now():
    if not _watchlist_sync_lock.acquire(blocking=False):
        return jsonify({'success': False, 'error': 'Sync already running'}), 429
    def _run():
        try:
            watchlist_sync_job()
        finally:
            _watchlist_sync_lock.release()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': 'Sync started in background'})


@app.route('/api/watchlist-sync/status')
@login_required
def watchlist_sync_status():
    return jsonify({
        'enabled': get_setting('WATCHLIST_SYNC_ENABLED', 'false').lower() == 'true',
        'last_run': get_setting('WATCHLIST_SYNC_LAST_RUN', ''),
        'last_summary': get_setting('WATCHLIST_SYNC_LAST_SUMMARY', ''),
        'running': not _watchlist_sync_lock.acquire(blocking=False) or _watchlist_sync_lock.release() or False,
        'recent': [
            {
                'id': r.id,
                'title': r.title,
                'year': r.year,
                'media_type': r.media_type,
                'status': r.status,
                'status_message': r.status_message,
                'processed_at': r.processed_at.strftime('%Y-%m-%d %H:%M') if r.processed_at else None,
                'first_seen_at': r.first_seen_at.strftime('%Y-%m-%d %H:%M') if r.first_seen_at else None,
            }
            for r in WatchlistSyncItem.query.order_by(WatchlistSyncItem.last_seen_at.desc()).limit(200).all()
        ]
    })


@app.route('/api/watchlist-sync/retry/<int:item_id>', methods=['POST'])
@login_required
def watchlist_sync_retry(item_id):
    row = WatchlistSyncItem.query.get_or_404(item_id)
    status, msg = _sync_watchlist_item(row)
    row.status = status
    row.status_message = msg
    row.processed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'status': status, 'message': msg})


@app.route('/api/watchlist-sync/diagnostic')
@login_required
def watchlist_sync_diagnostic():
    """Compare Plex watchlist against Sonarr/Radarr libraries and return what's missing."""
    plex_token = get_setting('PLEX_TOKEN', '').strip()
    sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
    sonarr_key = get_setting('SONARR_API_KEY', '').strip()
    radarr_url = get_setting('RADARR_URL', '').strip().rstrip('/')
    radarr_key = get_setting('RADARR_API_KEY', '').strip()

    errors = []
    if not plex_token:
        errors.append('Plex token not configured')
    if not sonarr_url or not sonarr_key:
        errors.append('Sonarr not configured')
    if not radarr_url or not radarr_key:
        errors.append('Radarr not configured')
    if errors:
        return jsonify({'success': False, 'errors': errors})

    plex_headers = {
        'X-Plex-Token': plex_token,
        'X-Plex-Client-Identifier': 'media-scrubber-diag',
        'X-Plex-Product': 'Media Scrubber',
        'X-Plex-Version': '1.0',
        'Accept': 'application/json',
    }

    # Fetch full Plex watchlist
    all_wl = []
    try:
        offset = 0
        page_size = 50
        total_size = None
        while True:
            r = requests.get(
                "https://discover.provider.plex.tv/library/sections/watchlist/all",
                params={'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': page_size},
                headers=plex_headers, timeout=20)
            if r.status_code == 401:
                return jsonify({'success': False, 'errors': ['Plex token is invalid or expired']})
            if r.status_code != 200:
                return jsonify({'success': False, 'errors': [f'Plex returned {r.status_code}']})
            container = r.json().get('MediaContainer', {})
            items = container.get('Metadata', [])
            if total_size is None:
                total_size = container.get('totalSize', len(items))
            if not items:
                break
            all_wl.extend(items)
            offset += len(items)
            if offset >= total_size:
                break
    except Exception as e:
        return jsonify({'success': False, 'errors': [f'Plex error: {e}']})

    # Fetch Sonarr library
    sonarr_titles = set()
    try:
        sv = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json()
        for s in sv:
            sonarr_titles.add((s.get('title', '').lower(), s.get('year')))
        sonarr_count = len(sv)
    except Exception as e:
        return jsonify({'success': False, 'errors': [f'Sonarr error: {e}']})

    # Fetch Radarr library
    radarr_titles = set()
    try:
        rv = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json()
        for m in rv:
            radarr_titles.add((m.get('title', '').lower(), m.get('year')))
        radarr_count = len(rv)
    except Exception as e:
        return jsonify({'success': False, 'errors': [f'Radarr error: {e}']})

    missing = []
    in_library = []
    for item in all_wl:
        t = item.get('title', '')
        raw_y = item.get('year')
        try:
            y = int(raw_y) if raw_y else None
        except (ValueError, TypeError):
            y = None
        ptype = item.get('type', '')
        tl = t.lower()

        if ptype == 'show':
            found = (tl, y) in sonarr_titles or any(st == tl for (st, _) in sonarr_titles)
            (in_library if found else missing).append({
                'title': t, 'year': y, 'type': 'show', 'in_library': found
            })
        elif ptype == 'movie':
            found = (tl, y) in radarr_titles or any(mt == tl for (mt, _) in radarr_titles)
            (in_library if found else missing).append({
                'title': t, 'year': y, 'type': 'movie', 'in_library': found
            })

    return jsonify({
        'success': True,
        'watchlist_total': len(all_wl),
        'sonarr_count': sonarr_count,
        'radarr_count': radarr_count,
        'missing': missing,
        'in_library': in_library,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
