import os
import json
import secrets
import smtplib
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
            if age.days < WATCH_HISTORY_CACHE_DAYS:
                return {
                    'history': json.loads(cache.history_json or '{}'),
                    'scanned_at': cache.scanned_at.isoformat(),
                    'age_days': age.days
                }
    except Exception as e:
        print(f"Error loading watch history cache: {e}")
    return None


def clear_watch_history_cache(media_type: str = None):
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
        return redirect(url_for('dashboard'))
    
    return render_template('setup/step6.html',
                           skip_added_days=get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
                           skip_watched_days=get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
                           deletion_delay=get_setting('DELETION_DELAY_SECONDS', '2.0'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_setup_complete():
        return redirect(url_for('setup_step1'))
    
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        
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
    settings = {
        'sonarr_url': get_setting('SONARR_URL'),
        'plex_url': get_setting('PLEX_URL'),
        'ombi_url': get_setting('OMBI_URL'),
        'skip_added_days': get_setting('SKIP_IF_ADDED_WITHIN_DAYS', '90'),
        'skip_watched_days': get_setting('SKIP_IF_WATCHED_WITHIN_DAYS', '180'),
    }
    return render_template('dashboard.html', settings=settings, cleanup_status=cleanup_status)


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
                    new_exclusion = Exclusion(title=show_title)
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
                    new_exclusion = MovieExclusion(title=movie_title, year=year_val)
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
    
    subject = f"Media Update: {media_label}"
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
    
    new_exclusion = MovieExclusion(title=title, year=year, tmdb_id=tmdb_id)
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

Keep it concise and actionable. Use the actual titles from the data. Focus on helping the user make quick decisions."""

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful media library cleanup advisor. Be concise, practical, and use the actual titles provided."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.5
        )
        
        analysis = response.choices[0].message.content
        return jsonify({'analysis': analysis})
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
        new_exclusion = Exclusion(title=title)
        db.session.add(new_exclusion)
        db.session.commit()
        
        email_sent = False
        if requester_email:
            email_sent, _ = send_exclusion_email('tv', title, requester_name, requester_email)
        
        return jsonify({'success': True, 'message': f'Added "{title}" to exclusion list', 'email_sent': email_sent})
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
            
            subject = "Media Library Cleanup - Please Review Your Requested Content"
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


@app.route('/review/<token>')
def requester_review_page(token):
    """Public page for requesters to review and exclude their content."""
    review = RequesterReviewToken.query.filter_by(token=token).first()
    
    if not review:
        return render_template('review_error.html', error="Invalid or expired review link."), 404
    
    if review.expires_at and datetime.utcnow() > review.expires_at:
        return render_template('review_error.html', error="This review link has expired."), 410
    
    items = json.loads(review.items_json or '{}')
    
    existing_tv_exclusions = Exclusion.query.filter_by(excluded_by_email=review.requester_email).all()
    existing_movie_exclusions = MovieExclusion.query.filter_by(excluded_by_email=review.requester_email).all()
    
    return render_template('requester_review.html',
        token=token,
        requester_name=review.requester_name,
        tv_items=items.get('tv', []),
        movie_items=items.get('movies', []),
        existing_tv_exclusions=existing_tv_exclusions,
        existing_movie_exclusions=existing_movie_exclusions,
        is_completed=review.is_used
    )


@app.route('/api/review/<token>/submit', methods=['POST'])
def submit_requester_exclusions(token):
    """Process requester's exclusion selections."""
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
