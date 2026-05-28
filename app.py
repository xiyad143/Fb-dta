import os
import json
import traceback
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI

app = Flask(__name__)
app.config['SECRET_KEY'] = 'xmediapro-secret-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///xmediapro.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
CORS(app)
db = SQLAlchemy(app)

# ---------- MODELS ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    facebook_app_id = db.Column(db.String(100))
    facebook_app_secret = db.Column(db.String(200))
    groq_api_key = db.Column(db.String(200))
    long_lived_token = db.Column(db.String(500))
    pages_json = db.Column(db.Text)

class ReelPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    page_id = db.Column(db.String(50))
    video_id = db.Column(db.String(100))
    status = db.Column(db.String(20))
    caption = db.Column(db.Text)
    scheduled_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ---------- HELPERS ----------
def get_user():
    user = User.query.get(1)
    if not user:
        user = User(id=1)
        db.session.add(user)
        db.session.commit()
    return user

def get_page_token(page_id):
    user = get_user()
    if not user.pages_json:
        return None
    try:
        pages = json.loads(user.pages_json)
        for p in pages:
            if p['id'] == page_id:
                return p['access_token']
    except:
        return None
    return None

def exchange_short_lived_token(user, short_token):
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": user.facebook_app_id,
        "client_secret": user.facebook_app_secret,
        "fb_exchange_token": short_token
    }
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        raise Exception(f"Token exchange failed: {resp.text}")
    data = resp.json()
    return data.get("access_token")

def fetch_facebook_pages(long_token):
    url = "https://graph.facebook.com/v19.0/me/accounts"
    params = {"access_token": long_token}
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        raise Exception(f"Page fetch failed: {resp.text}")
    data = resp.json()
    pages = []
    for p in data.get("data", []):
        fan_url = f"https://graph.facebook.com/v19.0/{p['id']}?fields=fan_count&access_token={p['access_token']}"
        fan_resp = requests.get(fan_url)
        fan_count = 0
        if fan_resp.status_code == 200:
            fan_count = fan_resp.json().get("fan_count", 0)
        pages.append({
            "id": p["id"],
            "name": p["name"],
            "access_token": p["access_token"],
            "fan_count": fan_count
        })
    return pages

def start_reel_upload(page_id, page_token):
    url = f"https://graph.facebook.com/v19.0/{page_id}/video_reels"
    params = {"upload_phase": "start", "access_token": page_token}
    resp = requests.post(url, params=params)
    if resp.status_code != 200:
        raise Exception(f"Reel start failed: {resp.text}")
    data = resp.json()
    return data["video_id"], data["upload_url"]

def upload_video_to_facebook(upload_url, video_data, content_type="video/mp4"):
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(video_data)),
    }
    resp = requests.post(upload_url, data=video_data, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Video upload failed: {resp.text}")
    return resp.json()

def finish_reel_publish(page_id, page_token, video_id, description="", scheduled_time=None):
    url = f"https://graph.facebook.com/v19.0/{page_id}/video_reels"
    params = {
        "upload_phase": "finish",
        "video_state": "SCHEDULED" if scheduled_time else "PUBLISHED",
        "description": description,
        "access_token": page_token
    }
    if scheduled_time:
        params["scheduled_publish_time"] = scheduled_time
    payload = {"video_id": video_id}
    resp = requests.post(url, params=params, json=payload)
    if resp.status_code != 200:
        raise Exception(f"Reel publish failed: {resp.text}")
    return resp.json()

# ---------- GLOBAL ERROR HANDLER ----------
@app.errorhandler(Exception)
def handle_exception(e):
    # Return JSON for all unhandled errors (even 404, 500)
    response = {
        "error": str(e),
        "type": type(e).__name__,
        "detail": traceback.format_exc()
    }
    return jsonify(response), 500

# ---------- ROUTES ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/settings/facebook', methods=['POST'])
def save_facebook_settings():
    data = request.get_json()
    if not data or 'app_id' not in data or 'app_secret' not in data:
        return jsonify({"error": "app_id and app_secret required"}), 400
    user = get_user()
    user.facebook_app_id = data['app_id']
    user.facebook_app_secret = data['app_secret']
    db.session.commit()
    return jsonify({"message": "Facebook credentials saved"})

@app.route('/api/settings/facebook', methods=['GET'])
def get_facebook_settings():
    user = get_user()
    return jsonify({"app_id": user.facebook_app_id or ""})

@app.route('/api/settings/groq', methods=['POST'])
def save_groq_settings():
    data = request.get_json()
    if not data or 'groq_key' not in data:
        return jsonify({"error": "groq_key required"}), 400
    user = get_user()
    user.groq_api_key = data['groq_key']
    db.session.commit()
    return jsonify({"message": "Groq API key saved"})

@app.route('/api/settings/groq', methods=['GET'])
def get_groq_settings():
    user = get_user()
    return jsonify({"has_key": bool(user.groq_api_key)})

@app.route('/api/connect-facebook', methods=['POST'])
def connect_facebook():
    data = request.get_json()
    short_token = data.get('short_lived_token')
    if not short_token:
        return jsonify({"error": "short_lived_token required"}), 400
    user = get_user()
    if not user.facebook_app_id or not user.facebook_app_secret:
        return jsonify({"error": "Save Facebook App ID and Secret first"}), 400
    try:
        long_token = exchange_short_lived_token(user, short_token)
        pages = fetch_facebook_pages(long_token)
        user.long_lived_token = long_token
        user.pages_json = json.dumps(pages)
        db.session.commit()
        return jsonify({"message": "Connected", "pages_count": len(pages), "pages": pages})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/pages', methods=['GET'])
def get_pages():
    user = get_user()
    if not user.pages_json:
        return jsonify({"pages": []})
    try:
        pages = json.loads(user.pages_json)
        safe_pages = [{"id": p["id"], "name": p["name"], "fan_count": p.get("fan_count", 0)} for p in pages]
        return jsonify({"pages": safe_pages})
    except:
        return jsonify({"pages": []})

@app.route('/api/ai/generate', methods=['POST'])
def generate_caption():
    data = request.get_json()
    description = data.get('description')
    tone = data.get('tone', 'Professional')
    language = data.get('language', 'English')
    if not description:
        return jsonify({"error": "description required"}), 400
    user = get_user()
    if not user.groq_api_key:
        return jsonify({"error": "Please save your Groq API key in Settings"}), 400
    try:
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=user.groq_api_key
        )
        prompt = f"""Generate a social media caption and 10 relevant hashtags for a Facebook Reel.
Description: {description}
Tone: {tone}
Language: {language}
Return only valid JSON with keys "caption" and "hashtags" (hashtags as a space-separated string with #). Example:
{{"caption": "Your caption here", "hashtags": "#tag1 #tag2 #tag3"}}"""

        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=400
        )
        content = response.choices[0].message.content.strip()
        result = json.loads(content)
        return jsonify({"caption": result.get("caption", ""), "hashtags": result.get("hashtags", "")})
    except json.JSONDecodeError:
        return jsonify({"error": "AI returned invalid JSON. Please try again."}), 500
    except Exception as e:
        return jsonify({"error": f"Groq API error: {str(e)}"}), 500

@app.route('/api/reels/upload-local', methods=['POST'])
def upload_local_reel():
    page_id = request.form.get('page_id')
    caption = request.form.get('caption', '')
    scheduled_str = request.form.get('scheduled_publish_time')
    scheduled_time = None
    if scheduled_str:
        try:
            scheduled_time = int(float(scheduled_str))
        except:
            pass
    if not page_id:
        return jsonify({"error": "page_id required"}), 400
    if 'video' not in request.files:
        return jsonify({"error": "video file required"}), 400
    file = request.files['video']
    if file.filename == '':
        return jsonify({"error": "no selected file"}), 400

    page_token = get_page_token(page_id)
    if not page_token:
        return jsonify({"error": "Page not found or not connected"}), 400

    try:
        video_id, upload_url = start_reel_upload(page_id, page_token)
        video_data = file.read()
        upload_video_to_facebook(upload_url, video_data)
        finish_reel_publish(page_id, page_token, video_id, caption, scheduled_time)

        user = get_user()
        reel = ReelPost(
            user_id=user.id,
            page_id=page_id,
            video_id=video_id,
            status="scheduled" if scheduled_time else "published",
            caption=caption,
            scheduled_time=datetime.fromtimestamp(scheduled_time, tz=timezone.utc) if scheduled_time else None
        )
        db.session.add(reel)
        db.session.commit()
        return jsonify({"message": "Reel uploaded and published", "video_id": video_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/reels/upload-hosted', methods=['POST'])
def upload_hosted_reel():
    data = request.get_json()
    page_id = data.get('page_id')
    file_url = data.get('file_url')
    caption = data.get('caption', '')
    scheduled_time = data.get('scheduled_publish_time')
    if not page_id or not file_url:
        return jsonify({"error": "page_id and file_url required"}), 400
    page_token = get_page_token(page_id)
    if not page_token:
        return jsonify({"error": "Page not found or not connected"}), 400
    try:
        resp = requests.get(file_url, stream=True, timeout=30)
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to download video: HTTP {resp.status_code}"}), 400
        video_data = resp.content
        video_id, upload_url = start_reel_upload(page_id, page_token)
        upload_video_to_facebook(upload_url, video_data)
        finish_reel_publish(page_id, page_token, video_id, caption, scheduled_time)

        user = get_user()
        reel = ReelPost(
            user_id=user.id,
            page_id=page_id,
            video_id=video_id,
            status="scheduled" if scheduled_time else "published",
            caption=caption,
            scheduled_time=datetime.fromtimestamp(scheduled_time, tz=timezone.utc) if scheduled_time else None
        )
        db.session.add(reel)
        db.session.commit()
        return jsonify({"message": "Reel uploaded from URL and published", "video_id": video_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/reels', methods=['GET'])
def list_reels():
    user = get_user()
    reels = ReelPost.query.filter_by(user_id=user.id).order_by(ReelPost.created_at.desc()).all()
    result = []
    for r in reels:
        result.append({
            "id": r.id,
            "page_id": r.page_id,
            "video_id": r.video_id,
            "status": r.status,
            "caption": r.caption,
            "scheduled_time": r.scheduled_time.isoformat() if r.scheduled_time else None,
            "created_at": r.created_at.isoformat()
        })
    return jsonify({"reels": result})

@app.route('/api/reels/publish/<video_id>', methods=['POST'])
def publish_reel(video_id):
    reel = ReelPost.query.filter_by(video_id=video_id).first()
    if not reel:
        return jsonify({"error": "Reel not found"}), 404
    page_token = get_page_token(reel.page_id)
    if not page_token:
        return jsonify({"error": "Page token not available"}), 400
    try:
        finish_reel_publish(reel.page_id, page_token, video_id, reel.caption or "")
        reel.status = "published"
        db.session.commit()
        return jsonify({"message": "Reel published successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analytics/<page_id>', methods=['GET'])
def get_analytics(page_id):
    page_token = get_page_token(page_id)
    if not page_token:
        return jsonify({"error": "Page not connected"}), 400
    try:
        metrics = "page_impressions_unique,page_engaged_users,page_fans,page_video_views"
        url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
        params = {"metric": metrics, "period": "week", "access_token": page_token}
        resp = requests.get(url, params=params)
        data = resp.json()
        reach = 0; engagement = 0; followers = 0; video_views = 0; daily_reach = []
        if resp.status_code == 200 and 'data' in data:
            for metric in data['data']:
                name = metric.get('name')
                values = metric.get('values', [])
                if values:
                    val = values[-1].get('value', 0)
                    if name == 'page_impressions_unique': reach = val
                    elif name == 'page_engaged_users': engagement = val
                    elif name == 'page_fans': followers = val
                    elif name == 'page_video_views': video_views = val
            if 'page_impressions_unique' in data['data']:
                day_values = data['data'][0].get('values', [])
                daily_reach = [{"day": v.get('end_time', ''), "value": v.get('value', 0)} for v in day_values]
        return jsonify({
            "reach": reach,
            "engagement": engagement,
            "followers": followers,
            "video_views": video_views,
            "daily_reach": daily_reach[-7:] if daily_reach else []
        })
    except Exception as e:
        return jsonify({"error": str(e), "reach":0,"engagement":0,"followers":0,"video_views":0,"daily_reach":[]})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
