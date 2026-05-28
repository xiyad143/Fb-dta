import os
import json
import datetime
import secrets
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import requests
from openai import OpenAI

app = Flask(__name__)
CORS(app)

# Hardcoded defaults – no .env needed
app.config['SECRET_KEY'] = 'hardcoded-secret-key-for-dev-only-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///xmedia.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------- MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(128), nullable=True)
    full_name = db.Column(db.String(150), default='')
    fb_app_id = db.Column(db.String(100))
    fb_app_secret = db.Column(db.String(200))
    fb_long_token = db.Column(db.String(500))
    fb_pages = db.Column(db.Text)                # JSON list of pages with tokens
    groq_api_key = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class ReelPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    page_id = db.Column(db.String(100), nullable=False)
    video_id = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(255))
    description = db.Column(db.Text)
    status = db.Column(db.String(50), default='created')  # created, uploading, processing, published, scheduled, failed
    scheduled_time = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    fb_post_id = db.Column(db.String(100))

# ---------- DEFAULT USER (always id=1) ----------
def get_user():
    user = User.query.get(1)
    if not user:
        user = User(id=1, username='default', email='default@example.com')
        db.session.add(user)
        db.session.commit()
    return user

# ---------- HELPERS ----------
def get_page_token(user, page_id):
    if not user.fb_pages:
        return None
    pages = json.loads(user.fb_pages)
    for p in pages:
        if p['id'] == page_id:
            return p.get('access_token')
    return None

# ---------- SERVE FRONTEND ----------
@app.route('/')
def serve():
    return send_from_directory('templates', 'index.html')

# ---------- FACEBOOK SETTINGS ----------
@app.route('/api/settings/facebook', methods=['POST'])
def save_fb_settings():
    user = get_user()
    data = request.get_json()
    user.fb_app_id = data['app_id']
    user.fb_app_secret = data['app_secret']
    db.session.commit()
    return jsonify({'message': 'Saved'})

@app.route('/api/settings/facebook', methods=['GET'])
def get_fb_settings():
    user = get_user()
    return jsonify({'app_id': user.fb_app_id or '', 'has_secret': bool(user.fb_app_secret)})

# ---------- GROQ KEY ----------
@app.route('/api/settings/groq', methods=['POST'])
def save_groq_key():
    user = get_user()
    data = request.get_json()
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'API key required'}), 400
    user.groq_api_key = api_key
    db.session.commit()
    return jsonify({'message': 'Groq key saved'})

# ---------- FACEBOOK CONNECT ----------
@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    user = get_user()
    data = request.get_json()
    short_token = data.get('short_lived_token')
    if not user.fb_app_id or not user.fb_app_secret:
        return jsonify({'error': 'Save your App ID/Secret first'}), 400
    if not short_token:
        return jsonify({'error': 'Short-lived token required'}), 400

    resp = requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": user.fb_app_id,
        "client_secret": user.fb_app_secret,
        "fb_exchange_token": short_token
    }).json()
    long_token = resp.get('access_token')
    if not long_token:
        return jsonify({'error': 'Token exchange failed'}), 400

    pages_resp = requests.get("https://graph.facebook.com/v19.0/me/accounts",
                              params={"access_token": long_token}).json()
    pages = pages_resp.get('data', [])
    pages_with_tokens = []
    for p in pages:
        pt_resp = requests.get(f"https://graph.facebook.com/v19.0/{p['id']}",
                               params={"fields": "access_token", "access_token": long_token}).json()
        pages_with_tokens.append({
            "id": p["id"],
            "name": p["name"],
            "category": p.get("category", ""),
            "fan_count": p.get("fan_count", 0),
            "access_token": pt_resp.get("access_token")
        })

    user.fb_long_token = long_token
    user.fb_pages = json.dumps(pages_with_tokens)
    db.session.commit()
    safe_pages = [{"id": p["id"], "name": p["name"], "fan_count": p["fan_count"]} for p in pages_with_tokens]
    return jsonify({"pages": safe_pages})

# ---------- GET PAGES ----------
@app.route('/api/pages', methods=['GET'])
def get_pages():
    user = get_user()
    if not user.fb_pages:
        return jsonify({'pages': []})
    all_pages = json.loads(user.fb_pages)
    safe = [{"id": p["id"], "name": p["name"], "fan_count": p.get("fan_count", 0)} for p in all_pages]
    return jsonify({'pages': safe})

# ---------- AI GENERATION ----------
@app.route('/api/ai/generate', methods=['POST'])
def ai_generate():
    user = get_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Save your Groq API key in Settings first.'}), 400

    data = request.get_json()
    description = data.get('description')
    style = data.get('style', 'viral')
    if not description:
        return jsonify({'error': 'Video description required'}), 400

    try:
        client = OpenAI(api_key=user.groq_api_key, base_url="https://api.groq.com/openai/v1")
        prompt = (
            f"Write a Facebook Reel caption in a {style} style based on: \"{description}\". "
            "Include a hook, body, CTA, and 10 trending hashtags. "
            "Reply ONLY with a valid JSON object: {\"caption\":\"...\", \"hashtags\":\"...\"}"
        )
        completion = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7,
            max_tokens=500
        )
        text = completion.choices[0].message.content
        try:
            start, end = text.find('{'), text.rfind('}')+1
            result = json.loads(text[start:end])
        except:
            result = {"caption": text, "hashtags": "#viral #reels"}
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'AI generation failed: {str(e)}'}), 500

# ---------- REELS ENDPOINTS (ready for future use) ----------
FB_API_VERSION = "v19.0"
FB_GRAPH_URL = f"https://graph.facebook.com/{FB_API_VERSION}"
FB_UPLOAD_URL = f"https://rupload.facebook.com/video-upload/{FB_API_VERSION}"

@app.route('/api/reels/create', methods=['POST'])
def create_reel_session():
    user = get_user()
    data = request.get_json()
    page_id = data.get('page_id')
    if not page_id:
        return jsonify({'error': 'page_id required'}), 400

    page_token = get_page_token(user, page_id)
    if not page_token:
        return jsonify({'error': 'Page not connected'}), 400

    resp = requests.post(f"{FB_GRAPH_URL}/me/video_reels", params={
        "access_token": page_token,
        "upload_phase": "start"
    })
    if resp.status_code != 200:
        return jsonify({'error': 'Session creation failed'}), 500

    result = resp.json()
    video_id = result['video_id']
    # Save a placeholder record
    reel = ReelPost(user_id=user.id, page_id=page_id, video_id=video_id, status='created')
    db.session.add(reel)
    db.session.commit()
    return jsonify({'video_id': video_id, 'upload_url': result.get('upload_url'), 'reel_id': reel.id})

@app.route('/api/reels/upload-local', methods=['POST'])
def upload_local_reel():
    user = get_user()
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400

    file = request.files['file']
    page_id = request.form.get('page_id')
    title = request.form.get('title', '')
    description = request.form.get('description', '')
    schedule_time = request.form.get('scheduled_publish_time')

    if not page_id:
        return jsonify({'error': 'page_id required'}), 400

    page_token = get_page_token(user, page_id)
    if not page_token:
        return jsonify({'error': 'Page token not found'}), 400

    # 1. Create session
    create_resp = requests.post(f"{FB_GRAPH_URL}/me/video_reels", params={
        "access_token": page_token,
        "upload_phase": "start"
    })
    if create_resp.status_code != 200:
        return jsonify({'error': 'Session creation failed'}), 500
    video_id = create_resp.json()['video_id']

    # 2. Upload file
    file_content = file.read()
    file_size = len(file_content)
    headers = {
        "Authorization": f"OAuth {page_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream"
    }
    upload_resp = requests.post(f"{FB_UPLOAD_URL}/{video_id}", data=file_content, headers=headers)
    if upload_resp.status_code != 200:
        ReelPost(user_id=user.id, page_id=page_id, video_id=video_id, status='failed').save()
        return jsonify({'error': 'Upload failed'}), 500

    # 3. Publish or schedule
    publish_params = {
        "access_token": page_token,
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "SCHEDULED" if schedule_time else "PUBLISHED"
    }
    if schedule_time:
        publish_params["scheduled_publish_time"] = schedule_time
    if title:
        publish_params["title"] = title
    if description:
        publish_params["description"] = description

    publish_resp = requests.post(f"{FB_GRAPH_URL}/me/video_reels", params=publish_params)
    if publish_resp.status_code != 200:
        ReelPost(user_id=user.id, page_id=page_id, video_id=video_id, status='failed').save()
        return jsonify({'error': 'Publishing failed'}), 500

    reel = ReelPost(
        user_id=user.id,
        page_id=page_id,
        video_id=video_id,
        title=title,
        description=description,
        status='scheduled' if schedule_time else 'published',
        scheduled_time=datetime.datetime.fromisoformat(schedule_time) if schedule_time else None
    )
    db.session.add(reel)
    db.session.commit()
    return jsonify({'success': True, 'video_id': video_id, 'reel_id': reel.id})

@app.route('/api/reels', methods=['GET'])
def list_reels():
    user = get_user()
    reels = ReelPost.query.filter_by(user_id=user.id).order_by(ReelPost.created_at.desc()).all()
    result = [{
        'id': r.id,
        'page_id': r.page_id,
        'video_id': r.video_id,
        'title': r.title,
        'description': r.description,
        'status': r.status,
        'scheduled_time': r.scheduled_time.isoformat() if r.scheduled_time else None,
        'created_at': r.created_at.isoformat()
    } for r in reels]
    return jsonify(result)

# ---------- RUN ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
