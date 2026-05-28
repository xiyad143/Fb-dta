import os, json, requests, datetime, time, tempfile
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from openai import OpenAI
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'xmedia-pro-secret-change-in-production'
CORS(app)

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'xmedia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db = SQLAlchemy(app)

class SchedulerConfig:
    SCHEDULER_API_ENABLED = True

app.config.from_object(SchedulerConfig())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# --- Models (unchanged) ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_app_id = db.Column(db.String(100), default='')
    fb_app_secret = db.Column(db.String(100), default='')
    fb_long_lived_token = db.Column(db.Text, nullable=True)
    fb_pages_json = db.Column(db.Text, nullable=True)
    groq_api_key = db.Column(db.String(100), nullable=True)
    telegram_bot_token = db.Column(db.String(100), nullable=True)
    telegram_chat_id = db.Column(db.String(100), nullable=True)
    developer_name = db.Column(db.String(100), default='Riyad Mahfuz')
    developer_email = db.Column(db.String(100), default='xiyad404@gmail.com')
    developer_facebook = db.Column(db.String(200), default='Facebook.com/xiyad.rd')
    developer_whatsapp = db.Column(db.String(50), default='+8801331373661')

class Page(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    name = db.Column(db.String(200))
    access_token = db.Column(db.Text)
    fan_count = db.Column(db.Integer, default=0)
    picture_url = db.Column(db.Text)
    tasks = db.Column(db.Text)

class MediaFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    original_name = db.Column(db.String(200))
    upload_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    file_size = db.Column(db.Integer, default=0)

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), nullable=False)
    media_id = db.Column(db.Integer, db.ForeignKey('media_file.id'))
    caption = db.Column(db.Text)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    job_id = db.Column(db.String(100), nullable=True)

# --- Helpers (unchanged) ---
def get_user():
    user = db.session.get(User, 1)
    if not user:
        user = User(id=1)
        db.session.add(user)
        db.session.commit()
    return user

def get_page_token(page_id):
    page = db.session.get(Page, page_id)
    if page:
        return page.access_token
    user = get_user()
    if user.fb_pages_json:
        pages = json.loads(user.fb_pages_json)
        for p in pages:
            if p['id'] == page_id:
                return p.get('access_token')
    return None

def exchange_long_lived_token(short_token):
    user = get_user()
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        'grant_type': 'fb_exchange_token',
        'client_id': user.fb_app_id,
        'client_secret': user.fb_app_secret,
        'fb_exchange_token': short_token
    }
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('access_token')

def upload_video_to_facebook(page_id, filepath, title='', description='', video_state='PUBLISHED', scheduled_time=None):
    token = get_page_token(page_id)
    if not token:
        raise Exception("No page token")
    file_size = os.path.getsize(filepath)
    start_url = "https://graph.facebook.com/v19.0/me/video_reels"
    start_params = {'access_token': token, 'upload_phase': 'start', 'file_size': file_size}
    start_resp = requests.post(start_url, params=start_params)
    start_data = start_resp.json()
    if 'error' in start_data:
        raise Exception(start_data['error']['message'])
    video_id = start_data['video_id']
    upload_url = start_data['upload_url']
    with open(filepath, 'rb') as f:
        upload_resp = requests.post(upload_url, headers={
            'Authorization': f'OAuth {token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream'
        }, data=f)
    if upload_resp.status_code != 200:
        raise Exception(f"Upload failed: {upload_resp.text}")
    finish_params = {
        'access_token': token,
        'upload_phase': 'finish',
        'video_id': video_id,
        'title': title,
        'description': description,
        'video_state': video_state
    }
    if video_state == 'SCHEDULED' and scheduled_time:
        finish_params['scheduled_publish_time'] = scheduled_time
    finish_resp = requests.post("https://graph.facebook.com/v19.0/me/video_reels", params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data:
        raise Exception(finish_data['error']['message'])
    return video_id

def execute_scheduled_post(post_id):
    with scheduler.app.app_context():
        post = db.session.get(ScheduledPost, post_id)
        if not post or post.status != 'pending':
            return
        post.status = 'processing'
        db.session.commit()
        try:
            media = db.session.get(MediaFile, post.media_id)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
            video_id = upload_video_to_facebook(post.page_id, filepath, title=post.caption or '', description='')
            post.status = 'published'
            send_telegram_notification(f"Post published on {post.page_id}: video ID {video_id}")
        except Exception as e:
            post.status = 'failed'
            send_telegram_notification(f"Post failed on {post.page_id}: {str(e)}")
        finally:
            db.session.commit()

def schedule_post_job(post):
    job = scheduler.add_job(
        id=f'post_{post.id}',
        func=execute_scheduled_post,
        args=[post.id],
        trigger='date',
        run_date=post.scheduled_time
    )
    post.job_id = job.id
    db.session.commit()

def send_telegram_notification(message):
    user = get_user()
    if user.telegram_bot_token and user.telegram_chat_id:
        url = f"https://api.telegram.org/bot{user.telegram_bot_token}/sendMessage"
        payload = {'chat_id': user.telegram_chat_id, 'text': message}
        try:
            requests.post(url, json=payload)
        except:
            pass

# ---------- Routes (keep all existing) ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ... (all previous routes for settings, connect, pages, media, schedule, etc.) ...
# Include the new user profile route:
@app.route('/api/user-profile')
def user_profile():
    user = get_user()
    token = user.fb_long_lived_token
    if not token:
        return jsonify(error='Not connected'), 400
    resp = requests.get(f"https://graph.facebook.com/v19.0/me?fields=name,picture&access_token={token}")
    data = resp.json()
    if 'error' in data:
        return jsonify(error=data['error']['message']), 400
    return jsonify(data)

# (The rest of the routes from previous version: /api/settings/..., /api/connect-facebook, /api/pages, etc.)
# I'll omit them for brevity – they remain exactly as in the previous complete app.py.
# Make sure all previous routes are present.

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        pending_posts = ScheduledPost.query.filter_by(status='pending').all()
        for post in pending_posts:
            if post.scheduled_time > datetime.datetime.utcnow():
                schedule_post_job(post)
    app.run(debug=True, host='0.0.0.0')
