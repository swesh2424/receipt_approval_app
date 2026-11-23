import json
import os
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("RECEIPT_DATA_DIR", str(BASE / "instance"))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(exist_ok=True)
secret_path = DATA / "session.key"
if not secret_path.exists():
    try:
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or secret_path.read_text(),
                  MAX_CONTENT_LENGTH=12 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1')
executor = ThreadPoolExecutor(max_workers=2)
STATUSES = ['Uploaded', 'Processing', 'Review', 'Submitted', 'Approved', 'Rejected']


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    db = sqlite3.connect(DATA / 'receipts.db', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def get_db():
    if 'db' not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def event(db, receipt_id, actor, action, comment=''):
    db.execute('INSERT INTO events(receipt_id,actor,action,comment,created_at) VALUES(?,?,?,?,?)',
               (receipt_id, actor, action, comment, now()))


def init_db():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('employee','manager')));
        CREATE TABLE IF NOT EXISTS receipts(
            id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES users(id),
            filename TEXT NOT NULL, storage_name TEXT NOT NULL, mime TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Uploaded', merchant TEXT NOT NULL DEFAULT '',
            purchase_date TEXT NOT NULL DEFAULT '', total TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '', tax TEXT NOT NULL DEFAULT '',
            line_items TEXT NOT NULL DEFAULT '[]', extraction_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL REFERENCES receipts(id),
            actor TEXT NOT NULL, action TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL);
        """)
        for email, role, env, default in [
            ('employee@example.com', 'employee', 'EMPLOYEE_PASSWORD', 'employee-demo'),
            ('manager@example.com', 'manager', 'MANAGER_PASSWORD', 'manager-demo')]:
            db.execute('INSERT OR IGNORE INTO users(email,password,role) VALUES(?,?,?)',
                       (email, generate_password_hash(os.environ.get(env, default)), role))
        stale = db.execute("SELECT id FROM receipts WHERE status IN ('Uploaded','Processing')").fetchall()
        for row in stale:
            db.execute("UPDATE receipts SET status='Review', extraction_error=?, updated_at=? WHERE id=?",
                       ('Extraction interrupted by restart. Enter the receipt details manually.', now(), row['id']))
            event(db, row['id'], 'System', 'Review', 'Interrupted extraction; manual entry required.')


@app.before_request
def load_user_and_check_csrf():
    g.user = get_db().execute('SELECT * FROM users WHERE id=?', (session.get('user_id'),)).fetchone()
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(32)
    if request.method == 'POST':
        token = request.form.get('csrf', '')
        if not token or not secrets.compare_digest(token, session['csrf']):
            abort(400, 'Invalid CSRF token. Reload the page and try again.')


def login_required(role=None):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for('login'))
            if role and g.user['role'] != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def receipt_or_404(receipt_id):
    row = get_db().execute('SELECT r.*,u.email FROM receipts r JOIN users u ON u.id=r.employee_id WHERE r.id=?',
                           (receipt_id,)).fetchone()
    if not row:
        abort(404)
    if g.user['role'] != 'manager' and row['employee_id'] != g.user['id']:
        abort(403)
    if g.user['role'] == 'manager' and row['status'] not in ('Submitted', 'Approved', 'Rejected'):
        abort(403)
    return row


class MockExtractor:
    def extract(self, path):
        # Deliberately synthetic: this adapter DOES NOT read the uploaded receipt.
        return dict(merchant='Demo Merchant — replace me', purchase_date=date.today().isoformat(),
                    total='24.00', currency='USD', tax='2.00',
                    line_items=[{'description': 'Example item — replace me', 'quantity': '2', 'amount': '22.00'}])


extractor = MockExtractor()


def process_receipt(receipt_id):
    try:
        with connect() as db:
            row = db.execute('SELECT * FROM receipts WHERE id=?', (receipt_id,)).fetchone()
            if not row or row['status'] != 'Uploaded':
                return
            db.execute("UPDATE receipts SET status='Processing', updated_at=? WHERE id=?", (now(), receipt_id))
            event(db, receipt_id, 'System', 'Processing', 'Mock extraction started.')
        result = extractor.extract(UPLOADS / row['storage_name'])
        with connect() as db:
            db.execute("""UPDATE receipts SET status='Review',merchant=?,purchase_date=?,total=?,currency=?,tax=?,
                        line_items=?,extraction_error='',updated_at=? WHERE id=? AND status='Processing' """,
                       (result['merchant'], result['purchase_date'], result['total'], result['currency'], result['tax'],
                        json.dumps(result.get('line_items', [])), now(), receipt_id))
            event(db, receipt_id, 'System', 'Review', 'Synthetic demo values ready. Verify every field against the receipt.')
    except Exception:
        app.logger.exception('Extraction failed for receipt %s', receipt_id)
        with connect() as db:
            db.execute("UPDATE receipts SET status='Review',extraction_error=?,updated_at=? WHERE id=?",
                       ('Extraction failed. Please enter the details manually.', now(), receipt_id))
            event(db, receipt_id, 'System', 'Review', 'Extraction failed; manual entry required.')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = get_db().execute('SELECT * FROM users WHERE email=?', (request.form.get('email', '').strip().lower(),)).fetchone()
        if user and check_password_hash(user['password'], request.form.get('password', '')):
            session.clear()
            session['user_id'] = user['id']
            session['csrf'] = secrets.token_hex(32)
            return redirect(url_for('index'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.get('/')
@login_required()
def index():
    db = get_db()
    if g.user['role'] == 'employee':
        rows = db.execute('SELECT r.*,u.email FROM receipts r JOIN users u ON u.id=r.employee_id WHERE employee_id=? ORDER BY r.id DESC', (g.user['id'],)).fetchall()
    else:
        rows = db.execute("SELECT r.*,u.email FROM receipts r JOIN users u ON u.id=r.employee_id WHERE status IN ('Submitted','Approved','Rejected') ORDER BY CASE status WHEN 'Submitted' THEN 0 ELSE 1 END,r.id DESC").fetchall()
    return render_template('index.html', receipts=rows)


@app.post('/upload')
@login_required('employee')
def upload():
    f = request.files.get('receipt')
    if not f or not f.filename:
        flash('Choose an image or PDF.', 'error')
        return redirect(url_for('index'))
    head = f.stream.read(16)
    f.stream.seek(0)
    if head.startswith(b'%PDF-'):
        ext, mime = '.pdf', 'application/pdf'
    elif head.startswith(b'\x89PNG\r\n\x1a\n'):
        ext, mime = '.png', 'image/png'
    elif head.startswith(b'\xff\xd8\xff'):
        ext, mime = '.jpg', 'image/jpeg'
    elif head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        ext, mime = '.webp', 'image/webp'
    else:
        flash('Unsupported file. Use a PDF, JPEG, PNG, or WebP.', 'error')
        return redirect(url_for('index'))
    storage_name = secrets.token_hex(24) + ext
    f.save(UPLOADS / storage_name)
    try:
        with connect() as db:
            cursor = db.execute('INSERT INTO receipts(employee_id,filename,storage_name,mime,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                                (g.user['id'], Path(f.filename).name[:200], storage_name, mime, now(), now()))
            receipt_id = cursor.lastrowid
            event(db, receipt_id, g.user['email'], 'Uploaded')
    except Exception:
        (UPLOADS / storage_name).unlink(missing_ok=True)
        raise
    executor.submit(process_receipt, receipt_id)
    return redirect(url_for('detail', receipt_id=receipt_id))


def money(raw, name, optional=False):
    raw = raw.strip()
    if optional and not raw:
        return ''
    try:
        value = Decimal(raw)
        if not value.is_finite() or value < 0 or value > Decimal('999999999.99') or value != value.quantize(Decimal('.01')):
            raise ValueError()
        return format(value, '.2f')
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name} must be a nonnegative amount with at most two decimal places.')


def validate_fields(form):
    merchant = form.get('merchant', '').strip()
    if not merchant or len(merchant) > 200:
        raise ValueError('Merchant is required (maximum 200 characters).')
    purchase_date = form.get('purchase_date', '')
    try:
        date.fromisoformat(purchase_date)
    except ValueError:
        raise ValueError('Enter a valid purchase date.')
    currency = form.get('currency', '').strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError('Currency must be a three-letter code, such as USD or EUR.')
    total = money(form.get('total', ''), 'Total')
    tax = money(form.get('tax', ''), 'Tax', optional=True)
    if tax and Decimal(tax) > Decimal(total):
        raise ValueError('Tax cannot exceed the total.')
    try:
        items = json.loads(form.get('line_items', '[]'))
    except (ValueError, TypeError):
        raise ValueError('Line items must be valid JSON.')
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError('Line items must be a JSON array with at most 200 entries.')
    clean = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Each line item must be an object.')
        description = str(item.get('description', '')).strip()
        if not description or len(description) > 500:
            raise ValueError('Each line item needs a description of at most 500 characters.')
        quantity = str(item.get('quantity', '1'))
        try:
            q = Decimal(quantity)
            if not q.is_finite() or q <= 0 or q > 1000000:
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError('Line-item quantities must be positive numbers, at most 1000000.')
        clean.append(dict(description=description, quantity=quantity, amount=money(str(item.get('amount', '')), 'Line-item amount')))
    return merchant, purchase_date, total, currency, tax, json.dumps(clean)


@app.route('/receipts/<int:receipt_id>', methods=['GET', 'POST'])
@login_required()
def detail(receipt_id):
    row = receipt_or_404(receipt_id)
    if request.method == 'POST':
        if g.user['role'] != 'employee' or row['status'] not in ('Review', 'Rejected'):
            abort(403)
        action = request.form.get('action')
        if action not in ('save', 'submit'):
            abort(400)
        try:
            fields = validate_fields(request.form)
        except ValueError as exc:
            flash(str(exc), 'error')
            draft = dict(row)
            for key in ('merchant', 'purchase_date', 'total', 'currency', 'tax', 'line_items'):
                draft[key] = request.form.get(key, '')
            events = get_db().execute('SELECT * FROM events WHERE receipt_id=? ORDER BY id DESC', (receipt_id,)).fetchall()
            return render_template('detail.html', r=draft, events=events, editable=True, items_json=draft['line_items']), 400
        status = 'Submitted' if action == 'submit' else 'Review'
        with connect() as db:
            cur = db.execute("""UPDATE receipts SET merchant=?,purchase_date=?,total=?,currency=?,tax=?,line_items=?,status=?,updated_at=?
                          WHERE id=? AND employee_id=? AND status IN ('Review','Rejected')""",
                          (*fields, status, now(), receipt_id, g.user['id']))
            if cur.rowcount != 1:
                abort(409, 'Receipt changed. Reload and try again.')
            event(db, receipt_id, g.user['email'], 'Submitted' if action == 'submit' else 'Saved')
        flash('Submitted for manager approval.' if action == 'submit' else 'Changes saved.', 'success')
        return redirect(url_for('detail', receipt_id=receipt_id))
    events = get_db().execute('SELECT * FROM events WHERE receipt_id=? ORDER BY id DESC', (receipt_id,)).fetchall()
    return render_template('detail.html', r=row, events=events,
                           editable=g.user['role'] == 'employee' and row['status'] in ('Review', 'Rejected'),
                           items_json=json.dumps(json.loads(row['line_items']), indent=2))


@app.post('/receipts/<int:receipt_id>/decision')
@login_required('manager')
def decision(receipt_id):
    receipt_or_404(receipt_id)
    action = request.form.get('action')
    comment = request.form.get('comment', '').strip()
    if action not in ('approve', 'reject'):
        abort(400)
    if len(comment) > 2000 or (action == 'reject' and not comment):
        flash('Rejection requires a comment. Comments may contain at most 2000 characters.', 'error')
        return redirect(url_for('detail', receipt_id=receipt_id))
    status = 'Approved' if action == 'approve' else 'Rejected'
    with connect() as db:
        cur = db.execute("UPDATE receipts SET status=?,updated_at=? WHERE id=? AND status='Submitted'", (status, now(), receipt_id))
        if cur.rowcount != 1:
            abort(409, 'Only pending submissions can be reviewed. Reload this page.')
        event(db, receipt_id, g.user['email'], status, comment)
    flash(f'Receipt {status.lower()}.', 'success')
    return redirect(url_for('detail', receipt_id=receipt_id))


@app.get('/receipts/<int:receipt_id>/file')
@login_required()
def receipt_file(receipt_id):
    row = receipt_or_404(receipt_id)
    response = send_file(UPLOADS / row['storage_name'], mimetype=row['mime'],
                         as_attachment=True, download_name=row['filename'])
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.after_request
def secure_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.errorhandler(413)
def too_large(error):
    return render_template('error.html', message='Upload too large. Maximum request size is 12 MB.'), 413


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '5000')), debug=False)
