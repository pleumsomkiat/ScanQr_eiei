from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import csv
import os
import socket
import sqlite3
from datetime import datetime, timedelta
import requests

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'attendance.db')
STUDENTS_FILE = os.path.join(BASE_DIR, 'students.csv')
QR_TOKEN_TTL_SECONDS = 60
REMOTE_API_BASE_URL = os.environ.get('REMOTE_API_BASE_URL', 'http://127.0.0.1:3000').rstrip('/')
LAST_SCAN = {"student_id": "รอสแกน QR...", "student_name": "กำลังรอการเช็คชื่อ..."}
QR_TOKEN_MAP = {}


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_attendance_table():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            student_id TEXT,
            name TEXT,
            status TEXT
        )
        '''
    )
    conn.commit()
    conn.close()


def load_students():
    students = {}
    if not os.path.exists(STUDENTS_FILE):
        return students

    with open(STUDENTS_FILE, 'r', encoding='utf-8-sig', newline='') as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            student_id = str(row.get('student_id', '')).strip()
            name = str(row.get('name', '')).strip()
            if student_id and name:
                students[student_id] = {
                    "student_id": student_id,
                    "name": name,
                }
    return students


def get_ip_address():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        sock.close()
    return ip


def purge_expired_qr_tokens():
    now = datetime.utcnow()
    expired_tokens = [
        token for token, payload in QR_TOKEN_MAP.items()
        if payload.get("expires_at") <= now
    ]
    for token in expired_tokens:
        QR_TOKEN_MAP.pop(token, None)


def fetch_remote_history():
    if not REMOTE_API_BASE_URL:
        return None

    try:
        response = requests.get(f"{REMOTE_API_BASE_URL}/api/report", timeout=5)
        data = response.json()
        if not response.ok or not isinstance(data, list):
            return None

        students = load_students()
        normalized = []
        for item in data:
            user_id = str(item.get('user_id', '')).strip()
            name = (
                item.get('full_name')
                or item.get('name')
                or students.get(user_id, {}).get('name')
                or user_id
            )
            normalized.append({
                "date": item.get('attend_date') or item.get('date'),
                "time": item.get('time'),
                "name": name,
            })

        normalized.sort(
            key=lambda row: ((row.get("date") or ""), (row.get("time") or "")),
            reverse=True,
        )
        return normalized[:10]
    except (requests.RequestException, ValueError):
        return None


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    sid = str(data.get('id', '')).strip()
    user = load_students().get(sid)

    if user:
        return jsonify({"status": "success", "name": user['name']})
    return jsonify({"status": "error", "message": "ไม่พบรหัสนักศึกษา"})


@app.route('/update_attendance_status', methods=['POST'])
def update_status():
    global LAST_SCAN
    data = request.get_json(silent=True) or {}
    LAST_SCAN = {
        "student_id": data.get("student_id"),
        "student_name": data.get("student_name")
    }
    return jsonify({"status": "success"})


@app.route('/get_last_student', methods=['GET'])
def get_last_student():
    return jsonify(LAST_SCAN)


@app.route('/get_history', methods=['GET'])
def get_history():
    try:
        remote_history = fetch_remote_history()
        if remote_history is not None:
            return jsonify(remote_history)

        ensure_attendance_table()
        conn = get_db_connection()
        records = conn.execute(
            'SELECT date, time, name FROM attendance ORDER BY id DESC LIMIT 10'
        ).fetchall()
        conn.close()
        return jsonify([dict(row) for row in records])
    except Exception:
        return jsonify([])


@app.route('/update_qr', methods=['POST'])
def update_qr():
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '')).strip()
    student_id = str(data.get('student_id', '')).strip()
    expires_in = data.get('expires_in', QR_TOKEN_TTL_SECONDS)

    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = QR_TOKEN_TTL_SECONDS

    expires_in = max(1, min(expires_in, 300))
    purge_expired_qr_tokens()

    if token and student_id:
        QR_TOKEN_MAP[token] = {
            "student_id": student_id,
            "expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
        }
    return jsonify({"status": "success", "expires_in": expires_in})


@app.route('/resolve_qr', methods=['GET'])
def resolve_qr():
    purge_expired_qr_tokens()
    token = request.args.get('token')
    if not token:
        return jsonify({"status": "error", "message": "missing token"}), 400

    payload = QR_TOKEN_MAP.pop(str(token), None)
    if payload:
        return jsonify({"status": "success", "student_id": payload["student_id"]})
    return jsonify({"status": "error", "message": "not found"}), 404


if __name__ == '__main__':
    ensure_attendance_table()
    ip = get_ip_address()
    print("\n" + "=" * 50)
    print(f"SERVER RUNNING AT: http://{ip}:5000")
    print("=" * 50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
