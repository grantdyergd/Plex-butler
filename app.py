import os
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import threading
from datetime import datetime

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
    'log': []
}


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
    
    excluded_shows = [e.title for e in Exclusion.query.order_by(Exclusion.title).all()]
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
    'log': []
}


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
        if movie_cleanup_status['running']:
            return jsonify({'error': 'A movie scan is already running'}), 400
        movie_cleanup_status['running'] = True
        movie_cleanup_status['log'] = []
        movie_cleanup_status['candidates'] = []
    
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
            except Exception as e:
                movie_cleanup_status['log'].append(f"[ERROR] {str(e)}")
            finally:
                with movie_cleanup_lock:
                    movie_cleanup_status['running'] = False
    
    thread = threading.Thread(target=run_scan_thread)
    thread.start()
    
    return jsonify({'message': 'Movie scan started'})


@app.route('/api/movies/execute', methods=['POST'])
@login_required
def execute_movies_api():
    global movie_cleanup_status
    
    with movie_cleanup_lock:
        if movie_cleanup_status['running']:
            return jsonify({'error': 'A movie operation is already running'}), 400
        movie_cleanup_status['running'] = True
    
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
    
    existing = MovieExclusion.query.filter(
        db.func.lower(MovieExclusion.title) == title.lower(),
        MovieExclusion.year == year
    ).first()
    
    if existing:
        return jsonify({'success': True, 'message': 'Movie already in exclusion list'})
    
    new_exclusion = MovieExclusion(title=title, year=year, tmdb_id=tmdb_id)
    db.session.add(new_exclusion)
    db.session.commit()
    
    return jsonify({'success': True, 'message': f"Added '{title}' to exclusion list"})


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
    
    if not title:
        return jsonify({'success': False, 'error': 'No title provided'}), 400
    
    existing = Exclusion.query.filter(db.func.lower(Exclusion.title) == title.lower()).first()
    if existing:
        return jsonify({'success': True, 'message': 'Already excluded'})
    
    try:
        new_exclusion = Exclusion(title=title)
        db.session.add(new_exclusion)
        db.session.commit()
        return jsonify({'success': True, 'message': f'Added "{title}" to exclusion list'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cleanup/scan', methods=['POST'])
@login_required
def scan_cleanup_api():
    global cleanup_status
    
    with cleanup_lock:
        if cleanup_status['running']:
            return jsonify({'error': 'A scan is already running'}), 400
        cleanup_status['running'] = True
        cleanup_status['phase'] = 'scanning'
        cleanup_status['log'] = []
        cleanup_status['candidates'] = []
        cleanup_status['skipped'] = []
    
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
        if cleanup_status['running']:
            return jsonify({'error': 'A cleanup operation is already running'}), 400
        cleanup_status['running'] = True
        cleanup_status['phase'] = 'executing'
    
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
