import os, re
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fbdownloader-secret-key')
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

def is_facebook_url(url: str) -> bool:
    url_lower = url.lower()
    return "facebook.com" in url_lower or "fb.watch" in url_lower

def classify_url(url: str):
    url_lower = url.lower()
    if any(x in url_lower for x in ["/reel/", "/watch?", "/videos/", "fb.watch"]):
        return "video"
    if "facebook.com" in url_lower:
        return "page"
    return None

def scan_page_videos_yt(url: str) -> list:
    """
    Scans a Facebook page or profile for public videos using yt-dlp.
    Handles both page (e.g. /NASA) and profile (e.g. profile.php?id=123) URLs.
    """
    parsed = urlparse(url)
    # ---- Normalize to a video listing URL ----
    if 'profile.php' in parsed.path:
        # Extract the numeric ID from query string
        qs = parse_qs(parsed.query)
        profile_id = qs.get('id', [None])[0]
        if not profile_id:
            raise ValueError("Could not extract profile ID from the URL.")
        # Build the correct “Videos” tab URL
        videos_url = f"https://www.facebook.com/profile.php?id={profile_id}&sk=videos"
    else:
        # Regular page: simply add /videos if not already present
        if not url.endswith('/videos'):
            url = url.rstrip('/') + '/videos'
        videos_url = url

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,      # fast, no download
        'skip_download': True,
        'force_generic_extractor': False,
        'playlistend': 100,        # fetch up to 100 recent videos
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(videos_url, download=False)

    entries = info.get('entries') or []
    videos = []
    for entry in entries:
        if not entry:
            continue
        vid_id = entry.get('id')
        vid_url = f"https://www.facebook.com/watch?v={vid_id}" if vid_id else entry.get('url', '')
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/scan-page', methods=['POST'])
@limiter.limit("5 per minute")
def scan_page():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not is_facebook_url(url):
        return jsonify({'error': 'Invalid Facebook URL'}), 400
    try:
        videos = scan_page_videos_yt(url)
        total_videos = len(videos)
        reels = [v for v in videos if v['is_reel']]
        return jsonify({
            'total_videos': total_videos,
            'total_reels': len(reels),
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
        return jsonify({'error': 'URL required'}), 400
    if not is_facebook_url(url):
        return jsonify({'error': 'Invalid Facebook URL'}), 400
    try:
        result = extract_single_video(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
    
