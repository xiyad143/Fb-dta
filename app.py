import os, re
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('xiyad404', 'fbdownloader-secret-key')
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

# ---------- Facebook URL validation ----------
def is_facebook_url(url: str) -> bool:
    patterns = [
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/reel\/\d+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/watch\/?\?v=\d+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/[\w\.\-]+\/videos\/\d+',
        r'(?:https?:\/\/)?fb\.watch\/[a-zA-Z0-9_-]+',
        r'(?:https?:\/\/)?(?:www\.|web\.|m\.)?facebook\.com\/[\w\.\-]+\/?$'  # page URL
    ]
    return any(re.search(p, url) for p in patterns)

# ---------- Video extraction (public only) ----------
def extract_video_info(url: str) -> dict:
    if not is_facebook_url(url):
        raise ValueError("Invalid Facebook URL")

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'force_generic_extractor': False,
        'noplaylist': False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # If it's a playlist/page, take first video
    if 'entries' in info and info['entries']:
        info = info['entries'][0]
    elif 'entries' in info:
        raise Exception("No public videos found on this page")

    # Get formats
    formats = info.get('formats', [])
    hd_url = None
    sd_url = None
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
        'url': url
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
    if not is_facebook_url(url):
        return jsonify({'error': 'Invalid Facebook URL. Only public video/reel/page links are supported.'}), 400
    try:
        result = extract_video_info(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
