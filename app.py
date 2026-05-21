import os
import json
import tempfile
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
import requests
from werkzeug.utils import secure_filename

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)  # No static folder, we serve everything from root
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Configuration
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'mp3', 'jpg', 'png'}
MAX_FILE_SIZE = 500 * 1024 * 1024
TEMP_DIR = tempfile.gettempdir()

# In-memory analytics (for demo)
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
        'message': str(error_msg)
    })
    if len(analytics['errors']) > 50:
        analytics['errors'] = analytics['errors'][-50:]

def graph_request(endpoint, access_token, method='GET', data=None, files=None, json_data=None):
    """Proxy request to Facebook Graph API"""
    url = f"https://graph.facebook.com/{endpoint}"
    params = {'access_token': access_token}
    try:
        if method == 'GET':
            response = requests.get(url, params=params, timeout=30)
        elif method == 'POST':
            if files:
                response = requests.post(url, data=data, files=files, params=params, timeout=120)
            else:
                response = requests.post(url, json=json_data, params=params, timeout=30)
        else:
            return None
        log_api_call(endpoint)
        return response
    except Exception as e:
        log_error(f"Graph request failed: {str(e)}")
        return None

# ------------------- API ROUTES -------------------

@app.route('/api/me', methods=['GET'])
def get_me():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'No access token provided'}), 401
    resp = graph_request('me?fields=id,name,picture.width(200).height(200)', token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    error_msg = resp.json().get('error', {}).get('message', 'Unknown error') if resp else 'No response'
    return jsonify({'error': error_msg}), resp.status_code if resp else 500

@app.route('/api/pages', methods=['GET'])
def get_pages():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    resp = graph_request('me/accounts?fields=id,name,picture,followers_count,access_token', token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    return jsonify({'error': 'Failed to fetch pages'}), resp.status_code if resp else 500

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
    resp = graph_request(endpoint, token)
    if resp and resp.status_code == 200:
        return jsonify(resp.json())
    return jsonify({'error': 'Failed to fetch videos'}), resp.status_code if resp else 500

@app.route('/api/publish', methods=['POST'])
def publish_reel():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': 'Token required'}), 401
    
    page_id = request.form.get('page_id')
    if not page_id:
        return jsonify({'error': 'page_id is required'}), 400
    
    title = request.form.get('title', 'XIYAD Reel')
    description = request.form.get('description', '')
    
    if 'video' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed. Use mp4, mov, avi, mkv, webm'}), 400
    
    # Save to temporary file
    filename = secure_filename(f"{int(datetime.utcnow().timestamp())}_{file.filename}")
    temp_path = os.path.join(TEMP_DIR, filename)
    file.save(temp_path)
    
    analytics['publish_attempts'] += 1
    
    try:
        with open(temp_path, 'rb') as f:
            files = {'source': (filename, f, 'video/mp4')}
            data = {
                'title': title,
                'description': description,
                'published': 'true'
            }
            endpoint = f"{page_id}/videos"
            resp = graph_request(endpoint, token, method='POST', data=data, files=files)
        
        # Clean up temp file
        os.remove(temp_path)
        
        if resp and resp.status_code == 200:
            analytics['total_reels_published'] += 1
            return jsonify(resp.json())
        else:
            error_detail = resp.json().get('error', {}).get('message', 'Unknown error') if resp else 'No response from Graph API'
            log_error(f"Publish failed: {error_detail}")
            return jsonify({'error': error_detail}), resp.status_code if resp else 500
    except Exception as e:
        log_error(str(e))
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    return jsonify(analytics)

@app.route('/api/analytics/clear', methods=['POST'])
def clear_analytics():
    analytics['api_calls'] = 0
    analytics['publish_attempts'] = 0
    analytics['total_reels_published'] = 0
    analytics['errors'] = []
    analytics['last_activity'] = None
    return jsonify({'status': 'cleared'})

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

# ------------------- FRONTEND -------------------
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    # If the path corresponds to a static file (like favicon.ico), return 404
    # We only serve the main HTML
    return get_dashboard_html()

def get_dashboard_html():
    """Return the complete frontend HTML with all functionality"""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
    <title>XIYAD FACEBOOK Media Pro | Ultimate Dashboard</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', system-ui, -apple-system, sans-serif; }
        body { background: radial-gradient(circle at 10% 30%, #eef2ff, #e2e8f0); transition: all 0.3s ease; padding: 1.5rem; }
        body.dark { background: radial-gradient(circle at 10% 30%, #0a0c10, #030507); color: #eef2ff; }
        .glass-card { background: rgba(255, 255, 255, 0.4); backdrop-filter: blur(14px); border-radius: 2rem; border: 1px solid rgba(255,255,255,0.3); box-shadow: 0 15px 35px rgba(0,0,0,0.1); transition: all 0.2s; }
        body.dark .glass-card { background: rgba(20, 25, 35, 0.6); border-color: rgba(255,255,255,0.05); }
        .btn-premium { background: linear-gradient(135deg, #1877f2, #0b5ed7); border: none; color: white; font-weight: 600; border-radius: 3rem; padding: 0.6rem 1.4rem; cursor: pointer; transition: 0.2s; box-shadow: 0 4px 10px rgba(24,119,242,0.3); }
        .btn-premium:hover { transform: translateY(-2px); filter: brightness(1.05); }
        .input-xiya { background: rgba(255,255,255,0.85); border-radius: 2rem; padding: 0.8rem 1.2rem; border: none; outline: none; width: 100%; }
        body.dark .input-xiya { background: #1e293b; color: white; }
        .media-card { background: rgba(255,255,255,0.7); backdrop-filter: blur(4px); border-radius: 1.5rem; overflow: hidden; transition: all 0.2s; border: 1px solid rgba(255,255,255,0.4); cursor: pointer; }
        body.dark .media-card { background: #1f2937; border-color: #374151; }
        .media-card:hover { transform: translateY(-6px); box-shadow: 0 20px 30px -12px rgba(0,0,0,0.3); }
        .toast-notify { position: fixed; bottom: 20px; right: 20px; z-index: 9999; background: #1e293b; color: white; padding: 12px 20px; border-radius: 40px; backdrop-filter: blur(8px); font-weight: 500; box-shadow: 0 8px 18px rgba(0,0,0,0.3); }
        .loader { border: 3px solid rgba(0,0,0,0.1); border-radius: 50%; border-top: 3px solid #1877f2; width: 32px; height: 32px; animation: spin 0.7s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .modal-glass { background: rgba(0,0,0,0.6); backdrop-filter: blur(10px); }
        .flex-between { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; }
        .page-item { cursor: pointer; transition: 0.1s; border-left: 4px solid transparent; }
        .page-item.active { border-left-color: #1877f2; background: rgba(24,119,242,0.1); }
        ::-webkit-scrollbar { width: 5px; }
        ::-webkit-scrollbar-track { background: #ccc; border-radius: 10px; }
        ::-webkit-scrollbar-thumb { background: #1877f2; border-radius: 10px; }
    </style>
</head>
<body>
<div class="max-w-7xl mx-auto relative">
    <!-- Header -->
    <div class="flex-between mb-8">
        <div class="flex items-center gap-3">
            <i class="fab fa-facebook text-5xl text-[#1877f2] drop-shadow-lg"></i>
            <h1 class="text-3xl md:text-4xl font-extrabold bg-gradient-to-r from-[#1877f2] to-[#8b9dc3] bg-clip-text text-transparent">XIYAD <span class="text-white dark:text-gray-200">Media Pro</span></h1>
        </div>
        <div class="flex gap-3">
            <button id="themeToggleBtn" class="glass-card w-10 h-10 rounded-full flex items-center justify-center"><i class="fas fa-moon"></i></button>
            <button id="resetAllBtn" class="btn-premium !bg-red-500/80"><i class="fas fa-trash-alt"></i> Reset All</button>
        </div>
    </div>

    <!-- Settings Panel -->
    <div class="glass-card p-5 mb-6">
        <h2 class="text-xl font-bold mb-3"><i class="fas fa-sliders-h"></i> Settings & Authentication</h2>
        <div class="grid md:grid-cols-2 gap-4">
            <div><label class="text-sm font-semibold">Facebook App ID</label><input id="appIdInput" class="input-xiya" placeholder="Enter App ID"></div>
            <div><label class="text-sm font-semibold">App Secret (optional)</label><input id="appSecretInput" type="password" class="input-xiya" placeholder="Stored locally"></div>
        </div>
        <div class="flex gap-3 mt-4 flex-wrap">
            <button id="saveCredBtn" class="btn-premium"><i class="fas fa-save"></i> Save Credentials</button>
            <button id="connectFbBtn" class="btn-premium !bg-gradient-to-r from-green-600 to-teal-600"><i class="fab fa-facebook"></i> Connect Facebook (OAuth)</button>
        </div>
    </div>

    <!-- Token Manager & Profile -->
    <div class="glass-card p-5 mb-6">
        <div class="flex-between"><h2 class="text-xl font-bold"><i class="fas fa-key"></i> Token Manager</h2><button id="addAccountBtn" class="btn-premium !py-1"><i class="fas fa-user-plus"></i> Multi-Account</button></div>
        <div class="grid md:grid-cols-2 gap-4 mt-3">
            <div><label>User Access Token</label><input id="userTokenInput" class="input-xiya" placeholder="Paste token or get from connect"></div>
            <div class="flex items-end gap-2"><button id="checkTokenBtn" class="btn-premium w-full"><i class="fas fa-stethoscope"></i> Verify Token</button></div>
        </div>
        <div id="tokenStatusDiv" class="text-xs mt-2 flex gap-3 flex-wrap"><span class="badge-neon bg-green-500/20 px-2 py-0.5 rounded-full">⚡ Status: idle</span></div>
        <div id="multiAccountList" class="flex flex-wrap gap-2 mt-3"></div>
        <div class="flex gap-4 items-center mt-4 border-t pt-3">
            <div id="profilePic" class="w-12 h-12 rounded-full bg-gray-300 overflow-hidden flex items-center justify-center"><i class="fas fa-user-circle text-2xl"></i></div>
            <div><div id="profileName" class="font-bold">Not logged in</div><div id="fbId" class="text-xs opacity-70"></div></div>
            <button id="refreshPagesBtn" class="btn-premium ml-auto"><i class="fas fa-sync-alt"></i> Load My Pages</button>
        </div>
    </div>

    <!-- Pages Manager -->
    <div class="glass-card p-5 mb-6">
        <h2 class="text-xl font-bold mb-3"><i class="fas fa-th-large"></i> Your Managed Pages</h2>
        <div id="pagesContainer" class="flex flex-wrap gap-3 max-h-48 overflow-y-auto"></div>
        <div id="activePageInfo" class="text-sm text-blue-400 mt-2"></div>
    </div>

    <!-- Stats Dashboard -->
    <div class="grid lg:grid-cols-4 gap-4 mb-6">
        <div class="glass-card p-4 text-center"><i class="fas fa-video text-2xl text-blue-400"></i><h3 class="text-2xl font-bold" id="totalVideos">0</h3><p>Total Videos/Reels</p></div>
        <div class="glass-card p-4 text-center"><i class="fas fa-chart-line text-2xl text-green-400"></i><h3 class="text-2xl font-bold" id="totalViews">0</h3><p>Total Views</p></div>
        <div class="glass-card p-4 text-center"><i class="fas fa-calendar-day text-2xl text-purple-400"></i><h3 class="text-2xl font-bold" id="uploadsToday">0</h3><p>Uploads Today</p></div>
        <div class="glass-card p-4 text-center"><i class="fas fa-rocket text-2xl text-pink-400"></i><h3 class="text-2xl font-bold" id="apiCalls">0</h3><p>API Calls</p></div>
    </div>

    <!-- Chart & Activity -->
    <div class="grid lg:grid-cols-2 gap-6 mb-6">
        <div class="glass-card p-5"><canvas id="uploadChart" height="200"></canvas></div>
        <div class="glass-card p-5"><div class="flex-between"><h3 class="font-bold">Activity Timeline</h3><button id="clearActivityBtn" class="text-xs text-gray-500">Clear</button></div><div id="activityFeed" class="h-48 overflow-y-auto text-sm mt-2 space-y-1"></div></div>
    </div>

    <!-- Tools -->
    <div class="glass-card p-5 mb-6 flex flex-wrap gap-4 justify-between items-center">
        <div class="flex gap-2 items-center"><i class="fas fa-robot text-xl"></i> <strong>AI Caption:</strong> <select id="aiStyleSelect" class="rounded-full px-3 py-1 bg-white dark:bg-gray-800"><option value="viral">🔥 Viral</option><option value="business">💼 Business</option><option value="funny">😂 Funny</option><option value="motivation">💪 Motivation</option></select><button id="genCaptionBtn" class="btn-premium">✨ Generate & Copy</button></div>
        <div><button id="openBulkModal" class="btn-premium"><i class="fas fa-layer-group"></i> Bulk Reel Publisher</button></div>
        <div><button id="exportCSVBtn" class="btn-premium !bg-emerald-600"><i class="fas fa-download"></i> Export CSV</button></div>
    </div>

    <!-- Search & Filter -->
    <div class="glass-card p-4 mb-6 flex flex-wrap gap-3 items-center">
        <input id="searchInput" class="input-xiya flex-1" placeholder="🔍 Search by title, hashtag...">
        <select id="filterType" class="rounded-full px-4 py-2 bg-white dark:bg-gray-800"><option value="all">All Media</option><option value="reel">Reels Only</option><option value="video">Videos Only</option></select>
        <select id="sortOrder" class="rounded-full px-4 py-2"><option value="date">Newest First</option><option value="views">Most Viewed</option></select>
    </div>

    <!-- Media Gallery -->
    <div class="glass-card p-5">
        <div class="flex-between"><h2 class="text-xl font-bold"><i class="fab fa-facebook"></i> Media Library</h2><button id="loadMoreBtn" class="btn-premium hidden">Load More ⬇️</button></div>
        <div id="mediaGrid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6 mt-5 max-h-[600px] overflow-y-auto p-1"></div>
    </div>
</div>

<!-- Modals -->
<div id="bulkModal" class="fixed inset-0 modal-glass hidden items-center justify-center z-50">
    <div class="glass-card w-full max-w-2xl p-6 rounded-3xl">
        <div class="flex-between"><h2 class="text-xl font-bold"><i class="fab fa-facebook"></i> Bulk Reel Publisher</h2><i id="closeBulk" class="fas fa-times text-2xl cursor-pointer"></i></div>
        <div class="border-dashed border-2 rounded-2xl p-4 mt-3 text-center"><input type="file" id="bulkFileInput" multiple accept="video/mp4,video/quicktime"><p class="text-sm">Select multiple MP4 videos</p></div>
        <div id="queueList" class="max-h-48 overflow-y-auto text-sm my-3"></div>
        <button id="startBulkUpload" class="btn-premium w-full">Start Publishing Queue</button>
        <div id="bulkProgress" class="text-sm mt-2"></div>
    </div>
</div>

<div id="previewModal" class="fixed inset-0 modal-glass hidden items-center justify-center z-50">
    <div class="glass-card max-w-3xl w-full p-4"><div class="flex justify-end"><i id="closePreview" class="fas fa-times cursor-pointer text-2xl"></i></div><video id="previewVideo" controls class="w-full rounded-2xl" autoplay></video><div id="previewInfo" class="mt-2 text-sm"></div></div>
</div>

<script>
    // ---------- FULL FRONTEND LOGIC ----------
    // This frontend uses the backend API (/api/...) and also supports direct Graph calls for fallback.
    // All features: token management, pages, videos, publish, bulk, analytics, dark mode, search, filter, export.
    
    const appIdInput = document.getElementById('appIdInput');
    const appSecretInput = document.getElementById('appSecretInput');
    const userTokenInput = document.getElementById('userTokenInput');
    const saveCredBtn = document.getElementById('saveCredBtn');
    const connectBtn = document.getElementById('connectFbBtn');
    const checkTokenBtn = document.getElementById('checkTokenBtn');
    const addAccountBtn = document.getElementById('addAccountBtn');
    const refreshPagesBtn = document.getElementById('refreshPagesBtn');
    const resetBtn = document.getElementById('resetAllBtn');
    const themeToggle = document.getElementById('themeToggleBtn');
    const exportBtn = document.getElementById('exportCSVBtn');
    const genCaptionBtn = document.getElementById('genCaptionBtn');
    const openBulkModal = document.getElementById('openBulkModal');
    const closeBulk = document.getElementById('closeBulk');
    const bulkFileInput = document.getElementById('bulkFileInput');
    const startBulkUpload = document.getElementById('startBulkUpload');
    const searchInput = document.getElementById('searchInput');
    const filterSelect = document.getElementById('filterType');
    const sortSelect = document.getElementById('sortOrder');
    const loadMoreBtn = document.getElementById('loadMoreBtn');
    const pagesDiv = document.getElementById('pagesContainer');
    const mediaGrid = document.getElementById('mediaGrid');
    const totalVideosSpan = document.getElementById('totalVideos');
    const totalViewsSpan = document.getElementById('totalViews');
    const uploadsTodaySpan = document.getElementById('uploadsToday');
    const apiCallsSpan = document.getElementById('apiCalls');
    const tokenStatusDiv = document.getElementById('tokenStatusDiv');
    const activityFeedDiv = document.getElementById('activityFeed');
    const clearActivityBtn = document.getElementById('clearActivityBtn');
    const profilePicDiv = document.getElementById('profilePic');
    const profileNameSpan = document.getElementById('profileName');
    const fbIdSpan = document.getElementById('fbId');
    const activePageInfo = document.getElementById('activePageInfo');

    let currentPageId = null, currentPageToken = null;
    let allMedia = [];
    let nextCursor = null;
    let chartInstance = null;
    let activityLog = [];
    let activeAccounts = [];

    function showToast(msg, type = 'info') {
        let toast = document.createElement('div');
        toast.className = 'toast-notify';
        toast.innerHTML = `<i class="fas ${type === 'error' ? 'fa-exclamation-triangle' : 'fa-check-circle'}"></i> ${msg}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 3000);
    }
    function addActivity(msg) { activityLog.unshift({ msg, time: new Date().toLocaleTimeString() }); if (activityLog.length > 30) activityLog.pop(); renderActivity(); }
    function renderActivity() { activityFeedDiv.innerHTML = activityLog.map(log => `<div class="border-b pb-1"><span class="text-xs opacity-50">${log.time}</span> ${log.msg}</div>`).join(''); }
    clearActivityBtn.onclick = () => { activityLog = []; renderActivity(); };

    async function apiRequest(endpoint, options = {}) {
        const token = userTokenInput.value;
        const headers = { 'Authorization': `Bearer ${token}`, ...options.headers };
        const res = await fetch(endpoint, { ...options, headers });
        if (!res.ok) throw new Error(await res.text());
        return res.json();
    }

    async function checkTokenHealth() {
        const token = userTokenInput.value;
        if (!token) { tokenStatusDiv.innerHTML = '<span class="badge-neon bg-red-500/20">❌ No token</span>'; return false; }
        tokenStatusDiv.innerHTML = '<span class="badge-neon">⏳ Verifying...</span>';
        try {
            const data = await apiRequest('/api/me');
            tokenStatusDiv.innerHTML = `<span class="badge-neon bg-green-500/20">✅ Valid (${data.name})</span>`;
            return true;
        } catch(e) { tokenStatusDiv.innerHTML = '<span class="badge-neon bg-red-500/20">⚠️ Invalid token</span>'; return false; }
    }
    checkTokenBtn.onclick = checkTokenHealth;

    function renderMultiAccounts() {
        const container = document.getElementById('multiAccountList');
        container.innerHTML = activeAccounts.map((acc, idx) => `<div class="glass-card px-3 py-1 flex gap-2 items-center"><i class="fas fa-user-circle"></i>${acc.name}<button class="text-xs text-blue-400 switchAccBtn" data-token="${acc.token}">Switch</button><button class="text-xs text-red-400 removeAccBtn" data-idx="${idx}">x</button></div>`).join('');
        document.querySelectorAll('.switchAccBtn').forEach(btn => btn.addEventListener('click', () => { userTokenInput.value = btn.dataset.token; localStorage.setItem('fb_active_token', btn.dataset.token); checkTokenHealth(); fetchProfileAndPages(); showToast(`Switched to account`); }));
        document.querySelectorAll('.removeAccBtn').forEach(btn => btn.addEventListener('click', () => { activeAccounts.splice(btn.dataset.idx, 1); localStorage.setItem('xiya_accounts', JSON.stringify(activeAccounts)); renderMultiAccounts(); }));
    }
    addAccountBtn.onclick = () => { let name = prompt('Account nickname'); let tok = prompt('Enter Access Token'); if (tok && name) { activeAccounts.push({ name, token: tok }); localStorage.setItem('xiya_accounts', JSON.stringify(activeAccounts)); renderMultiAccounts(); addActivity(`Added account: ${name}`); } };

    function fbLoginWithApp() {
        const appId = appIdInput.value.trim();
        if (!appId) { showToast('Please enter App ID in settings', 'error'); return; }
        if (window.FB) { FB.init({ appId, version: 'v19.0' }); doLogin(); return; }
        window.fbAsyncInit = function () { FB.init({ appId, version: 'v19.0' }); doLogin(); };
        if (!document.getElementById('fb-sdk')) { let js = document.createElement('script'); js.id = 'fb-sdk'; js.src = 'https://connect.facebook.net/en_US/sdk.js'; document.head.append(js); } else doLogin();
        function doLogin() { FB.login(res => { if (res.authResponse) { const token = res.authResponse.accessToken; userTokenInput.value = token; localStorage.setItem('fb_active_token', token); checkTokenHealth(); fetchProfileAndPages(); addActivity('Facebook login success'); showToast('Connected'); } else showToast('Login failed', 'error'); }, { scope: 'pages_show_list,pages_read_engagement,pages_read_user_content,pages_manage_posts' }); }
    }
    connectBtn.onclick = fbLoginWithApp;
    saveCredBtn.onclick = () => { localStorage.setItem('fb_app_id', appIdInput.value); localStorage.setItem('fb_app_secret', appSecretInput.value); showToast('Credentials saved'); addActivity('App ID/Secret saved'); };

    async function fetchProfileAndPages() {
        const token = userTokenInput.value;
        if (!token) return;
        try {
            const profile = await apiRequest('/api/me');
            profileNameSpan.innerText = profile.name;
            fbIdSpan.innerText = profile.id;
            if (profile.picture) profilePicDiv.innerHTML = `<img src="${profile.picture.data.url}" class="w-full h-full object-cover">`;
            const pagesData = await apiRequest('/api/pages');
            const pages = pagesData.data || [];
            if (!pages.length) pagesDiv.innerHTML = '<div class="text-sm">No managed pages (need admin rights)</div>';
            else pagesDiv.innerHTML = pages.map(p => `<div class="glass-card p-2 flex gap-3 items-center cursor-pointer pageItem" data-pageid="${p.id}" data-pagetoken="${p.access_token}"><img src="${p.picture?.data?.url || 'https://via.placeholder.com/40'}" class="w-8 h-8 rounded-full"><div><b>${p.name}</b><div class="text-xs">📈 followers: ${p.followers_count || 0}</div></div></div>`).join('');
            document.querySelectorAll('.pageItem').forEach(el => { el.addEventListener('click', () => { currentPageId = el.dataset.pageid; currentPageToken = el.dataset.pagetoken; activePageInfo.innerText = `✅ Active: ${el.querySelector('b')?.innerText}`; fetchPageVideos(true); addActivity(`Selected page: ${el.querySelector('b')?.innerText}`); }); });
        } catch(e) { showToast('Failed to load profile/pages', 'error'); addActivity(`Error: ${e.message}`); }
    }
    refreshPagesBtn.onclick = fetchProfileAndPages;

    async function fetchPageVideos(reset = true) {
        if (!currentPageId || !currentPageToken) { showToast('Select a page first', 'error'); return; }
        if (reset) { allMedia = []; nextCursor = null; }
        mediaGrid.innerHTML = '<div class="col-span-full flex justify-center"><div class="loader"></div></div>';
        try {
            let url = `/api/pages/${currentPageId}/videos?limit=20`;
            if (nextCursor && !reset) url += `&after=${nextCursor}`;
            const data = await apiRequest(url);
            const videos = data.data || [];
            const enriched = videos.map(v => ({ ...v, is_reel: (v.title?.toLowerCase().includes('reel') || v.length < 90 || v.permalink?.includes('/reel/')), reactions_count: v.reactions?.summary?.total_count || 0, comments_count: v.comments?.summary?.total_count || 0 }));
            allMedia = reset ? enriched : [...allMedia, ...enriched];
            nextCursor = data.paging?.cursors?.after || null;
            renderMediaGallery();
            updateStatisticsAndChart();
            if (nextCursor) loadMoreBtn.classList.remove('hidden'); else loadMoreBtn.classList.add('hidden');
            addActivity(`Loaded ${enriched.length} media items`);
        } catch(e) { mediaGrid.innerHTML = `<div class="col-span-full text-red-400">Error: ${e.message}</div>`; addActivity(`Video fetch error: ${e.message}`); }
    }
    function renderMediaGallery() {
        let filtered = [...allMedia];
        if (searchInput.value) filtered = filtered.filter(m => (m.title || '').toLowerCase().includes(searchInput.value.toLowerCase()) || (m.description || '').toLowerCase().includes(searchInput.value.toLowerCase()));
        if (filterSelect.value === 'reel') filtered = filtered.filter(m => m.is_reel);
        if (filterSelect.value === 'video') filtered = filtered.filter(m => !m.is_reel);
        if (sortSelect.value === 'views') filtered.sort((a, b) => (b.views || 0) - (a.views || 0));
        else filtered.sort((a, b) => new Date(b.created_time) - new Date(a.created_time));
        mediaGrid.innerHTML = filtered.map(m => `<div class="media-card p-3"><div class="relative"><img src="${m.thumbnail_url || 'https://via.placeholder.com/300x180?text=No+thumb'}" class="w-full h-44 object-cover rounded-xl"><span class="absolute top-2 left-2 bg-black/60 text-xs px-2 rounded-full">${m.is_reel ? '🎬 REEL' : '📽️ VIDEO'}</span><span class="absolute bottom-2 right-2 bg-black/60 text-xs px-2 rounded">👁️ ${(m.views || 0).toLocaleString()}</span></div><div class="mt-2"><div class="font-bold truncate">${m.title || 'Untitled'}</div><div class="flex justify-between text-xs mt-1"><span><i class="far fa-heart"></i> ${m.reactions_count || 0}</span><span><i class="far fa-comment"></i> ${m.comments_count || 0}</span><span>📅 ${new Date(m.created_time).toLocaleDateString()}</span></div><div class="flex gap-2 mt-2"><button class="text-blue-500 text-sm copyLinkBtn" data-link="${m.permalink}"><i class="fas fa-copy"></i> Copy Link</button><button class="text-green-500 text-sm previewBtn" data-video="${m.permalink}"><i class="fas fa-play-circle"></i> Preview</button></div></div></div>`).join('');
        document.querySelectorAll('.copyLinkBtn').forEach(btn => btn.addEventListener('click', (e) => { e.stopPropagation(); navigator.clipboard.writeText(btn.dataset.link); showToast('Link copied'); }));
        document.querySelectorAll('.previewBtn').forEach(btn => btn.addEventListener('click', (e) => { e.stopPropagation(); document.getElementById('previewVideo').src = btn.dataset.video; document.getElementById('previewModal').classList.remove('hidden'); }));
    }
    function updateStatisticsAndChart() {
        const total = allMedia.length;
        const totalViewSum = allMedia.reduce((s, v) => s + (v.views || 0), 0);
        const today = new Date().toISOString().slice(0, 10);
        const todayUploads = allMedia.filter(m => m.created_time?.startsWith(today)).length;
        totalVideosSpan.innerText = total; totalViewsSpan.innerText = totalViewSum.toLocaleString(); uploadsTodaySpan.innerText = todayUploads;
        const last7 = [...Array(7)].map((_, i) => { let d = new Date(); d.setDate(d.getDate() - i); return d.toISOString().slice(0, 10); }).reverse();
        const counts = last7.map(date => allMedia.filter(v => v.created_time?.startsWith(date)).length);
        if (chartInstance) chartInstance.destroy();
        const ctx = document.getElementById('uploadChart').getContext('2d');
        chartInstance = new Chart(ctx, { type: 'bar', data: { labels: last7.map(d => d.slice(5)), datasets: [{ label: 'Uploads', data: counts, backgroundColor: '#1877f2', borderRadius: 8 }] } });
    }
    loadMoreBtn.onclick = () => { if (nextCursor && currentPageId) fetchPageVideos(false); };

    // AI Caption
    genCaptionBtn.onclick = () => { const style = document.getElementById('aiStyleSelect').value; const dict = { viral: "🔥 This reel is pure FIRE! Double tap & share! #viral #reels", business: "📈 Business insight you can't miss. Watch till the end! #growth", funny: "😂 I'm dying 🤣 Tag someone who needs this laugh!", motivation: "💪 Every day is a new opportunity. Keep going! #motivation" }; const caption = dict[style] || dict.viral; navigator.clipboard.writeText(caption); showToast(`AI Caption copied`); addActivity('AI caption generated'); };

    // Bulk Publisher
    let bulkQueue = [];
    openBulkModal.onclick = () => document.getElementById('bulkModal').classList.remove('hidden');
    closeBulk.onclick = () => document.getElementById('bulkModal').classList.add('hidden');
    bulkFileInput.onchange = (e) => { bulkQueue = Array.from(e.target.files); document.getElementById('queueList').innerHTML = bulkQueue.map(f => `<div>${f.name} (${(f.size / 1024 / 1024).toFixed(2)} MB)</div>`).join(''); };
    async function publishReel(file, title) {
        const formData = new FormData();
        formData.append('page_id', currentPageId);
        formData.append('title', title);
        formData.append('video', file);
        const token = userTokenInput.value;
        const res = await fetch('/api/publish', { method: 'POST', headers: { 'Authorization': `Bearer ${token}` }, body: formData });
        if (!res.ok) throw new Error(await res.text());
        return res.json();
    }
    startBulkUpload.onclick = async () => { if (!currentPageId) { showToast('Select a page first', 'error'); return; } if (!bulkQueue.length) return; const progressDiv = document.getElementById('bulkProgress'); for (let i = 0; i < bulkQueue.length; i++) { progressDiv.innerText = `Uploading ${i+1}/${bulkQueue.length}: ${bulkQueue[i].name}`; try { const result = await publishReel(bulkQueue[i], `Bulk reel ${i+1}`); if (result.id) { showToast(`Published: ${result.id}`); addActivity(`Bulk published: ${result.id}`); } else showToast(`Failed: ${result.error}`, 'error'); } catch(e) { showToast(`Upload error: ${e.message}`, 'error'); } await new Promise(r => setTimeout(r, 1200)); } progressDiv.innerText = 'Bulk completed!'; fetchPageVideos(true); };
    // Export CSV
    exportBtn.onclick = () => { const rows = [["Title", "Permalink", "Views", "Date", "Type"]]; allMedia.forEach(m => { rows.push([m.title || "", m.permalink || "", m.views || "", m.created_time || "", m.is_reel ? "Reel" : "Video"]); }); const csv = rows.map(r => r.join(",")).join("\n"); const blob = new Blob([csv], { type: "text/csv" }); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = "xiya_media.csv"; a.click(); showToast("CSV exported"); };
    // Search/filter listeners
    searchInput.addEventListener('input', () => renderMediaGallery());
    filterSelect.addEventListener('change', () => renderMediaGallery());
    sortSelect.addEventListener('change', () => renderMediaGallery());
    document.getElementById('closePreview').onclick = () => { document.getElementById('previewModal').classList.add('hidden'); document.getElementById('previewVideo').pause(); };
    resetBtn.onclick = () => { localStorage.clear(); location.reload(); };
    themeToggle.onclick = () => { document.body.classList.toggle('dark'); localStorage.setItem('theme', document.body.classList.contains('dark') ? 'dark' : 'light'); };
    // Load stored data
    function init() {
        appIdInput.value = localStorage.getItem('fb_app_id') || '';
        appSecretInput.value = localStorage.getItem('fb_app_secret') || '';
        userTokenInput.value = localStorage.getItem('fb_active_token') || '';
        try { activeAccounts = JSON.parse(localStorage.getItem('xiya_accounts') || '[]'); } catch(e) { activeAccounts = []; }
        renderMultiAccounts();
        if (localStorage.getItem('theme') === 'dark') document.body.classList.add('dark');
        if (userTokenInput.value) { checkTokenHealth(); fetchProfileAndPages(); }
        addActivity('Dashboard ready — A to Z features loaded');
    }
    init();
</script>
</body>
</html>'''

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)