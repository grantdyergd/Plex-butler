import os
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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def is_setup_complete():
    user_exists = User.query.first() is not None
    sonarr_url = Settings.query.filter_by(key='SONARR_URL').first()
    return user_exists and sonarr_url is not None


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
    
    try:
        intent_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """You control Sonarr (TV shows), Radarr (movies), Plex, and Ombi (requests). Return ONLY valid JSON, no markdown fences:
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

IMPORTANT: Understand complex requests. Examples:
- "Delete everything on the watchlist before 2025" → intent: plex_watchlist_remove, filter: {"before_year": 2025}
- "Remove all movies from my watchlist" → intent: plex_watchlist_remove, filter: {"type": "movie"}
- "Delete all ended shows from Sonarr" → intent: delete_show, filter: {"status": "ended"}
- "Remove The Bear from my watchlist" → intent: plex_watchlist_remove, query: "The Bear"
- "Remove Wayward, Together, Alien Earth from my watchlist" → intent: plex_watchlist_remove, query: "Wayward, Together, Alien Earth" (comma-separated list of titles)
- "Delete Severance and The Bear from Sonarr" → intent: delete_show, query: "Severance, The Bear"

When the user lists multiple titles, put them ALL in the query field as a comma-separated list. Do NOT paraphrase or reword titles.
"""},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,
            max_tokens=300
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
            
            all_series = requests.get(f"{sonarr_url}/api/v3/series", params={'apikey': sonarr_key}, timeout=15).json()
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
            
            all_movies = requests.get(f"{radarr_url}/api/v3/movie", params={'apikey': radarr_key}, timeout=15).json()
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
                    search_result_groups = container.get('SearchResult', [])
                    if isinstance(search_result_groups, dict):
                        search_result_groups = [search_result_groups]
                    for group in search_result_groups:
                        for sr in group.get('SearchResult', []):
                            m = sr.get('Metadata', sr)
                            if m.get('title'):
                                all_metadata.append(m)
                
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
                    target_list.append({'title': name, 'year': year, 'rating': round(rating, 1), 'popularity': round(popularity, 1), 'overview': overview, 'air_date': first_air})
                
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
                    target_list.append({'title': title, 'year': year, 'rating': round(rating, 1), 'popularity': round(popularity, 1), 'overview': overview, 'release_date': release_date})
                
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
                
                sections = []
                
                if tmdb_anticipated_shows:
                    sections.append({
                        'label': '🔥 Most Anticipated TV Shows',
                        'addType': 'show',
                        'items': [{'title': s['title'], 'year': s['year'], 'rating': s['rating'], 'date': s.get('air_date', ''), 'overview': s['overview']} for s in tmdb_anticipated_shows[:10]]
                    })
                
                if tmdb_anticipated_movies:
                    sections.append({
                        'label': '🔥 Most Anticipated Movies',
                        'addType': 'movie',
                        'items': [{'title': m['title'], 'year': m['year'], 'rating': m['rating'], 'date': m.get('release_date', ''), 'overview': m['overview']} for m in tmdb_anticipated_movies[:10]]
                    })
                
                if tmdb_trending_shows:
                    sections.append({
                        'label': '📺 Trending TV Right Now',
                        'addType': 'show',
                        'items': [{'title': s['title'], 'year': s['year'], 'rating': s['rating'], 'date': s.get('air_date', ''), 'overview': s['overview']} for s in tmdb_trending_shows[:8]]
                    })
                
                if tmdb_trending_movies:
                    sections.append({
                        'label': '🎬 Trending Movies Right Now',
                        'addType': 'movie',
                        'items': [{'title': m['title'], 'year': m['year'], 'rating': m['rating'], 'date': m.get('release_date', ''), 'overview': m['overview']} for m in tmdb_trending_movies[:8]]
                    })
                
                if tmdb_upcoming_movies:
                    anticipated_titles = {am['title'] for am in tmdb_anticipated_movies}
                    trending_titles = {tm['title'] for tm in tmdb_trending_movies}
                    new_upcoming = [m for m in tmdb_upcoming_movies if m['title'] not in anticipated_titles and m['title'] not in trending_titles]
                    if new_upcoming:
                        sections.append({
                            'label': '🗓️ More Upcoming Movies',
                            'addType': 'movie',
                            'items': [{'title': m['title'], 'year': m['year'], 'rating': m['rating'], 'date': m.get('release_date', ''), 'overview': m['overview']} for m in new_upcoming[:6]]
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
            return jsonify({'reply': reply or raw})
    
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
    
    if media_type == 'show':
        sonarr_url = get_setting('SONARR_URL', '').strip().rstrip('/')
        sonarr_key = get_setting('SONARR_API_KEY', '').strip()
        
        if not sonarr_url or not sonarr_key:
            return jsonify({'success': False, 'error': 'Sonarr not configured'})
        
        try:
            roots = requests.get(f"{sonarr_url}/api/v3/rootfolder", params={'apikey': sonarr_key}, timeout=10).json()
            root_path = roots[0]['path'] if roots else '/tv'
            
            tvdb_id = item.get('tvdbId')
            try:
                tvdb_id = int(tvdb_id) if tvdb_id else 0
            except (ValueError, TypeError):
                tvdb_id = 0
            
            qual_id = profile_id
            try:
                qual_id = int(qual_id) if qual_id else None
            except (ValueError, TypeError):
                qual_id = None
            
            if not qual_id:
                profiles = requests.get(f"{sonarr_url}/api/v3/qualityprofile", params={'apikey': sonarr_key}, timeout=10).json()
                qual_id = profiles[0]['id'] if profiles else 1
            
            payload = {
                'title': item.get('title'),
                'tvdbId': tvdb_id,
                'qualityProfileId': int(qual_id),
                'titleSlug': item.get('titleSlug') or item.get('title', '').lower().replace(' ', '-'),
                'images': item.get('images', []),
                'seasons': item.get('seasons', []),
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
            root_path = roots[0]['path'] if roots else '/movies'
            
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
