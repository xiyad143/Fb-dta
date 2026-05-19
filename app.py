import os, sqlite3, re
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
from cryptography.fernet import Fernet
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# -------------------- App config --------------------
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-me')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set True in production with HTTPS

limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
DATABASE = 'downloads.db'

# -------------------- Security (inline) --------------------
SALT = b'fb-downloader-static-salt'

def _get_fernet():
    master_key = os.environ.get('SESSION_ENCRYPTION_KEY', app.secret_key)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=480000)
    key = base64.urlsafe_b64encode(kdf.derive(master_key.encode()))
    return Fernet(key)

def encrypt_secret(plain_text: str) -> str:
    return _get_fernet().encrypt(plain_text.encode()).decode()

def decrypt_secret(encrypted_text: str) -> str:
    return _get_fernet().decrypt(encrypted_text.encode()).decode()

# -------------------- Database helpers --------------------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT, title TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT, user_ip TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            action TEXT, details TEXT)''')
        db.commit()

# -------------------- Facebook video extraction (inline) --------------------
def is_facebook_url(url: str) -> bool:
    patterns = [
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/reel\/\d+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/watch\/?\?v=\d+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/[\w\.\-]+\/videos\/\d+',
        r'(?:https?:\/\/)?fb\.watch\/[a-zA-Z0-9_-]+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/[\w\.\-]+\/?$'
    ]
    return any(re.search(p, url) for p in patterns)

def extract_video_info(url: str) -> dict:
    if not is_facebook_url(url):
        raise ValueError("Invalid Facebook URL")

    ydl_opts = {
        'quiet': True, 'no_warnings': True, 'extract_flat': False,
        'skip_download': True, 'force_generic_extractor': False, 'noplaylist': False
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if 'entries' in info and info['entries']:
        info = info['entries'][0]
    elif 'entries' in info:
        raise Exception("No public videos found on this page")

    formats = info.get('formats', [])
    hd_url = sd_url = None
    for fmt in formats:
        if fmt.get('acodec') != 'none' and fmt.get('vcodec') != 'none':
            height = fmt.get('height', 0) or 0
            if height >= 720 and not hd_url:
                hd_url = fmt['url']
            elif height > 0 and height < 720 and not sd_url:
                sd_url = fmt['url']
    if not hd_url:
        for fmt in formats:
            if fmt.get('format_id') == 'hd' and fmt.get('url'):
                hd_url = fmt['url']; break
    if not sd_url:
        for fmt in formats:
            if fmt.get('format_id') == 'sd' and fmt.get('url'):
                sd_url = fmt['url']; break

    description = info.get('description') or ''
    hashtags = list(set(re.findall(r'#(\w+)', description)))
    return {
        'id': info.get('id'),
        'title': info.get('title', ''),
        'thumbnail': info.get('thumbnail', ''),
        'duration': info.get('duration', 0),
        'hd_url': hd_url,
        'sd_url': sd_url,
        'caption': description,
        'hashtags': hashtags,
        'upload_date': info.get('upload_date', ''),
        'view_count': info.get('view_count', 0),
        'like_count': info.get('like_count', 0),
    }

# -------------------- Routes --------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not is_facebook_url(url):
        return jsonify({'error': 'Invalid Facebook URL. Only public video/reel/page links are supported.'}), 400
    try:
        result = extract_video_info(url)
        db = get_db()
        db.execute('INSERT INTO downloads (url, title, status, user_ip) VALUES (?,?,?,?)',
                   (url, result.get('title',''), 'success', request.remote_addr))
        db.execute('INSERT INTO logs (action, details) VALUES (?,?)',
                   ('extract', f"Success: {url}"))
        db.commit()
        return jsonify(result)
    except Exception as e:
        db = get_db()
        db.execute('INSERT INTO downloads (url, title, status, user_ip) VALUES (?,?,?,?)',
                   (url, '', f'error: {str(e)}', request.remote_addr))
        db.execute('INSERT INTO logs (action, details) VALUES (?,?)',
                   ('extract_error', f"{url} - {str(e)}"))
        db.commit()
        return jsonify({'error': str(e)}), 500

# Admin authentication
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json()
    if data.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Wrong password'}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin', None)
    session.pop('fb_app_id', None)
    session.pop('fb_secret_enc', None)
    return jsonify({'success': True})

@app.route('/api/admin/check')
def admin_check():
    return jsonify({'admin': session.get('admin', False)})

# Admin data endpoints
@app.route('/api/admin/dashboard')
def admin_dashboard():
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    db = get_db()
    downloads = db.execute('SELECT * FROM downloads ORDER BY timestamp DESC LIMIT 50').fetchall()
    logs = db.execute('SELECT * FROM logs ORDER BY timestamp DESC LIMIT 50').fetchall()
    total = db.execute('SELECT COUNT(*) FROM downloads').fetchone()[0]
    success = db.execute("SELECT COUNT(*) FROM downloads WHERE status='success'").fetchone()[0]
    errors = total - success
    chart_data = db.execute('''SELECT DATE(timestamp) as day, COUNT(*) as count FROM downloads
                               WHERE timestamp >= DATE('now','-7 days') GROUP BY day ORDER BY day''').fetchall()
    return jsonify({
        'total': total, 'success': success, 'errors': errors,
        'downloads': [dict(row) for row in downloads],
        'logs': [dict(row) for row in logs],
        'chart_labels': [row['day'] for row in chart_data],
        'chart_values': [row['count'] for row in chart_data],
        'fb_app_id': session.get('fb_app_id', ''),
        'secret_masked': bool(session.get('fb_secret_enc'))
    })

@app.route('/api/admin/save', methods=['POST'])
def admin_save():
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    app_id = data.get('app_id', '').strip()
    secret = data.get('app_secret', '').strip()
    if not app_id or not secret:
        return jsonify({'error': 'Both App ID and Secret are required'}), 400
    session['fb_app_id'] = app_id
    session['fb_secret_enc'] = encrypt_secret(secret)
    db = get_db()
    db.execute('INSERT INTO logs (action, details) VALUES (?,?)',
               ('config_update', 'Facebook API credentials saved in session'))
    db.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0')