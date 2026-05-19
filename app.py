import os, re
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fbdownloader-secret-key')
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

# ---------- URL helpers ----------
FACEBOOK_VIDEO_PATTERN = re.compile(
    r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/(?:reel\/|watch\/?\?v=|[\w\.\-]+\/videos\/)(\d+)'
)
FB_WATCH_PATTERN = re.compile(r'(?:https?:\/\/)?fb\.watch\/([a-zA-Z0-9_-]+)')

def is_facebook_video_url(url: str) -> bool:
    return bool(FACEBOOK_VIDEO_PATTERN.search(url) or FB_WATCH_PATTERN.search(url))

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

@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not is_facebook_video_url(url):
        return jsonify({'error': 'Invalid Facebook video URL.'}), 400
    try:
        result = extract_single_video(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')