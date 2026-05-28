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

# Scheduler config
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
    timezone = db.Column(db.String(50), default='UTC')
    auto_upload = db.Column(db.Boolean, default=False)

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
    folder = db.Column(db.String(100), default='My Videos')

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
    if page: return page.access_token
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
    if 'error' in data: raise Exception(data['error']['message'])
    return data.get('access_token')

def upload_to_facebook(page_id, filepath, title='', description='', video_state='PUBLISHED', scheduled_time=None):
    token = get_page_token(page_id)
    if not token: raise Exception('No page token')
    file_size = os.path.getsize(filepath)
    # start upload
    start_params = {'access_token': token, 'upload_phase': 'start', 'file_size': file_size}
    start_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=start_params)
    start_data = start_resp.json()
    if 'error' in start_data: raise Exception(start_data['error']['message'])
    video_id = start_data['video_id']
    upload_url = start_data['upload_url']
    # upload binary
    with open(filepath, 'rb') as f:
        upload_resp = requests.post(upload_url, headers={
            'Authorization': f'OAuth {token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream'
        }, data=f)
    if upload_resp.status_code != 200: raise Exception(f'Upload failed: {upload_resp.text}')
    # finish
    finish_params = {
        'access_token': token, 'upload_phase': 'finish',
        'video_id': video_id, 'title': title, 'description': description,
        'video_state': video_state
    }
    if video_state == 'SCHEDULED' and scheduled_time:
        finish_params['scheduled_publish_time'] = scheduled_time
    finish_resp = requests.post('https://graph.facebook.com/v19.0/me/video_reels', params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data: raise Exception(finish_data['error']['message'])
    return video_id

def execute_scheduled_post(post_id):
    with scheduler.app.app_context():
        post = db.session.get(ScheduledPost, post_id)
        if not post or post.status != 'pending': return
        post.status = 'processing'
        db.session.commit()
        try:
            media = db.session.get(MediaFile, post.media_id)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
            upload_to_facebook(post.page_id, filepath, title=post.caption or '', description='')
            post.status = 'published'
        except Exception as e:
            post.status = 'failed'
        finally:
            db.session.commit()

def schedule_job(post):
    job = scheduler.add_job(
        id=f'post_{post.id}',
        func=execute_scheduled_post,
        args=[post.id],
        trigger='date',
        run_date=post.scheduled_time
    )
    post.job_id = job.id
    db.session.commit()

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Settings
@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    user = get_user()
    if request.method == 'GET':
        return jsonify({
            'fb_app_id': user.fb_app_id,
            'fb_app_secret': user.fb_app_secret,
            'groq_api_key': user.groq_api_key,
            'telegram_bot_token': user.telegram_bot_token,
            'telegram_chat_id': user.telegram_chat_id,
            'timezone': user.timezone,
            'auto_upload': user.auto_upload
        })
    else:
        data = request.json
        user.fb_app_id = data.get('fb_app_id', user.fb_app_id)
        user.fb_app_secret = data.get('fb_app_secret', user.fb_app_secret)
        user.groq_api_key = data.get('groq_api_key', user.groq_api_key)
        user.telegram_bot_token = data.get('telegram_bot_token', user.telegram_bot_token)
        user.telegram_chat_id = data.get('telegram_chat_id', user.telegram_chat_id)
        user.timezone = data.get('timezone', user.timezone)
        user.auto_upload = data.get('auto_upload', user.auto_upload)
        db.session.commit()
        return jsonify(success=True)

# Facebook Connect / Disconnect
@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    data = request.json
    short_token = data.get('short_lived_token')
    if not short_token: return jsonify(error='Missing token'), 400
    try:
        long_token = exchange_long_lived_token(short_token)
        user = get_user()
        user.fb_long_lived_token = long_token
        # fetch pages
        pages_url = "https://graph.facebook.com/v19.0/me/accounts"
        params = {'access_token': long_token, 'fields': 'id,name,access_token,fan_count,picture,tasks'}
        resp = requests.get(pages_url, params=params)
        pages_data = resp.json()
        if 'error' in pages_data: return jsonify(error=pages_data['error']['message']), 400
        # clear old pages
        Page.query.delete()
        for page in pages_data.get('data', []):
            p = Page(
                id=page['id'], name=page['name'],
                access_token=page['access_token'],
                fan_count=page.get('fan_count', 0),
                picture_url=page.get('picture', {}).get('data', {}).get('url', ''),
                tasks=json.dumps(page.get('tasks', []))
            )
            db.session.add(p)
        db.session.commit()
        return jsonify(success=True, pages=pages_data['data'])
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/disconnect-facebook', methods=['POST'])
def disconnect_facebook():
    user = get_user()
    user.fb_long_lived_token = None
    user.fb_pages_json = None
    Page.query.delete()
    db.session.commit()
    return jsonify(success=True)

# User profile
@app.route('/api/user-profile')
def user_profile():
    user = get_user()
    token = user.fb_long_lived_token
    if not token: return jsonify(error='Not connected'), 400
    resp = requests.get(f"https://graph.facebook.com/v19.0/me?fields=name,picture&access_token={token}")
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    return jsonify(data)

# Pages list
@app.route('/api/pages')
def get_pages():
    pages = Page.query.all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'fan_count': p.fan_count,
        'picture_url': p.picture_url
    } for p in pages])

# Media Library
@app.route('/api/media', methods=['GET'])
def get_media():
    media = MediaFile.query.order_by(MediaFile.upload_date.desc()).all()
    return jsonify([{
        'id': m.id, 'filename': m.filename, 'original_name': m.original_name,
        'upload_date': m.upload_date.isoformat(), 'file_size': m.file_size,
        'folder': m.folder, 'url': f'/uploads/{m.filename}'
    } for m in media])

@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    file = request.files.get('file')
    if not file: return jsonify(error='No file'), 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    folder = request.form.get('folder', 'My Videos')
    media = MediaFile(
        filename=filename, original_name=file.filename,
        file_size=os.path.getsize(filepath), folder=folder
    )
    db.session.add(media)
    db.session.commit()
    return jsonify(success=True, id=media.id)

@app.route('/api/media/<int:media_id>', methods=['DELETE'])
def delete_media(media_id):
    media = db.session.get(MediaFile, media_id)
    if media:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
        if os.path.exists(filepath): os.remove(filepath)
        db.session.delete(media)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(error='Not found'), 404

@app.route('/api/media/<int:media_id>/assign', methods=['POST'])
def assign_media(media_id):
    data = request.json
    media = db.session.get(MediaFile, media_id)
    if not media: return jsonify(error='Not found'), 404
    media.folder = data.get('page_id', 'My Videos')
    db.session.commit()
    return jsonify(success=True)

# Scheduling
@app.route('/api/schedule', methods=['POST'])
def schedule_post():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    scheduled_time_str = data.get('scheduled_time')
    if not page_id or not media_id or not scheduled_time_str: return jsonify(error='Missing fields'), 400
    try:
        scheduled_time = datetime.datetime.fromisoformat(scheduled_time_str)
    except:
        scheduled_time = datetime.datetime.strptime(scheduled_time_str, '%Y-%m-%dT%H:%M')
    post = ScheduledPost(
        page_id=page_id, media_id=media_id, caption=caption,
        scheduled_time=scheduled_time, status='pending'
    )
    db.session.add(post)
    db.session.commit()
    schedule_job(post)
    return jsonify(success=True, id=post.id)

@app.route('/api/scheduled-posts', methods=['GET'])
def get_scheduled():
    posts = ScheduledPost.query.order_by(ScheduledPost.scheduled_time).all()
    return jsonify([{
        'id': p.id, 'page_id': p.page_id, 'media_id': p.media_id,
        'media_filename': MediaFile.query.get(p.media_id).original_name if p.media_id else '',
        'caption': p.caption, 'scheduled_time': p.scheduled_time.isoformat(),
        'status': p.status, 'created_at': p.created_at.isoformat()
    } for p in posts])

@app.route('/api/scheduled-posts/<int:post_id>', methods=['DELETE'])
def delete_scheduled(post_id):
    post = db.session.get(ScheduledPost, post_id)
    if post and post.status == 'pending':
        if post.job_id:
            try: scheduler.remove_job(post.job_id)
            except: pass
        db.session.delete(post)
        db.session.commit()
        return jsonify(success=True)
    return jsonify(error='Cannot delete'), 400

# Instant Publish
@app.route('/api/publish-now', methods=['POST'])
def publish_now():
    data = request.json
    page_id = data.get('page_id')
    media_id = data.get('media_id')
    caption = data.get('caption', '')
    if not page_id or not media_id: return jsonify(error='Missing fields'), 400
    media = db.session.get(MediaFile, media_id)
    if not media: return jsonify(error='Media not found'), 404
    try:
        video_id = upload_to_facebook(
            page_id, os.path.join(app.config['UPLOAD_FOLDER'], media.filename),
            title=caption, description='', video_state='PUBLISHED'
        )
        post = ScheduledPost(
            page_id=page_id, media_id=media_id, caption=caption,
            scheduled_time=datetime.datetime.utcnow(), status='published'
        )
        db.session.add(post)
        db.session.commit()
        return jsonify(success=True, video_id=video_id)
    except Exception as e:
        return jsonify(error=str(e)), 500

# Analytics
@app.route('/api/analytics/<page_id>')
def analytics(page_id):
    token = get_page_token(page_id)
    if not token: return jsonify(error='No token'), 400
    metrics = 'page_fans,page_impressions,page_engaged_users'
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    params = {'metric': metrics, 'access_token': token}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data: return jsonify(error=data['error']['message']), 400
    result = {}
    for metric in data.get('data', []):
        name = metric['name']
        values = metric['values']
        result[name] = [{'date': v.get('end_time',''), 'value': v.get('value',0)} for v in values]
    return jsonify(result)

# AI Caption (Groq)
@app.route('/api/ai-caption', methods=['POST'])
def ai_caption():
    user = get_user()
    if not user.groq_api_key: return jsonify(error='Groq API key not set'), 400
    data = request.json
    prompt = data.get('prompt', '')
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":f"Generate a social media caption and 5 hashtags for: {prompt}"}],
            max_tokens=200
        )
        caption = response.choices[0].message.content.strip()
        return jsonify(caption=caption)
    except Exception as e:
        return jsonify(error=str(e)), 500

# Run
with app.app_context():
    db.create_all()
    # Reschedule pending posts after restart
    pending = ScheduledPost.query.filter_by(status='pending').all()
    for post in pending:
        if post.scheduled_time > datetime.datetime.utcnow():
            schedule_job(post)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
