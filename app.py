import os, re, requests
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'xiyad-media-pro-secret')
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

def is_facebook_url(url: str) -> bool:
    return "facebook.com" in url.lower() or "fb.watch" in url.lower()

def classify_url(url: str):
    if any(x in url.lower() for x in ["/reel/", "/watch?", "/videos/", "fb.watch"]):
        return "video"
    if "facebook.com" in url.lower():
        return "page"
    return None

def scan_page_videos_yt(url: str) -> list:
    parsed = urlparse(url)
    if 'profile.php' in parsed.path:
        qs = parse_qs(parsed.query)
        profile_id = qs.get('id', [None])[0]
        if not profile_id:
            raise ValueError("Could not extract profile ID")
        videos_url = f"https://www.facebook.com/profile.php?id={profile_id}&sk=videos"
    else:
        if not url.endswith('/videos'):
            url = url.rstrip('/') + '/videos'
        videos_url = url

    ydl_opts = {
        'quiet': True, 'no_warnings': True, 'extract_flat': True,
        'skip_download': True, 'force_generic_extractor': False, 'playlistend': 200,
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
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False, 'skip_download': True}
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
        'id': info.get('id'), 'title': info.get('title', ''),
        'thumbnail': info.get('thumbnail', ''),
        'duration': info.get('duration', 0),
        'hd_url': hd_url, 'sd_url': sd_url,
        'caption': description, 'hashtags': hashtags,
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
    if not url or not is_facebook_url(url):
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
    if not url or not is_facebook_url(url):
        return jsonify({'error': 'Invalid Facebook URL'}), 400
    try:
        result = extract_single_video(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload-reel', methods=['POST'])
@limiter.limit("2 per minute")
def upload_reel():
    page_token = request.form.get('page_token')
    title = request.form.get('title', '')
    description = request.form.get('description', '')
    schedule_time = request.form.get('schedule_time', '')
    publish_mode = request.form.get('publish_mode', 'PUBLISH_NOW')
    file = request.files.get('video_file')

    if not page_token or not file:
        return jsonify({'error': 'Missing page token or video file'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join('/tmp', filename)
    file.save(filepath)
    file_size = os.path.getsize(filepath)

    try:
        # 1. Init upload session
        init_url = "https://graph.facebook.com/v20.0/me/video_reels"
        params = {
            'access_token': page_token,
            'upload_phase': 'start',
            'file_size': file_size,
        }
        if publish_mode == 'SCHEDULED' and schedule_time:
            params['scheduled_publish_time'] = schedule_time
        init_resp = requests.post(init_url, params=params)
        init_data = init_resp.json()
        if 'error' in init_data:
            raise Exception(init_data['error']['message'])

        video_id = init_data.get('video_id')
        upload_url = init_data.get('upload_url')

        # 2. Upload
        with open(filepath, 'rb') as f:
            upload_resp = requests.post(upload_url, headers={
                'Authorization': f'OAuth {page_token}',
                'offset': '0',
                'file_size': str(file_size),
                'Content-Type': 'application/octet-stream',
            }, data=f)
        if upload_resp.status_code != 200:
            raise Exception(f"Upload failed: {upload_resp.text}")

        # 3. Finish
        finish_url = "https://graph.facebook.com/v20.0/me/video_reels"
        finish_params = {
            'access_token': page_token,
            'upload_phase': 'finish',
            'video_id': video_id,
            'title': title,
            'description': description,
            'video_state': publish_mode,
        }
        if publish_mode == 'SCHEDULED' and schedule_time:
            finish_params['scheduled_publish_time'] = schedule_time
        finish_resp = requests.post(finish_url, params=finish_params)
        finish_data = finish_resp.json()
        if 'error' in finish_data:
            raise Exception(finish_data['error']['message'])

        os.remove(filepath)
        return jsonify({
            'success': True,
            'video_id': finish_data.get('id'),
            'status': finish_data.get('status', 'published'),
        })
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
