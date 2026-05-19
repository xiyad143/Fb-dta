import os, re
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fbdownloader-secret-key')
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

# ---------- URL detection ----------
FACEBOOK_PAGE_PATTERN = re.compile(
    r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/(?!reel\/|watch\/|[\w\.\-]+\/videos\/)([\w\.\-]+)\/?$'
)
FACEBOOK_PAGE_VIDEOS_PATTERN = re.compile(
    r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/([\w\.\-]+)\/videos\/?$'
)
FACEBOOK_VIDEO_PATTERN = re.compile(
    r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/(?:reel\/|watch\/?\?v=|[\w\.\-]+\/videos\/)(\d+)'
)
FB_WATCH_PATTERN = re.compile(r'(?:https?:\/\/)?fb\.watch\/([a-zA-Z0-9_-]+)')

def classify_url(url: str):
    if FACEBOOK_VIDEO_PATTERN.search(url) or FB_WATCH_PATTERN.search(url):
        return 'video'
    if FACEBOOK_PAGE_VIDEOS_PATTERN.search(url):
        return 'page_videos'
    if FACEBOOK_PAGE_PATTERN.search(url):
        return 'page'
    return None

# ---------- Page scanner (public videos only, no token) ----------
def scan_page_videos(url: str) -> list:
    page_match = FACEBOOK_PAGE_PATTERN.search(url) or FACEBOOK_PAGE_VIDEOS_PATTERN.search(url)
    if page_match:
        page_name = page_match.group(1)
        url = f"https://www.facebook.com/{page_name}/videos"
    # else use as is

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'skip_download': True,
        'force_generic_extractor': False,
        'playlistend': 100,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get('entries') or []
    videos = []
    for entry in entries:
        if not entry:
            continue
        vid_id = entry.get('id')
        vid_url = ''
        if vid_id:
            vid_url = f"https://www.facebook.com/watch?v={vid_id}"
        else:
            vid_url = entry.get('url', '')
        is_reel = '/reel/' in (entry.get('url') or '')
        videos.append({
            'id': vid_id or entry.get('id'),
            'title': entry.get('title', 'Untitled'),
            'thumbnail': entry.get('thumbnail', ''),
            'duration': entry.get('duration', 0),
            'url': vid_url,
            'is_reel': is_reel,
        })
    return videos

# ---------- Single video extraction (public only) ----------
def extract_single_video(url: str) -> dict:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'force_generic_extractor': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get('formats', [])
    hd_url = sd_url = None
    for fmt in formats:
        if fmt.get('acodec') != 'none' and fmt.get('vcodec') != 'none':
            height = fmt.get('height', 0) or 0
            if height >= 720 and not hd_url:
                hd_url = fmt['url']
            elif height > 0 and height < 720 and not sd_url:
                sd_url = fmt['url']
    if not hd_url:
        for fmt in formats:
            if fmt.get('format_id') == 'hd' and fmt.get('url'):
                hd_url = fmt['url']; break
    if not sd_url:
        for fmt in formats:
            if fmt.get('format_id') == 'sd' and fmt.get('url'):
                sd_url = fmt['url']; break

    description = info.get('description') or ''
    hashtags = list(set(re.findall(r'#(\w+)', description)))

    return {
        'id': info.get('id'),
        'title': info.get('title', ''),
        'thumbnail': info.get('thumbnail', ''),
        'duration': info.get('duration', 0),
        'hd_url': hd_url,
        'sd_url': sd_url,
        'caption': description,
        'hashtags': hashtags,
        'upload_date': info.get('upload_date', ''),
        'view_count': info.get('view_count', 0),
        'like_count': info.get('like_count', 0),
    }

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/scan-page', methods=['POST'])
@limiter.limit("5 per minute")
def scan_page():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if classify_url(url) not in ('page', 'page_videos'):
        return jsonify({'error': 'Invalid Facebook page URL.'}), 400

    try:
        videos = scan_page_videos(url)
        total_videos = len(videos)
        reels = [v for v in videos if v['is_reel']]
        total_reels = len(reels)
        return jsonify({
            'total_videos': total_videos,
            'total_reels': total_reels,
            'videos': videos,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if classify_url(url) != 'video':
        return jsonify({'error': 'Invalid video URL.'}), 400
    try:
        result = extract_single_video(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')