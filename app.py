import os
import time
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from ebooklib import epub
from bs4 import BeautifulSoup
from openai import OpenAI
from deep_translator import GoogleTranslator

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

CHUTES_API_KEY = os.getenv("CHUTES_API_KEY")
client = OpenAI(base_url="https://api.chutes.ai/v1", api_key=CHUTES_API_KEY)

# Active users tracker (in-memory)
active_users = {}

# --- Database Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    custom_prompt = db.Column(db.Text, nullable=True)
    bookmarks = db.relationship('Bookmark', backref='user', lazy=True)

class Bookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    raw_filename = db.Column(db.String(255), nullable=False)
    html_name = db.Column(db.String(255), nullable=False)
    chapter_title = db.Column(db.String(255), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.before_request
def track_active_users():
    if current_user.is_authenticated:
        active_users[current_user.username] = time.time()
    # Clean up inactive users (e.g., no activity for 5 minutes)
    current_time = time.time()
    for user in list(active_users.keys()):
        if current_time - active_users[user] > 300:
            del active_users[user]

# --- Routes ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user:
            flash('Username already exists.')
            return redirect(url_for('signup'))
            
        new_user = User(username=username, password=generate_password_hash(password, method='pbkdf2:sha256'))
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        flash('Please check your login details and try again.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    if current_user.username in active_users:
        del active_users[current_user.username]
    logout_user()
    return redirect(url_for('login'))

@app.route("/")
@login_required
def index():
    active_count = len(active_users)
    bookmarks = Bookmark.query.filter_by(user_id=current_user.id).all()
    return render_template("index.html", active_count=active_count, bookmarks=bookmarks)

@app.route("/profile", methods=['POST'])
@login_required
def update_profile():
    new_prompt = request.form.get('custom_prompt')
    current_user.custom_prompt = new_prompt
    db.session.commit()
    return redirect(url_for('index'))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    raw_filename = file.filename
    
    # Translate book title
    try:
        translated_title = GoogleTranslator(source='auto', target='en').translate(raw_filename)
    except:
        translated_title = raw_filename
    
    filepath = f"temp_{raw_filename}"
    file.save(filepath)
    
    book = epub.read_epub(filepath)
    chapters = []
    
    for item in book.get_items():
        if item.get_type() == epub.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            text = soup.get_text(separator='\n').strip()
            if len(text) > 200:
                html_name = item.get_name()
                # Use a rough chapter title extraction or default
                chapter_title = f"Chapter {len(chapters) + 1}" 
                
                chapters.append({
                    "text": text,
                    "html_name": html_name,
                    "chapter_title": chapter_title
                })
                
                # Save Bookmark with Raw Name
                new_bookmark = Bookmark(
                    user_id=current_user.id,
                    raw_filename=raw_filename,
                    html_name=html_name,
                    chapter_title=chapter_title
                )
                db.session.add(new_bookmark)
                
    db.session.commit()
    os.remove(filepath)
    
    return jsonify({"chapters": chapters, "translated_title": translated_title})

@app.route("/read")
@login_required
def read():
    # Pass dummy data for the reader UI rendering, in a real scenario you pass specific chapter text
    active_count = len(active_users)
    return render_template("reader.html", active_count=active_count)

if __name__ == "__main__":
    with app.app_context():
        db.create_all() # Creates the database tables
    app.run(debug=True, port=5000)
