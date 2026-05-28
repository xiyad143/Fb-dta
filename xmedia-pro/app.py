import os, json, datetime, secrets
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import requests
from openai import OpenAI

app = Flask(__name__)
CORS(app)

# CONFIG
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_urlsafe(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///xmedia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------- MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=True)   # now optional
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(128), nullable=True)
    full_name = db.Column(db.String(150), default='')
    fb_app_id = db.Column(db.String(100))
    fb_app_secret = db.Column(db.String(200))
    fb_long_token = db.Column(db.String(500))
    fb_pages = db.Column(db.Text)
    groq_api_key = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

# ---------- DUMMY USER (since no login, we use a single session) ----------
# We'll create a single user with id=1 if it doesn't exist, used for storing settings.
# This avoids the need for authentication while still persisting data.
# The frontend will interact as this anonymous user.

def get_or_create_default_user():
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
def serve_frontend():
    return send_from_directory('templates', 'index.html')

# ---------- FACEBOOK SETTINGS ----------
@app.route('/api/settings/facebook', methods=['POST'])
def save_fb_settings():
    user = get_or_create_default_user()
    data = request.get_json()
    user.fb_app_id = data['app_id']
    user.fb_app_secret = data['app_secret']
    db.session.commit()
    return jsonify({'message': 'Facebook credentials saved'})

@app.route('/api/settings/facebook', methods=['GET'])
def get_fb_settings():
    user = get_or_create_default_user()
    return jsonify({'app_id': user.fb_app_id or '', 'has_secret': bool(user.fb_app_secret)})

# ---------- GROQ KEY ----------
@app.route('/api/settings/groq', methods=['POST'])
def save_groq_key():
    user = get_or_create_default_user()
    data = request.get_json()
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'API key required'}), 400
    user.groq_api_key = api_key
    db.session.commit()
    return jsonify({'message': 'Groq API key saved'})

# ---------- FACEBOOK CONNECT ----------
@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    user = get_or_create_default_user()
    data = request.get_json()
    short_token = data.get('short_lived_token')
    if not user.fb_app_id or not user.fb_app_secret:
        return jsonify({'error': 'Save your Facebook App credentials first'}), 400
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
    user = get_or_create_default_user()
    if not user.fb_pages:
        return jsonify({'pages': []})
    all_pages = json.loads(user.fb_pages)
    safe = [{"id": p["id"], "name": p["name"], "fan_count": p.get("fan_count", 0)} for p in all_pages]
    return jsonify({'pages': safe})

# ---------- AI GENERATION ----------
@app.route('/api/ai/generate', methods=['POST'])
def ai_generate():
    user = get_or_create_default_user()
    if not user.groq_api_key:
        return jsonify({'error': 'Please save your Groq API key in Settings first.'}), 400

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

# ---------- RUN ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)