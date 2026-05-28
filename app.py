import os
import json
import requests
import datetime
import time
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

# ---------- APScheduler ----------
class SchedulerConfig:
    SCHEDULER_API_ENABLED = True
app.config.from_object(SchedulerConfig())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# ---------- Models ----------
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
    assigned_page_id = db.Column(db.String(50), nullable=True)

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(50), nullable=False)
    media_id = db.Column(db.Integer, db.ForeignKey('media_file.id'))
    caption = db.Column(db.Text)
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    job_id = db.Column(db.String(100), nullable=True)

# ---------- Helpers ----------
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

def generate_caption(prompt):
    user = get_user()
    if not user.groq_api_key:
        raise Exception("Groq API key not set")
    client = OpenAI(api_key=user.groq_api_key, base_url="https://api.groq.com/openai/v1")
    response = client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=[
            {"role": "system", "content": "You are a social media caption writer. Keep it engaging and concise."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )
    return response.choices[0].message.content

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

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

@app.route('/api/settings', methods=['GET'])
def get_settings():
    user = get_user()
    return jsonify({
        'fb_app_id': user.fb_app_id,
        'fb_app_secret': user.fb_app_secret,
        'groq_api_key': user.groq_api_key,
        'telegram_bot_token': user.telegram_bot_token,
        'telegram_chat_id': user.telegram_chat_id,
        'developer_name': user.developer_name,
        'developer_email': user.developer_email,
        'developer_facebook': user.developer_facebook,
        'developer_whatsapp': user.developer_whatsapp
    })

@app.route('/api/settings', methods=['POST'])
def save_settings():
    user = get_user()
    data = request.json
    if 'fb_app_id' in data:
        user.fb_app_id = data['fb_app_id']
    if 'fb_app_secret' in data:
        user.fb_app_secret = data['fb_app_secret']
    if 'groq_api_key' in data:
        user.groq_api_key = data['groq_api_key']
    if 'telegram_bot_token' in data:
        user.telegram_bot_token = data['telegram_bot_token']
    if 'telegram_chat_id' in data:
        user.telegram_chat_id = data['telegram_chat_id']
    if 'developer_name' in data:
        user.developer_name = data['developer_name']
    if 'developer_email' in data:
        user.developer_email = data['developer_email']
    if 'developer_facebook' in data:
        user.developer_facebook = data['developer_facebook']
    if 'developer_whatsapp' in data:
        user.developer_whatsapp = data['developer_whatsapp']
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    user = get_user()
    data = request.json
    short_token = data.get('short_lived_token')
    if not short_token:
        return jsonify(error='No token provided'), 400
    try:
        long_token = exchange_long_lived_token(short_token)
        user.fb_long_lived_token = long_token
        db.session.commit()
        return jsonify(success=True, long_lived_token=long_token)
    except Exception as e:
        return jsonify(error=str(e)), 400

@app.route('/api/disconnect-facebook', methods=['POST'])
def disconnect_facebook():
    user = get_user()
    user.fb_long_lived_token = None
    user.fb_pages_json = None
    Page.query.delete()
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/pages')
def get_pages():
    user = get_user()
    if not user.fb_long_lived_token:
        return jsonify(error='Not connected to Facebook'), 400
    resp = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={user.fb_long_lived_token}")
    data = resp.json()
    if 'error' in data:
        return jsonify(error=data['error']['message']), 400
    pages = []
    for p in data.get('data', []):
        pages.append({
            'id': p['id'],
            'name': p['name'],
            'access_token': p['access_token'],
            'category': p.get('category', ''),
            'tasks': p.get('tasks', []),
            'fan_count': p.get('fan_count', 0),
            'picture': p.get('picture', {}).get('data', {}).get('url', '')
        })
    return jsonify(pages)

@app.route('/api/refresh-pages', methods=['POST'])
def refresh_pages():
    user = get_user()
    if not user.fb_long_lived_token:
        return jsonify(error='Not connected to Facebook'), 400
    resp = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={user.fb_long_lived_token}")
    data = resp.json()
    if 'error' in data:
        return jsonify(error=data['error']['message']), 400
    Page.query.delete()
    for p in data.get('data', []):
        page = Page(
            id=p['id'],
            name=p['name'],
            access_token=p['access_token'],
            fan_count=p.get('fan_count', 0),
            picture_url=p.get('picture', {}).get('data', {}).get('url', ''),
            tasks=json.dumps(p.get('tasks', []))
        )
        db.session.add(page)
    user.fb_pages_json = json.dumps(data.get('data', []))
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    if 'file' not in request.files:
        return jsonify(error='No file part'), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify(error='No selected file'), 400
    filename = secure_filename(file.filename)
    timestamp = str(int(time.time()))
    saved_name = f"{timestamp}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], saved_name)
    file.save(filepath)
    media = MediaFile(
        filename=saved_name,
        original_name=filename,
        file_size=os.path.getsize(filepath)
    )
    db.session.add(media)
    db.session.commit()
    return jsonify({
        'id': media.id,
        'filename': media.filename,
        'original_name': media.original_name,
        'upload_date': media.upload_date.isoformat(),
        'file_size': media.file_size,
        'assigned_page_id': media.assigned_page_id
    })

@app.route('/api/media')
def list_media():
    media_list = MediaFile.query.order_by(MediaFile.upload_date.desc()).all()
    return jsonify([{
        'id': m.id,
        'filename': m.filename,
        'original_name': m.original_name,
        'upload_date': m.upload_date.isoformat(),
        'file_size': m.file_size,
        'url': f"/uploads/{m.filename}",
        'assigned_page_id': m.assigned_page_id
    } for m in media_list])

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify(error='Not found'), 404
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    db.session.delete(media)
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/media/<int:media_id>/assign', methods=['POST'])
def assign_media_to_page(media_id):
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify(error='Not found'), 404
    data = request.json
    page_id = data.get('page_id')
    media.assigned_page_id = page_id
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/schedule', methods=['POST'])
def schedule_post():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    scheduled_time_str = data.get('scheduled_time')
    if not page_id or not media_id or not scheduled_time_str:
        return jsonify(error='Missing required fields'), 400
    try:
        scheduled_time = datetime.datetime.fromisoformat(scheduled_time_str.replace('Z', '+00:00'))
        if scheduled_time.tzinfo:
            scheduled_time = scheduled_time.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except:
        return jsonify(error='Invalid datetime format'), 400
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify(error='Media not found'), 404
    post = ScheduledPost(
        page_id=page_id,
        media_id=media_id,
        caption=caption,
        scheduled_time=scheduled_time,
        status='pending'
    )
    db.session.add(post)
    db.session.commit()
    schedule_post_job(post)
    return jsonify({'id': post.id, 'status': 'scheduled'})

@app.route('/api/scheduled-posts')
def list_scheduled_posts():
    posts = ScheduledPost.query.order_by(ScheduledPost.scheduled_time.asc()).all()
    result = []
    for p in posts:
        media = db.session.get(MediaFile, p.media_id)
        result.append({
            'id': p.id,
            'page_id': p.page_id,
            'media_id': p.media_id,
            'media_filename': media.original_name if media else None,
            'caption': p.caption,
            'scheduled_time': p.scheduled_time.isoformat(),
            'status': p.status,
            'created_at': p.created_at.isoformat()
        })
    return jsonify(result)

@app.route('/api/scheduled-posts/<int:post_id>', methods=['DELETE'])
def delete_scheduled_post(post_id):
    post = db.session.get(ScheduledPost, post_id)
    if not post:
        return jsonify(error='Not found'), 404
    if post.job_id:
        try:
            scheduler.remove_job(post.job_id)
        except:
            pass
    db.session.delete(post)
    db.session.commit()
    return jsonify(success=True)

@app.route('/api/ai-caption', methods=['POST'])
def ai_caption():
    data = request.json
    prompt = data.get('prompt', '')
    if not prompt:
        return jsonify(error='No prompt provided'), 400
    try:
        caption = generate_caption(prompt)
        return jsonify(caption=caption)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/ai-schedule-suggest', methods=['POST'])
def ai_schedule_suggest():
    now = datetime.datetime.utcnow()
    suggestions = [
        (now + datetime.timedelta(hours=1)).isoformat(),
        (now + datetime.timedelta(hours=2)).isoformat(),
        (now + datetime.timedelta(days=1)).isoformat(),
        (now + datetime.timedelta(days=2)).isoformat()
    ]
    return jsonify(suggestions=suggestions)

@app.route('/api/publish-now', methods=['POST'])
def publish_now():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    if not page_id or not media_id:
        return jsonify(error='Missing page_id or media_id'), 400
    media = db.session.get(MediaFile, media_id)
    if not media:
        return jsonify(error='Media not found'), 404
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
    try:
        video_id = upload_video_to_facebook(page_id, filepath, title=caption, description='', video_state='PUBLISHED')
        send_telegram_notification(f"Video published instantly on page {page_id}: {video_id}")
        return jsonify(success=True, video_id=video_id)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/analytics/<page_id>')
def page_analytics(page_id):
    token = get_page_token(page_id)
    if not token:
        return jsonify(error='Page token not found'), 400
    since = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    until = datetime.datetime.now().strftime('%Y-%m-%d')
    metrics = ['page_follows', 'page_impressions', 'page_engaged_users']
    result = {}
    for metric in metrics:
        url = f"https://graph.facebook.com/v25.0/{page_id}/insights"
        params = {
            'metric': metric,
            'period': 'day',
            'since': since,
            'until': until,
            'access_token': token
        }
        resp = requests.get(url, params=params)
        data = resp.json()
        if 'error' not in data and data.get('data'):
            values = data['data'][0]['values']
            result[metric] = [{'date': v['end_time'][:10], 'value': v['value']} for v in values]
        else:
            result[metric] = []
    return jsonify(result)

@app.route('/api/status')
def status():
    return jsonify(status='running', time=datetime.datetime.utcnow().isoformat())

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Add missing column if needed (for existing DB)
        try:
            db.session.execute('ALTER TABLE media_file ADD COLUMN assigned_page_id VARCHAR(50)')
            db.session.commit()
        except:
            pass
        pending_posts = ScheduledPost.query.filter_by(status='pending').all()
        for post in pending_posts:
            if post.scheduled_time > datetime.datetime.utcnow():
                schedule_post_job(post)
    app.run(debug=True, host='0.0.0.0')
