import os
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
import requests

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SECRET_KEY'] = 'your-secret-key'

db = SQLAlchemy(app)
Session(app)

# ---------- User Model ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    facebook_app_id = db.Column(db.String(255))
    facebook_app_secret = db.Column(db.String(255))
    long_lived_token = db.Column(db.Text)
    pages_json = db.Column(db.Text)

with app.app_context():
    db.create_all()

# ========== আপনার আগের স্ক্র্যাপার রুটগুলো এখানে বসবে ==========
# উদাহরণ:
# @app.route('/scan', methods=['POST'])
# def scan_url():
#     ... আপনার yt-dlp কোড ...
# =================================================================

# ---------- Login Routes ----------
@app.route('/save-settings', methods=['POST'])
def save_settings():
    data = request.json
    app_id = data.get('app_id')
    app_secret = data.get('app_secret')
    user = User.query.get(1)
    if not user:
        user = User(id=1, facebook_app_id=app_id, facebook_app_secret=app_secret)
        db.session.add(user)
    else:
        user.facebook_app_id = app_id
        user.facebook_app_secret = app_secret
    db.session.commit()
    return jsonify({'success': True})

@app.route('/facebook-login')
def facebook_login():
    user = User.query.get(1)
    app_id = user.facebook_app_id
    redirect_uri = "http://localhost:5000/facebook-callback"
    login_url = (
        f"https://www.facebook.com/v25.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=pages_show_list,pages_manage_posts,pages_read_engagement"
    )
    return jsonify({'login_url': login_url})

@app.route('/facebook-callback')
def facebook_callback():
    code = request.args.get('code')
    user = User.query.get(1)
    app_id = user.facebook_app_id
    app_secret = user.facebook_app_secret
    redirect_uri = "http://localhost:5000/facebook-callback"
    token_url = (
        f"https://graph.facebook.com/v25.0/oauth/access_token"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&client_secret={app_secret}"
        f"&code={code}"
    )
    resp = requests.get(token_url).json()
    access_token = resp.get('access_token')
    user.long_lived_token = access_token
    db.session.commit()
    return jsonify({'success': True, 'token_saved': True})

@app.route('/logout')
def logout():
    user = User.query.get(1)
    user.long_lived_token = None
    db.session.commit()
    return jsonify({'success': True})

@app.route('/reset')
def reset():
    user = User.query.get(1)
    user.facebook_app_id = None
    user.facebook_app_secret = None
    user.long_lived_token = None
    user.pages_json = None
    db.session.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True)
