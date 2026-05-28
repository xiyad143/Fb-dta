import os, json, datetime, secrets
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_urlsafe(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///xmedia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------- MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    full_name = db.Column(db.String(150), default='')
    fb_app_id = db.Column(db.String(100))
    fb_app_secret = db.Column(db.String(200))
    fb_long_token = db.Column(db.String(500))
    fb_pages = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    token = db.Column(db.String(200), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

# ---------- HELPERS ----------
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = db.session.get(User, payload['user_id'])
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except:
            return jsonify({'error': 'Invalid token'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

def generate_token(user_id):
    return jwt.encode({'user_id': user_id, 'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7)},
                      app.config['SECRET_KEY'], algorithm='HS256')

# ---------- AUTH ROUTES ----------
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    if not data.get('email') or not data.get('password') or not data.get('username'):
        return jsonify({'error': 'Missing fields'}), 400
    if User.query.filter((User.email == data['email']) | (User.username == data['username'])).first():
        return jsonify({'error': 'User already exists'}), 409
    user = User(
        username=data['username'],
        email=data['email'],
        full_name=data.get('full_name', ''),
        password_hash=generate_password_hash(data['password'])
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({'token': generate_token(user.id), 'user': {'id': user.id, 'username': user.username}}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    login_field = data.get('login')
    user = User.query.filter((User.email == login_field) | (User.username == login_field)).first()
    if not user or not check_password_hash(user.password_hash, data.get('password', '')):
        return jsonify({'error': 'Invalid credentials'}), 401
    return jsonify({'token': generate_token(user.id), 'user': {'id': user.id, 'username': user.username}})

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    email = request.get_json().get('email')
    user = User.query.filter_by(email=email).first()
    if user:
        token = secrets.token_urlsafe(32)
        db.session.add(PasswordResetToken(user_id=user.id, token=token, expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=1)))
        db.session.commit()
        print(f"Reset link: http://localhost:5000/api/reset-password/{token}")
    return jsonify({'message': 'If email exists, reset link sent'})

@app.route('/api/reset-password/<token>', methods=['POST'])
def reset_password(token):
    entry = PasswordResetToken.query.filter_by(token=token).first()
    if not entry or entry.expires_at < datetime.datetime.utcnow():
        return jsonify({'error': 'Invalid or expired token'}), 400
    user = db.session.get(User, entry.user_id)
    user.password_hash = generate_password_hash(request.get_json().get('password'))
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'message': 'Password reset successful'})

# ---------- FACEBOOK SETTINGS ----------
@app.route('/api/settings/facebook', methods=['POST'])
@token_required
def save_fb_settings(user):
    data = request.get_json()
    user.fb_app_id = data['app_id']
    user.fb_app_secret = data['app_secret']
    db.session.commit()
    return jsonify({'message': 'Credentials saved'})

@app.route('/api/settings/facebook', methods=['GET'])
@token_required
def get_fb_settings(user):
    return jsonify({'app_id': user.fb_app_id or '', 'has_secret': bool(user.fb_app_secret)})

# ---------- FACEBOOK CONNECT ----------
@app.route('/api/connect-facebook', methods=['POST'])
@token_required
def connect_facebook(user):
    data = request.get_json()
    short_token = data.get('short_lived_token')
    if not user.fb_app_id or not user.fb_app_secret:
        return jsonify({'error': 'Save your Facebook App credentials in Settings first'}), 400
    if not short_token:
        return jsonify({'error': 'Short-lived token required'}), 400

    # Exchange for long-lived token
    resp = requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": user.fb_app_id,
        "client_secret": user.fb_app_secret,
        "fb_exchange_token": short_token
    }).json()
    long_token = resp.get('access_token')
    if not long_token:
        return jsonify({'error': 'Token exchange failed. Check your App ID/Secret.'}), 400

    # Fetch pages
    pages_resp = requests.get("https://graph.facebook.com/v19.0/me/accounts",
                              params={"access_token": long_token}).json()
    pages = pages_resp.get('data', [])
    pages_with_tokens = []
    for p in pages:
        page_token_resp = requests.get(f"https://graph.facebook.com/v19.0/{p['id']}",
                                       params={"fields": "access_token", "access_token": long_token}).json()
        pages_with_tokens.append({
            "id": p["id"],
            "name": p["name"],
            "category": p.get("category", ""),
            "fan_count": p.get("fan_count", 0),
            "access_token": page_token_resp.get("access_token")
        })

    user.fb_long_token = long_token
    user.fb_pages = json.dumps(pages_with_tokens)
    db.session.commit()

    safe_pages = [{"id": p["id"], "name": p["name"], "fan_count": p["fan_count"]} for p in pages_with_tokens]
    return jsonify({"pages": safe_pages})

# ---------- GET PAGES ----------
@app.route('/api/pages', methods=['GET'])
@token_required
def get_pages(user):
    if not user.fb_pages:
        return jsonify({'pages': []})
    all_pages = json.loads(user.fb_pages)
    safe = [{"id": p["id"], "name": p["name"], "fan_count": p.get("fan_count", 0)} for p in all_pages]
    return jsonify({'pages': safe})

# ---------- AI GENERATION (Groq) ----------
@app.route('/api/ai/generate', methods=['POST'])
@token_required
def ai_generate(user):
    data = request.get_json()
    description = data.get('description')
    style = data.get('style', 'viral')
    if not description:
        return jsonify({'error': 'Video description required'}), 400

    groq_key = os.getenv('GROQ_API_KEY')
    if not groq_key:
        return jsonify({'error': 'AI service not configured on server'}), 500

    try:
        client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
        prompt = (
            f"Write a Facebook Reel caption in a {style} style based on: \"{description}\". "
            "Include hook, body, CTA, and 10 trending hashtags. Reply JSON: {\"caption\":\"...\", \"hashtags\":\"...\"}"
        )
        completion = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7,
            max_tokens=500
        )
        text = completion.choices[0].message.content.strip()
        start, end = text.find('{'), text.rfind('}')+1
        result = json.loads(text[start:end]) if start!=-1 and end>0 else {"caption": text, "hashtags": "#viral"}
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'AI generation failed: {str(e)}'}), 500

# ---------- CREATE DB & RUN ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)
