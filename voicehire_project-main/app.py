import sqlite3
import traceback
import os
import datetime
import uuid
import re
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from translations import get_translation

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_voicehire_key') # Fallback for local
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB max upload
CORS(app, resources={r"/api/*": {"origins": "*"}}) # restrict CORS if needed

DATABASE = 'voicehire.db'
UPLOAD_FOLDER = os.path.join('static', 'uploads')
AUDIO_FOLDER = os.path.join(UPLOAD_FOLDER, 'audio')
VIDEO_FOLDER = os.path.join(UPLOAD_FOLDER, 'video')

os.makedirs(AUDIO_FOLDER, exist_ok=True)
os.makedirs(VIDEO_FOLDER, exist_ok=True)

ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a', 'aac'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov'}

def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set

def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Create workers table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            work TEXT NOT NULL,
            location TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            voice_note TEXT,
            video TEXT
        )
    ''')
    
    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
    # Create jobs table (User Queries)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            user_phone TEXT NOT NULL,
            service_type TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Schema Migrations: add missing columns safely
    cursor.execute("PRAGMA table_info(jobs)")
    job_columns = [row[1] for row in cursor.fetchall()]
    if 'location' not in job_columns:
        cursor.execute('ALTER TABLE jobs ADD COLUMN location TEXT')
    if 'status' not in job_columns:
        cursor.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'open'")
    if 'worker_id' not in job_columns:
        cursor.execute('ALTER TABLE jobs ADD COLUMN worker_id INTEGER')

    # Create reviews table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs (id),
            FOREIGN KEY (worker_id) REFERENCES workers (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Add indexes for search performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_workers_phone ON workers(phone)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_workers_work ON workers(work)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_workers_location ON workers(location)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)')

    conn.commit()
    conn.close()

# Initialize DB on startup
init_db()

# ---- TRANSLATION CONFIG ----
@app.context_processor
def inject_translation():
    def t(text):
        lang = session.get('lang', 'en')
        return get_translation(lang, text)
    return dict(t=t)

@app.route('/set_lang/<lang_code>')
def set_lang(lang_code):
    session['lang'] = lang_code
    return redirect(url_for('home'))

# ---- TEMPLATE ROUTES ----

@app.route('/')
def index():
    # Language selection is the very first screen
    return render_template('select_language.html')

@app.route('/home')
def home():
    # Main landing page — after language is chosen
    if 'user_id' in session:
        if session.get('role') == 'user':
            return redirect(url_for('user_dashboard'))
        else:
            return redirect(url_for('worker_dashboard'))
    return render_template('language.html')

@app.route('/role')
def role_selection():
    return render_template('role.html')

@app.route('/login')
def login_page():
    # Passed from URL args e.g. ?role=user
    role = request.args.get('role', 'user')
    return render_template('login.html', role=role)

@app.route('/signup/user')
def user_signup_page():
    return render_template('user_signup.html')

@app.route('/signup/worker')
def worker_signup_page():
    return render_template('worker_signup.html')

@app.route('/stitch_worker_dashboard')
def stitch_worker_dashboard():
    return render_template('stitch_worker_dashboard.html')

@app.route('/dashboard/user')
def user_dashboard():
    if 'user_id' not in session or session.get('role') != 'user':
        return redirect(url_for('login_page', role='user'))
    return render_template('user_dashboard.html', name=session.get('name'))

@app.route('/dashboard/worker')
def worker_dashboard():
    if 'user_id' not in session or session.get('role') != 'worker':
        return redirect(url_for('login_page', role='worker'))
    
    # Fetch full worker profile to render
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workers WHERE id = ?', (session['user_id'],))
        worker = cursor.fetchone()
        conn.close()
        
        if not worker:
            session.clear()
            return redirect(url_for('login_page', role='worker'))
            
        worker_data = dict(worker)
        return render_template('worker_dashboard.html', worker=worker_data)
    except Exception as e:
        return f"Database error: {e}"

# ---- HELPER FUNCTIONS ----
def is_valid_phone(phone):
    return bool(re.match(r'^[6-9]\d{9}$', str(phone).strip()))

def safe_str(val):
    return str(val).strip() if val else ''

# ---- AUTHENTICATION API ENDPOINTS ----

@app.route('/api/auth/signup/user', methods=['POST'])
def signup_user():
    try:
        data = request.get_json() or {}
    except:
        return jsonify({'error': 'Invalid JSON data'}), 400

    name = safe_str(data.get('name'))
    phone = safe_str(data.get('phone'))
    password = safe_str(data.get('password'))

    if not all([name, phone, password]):
        return jsonify({'error': 'All fields are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number (10 digits starting with 6-9)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    hashed_pw = generate_password_hash(password)
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO users (name, phone, password) VALUES (?, ?, ?)', (name, phone, hashed_pw))
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        session['user_id'] = user_id
        session['role'] = 'user'
        session['name'] = name
        session['phone'] = phone
        
        return jsonify({'message': 'User registered successfully', 'redirect': url_for('user_dashboard')}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Phone number already registered'}), 400
    except Exception as e:
        print("Error during user signup:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/signup/worker', methods=['POST'])
def signup_worker():
    name = safe_str(request.form.get('name'))
    work = safe_str(request.form.get('work'))
    location = safe_str(request.form.get('location'))
    phone = safe_str(request.form.get('phone'))
    password = safe_str(request.form.get('password'))

    if not all([name, work, location, phone, password]):
        return jsonify({'error': 'All textual fields are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number (10 digits starting with 6-9)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
        
    hashed_pw = generate_password_hash(password)
    voice_file = request.files.get('voice_note')
    video_file = request.files.get('video')
    
    voice_path = None
    video_path = None
    
    try:
        if voice_file and voice_file.filename != '':
            if not allowed_file(voice_file.filename, ALLOWED_AUDIO_EXTENSIONS):
                return jsonify({'error': 'Invalid audio file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(voice_file.filename)}"
            voice_file.save(os.path.join(AUDIO_FOLDER, filename))
            voice_path = f'uploads/audio/{filename}'

        if video_file and video_file.filename != '':
            if not allowed_file(video_file.filename, ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid video file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(video_file.filename)}"
            video_file.save(os.path.join(VIDEO_FOLDER, filename))
            video_path = f'uploads/video/{filename}'

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO workers (name, work, location, phone, password, voice_note, video) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (name, work, location, phone, hashed_pw, voice_path, video_path)
        )
        conn.commit()
        worker_id = cursor.lastrowid
        conn.close()
        
        session['user_id'] = worker_id
        session['role'] = 'worker'
        session['name'] = name
        
        return jsonify({'message': 'Worker registered successfully', 'redirect': url_for('worker_dashboard')}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Phone number already registered. Please login.'}), 400
    except Exception as e:
        print("Error during worker signup:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        data = request.get_json() or {}
    except:
        return jsonify({'error': 'Invalid JSON data'}), 400

    phone = safe_str(data.get('phone'))
    password = safe_str(data.get('password'))
    role = safe_str(data.get('role'))

    if not all([phone, password, role]) or role not in ['user', 'worker']:
        return jsonify({'error': 'Valid phone, password, and role ("user" or "worker") are required'}), 400

    table = 'users' if role == 'user' else 'workers'
    
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f'SELECT * FROM {table} WHERE phone = ?', (phone,))
        user = cursor.fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['role'] = role
            session['name'] = user['name']
            if role == 'user':
                session['phone'] = user['phone']
            
            redirect_url = url_for('user_dashboard') if role == 'user' else url_for('worker_dashboard')
            return jsonify({'message': 'Login successful', 'redirect': redirect_url}), 200
        else:
            return jsonify({'error': 'Invalid phone or password'}), 401
    except Exception as e:
        print("Error during login:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout_api():
    session.clear()
    return jsonify({'redirect': url_for('home')}), 200

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# ---- DATA API ENDPOINTS ----

@app.route('/api/worker/edit', methods=['POST'])
def edit_worker_profile():
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401
        
    name = safe_str(request.form.get('name'))
    work = safe_str(request.form.get('work'))
    location = safe_str(request.form.get('location'))
    phone = safe_str(request.form.get('phone'))
    password = safe_str(request.form.get('password'))
    
    if not all([name, work, location, phone]):
        return jsonify({'error': 'Name, work, location, and phone are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number'}), 400
        
    worker_id = session['user_id']
    voice_file = request.files.get('voice_note')
    video_file = request.files.get('video')
    
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('SELECT voice_note, video FROM workers WHERE id = ?', (worker_id,))
        existing_media = cursor.fetchone()
        
        voice_path = existing_media[0]
        video_path = existing_media[1]
        
        if voice_file and voice_file.filename != '':
            if not allowed_file(voice_file.filename, ALLOWED_AUDIO_EXTENSIONS):
                return jsonify({'error': 'Invalid audio file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(voice_file.filename)}"
            voice_file.save(os.path.join(AUDIO_FOLDER, filename))
            voice_path = f'uploads/audio/{filename}'

        if video_file and video_file.filename != '':
            if not allowed_file(video_file.filename, ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid video file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(video_file.filename)}"
            video_file.save(os.path.join(VIDEO_FOLDER, filename))
            video_path = f'uploads/video/{filename}'

        if password:
            if len(password) < 6:
                return jsonify({'error': 'Password must be at least 6 characters'}), 400
            hashed_pw = generate_password_hash(password)
            cursor.execute(
                'UPDATE workers SET name=?, work=?, location=?, phone=?, password=?, voice_note=?, video=? WHERE id=?',
                (name, work, location, phone, hashed_pw, voice_path, video_path, worker_id)
            )
        else:
            cursor.execute(
                'UPDATE workers SET name=?, work=?, location=?, phone=?, voice_note=?, video=? WHERE id=?',
                (name, work, location, phone, voice_path, video_path, worker_id)
            )
            
        conn.commit()
        conn.close()
        session['name'] = name
        return jsonify({'message': 'Profile updated successfully', 'redirect': url_for('worker_dashboard')}), 200
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Phone number already registered by another user.'}), 400
    except Exception as e:
        print("Error updating profile:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/get_workers', methods=['GET'])
def get_workers():
    work_filter = safe_str(request.args.get('work'))
    location_filter = safe_str(request.args.get('location'))
    
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = '''
            SELECT w.id, w.name, w.work, w.location, w.phone, w.voice_note, w.video,
                   IFNULL(AVG(r.rating), 0) as avg_rating, COUNT(r.id) as review_count
            FROM workers w
            LEFT JOIN reviews r ON w.id = r.worker_id
            WHERE 1=1
        '''
        params = []
        if work_filter and work_filter.lower() != 'all':
            query += ' AND LOWER(w.work) LIKE ?'
            params.append(f'%{work_filter.lower()}%')
        if location_filter:
            query += ' AND LOWER(w.location) LIKE ?'
            params.append(f'%{location_filter.lower()}%')
            
        query += ' GROUP BY w.id ORDER BY avg_rating DESC, review_count DESC'
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        workers = [dict(row) for row in rows]
        conn.close()
        
        return jsonify(workers), 200
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs', methods=['POST'])
def post_job():
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401
        
    try:
        data = request.get_json() or {}
    except:
        return jsonify({'error': 'Invalid JSON data'}), 400
        
    service_type = safe_str(data.get('service_type'))
    description = safe_str(data.get('description'))
    location = safe_str(data.get('location'))
    
    if not service_type or not description or not location:
        return jsonify({'error': 'Service type, description, and location are required'}), 400
        
    user_id = session['user_id']
    user_name = session['name']
    user_phone = session.get('phone', 'Unknown')
    
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO jobs (user_id, user_name, user_phone, service_type, description, location) VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, user_name, user_phone, service_type, description, location)
        )
        conn.commit()
        conn.close()
        return jsonify({'message': 'Job posted successfully!'}), 201
    except Exception as e:
        print("Error posting job:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401
        
    work_type = safe_str(request.args.get('work'))
    location = safe_str(request.args.get('location'))
    worker_id = session['user_id']
    
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = 'SELECT * FROM jobs WHERE (status = "open" OR (status = "accepted" AND worker_id = ?))'
        params = [worker_id]
        
        if work_type:
            query += ' AND (LOWER(service_type) LIKE ? OR LOWER(description) LIKE ?)'
            params.extend([f'%{work_type.lower()}%', f'%{work_type.lower()}%'])
        if location:
            query += ' AND LOWER(location) LIKE ?'
            params.append(f'%{location.lower()}%')
            
        query += ' ORDER BY created_at DESC'
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        jobs = [dict(row) for row in rows]
        conn.close()
        
        return jsonify(jobs), 200
    except Exception as e:
        print("Error fetching jobs:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/customer', methods=['GET'])
def get_customer_jobs():
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401
        
    user_id = session['user_id']
    try:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Include worker details if accepted/completed
        cursor.execute('''
            SELECT j.*, w.name as worker_name, w.phone as worker_phone 
            FROM jobs j 
            LEFT JOIN workers w ON j.worker_id = w.id 
            WHERE j.user_id = ? 
            ORDER BY j.created_at DESC
        ''', (user_id,))
        rows = cursor.fetchall()
        jobs = [dict(row) for row in rows]
        conn.close()
        return jsonify(jobs), 200
    except Exception as e:
        print("Error fetching customer jobs:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/<int:job_id>/accept', methods=['POST'])
def accept_job(job_id):
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401
        
    worker_id = session['user_id']
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Ensure job is still open
        cursor.execute('SELECT status FROM jobs WHERE id = ?', (job_id,))
        job = cursor.fetchone()
        if not job:
            conn.close()
            return jsonify({'error': 'Job not found'}), 404
        if job[0] != 'open':
            conn.close()
            return jsonify({'error': 'Job is no longer open'}), 400
            
        cursor.execute('UPDATE jobs SET status = "accepted", worker_id = ? WHERE id = ?', (worker_id, job_id))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Job accepted successfully'}), 200
    except Exception as e:
        print("Error accepting job:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/<int:job_id>/status', methods=['POST'])
def update_job_status(job_id):
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401
        
    user_id = session['user_id']
    try:
        data = request.get_json() or {}
        new_status = safe_str(data.get('status'))
        if new_status not in ['open', 'completed', 'cancelled']:
            return jsonify({'error': 'Invalid status'}), 400
            
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Verify ownership
        cursor.execute('SELECT id FROM jobs WHERE id = ? AND user_id = ?', (job_id, user_id))
        if not cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Job not found or unauthorized'}), 404
            
        cursor.execute('UPDATE jobs SET status = ? WHERE id = ?', (new_status, job_id))
        conn.commit()
        conn.close()
        return jsonify({'message': f'Job marked as {new_status}'}), 200
    except Exception as e:
        print("Error updating job status:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/workers/<int:worker_id>/rate', methods=['POST'])
def rate_worker(worker_id):
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401
        
    try:
        data = request.get_json() or {}
        job_id = data.get('job_id')
        rating = int(data.get('rating', 0))
        review = safe_str(data.get('review', ''))
        
        if not job_id or not (1 <= rating <= 5):
            return jsonify({'error': 'Valid job ID and rating (1-5) are required'}), 400
            
        user_id = session['user_id']
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Check if user owns job, job is completed, and worker matches
        cursor.execute('SELECT status, worker_id FROM jobs WHERE id = ? AND user_id = ?', (job_id, user_id))
        job = cursor.fetchone()
        
        if not job or job[0] != 'completed' or job[1] != worker_id:
            conn.close()
            return jsonify({'error': 'Job must be completed to leave a review'}), 400
            
        # Prevent duplicate reviews for the same job
        cursor.execute('SELECT id FROM reviews WHERE job_id = ?', (job_id,))
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Review already submitted for this job'}), 400
            
        cursor.execute(
            'INSERT INTO reviews (job_id, worker_id, user_id, rating, review) VALUES (?, ?, ?, ?, ?)',
            (job_id, worker_id, user_id, rating, review)
        )
        conn.commit()
        conn.close()
        
        return jsonify({'message': 'Review submitted successfully'}), 201
    except ValueError:
        return jsonify({'error': 'Rating must be an integer'}), 400
    except Exception as e:
        print("Error submitting review:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(debug=debug_mode)
