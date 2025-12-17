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
    'last_run': None,
    'last_result': None,
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
    return redirect(url_for('dashboard'))


@app.route('/setup/step1', methods=['GET', 'POST'])
def setup_step1():
    if is_setup_complete():
        return redirect(url_for('login'))
    
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
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        flash('Account created successfully!', 'success')
        return redirect(url_for('setup_step2'))
    
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
        
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings_page'))
    
    settings = {
        'sonarr_url': get_setting('SONARR_URL'),
        'sonarr_api_key': get_setting('SONARR_API_KEY'),
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
    }
    return render_template('settings.html', settings=settings)


@app.route('/exclusions', methods=['GET', 'POST'])
@login_required
def exclusions():
    exclusion_file = 'excluded_shows.txt'
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            show_title = request.form.get('show_title', '').strip()
            if show_title:
                with open(exclusion_file, 'a') as f:
                    f.write(f"{show_title}\n")
                flash(f"Added '{show_title}' to exclusion list", 'success')
        
        elif action == 'remove':
            show_to_remove = request.form.get('show_to_remove', '').strip().lower()
            if show_to_remove:
                lines = []
                if os.path.exists(exclusion_file):
                    with open(exclusion_file, 'r') as f:
                        lines = f.readlines()
                
                new_lines = [line for line in lines if line.strip().lower() != show_to_remove]
                
                with open(exclusion_file, 'w') as f:
                    f.writelines(new_lines)
                
                flash(f"Removed show from exclusion list", 'success')
        
        return redirect(url_for('exclusions'))
    
    excluded_shows = []
    if os.path.exists(exclusion_file):
        with open(exclusion_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    excluded_shows.append(line)
    
    return render_template('exclusions.html', excluded_shows=excluded_shows)


@app.route('/api/run-cleanup', methods=['POST'])
@login_required
def run_cleanup_api():
    global cleanup_status
    
    with cleanup_lock:
        if cleanup_status['running']:
            return jsonify({'error': 'Cleanup is already running'}), 400
        cleanup_status['running'] = True
        cleanup_status['log'] = []
    
    dry_run = request.json.get('dry_run', True)
    
    def run_cleanup_thread():
        global cleanup_status
        try:
            from cleanup_web import run_cleanup_with_settings
            result = run_cleanup_with_settings(
                get_setting, 
                dry_run=dry_run,
                log_callback=lambda msg: cleanup_status['log'].append(msg)
            )
            cleanup_status['last_result'] = result
        except Exception as e:
            cleanup_status['last_result'] = {'error': str(e)}
            cleanup_status['log'].append(f"[ERROR] {str(e)}")
        finally:
            with cleanup_lock:
                cleanup_status['running'] = False
                cleanup_status['last_run'] = datetime.now().isoformat()
    
    thread = threading.Thread(target=run_cleanup_thread)
    thread.start()
    
    return jsonify({'message': 'Cleanup started', 'dry_run': dry_run})


@app.route('/api/cleanup-status')
@login_required
def cleanup_status_api():
    return jsonify(cleanup_status)


with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
