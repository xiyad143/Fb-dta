import os
import requests
import json
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from werkzeug.utils import secure_filename
import logging
from datetime import datetime
import time

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'xiya-super-secret-key-change-in-production')
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Configuration
UPLOAD_FOLDER = '/tmp/uploads' if not os.path.exists('/tmp') else './uploads'
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

# Simple in-memory analytics (for demo, use Redis/DB in production)
analytics = {
    'api_calls': 0,
    'publish_attempts': 0,
    'total_reels_published': 0,
    'errors': [],
    'last_activity': None
}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def log_api_call(endpoint):
    analytics['api_calls'] += 1
    analytics['last_activity'] = datetime.now().isoformat()

def log_error(error_msg):
    analytics['errors'].append({
        'time': datetime.now().isoformat(),
        'message': error_msg
    })
    if len(analytics['errors']) > 50:
        analytics['errors'] = analytics['errors'][-50:]

# ---------- Proxy Helper ----------
def graph_proxy(endpoint, access_token, method='GET', data=None, files=None):
    """Forward request to Facebook Graph API"""
    url = f"https://graph.facebook.com/{endpoint}"
    params = {'access_token': access_token}
    if method == 'GET':
        response = requests.get(url, params=params)
    elif method == 'POST':
        if files:
            # Multipart form for file upload
            response = requests.post(url, data=data, files=files, params=params)
        else:
            response = requests.post(url, json=data, params=params)
    else:
        return None
    log_api_call(endpoint)
    return response

# ---------- Routes ----------

# Serve the main dashboard HTML (embed or static)
@app.route('/')
def index():
    # You can also serve an external index.html file from 'static' folder
    # For simplicity, redirect to static file if exists, else return built-in HTML
    try:
        return send_from_directory('static', 'index.html')
    except:
        # Fallback: return the complete HTML from previous step (embedded)
        return get_dashboard_html()

# API: Get user profile
@app.route('/api/me', methods=['GET'])
def get_me():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'No access token provided'}), 401
    resp = graph_proxy('me?fields=id,name,picture.width(200).height(200)', token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    return jsonify({'error': 'Failed to fetch profile'}), resp.status_code if resp else 500

# API: Get user's managed pages
@app.route('/api/pages', methods=['GET'])
def get_pages():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    resp = graph_proxy('me/accounts?fields=id,name,picture,followers_count,access_token', token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    return jsonify({'error': 'Failed to fetch pages'}), resp.status_code if resp else 500

# API: Get videos from a specific page (with pagination)
@app.route('/api/pages/<page_id>/videos', methods=['GET'])
def get_page_videos(page_id):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    after = request.args.get('after', '')
    limit = request.args.get('limit', 20)
    fields = 'id,title,description,permalink,created_time,thumbnail_url,views,length,comments.summary(true),reactions.summary(true)'
    endpoint = f"{page_id}/videos?fields={fields}&limit={limit}"
    if after:
        endpoint += f"&after={after}"
    resp = graph_proxy(endpoint, token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    return jsonify({'error': 'Failed to fetch videos'}), resp.status_code if resp else 500

# API: Publish a reel (video upload)
@app.route('/api/publish', methods=['POST'])
def publish_reel():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    page_id = request.form.get('page_id')
    if not page_id:
        return jsonify({'error': 'page_id required'}), 400
    title = request.form.get('title', 'XIYAD Reel')
    description = request.form.get('description', '')
    file = request.files.get('video')
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Valid video file required (mp4/mov/avi/mkv/webm)'}), 400

    # Save file temporarily
    filename = secure_filename(f"{int(time.time())}_{file.filename}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    analytics['publish_attempts'] += 1
    try:
        # Step 1: Initialize upload session (for large files) or direct POST
        # Using direct POST to page/videos endpoint (simpler)
        with open(filepath, 'rb') as f:
            files = {'source': (filename, f, 'video/mp4')}
            data = {
                'title': title,
                'description': description,
                'published': 'true'
            }
            endpoint = f"{page_id}/videos"
            resp = graph_proxy(endpoint, token, method='POST', data=data, files=files)
        os.remove(filepath)
        if resp and resp.status_code == 200:
            analytics['total_reels_published'] += 1
            return jsonify(resp.json())
        else:
            error_msg = resp.json().get('error', {}).get('message', 'Unknown error') if resp else 'No response'
            log_error(f"Publish failed: {error_msg}")
            return jsonify({'error': error_msg}), resp.status_code if resp else 500
    except Exception as e:
        log_error(str(e))
        return jsonify({'error': str(e)}), 500

# API: Get analytics dashboard data
@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    return jsonify(analytics)

# API: Clear analytics (optional)
@app.route('/api/analytics/clear', methods=['POST'])
def clear_analytics():
    analytics['api_calls'] = 0
    analytics['publish_attempts'] = 0
    analytics['total_reels_published'] = 0
    analytics['errors'] = []
    analytics['last_activity'] = None
    return jsonify({'status': 'cleared'})

# Health check
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ---------- Embedded HTML (fallback) ----------
def get_dashboard_html():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>XIYAD Media Pro Backend</title><style>body{font-family:sans-serif;text-align:center;padding:50px;}</style></head>
    <body><h1>XIYAD Facebook Media Pro API</h1><p>Backend is running. Serve your frontend separately or place index.html in /static folder.</p><p>Endpoints: /api/me, /api/pages, /api/pages/&lt;page_id&gt;/videos, /api/publish, /api/analytics</p></body>
    </html>
    """

# ---------- Run Server ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)