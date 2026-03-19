import os
import time
import json
import traceback
from urllib.parse import unquote
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from openai import OpenAI
from deep_translator import GoogleTranslator

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 

os.makedirs('static', exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

CHUTES_API_KEY = os.getenv("CHUTES_API_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

try:
    with open("prompt.txt", "r", encoding="utf-8") as f:
        PRIVATE_PROMPT = f.read()
except FileNotFoundError:
    PRIVATE_PROMPT = "You are a literary assistant. Please translate this chapter into English."

client = OpenAI(base_url="https://llm.chutes.ai/v1", api_key=CHUTES_API_KEY)
STATUS_FILE = "site_status.txt"
active_users = {}

# --- Database Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    custom_prompt = db.Column(db.Text, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    profile_pic = db.Column(db.String(255), default='default_profile.png') 
    bookmarks = db.relationship('Bookmark', backref='user', lazy=True)

class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    raw_filename = db.Column(db.String(255), nullable=False)
    translated_title = db.Column(db.String(255), nullable=False)
    cover_image = db.Column(db.String(255), default='default_cover.png') 
    chapters = db.relationship('Chapter', backref='book', lazy=True, cascade="all, delete-orphan")

class Chapter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    chapter_number = db.Column(db.Integer, nullable=False)
    chapter_title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)

class Bookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    current_chapter_number = db.Column(db.Integer, nullable=False, default=1)
    book = db.relationship('Book', backref='bookmarks')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def is_offline():
    if not os.path.exists(STATUS_FILE):
        return False
    with open(STATUS_FILE, "r") as f:
        return f.read().strip() == "offline"

@app.before_request
def track_active_users():
    if is_offline() and request.endpoint not in ['admin', 'login', 'static']:
        if not (current_user.is_authenticated and current_user.is_admin):
            return "<h1>The website is currently offline. Please check back later.</h1>", 503

    if current_user.is_authenticated:
        active_users[current_user.username] = time.time()
    
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
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists.')
            return redirect(url_for('signup'))
            
        is_user_admin = True if username == ADMIN_USERNAME else False
        new_user = User(
            username=username, 
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            is_admin=is_user_admin
        )
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
        flash('Invalid credentials.')
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
    bookmarks = Bookmark.query.filter_by(user_id=current_user.id).all()
    return render_template("index.html", active_users_count=len(active_users), bookmarks=bookmarks)

@app.route("/edit-profile", methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        if new_password:
            current_user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
        
        if 'profile_pic' in request.files:
            pic = request.files['profile_pic']
            if pic.filename != '':
                filename = secure_filename(f"user_{current_user.id}_{pic.filename}")
                pic.save(os.path.join('static', filename))
                current_user.profile_pic = filename

        db.session.commit()
        flash("Profile updated successfully.")
        return redirect(url_for('index'))
    return render_template("edit_profile.html", active_users_count=len(active_users))

@app.route("/modify-prompt", methods=['GET', 'POST'])
@login_required
def modify_prompt():
    if request.method == 'POST':
        current_user.custom_prompt = request.form.get('custom_prompt')
        db.session.commit()
        flash("Custom prompt updated successfully.")
        return redirect(url_for('index'))
    return render_template("modify_prompt.html", active_users_count=len(active_users))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    raw_filename = secure_filename(file.filename)
    filepath = f"temp_{raw_filename}"
    
    try:
        try:
            translated_title = GoogleTranslator(source='auto', target='en').translate(raw_filename)
        except:
            translated_title = raw_filename
        
        file.save(filepath)
        book = epub.read_epub(filepath)
        
        new_book = Book(raw_filename=raw_filename, translated_title=translated_title)
        db.session.add(new_book)
        db.session.flush() 
        
        book_folder = os.path.join('static', f'book_{new_book.id}')
        os.makedirs(book_folder, exist_ok=True)
        
        image_map = {}
        for item in book.get_items():
            item_name = item.get_name().lower()
            is_img = item.get_type() == ebooklib.ITEM_IMAGE or item.get_type() == ebooklib.ITEM_COVER
            is_img_ext = item_name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'))
            media_type = getattr(item, 'media_type', '') or ""
            
            if is_img or is_img_ext or 'image' in media_type:
                safe_name = secure_filename(item.get_name().replace('/', '_'))
                file_path = os.path.join(book_folder, safe_name)
                with open(file_path, 'wb') as f:
                    f.write(item.get_content())
                
                web_path = url_for('static', filename=f'book_{new_book.id}/{safe_name}')
                image_map[item.get_name()] = web_path
                
                if item.get_type() == ebooklib.ITEM_COVER or 'cover' in item_name:
                    new_book.cover_image = f'book_{new_book.id}/{safe_name}'
        
        chapter_num = 1
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                
                for img in soup.find_all(['img', 'image']):
                    src = img.get('src') or img.get('xlink:href')
                    if src:
                        base_src_name = unquote(src.split('/')[-1])
                        for orig_name, web_path in image_map.items():
                            if unquote(orig_name).endswith(base_src_name):
                                if img.name == 'img':
                                    img['src'] = web_path
                                else:
                                    img['xlink:href'] = web_path
                                if img.get('srcset'):
                                    del img['srcset']
                                break
                
                body = soup.find('body')
                if body:
                    text = body.decode_contents().strip()
                else:
                    text = soup.get_text(separator='\n').strip()
                
                if len(text) > 200:
                    chap = Chapter(book_id=new_book.id, chapter_number=chapter_num, chapter_title=f"Chapter {chapter_num}", content=text)
                    db.session.add(chap)
                    chapter_num += 1
                    
        if chapter_num == 1:
            raise Exception("No readable text found.")
            
        new_bookmark = Bookmark(user_id=current_user.id, book_id=new_book.id, current_chapter_number=1)
        db.session.add(new_bookmark)
        db.session.commit()
        
        return jsonify({"success": True})
        
    except Exception as e:
        db.session.rollback()
        print(f"UPLOAD CRASHED: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

@app.route("/book/<int:book_id>")
@login_required
def book_details(book_id):
    book = Book.query.get_or_404(book_id)
    chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.chapter_number).all()
    
    bm = Bookmark.query.filter_by(user_id=current_user.id, book_id=book_id).first()
    current_chapter = bm.current_chapter_number if bm else 1
    
    return render_template("book_details.html", active_users_count=len(active_users), book=book, chapters=chapters, current_chapter=current_chapter)

@app.route("/read/<int:book_id>/<int:chapter_num>")
@login_required
def read(book_id, chapter_num):
    book = Book.query.get_or_404(book_id)
    chapter = Chapter.query.filter_by(book_id=book_id, chapter_number=chapter_num).first_or_404()
    
    bm = Bookmark.query.filter_by(user_id=current_user.id, book_id=book_id).first()
    if bm:
        bm.current_chapter_number = chapter_num
        db.session.commit()

    total_chapters = Chapter.query.filter_by(book_id=book_id).count()
    return render_template("reader.html", active_users_count=len(active_users), book=book, chapter=chapter, total=total_chapters)

@app.route("/api/translate", methods=["POST"])
@login_required
@app.route("/api/translate", methods=["POST"])
@login_required
def translate_stream():
    data = request.json
    chapter_id = data.get('chapter_id')
    chapter = Chapter.query.get(chapter_id)
    
    prompt = current_user.custom_prompt if current_user.custom_prompt else PRIVATE_PROMPT

    def generate():
        try:
            # We enforce strict formatting and explicitly mention short notices
            enforcer = (
                "\n\n[CRITICAL INSTRUCTION: The text above may be a short notice, info page, or normal chapter. "
                "Regardless of the content, you MUST translate it and output ONLY valid HTML. "
                "Do NOT explain your thought process. Do NOT output 'Okay'. The VERY FIRST character must be '<'.]"
            )
            
            response = client.chat.completions.create(
                model="Qwen/Qwen3-14B",
                messages=[
                    {"role": "system", "content": prompt},
                    # Wrapping the content in markdown HTML blocks forces the AI into "code output" mode
                    {"role": "user", "content": f"Translate this text:\n\n```html\n{chapter.content}\n```\n{enforcer}"}
                ],
                temperature=0.1, 
                stream=True,
                max_tokens=65536 
            )
            
            tokens = 0
            started_html = False
            text_buffer = ""
            
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    
                    # AI models love to output ```html at the start of code. We strip it so it doesn't show on the website.
                    text = text.replace("```html", "").replace("```", "")
                    
                    # THE "GAG ORDER" FILTER: 
                    # We collect the text secretly and refuse to send anything to the browser until we hit an HTML tag.
                    if not started_html:
                        text_buffer += text
                        html_start = text_buffer.find('<')
                        
                        if html_start != -1:
                            started_html = True
                            text = text_buffer[html_start:] # Cut off all the rambling before the '<'
                            if text.strip(): # Only yield if there's actual text left
                                tokens += 1
                                yield f"data: {json.dumps({'text': text, 'tokens': tokens, 'status': 'Translating...'})}\n\n"
                    else:
                        if text:
                            tokens += 1 
                            yield f"data: {json.dumps({'text': text, 'tokens': tokens, 'status': 'Translating...'})}\n\n"
                            
            yield f"data: {json.dumps({'text': '', 'tokens': tokens, 'status': 'Completed'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    if not current_user.is_admin:
        return "Unauthorized", 403
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            with open(STATUS_FILE, "w") as f:
                f.write(request.form.get("status"))
            return redirect(url_for("admin"))
    return render_template("admin.html", status="offline" if is_offline() else "online", active_users_count=len(active_users))

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
