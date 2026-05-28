import os
import json
import requests
import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

app = Flask(__name__)
app.secret_key = 'xmedia-pro-secret-key-change-in-production'
CORS(app)

# Database setup – SQLite for development, PostgreSQL in production
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'xmedia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ----------------------------
# Database Models
# ----------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_app_id = db.Column(db.String(100), nullable=False, default='')
    fb_app_secret = db.Column(db.String(100), nullable=False, default='')
    fb_long_lived_token = db.Column(db.Text, nullable=True)
    fb_pages_json = db.Column(db.Text, nullable=True)   # JSON of page data
    groq_api_key = db.Column(db.String(100), nullable=True)

class ReelPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.String(100), unique=True, nullable=False)
    page_id = db.Column(db.String(100), nullable=False)
    page_name = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='uploading')
    scheduled_time = db.Column(db.String(30), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

# ----------------------------
# Helpers
# ----------------------------
def get_user():
    """Return the single default user (id=1) – create if not exists."""
    user = db.session.get(User, 1)
    if not user:
        user = User(id=1, fb_app_id='', fb_app_secret='')
        db.session.add(user)
        db.session.commit()
    return user

def get_page_token(page_id):
    """Get page access token from stored JSON."""
    user = get_user()
    if not user.fb_pages_json:
        return None
    pages = json.loads(user.fb_pages_json)
    for page in pages:
        if page['id'] == page_id:
            return page.get('access_token')
    return None

def exchange_long_lived_token(short_token):
    """Exchange a short‑lived token for a long‑lived one using the saved App credentials."""
    user = get_user()
    if not user.fb_app_id or not user.fb_app_secret:
        raise Exception("Facebook App credentials not configured")
    url = f"https://graph.facebook.com/v19.0/oauth/access_token"
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

def fetch_facebook_pages(token):
    """Get the list of managed pages with their access tokens."""
    url = "https://graph.facebook.com/v19.0/me/accounts"
    params = {'access_token': token, 'fields': 'id,name,fan_count,access_token,picture'}
    resp = requests.get(url, params=params)
    data = resp.json()
    if 'error' in data:
        raise Exception(data['error']['message'])
    return data.get('data', [])

# ----------------------------
# Routes
# ----------------------------
@app.route('/')
def index():
    return render_template('index.html')

# Save / retrieve Facebook settings
@app.route('/api/settings/facebook', methods=['POST'])
def save_facebook_settings():
    data = request.json
    user = get_user()
    user.fb_app_id = data.get('app_id', '')
    user.fb_app_secret = data.get('app_secret', '')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/settings/facebook', methods=['GET'])
def get_facebook_settings():
    user = get_user()
    return jsonify({'app_id': user.fb_app_id, 'app_secret': ''})

# Save Groq key
@app.route('/api/settings/groq', methods=['POST'])
def save_groq_key():
    data = request.json
    user = get_user()
    user.groq_api_key = data.get('api_key', '')
    db.session.commit()
    return jsonify({'success': True})

# Connect Facebook – receive short‑lived token from frontend JS SDK
@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    data = request.json
    short_token = data.get('short_lived_token')
    if not short_token:
        return jsonify({'error': 'Missing token'}), 400
    try:
        long_token = exchange_long_lived_token(short_token)
        pages = fetch_facebook_pages(long_token)
        user = get_user()
        user.fb_long_lived_token = long_token
        user.fb_pages_json = json.dumps(pages)
        db.session.commit()
        return jsonify({'success': True, 'pages': pages})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# List pages
@app.route('/api/pages')
def get_pages():
    user = get_user()
    if not user.fb_pages_json:
        return jsonify([])
    pages = json.loads(user.fb_pages_json)
    return jsonify(pages)

# AI Generation (Groq)
@app.route('/api/ai/generate', methods=['POST'])
def ai_generate():
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Groq API key not set'}), 400
    data = request.json
    prompt = f"""You are a social media expert. Generate a creative caption and 5 relevant hashtags for a Facebook Reel with this description: "{data.get('description', '')}". Output ONLY valid JSON: {{"caption": "...", "hashtags": ["#tag1", ...]}}"""
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=user.groq_api_key)
    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300
        )
        content = response.choices[0].message.content.strip()
        result = json.loads(content)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Reels endpoints
@app.route('/api/reels/create', methods=['POST'])
def create_reel_session():
    user = get_user()
    page_id = request.json.get('page_id')
    if not page_id:
        return jsonify({'error': 'page_id required'}), 400
    token = get_page_token(page_id)
    if not token:
        return jsonify({'error': 'Page not found or token missing'}), 400

    start_url = "https://graph.facebook.com/v19.0/me/video_reels"
    params = {
        'access_token': token,
        'upload_phase': 'start',
        'file_size': request.json.get('file_size', 0)
    }
    resp = requests.post(start_url, params=params)
    data = resp.json()
    if 'error' in data:
        return jsonify({'error': data['error']['message']}), 400
    return jsonify(data)

@app.route('/api/reels/upload-local', methods=['POST'])
def upload_local_reel():
    user = get_user()
    page_id = request.form.get('page_id')
    token = get_page_token(page_id)
    if not token:
        return jsonify({'error': 'Invalid page'}), 400

    file = request.files.get('video_file')
    if not file:
        return jsonify({'error': 'No video file'}), 400

    # Save temp file
    filepath = os.path.join('/tmp', file.filename)
    file.save(filepath)
    file_size = os.path.getsize(filepath)

    # Start upload session
    start_url = "https://graph.facebook.com/v19.0/me/video_reels"
    init_params = {
        'access_token': token,
        'upload_phase': 'start',
        'file_size': file_size
    }
    init_resp = requests.post(start_url, params=init_params)
    init_data = init_resp.json()
    if 'error' in init_data:
        os.remove(filepath)
        return jsonify({'error': init_data['error']['message']}), 400

    video_id = init_data['video_id']
    upload_url = init_data['upload_url']

    # Upload file
    with open(filepath, 'rb') as f:
        upload_resp = requests.post(upload_url, headers={
            'Authorization': f'OAuth {token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream',
        }, data=f)
    os.remove(filepath)
    if upload_resp.status_code != 200:
        return jsonify({'error': f'Upload failed: {upload_resp.text}'}), 500

    # Publish (or schedule)
    title = request.form.get('title', '')
    description = request.form.get('description', '')
    video_state = request.form.get('video_state', 'PUBLISHED')
    schedule_time = request.form.get('scheduled_publish_time', None)

    finish_params = {
        'access_token': token,
        'upload_phase': 'finish',
        'video_id': video_id,
        'title': title,
        'description': description,
        'video_state': video_state
    }
    if video_state == 'SCHEDULED' and schedule_time:
        finish_params['scheduled_publish_time'] = schedule_time

    finish_resp = requests.post("https://graph.facebook.com/v19.0/me/video_reels", params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data:
        return jsonify({'error': finish_data['error']['message']}), 500

    # Save to database
    new_reel = ReelPost(
        video_id=video_id,
        page_id=page_id,
        page_name=request.form.get('page_name', ''),
        status=video_state,
        scheduled_time=schedule_time
    )
    db.session.add(new_reel)
    db.session.commit()

    return jsonify({'success': True, 'video_id': video_id})

@app.route('/api/reels/upload-hosted', methods=['POST'])
def upload_hosted_reel():
    # Similar to local upload, but download video from URL first
    user = get_user()
    page_id = request.json.get('page_id')
    video_url = request.json.get('video_url')
    token = get_page_token(page_id)
    if not token or not video_url:
        return jsonify({'error': 'Invalid parameters'}), 400

    # Download video to temp
    import tempfile
    resp = requests.get(video_url, stream=True)
    if resp.status_code != 200:
        return jsonify({'error': 'Failed to download video'}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    for chunk in resp.iter_content(8192):
        if chunk:
            tmp.write(chunk)
    tmp.close()
    file_size = os.path.getsize(tmp.name)

    # Start upload session
    start_url = "https://graph.facebook.com/v19.0/me/video_reels"
    init_params = {
        'access_token': token,
        'upload_phase': 'start',
        'file_size': file_size
    }
    init_resp = requests.post(start_url, params=init_params)
    init_data = init_resp.json()
    if 'error' in init_data:
        os.remove(tmp.name)
        return jsonify({'error': init_data['error']['message']}), 400

    video_id = init_data['video_id']
    upload_url = init_data['upload_url']

    # Upload
    with open(tmp.name, 'rb') as f:
        upload_resp = requests.post(upload_url, headers={
            'Authorization': f'OAuth {token}',
            'offset': '0',
            'file_size': str(file_size),
            'Content-Type': 'application/octet-stream',
        }, data=f)
    os.remove(tmp.name)
    if upload_resp.status_code != 200:
        return jsonify({'error': f'Upload failed: {upload_resp.text}'}), 500

    # Publish
    finish_params = {
        'access_token': token,
        'upload_phase': 'finish',
        'video_id': video_id,
        'title': request.json.get('title', ''),
        'description': request.json.get('description', ''),
        'video_state': request.json.get('video_state', 'PUBLISHED')
    }
    schedule_time = request.json.get('scheduled_publish_time')
    if schedule_time and finish_params['video_state'] == 'SCHEDULED':
        finish_params['scheduled_publish_time'] = schedule_time
    finish_resp = requests.post("https://graph.facebook.com/v19.0/me/video_reels", params=finish_params)
    finish_data = finish_resp.json()
    if 'error' in finish_data:
        return jsonify({'error': finish_data['error']['message']}), 500

    # Save record
    new_reel = ReelPost(
        video_id=video_id,
        page_id=page_id,
        page_name=request.json.get('page_name', ''),
        status=request.json.get('video_state', 'PUBLISHED'),
        scheduled_time=schedule_time
    )
    db.session.add(new_reel)
    db.session.commit()
    return jsonify({'success': True, 'video_id': video_id})

@app.route('/api/reels/status/<video_id>')
def reel_status(video_id):
    reel = ReelPost.query.filter_by(video_id=video_id).first()
    if not reel:
        return jsonify({'status': 'not_found'})
    return jsonify({'status': reel.status})

@app.route('/api/reels')
def list_reels():
    reels = ReelPost.query.order_by(ReelPost.created_at.desc()).all()
    return jsonify([{
        'video_id': r.video_id,
        'page_name': r.page_name,
        'status': r.status,
        'scheduled_time': r.scheduled_time,
        'created_at': r.created_at.isoformat() if r.created_at else None
    } for r in reels])

# ----------------------------
# Database creation
# ----------------------------
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
