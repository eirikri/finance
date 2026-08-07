import sqlite3
import os
import re
import time
import hmac
import hashlib
import urllib.parse
import datetime
import xml.etree.ElementTree as ET
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'finance.db')

COINTRACKING_KEY = os.environ.get('COINTRACKING_KEY')
COINTRACKING_SECRET = os.environ.get('COINTRACKING_SECRET')

_quote_cache = {}       # ticker -> (timestamp, data)
QUOTE_CACHE_TTL = 120

_ct_cache = {'data': None, 'ts': 0, 'error': None}
CT_CACHE_TTL = 900       # auto-refresh at most every 15 min
CT_MIN_FORCE_GAP = 200   # manual refresh throttled to ~once per 3.3 min (safe under 20/hr plan limit)

_history_cache = {}      # ticker -> (timestamp, {'series': [(ts, close), ...], 'error': ...})
HISTORY_CACHE_TTL = 900

_news_cache = {}         # ticker -> (timestamp, [{'title','link','source','published'}, ...])
NEWS_CACHE_TTL = 1800

CORP_SUFFIXES = re.compile(
    r'\b(ASA|AB\s*\(publ\)|AB|A/S|AS|PLC|Ltd\.?|Inc\.?|Corp(oration)?\.?|Co\.?|SE|NV|SA)\b\.?',
    re.IGNORECASE
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            balance REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            date TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            cost_type TEXT NOT NULL,
            necessary INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recurring_bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            day_of_month INTEGER,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            necessary INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            to_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            amount REAL NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS monthly_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            month TEXT NOT NULL,
            balance REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, month)
        );
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL UNIQUE,
            monthly_amount REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT,
            shares REAL NOT NULL,
            avg_price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'NOK',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS lottery_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INTEGER NOT NULL UNIQUE,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    # Migrate: ticket tracking for lottery. `tickets` is sticky - NULL means
    # "carry forward the last drawing's ticket count" (resolved at read time).
    try:
        conn.execute('ALTER TABLE lottery_entries ADD COLUMN tickets INTEGER')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE lottery_entries ADD COLUMN winning_tickets INTEGER')
        conn.commit()
    except Exception:
        pass
    # Migrate: add category column if not present
    try:
        conn.execute('ALTER TABLE transactions ADD COLUMN category TEXT')
        conn.commit()
    except Exception:
        pass
    # Migrate: add total_deposited for investment tracking
    try:
        conn.execute('ALTER TABLE accounts ADD COLUMN total_deposited REAL DEFAULT 0')
        conn.commit()
    except Exception:
        pass
    # Migrate: add monthly flag to transactions
    try:
        conn.execute('ALTER TABLE transactions ADD COLUMN monthly INTEGER NOT NULL DEFAULT 0')
        conn.commit()
    except Exception:
        pass
    conn.close()


@app.route('/')
def index():
    return render_template('index.html')


# Accounts
@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    conn = get_db()
    accounts = conn.execute('SELECT * FROM accounts ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(a) for a in accounts])


@app.route('/api/accounts', methods=['POST'])
def add_account():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO accounts (name, type, balance) VALUES (?, ?, ?)',
        (data['name'], data['type'], float(data.get('balance', 0)))
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:id>', methods=['PUT'])
def update_account(id):
    data = request.json
    conn = get_db()
    conn.execute(
        'UPDATE accounts SET name=?, type=?, balance=?, total_deposited=? WHERE id=?',
        (data['name'], data['type'], float(data['balance']), float(data.get('total_deposited', 0)), id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:id>', methods=['DELETE'])
def delete_account(id):
    conn = get_db()
    conn.execute('DELETE FROM accounts WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Transactions
@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    month = request.args.get('month')
    conn = get_db()
    if month:
        txns = conn.execute(
            '''SELECT t.*, a.name as account_name
               FROM transactions t
               LEFT JOIN accounts a ON t.account_id = a.id
               WHERE strftime('%Y-%m', t.date) = ?
               ORDER BY t.date DESC, t.id DESC''',
            (month,)
        ).fetchall()
    else:
        txns = conn.execute(
            '''SELECT t.*, a.name as account_name
               FROM transactions t
               LEFT JOIN accounts a ON t.account_id = a.id
               ORDER BY t.date DESC, t.id DESC
               LIMIT 200'''
        ).fetchall()
    conn.close()
    return jsonify([dict(t) for t in txns])


@app.route('/api/transactions', methods=['POST'])
def add_transaction():
    data = request.json
    category = (data.get('category') or '').strip() or None
    conn = get_db()
    if category:
        conn.execute('INSERT OR IGNORE INTO categories (name) VALUES (?)', (category,))
    conn.execute(
        'INSERT INTO transactions (account_id, date, description, amount, cost_type, necessary, category, monthly) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (
            data.get('account_id') or None,
            data['date'],
            data.get('description', ''),
            float(data['amount']),
            data['cost_type'],
            1 if data.get('necessary') else 0,
            category,
            1 if data.get('monthly') else 0,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:id>', methods=['PUT'])
def update_transaction(id):
    data = request.json
    category = (data.get('category') or '').strip() or None
    conn = get_db()
    if category:
        conn.execute('INSERT OR IGNORE INTO categories (name) VALUES (?)', (category,))
    conn.execute(
        'UPDATE transactions SET account_id=?, date=?, description=?, amount=?, cost_type=?, necessary=?, category=?, monthly=? WHERE id=?',
        (
            data.get('account_id') or None,
            data['date'],
            data.get('description', ''),
            float(data['amount']),
            data['cost_type'],
            1 if data.get('necessary') else 0,
            category,
            1 if data.get('monthly') else 0,
            id,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:id>', methods=['DELETE'])
def delete_transaction(id):
    conn = get_db()
    conn.execute('DELETE FROM transactions WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Categories
@app.route('/api/categories', methods=['GET'])
def get_categories():
    conn = get_db()
    cats = conn.execute('SELECT name FROM categories ORDER BY name').fetchall()
    conn.close()
    return jsonify([r['name'] for r in cats])


# Transfers
@app.route('/api/transfers', methods=['GET'])
def get_transfers():
    conn = get_db()
    rows = conn.execute(
        '''SELECT t.*, f.name as from_name, o.name as to_name
           FROM transfers t
           LEFT JOIN accounts f ON t.from_account_id = f.id
           LEFT JOIN accounts o ON t.to_account_id = o.id
           ORDER BY t.date DESC, t.id DESC
           LIMIT 50'''
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transfers', methods=['POST'])
def add_transfer():
    data = request.json
    from_id = data['from_account_id']
    to_id = data['to_account_id']
    amount = float(data['amount'])
    if from_id == to_id:
        return jsonify({'error': 'Same account'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400
    conn = get_db()
    conn.execute('UPDATE accounts SET balance = balance - ? WHERE id = ?', (amount, from_id))
    conn.execute('UPDATE accounts SET balance = balance + ? WHERE id = ?', (amount, to_id))
    conn.execute(
        'INSERT INTO transfers (from_account_id, to_account_id, amount, description, date) VALUES (?, ?, ?, ?, ?)',
        (from_id, to_id, amount, data.get('description', ''), data['date'])
    )
    # Track deposits/withdrawals for investment accounts
    to_acc = conn.execute('SELECT type FROM accounts WHERE id=?', (to_id,)).fetchone()
    if to_acc and to_acc['type'] == 'investment':
        conn.execute('UPDATE accounts SET total_deposited = COALESCE(total_deposited,0) + ? WHERE id=?', (amount, to_id))
    from_acc = conn.execute('SELECT type FROM accounts WHERE id=?', (from_id,)).fetchone()
    if from_acc and from_acc['type'] == 'investment':
        conn.execute('UPDATE accounts SET total_deposited = COALESCE(total_deposited,0) - ? WHERE id=?', (amount, from_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transfers/<int:id>', methods=['DELETE'])
def delete_transfer(id):
    conn = get_db()
    row = conn.execute('SELECT * FROM transfers WHERE id=?', (id,)).fetchone()
    if row:
        # Reverse deposit tracking for investment accounts
        to_acc = conn.execute('SELECT type FROM accounts WHERE id=?', (row['to_account_id'],)).fetchone()
        if to_acc and to_acc['type'] == 'investment':
            conn.execute('UPDATE accounts SET total_deposited = COALESCE(total_deposited,0) - ? WHERE id=?', (row['amount'], row['to_account_id']))
        from_acc = conn.execute('SELECT type FROM accounts WHERE id=?', (row['from_account_id'],)).fetchone()
        if from_acc and from_acc['type'] == 'investment':
            conn.execute('UPDATE accounts SET total_deposited = COALESCE(total_deposited,0) + ? WHERE id=?', (row['amount'], row['from_account_id']))
        conn.execute('UPDATE accounts SET balance = balance + ? WHERE id = ?', (row['amount'], row['from_account_id']))
        conn.execute('UPDATE accounts SET balance = balance - ? WHERE id = ?', (row['amount'], row['to_account_id']))
        conn.execute('DELETE FROM transfers WHERE id=?', (id,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Recurring bills
@app.route('/api/recurring', methods=['GET'])
def get_recurring():
    conn = get_db()
    bills = conn.execute(
        '''SELECT r.*, a.name as account_name
           FROM recurring_bills r
           LEFT JOIN accounts a ON r.account_id = a.id
           ORDER BY r.day_of_month, r.name'''
    ).fetchall()
    conn.close()
    return jsonify([dict(b) for b in bills])


@app.route('/api/recurring', methods=['POST'])
def add_recurring():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO recurring_bills (name, amount, day_of_month, account_id, necessary) VALUES (?, ?, ?, ?, ?)',
        (
            data['name'],
            float(data['amount']),
            data.get('day_of_month') or None,
            data.get('account_id') or None,
            1 if data.get('necessary', True) else 0
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/recurring/<int:id>', methods=['DELETE'])
def delete_recurring(id):
    conn = get_db()
    conn.execute('DELETE FROM recurring_bills WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Overview stats
@app.route('/api/overview', methods=['GET'])
def get_overview():
    month = request.args.get('month')
    conn = get_db()

    accounts = conn.execute('''
        SELECT a.*,
               a.balance + COALESCE((
                   SELECT SUM(t.amount)
                   FROM transactions t
                   WHERE t.account_id = a.id AND t.date > DATE(a.created_at)
               ), 0) AS effective_balance
        FROM accounts a
        ORDER BY a.name
    ''').fetchall()

    if month:
        txns = conn.execute(
            "SELECT * FROM transactions WHERE strftime('%Y-%m', date) = ?",
            (month,)
        ).fetchall()
    else:
        txns = []

    expenses = [t for t in txns if t['amount'] < 0]
    income = [t for t in txns if t['amount'] >= 0]

    total_expenses = sum(abs(t['amount']) for t in expenses)
    total_income = sum(t['amount'] for t in income)
    necessary = sum(abs(t['amount']) for t in expenses if t['necessary'])
    unnecessary = sum(abs(t['amount']) for t in expenses if not t['necessary'])
    recurring = sum(abs(t['amount']) for t in expenses if t['cost_type'] == 'recurring')
    variable = sum(abs(t['amount']) for t in expenses if t['cost_type'] == 'variable')
    monthly_expenses = sum(abs(t['amount']) for t in expenses if t['monthly'])

    # Savings rate and emergency fund
    savings_rate = round((1 - total_expenses / total_income) * 100, 1) if total_income > 0 else 0
    six_ago = (datetime.date.today() - datetime.timedelta(days=180)).strftime('%Y-%m')
    avg_rows = conn.execute(
        "SELECT strftime('%Y-%m', date) as m, SUM(ABS(amount)) as tot FROM transactions WHERE amount < 0 AND strftime('%Y-%m', date) >= ? GROUP BY m",
        (six_ago,)
    ).fetchall()
    avg_monthly_exp = sum(r['tot'] for r in avg_rows) / len(avg_rows) if avg_rows else (total_expenses or 1)
    liquid = sum(a['effective_balance'] for a in accounts if a['type'] in ('checking', 'savings'))
    emergency_fund_months = round(liquid / avg_monthly_exp, 1) if avg_monthly_exp > 0 else 0

    # Transfers to investment accounts count as expenses (variable, unnecessary)
    EXPENSE_TRANSFER_TARGETS = {'Anneli Sparekonto', 'Nordnet', 'Firi'}
    if month:
        transfer_rows = conn.execute(
            '''SELECT tr.amount
               FROM transfers tr
               JOIN accounts f ON tr.from_account_id = f.id
               JOIN accounts o ON tr.to_account_id = o.id
               WHERE strftime('%Y-%m', tr.date) = ?
                 AND o.name IN ('Anneli Sparekonto', 'Nordnet', 'Firi')
                 AND f.name != 'Sparekonto' ''',
            (month,)
        ).fetchall()
        transfer_total = sum(r['amount'] for r in transfer_rows)
        total_expenses += transfer_total
        unnecessary += transfer_total
        variable += transfer_total

    conn.close()
    return jsonify({
        'accounts': [dict(a) for a in accounts],
        'total_expenses': total_expenses,
        'total_income': total_income,
        'net': total_income - total_expenses,
        'necessary': necessary,
        'unnecessary': unnecessary,
        'recurring': recurring,
        'monthly_expenses': monthly_expenses,
        'variable': variable,
        'savings_rate': savings_rate,
        'emergency_fund_months': emergency_fund_months,
        'liquid': liquid,
    })


# Mark-to-market for investment accounts
@app.route('/api/accounts/<int:id>/mark-to-market', methods=['PUT'])
def mark_to_market(id):
    data = request.json
    conn = get_db()
    conn.execute(
        'UPDATE accounts SET balance=?, total_deposited=? WHERE id=?',
        (float(data['balance']), float(data.get('total_deposited', 0)), id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Monthly snapshots
@app.route('/api/snapshots', methods=['GET'])
def get_snapshots():
    conn = get_db()
    rows = conn.execute('''
        SELECT s.*, a.name as account_name
        FROM monthly_snapshots s
        JOIN accounts a ON s.account_id = a.id
        ORDER BY s.month DESC, a.name
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/snapshots', methods=['POST'])
def save_snapshot():
    data = request.json
    month = data['month']
    conn = get_db()
    accounts = conn.execute('''
        SELECT a.id,
               a.balance + COALESCE((
                   SELECT SUM(t.amount)
                   FROM transactions t
                   WHERE t.account_id = a.id AND t.date > DATE(a.created_at)
               ), 0) AS effective_balance
        FROM accounts a
    ''').fetchall()
    for acc in accounts:
        conn.execute('''
            INSERT OR REPLACE INTO monthly_snapshots (account_id, month, balance)
            VALUES (?, ?, ?)
        ''', (acc['id'], month, acc['effective_balance']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/snapshots/<month>', methods=['DELETE'])
def delete_snapshot(month):
    conn = get_db()
    conn.execute('DELETE FROM monthly_snapshots WHERE month=?', (month,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Monthly category trends
@app.route('/api/category-trends', methods=['GET'])
def get_category_trends():
    conn = get_db()
    rows = conn.execute('''
        SELECT strftime('%Y-%m', date) as month,
               COALESCE(category, 'Ukategorisert') as category,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as expenses,
               SUM(CASE WHEN amount >= 0 THEN amount ELSE 0 END) as income
        FROM transactions
        GROUP BY month, category
        ORDER BY month ASC
    ''').fetchall()
    conn.close()

    from collections import defaultdict
    data = [dict(r) for r in rows]
    months_all = sorted(set(r['month'] for r in data))
    cat_month = defaultdict(dict)
    for r in data:
        cat_month[r['category']][r['month']] = r['expenses']
    for r in data:
        idx = months_all.index(r['month'])
        window = months_all[max(0, idx - 2): idx + 1]
        vals = [cat_month[r['category']].get(m, 0) for m in window]
        r['rolling_3m'] = round(sum(vals) / len(vals), 0) if vals else 0

    data.sort(key=lambda x: (x['month'], -x['expenses']), reverse=True)
    return jsonify(data[:300])


# Budgets
@app.route('/api/budgets', methods=['GET'])
def get_budgets():
    conn = get_db()
    rows = conn.execute('SELECT * FROM budgets ORDER BY category').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/budgets', methods=['POST'])
def upsert_budget():
    data = request.json
    conn = get_db()
    conn.execute('''
        INSERT INTO budgets (category, monthly_amount) VALUES (?, ?)
        ON CONFLICT(category) DO UPDATE SET monthly_amount = excluded.monthly_amount
    ''', (data['category'], float(data['monthly_amount'])))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/budgets/<int:id>', methods=['DELETE'])
def delete_budget(id):
    conn = get_db()
    conn.execute('DELETE FROM budgets WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Projection + bill reconciliation
@app.route('/api/projection', methods=['GET'])
def get_projection():
    month = request.args.get('month', datetime.date.today().strftime('%Y-%m'))
    today = datetime.date.today()
    conn = get_db()

    bills = conn.execute('''
        SELECT r.*, a.name as account_name
        FROM recurring_bills r LEFT JOIN accounts a ON r.account_id = a.id
    ''').fetchall()

    txns = conn.execute(
        "SELECT * FROM transactions WHERE strftime('%Y-%m', date) = ?", (month,)
    ).fetchall()

    reconciled = []
    unlogged = []
    for bill in bills:
        match = None
        for t in txns:
            if t['amount'] >= 0:
                continue
            if bill['account_id'] and t['account_id'] != bill['account_id']:
                continue
            if abs(abs(t['amount']) - bill['amount']) <= bill['amount'] * 0.15 + 1:
                match = t
                break
        b = dict(bill)
        if match:
            b['txn_date'] = match['date']
            b['txn_amount'] = match['amount']
            reconciled.append(b)
        else:
            unlogged.append(b)

    try:
        year, mo = map(int, month.split('-'))
        is_current = (year == today.year and mo == today.month)
    except Exception:
        is_current = False

    future_bills = [b for b in unlogged if b['day_of_month'] and b['day_of_month'] > today.day] if is_current else unlogged
    past_due    = [b for b in unlogged if not b['day_of_month'] or b['day_of_month'] <= today.day] if is_current else []
    remaining   = sum(b['amount'] for b in future_bills)

    checking = conn.execute('''
        SELECT a.balance + COALESCE((
            SELECT SUM(t.amount) FROM transactions t
            WHERE t.account_id = a.id AND t.date > DATE(a.created_at)
        ), 0) AS eb
        FROM accounts a WHERE a.type = 'checking'
    ''').fetchall()
    current_checking = sum(r['eb'] for r in checking)
    projected = current_checking - remaining

    conn.close()
    return jsonify({
        'reconciled': reconciled,
        'unlogged': unlogged,
        'future_bills': future_bills,
        'past_due': past_due,
        'remaining_amount': remaining,
        'current_checking': current_checking,
        'projected_balance': projected,
        'month': month,
    })


# ---------------------------------------------------------------------------
# Investments: stocks (Yahoo Finance quotes) + crypto (CoinTracking API)
# ---------------------------------------------------------------------------

def fetch_quote(ticker, force=False):
    now = time.time()
    cached = _quote_cache.get(ticker)
    if not force and cached and now - cached[0] < QUOTE_CACHE_TTL:
        return cached[1]
    try:
        r = requests.get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=10
        )
        r.raise_for_status()
        meta = r.json()['chart']['result'][0]['meta']
        data = {
            'price': meta.get('regularMarketPrice'),
            'currency': meta.get('currency'),
            'name': meta.get('longName') or meta.get('shortName') or ticker,
        }
    except Exception as e:
        data = {'price': None, 'currency': None, 'name': None, 'error': str(e)}
    _quote_cache[ticker] = (now, data)
    return data


def fetch_fx_rate(from_ccy, to_ccy='NOK', force=False):
    if not from_ccy or from_ccy == to_ccy:
        return 1.0
    q = fetch_quote(f'{from_ccy}{to_ccy}=X', force=force)
    return q.get('price') or 1.0


def fetch_price_history(ticker, force=False):
    now = time.time()
    cached = _history_cache.get(ticker)
    if not force and cached and now - cached[0] < HISTORY_CACHE_TTL:
        return cached[1]
    try:
        r = requests.get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',
            params={'range': '1y', 'interval': '1d'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=10
        )
        r.raise_for_status()
        result = r.json()['chart']['result'][0]
        timestamps = result.get('timestamp') or []
        closes = result['indicators']['quote'][0].get('close') or []
        series = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        data = {'series': series, 'error': None}
    except Exception as e:
        data = {'series': [], 'error': str(e)}
    _history_cache[ticker] = (now, data)
    return data


def pct_change_over(series, days):
    if not series:
        return None
    latest_ts, latest_close = series[-1]
    target_ts = latest_ts - days * 86400
    candidates = [p for p in series if p[0] <= target_ts]
    base_close = (candidates[-1] if candidates else series[0])[1]
    if not base_close:
        return None
    return (latest_close - base_close) / base_close * 100


def compute_price_changes(ticker, force=False):
    hist = fetch_price_history(ticker, force=force)
    series = hist['series']
    if not series:
        return {'change24h': None, 'change7d': None, 'change30d': None, 'change1y': None, 'error': hist.get('error')}
    return {
        'change24h': pct_change_over(series, 1),
        'change7d': pct_change_over(series, 7),
        'change30d': pct_change_over(series, 30),
        'change1y': pct_change_over(series, 365),
        'error': None,
    }


def fetch_news(query, force=False):
    cache_key = query.lower()
    now = time.time()
    cached = _news_cache.get(cache_key)
    if not force and cached and now - cached[0] < NEWS_CACHE_TTL:
        return cached[1]
    items = []
    try:
        url = 'https://news.google.com/rss/search'
        r = requests.get(
            url,
            params={'q': f'{query} when:7d', 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=10
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall('.//item')[:8]:
            title_full = (item.findtext('title') or '').strip()
            title, _, source = title_full.rpartition(' - ')
            items.append({
                'title': title or title_full,
                'source': source or None,
                'link': item.findtext('link'),
                'published': item.findtext('pubDate'),
            })
    except Exception:
        items = []
    _news_cache[cache_key] = (now, items)
    return items


def stock_news_query(name, ticker):
    base = CORP_SUFFIXES.sub('', name or '').strip() if name else None
    ticker_base = ticker.split('.')[0]
    return base if base and len(base) > 2 else ticker_base


@app.route('/api/stocks/details/<ticker>', methods=['GET'])
def stock_details(ticker):
    force = request.args.get('refresh') == '1'
    name = request.args.get('name') or ''
    changes = compute_price_changes(ticker, force=force)
    query = stock_news_query(name, ticker)
    news = fetch_news(query, force=force)
    if not news and query != ticker.split('.')[0]:
        news = fetch_news(ticker.split('.')[0], force=force)
    return jsonify({'changes': changes, 'news': news})


@app.route('/api/crypto/details/<coin>', methods=['GET'])
def crypto_details(coin):
    force = request.args.get('refresh') == '1'
    changes = compute_price_changes(f'{coin}-USD', force=force)
    return jsonify({'change1y': changes.get('change1y')})


@app.route('/api/stocks', methods=['GET'])
def get_stocks():
    conn = get_db()
    rows = conn.execute('SELECT * FROM stock_holdings ORDER BY ticker').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/stocks', methods=['POST'])
def add_stock():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO stock_holdings (ticker, name, shares, avg_price, currency) VALUES (?, ?, ?, ?, ?)',
        (
            data['ticker'].strip().upper(),
            (data.get('name') or '').strip(),
            float(data['shares']),
            float(data['avg_price']),
            data.get('currency', 'NOK'),
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/stocks/<int:id>', methods=['PUT'])
def update_stock(id):
    data = request.json
    conn = get_db()
    conn.execute(
        'UPDATE stock_holdings SET ticker=?, name=?, shares=?, avg_price=?, currency=? WHERE id=?',
        (
            data['ticker'].strip().upper(),
            (data.get('name') or '').strip(),
            float(data['shares']),
            float(data['avg_price']),
            data.get('currency', 'NOK'),
            id,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/stocks/<int:id>', methods=['DELETE'])
def delete_stock(id):
    conn = get_db()
    conn.execute('DELETE FROM stock_holdings WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/stocks/overview', methods=['GET'])
def stocks_overview():
    force = request.args.get('refresh') == '1'
    conn = get_db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM stock_holdings ORDER BY ticker').fetchall()]
    conn.close()

    out = []
    total_value_nok = 0.0
    total_cost_nok = 0.0
    for h in rows:
        q = fetch_quote(h['ticker'], force=force)
        price = q.get('price')
        quote_ccy = q.get('currency') or h['currency']
        fx_quote = fetch_fx_rate(quote_ccy, 'NOK', force=force)
        fx_cost = fetch_fx_rate(h['currency'], 'NOK', force=force)

        market_value_native = (price or 0) * h['shares']
        cost_native = h['avg_price'] * h['shares']
        market_value_nok = market_value_native * fx_quote
        cost_nok = cost_native * fx_cost
        gain_nok = market_value_nok - cost_nok
        gain_pct = (gain_nok / cost_nok * 100) if cost_nok else 0.0

        total_value_nok += market_value_nok
        total_cost_nok += cost_nok

        out.append({
            **h,
            'price': price,
            'quote_currency': quote_ccy,
            'name': h['name'] or q.get('name'),
            'market_value_nok': market_value_nok,
            'cost_nok': cost_nok,
            'gain_nok': gain_nok,
            'gain_pct': gain_pct,
            'error': q.get('error'),
        })

    total_gain_nok = total_value_nok - total_cost_nok
    return jsonify({
        'holdings': out,
        'total_value_nok': total_value_nok,
        'total_cost_nok': total_cost_nok,
        'total_gain_nok': total_gain_nok,
        'total_gain_pct': (total_gain_nok / total_cost_nok * 100) if total_cost_nok else 0.0,
    })


# ---------------------------------------------------------------------------
# Shuffle.com weekly lottery tracker
# ---------------------------------------------------------------------------

def _with_effective_tickets(rows):
    """Tickets are sticky: a NULL value means 'same as the last drawing that set one'.
    Resolves that carry-forward and adds win_pct (winning_tickets / effective_tickets)."""
    out = []
    last_tickets = None
    for r in rows:
        d = dict(r)
        if d['tickets'] is not None:
            last_tickets = d['tickets']
        d['effective_tickets'] = last_tickets
        if d['winning_tickets'] is not None and last_tickets:
            d['win_pct'] = d['winning_tickets'] / last_tickets * 100
        else:
            d['win_pct'] = None
        out.append(d)
    return out


@app.route('/api/lottery', methods=['GET'])
def get_lottery():
    conn = get_db()
    rows = conn.execute('SELECT * FROM lottery_entries ORDER BY week_number').fetchall()
    conn.close()
    return jsonify(_with_effective_tickets(rows))


@app.route('/api/lottery', methods=['POST'])
def add_lottery_entry():
    data = request.json
    conn = get_db()
    next_week = conn.execute('SELECT COALESCE(MAX(week_number), 0) + 1 AS w FROM lottery_entries').fetchone()['w']
    conn.execute(
        'INSERT INTO lottery_entries (week_number, date, amount, tickets, winning_tickets) VALUES (?, ?, ?, ?, ?)',
        (
            next_week,
            data['date'],
            float(data['amount']),
            int(data['tickets']) if data.get('tickets') not in (None, '') else None,
            int(data['winning_tickets']) if data.get('winning_tickets') not in (None, '') else None,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/lottery/<int:id>', methods=['PUT'])
def update_lottery_entry(id):
    data = request.json
    conn = get_db()
    conn.execute(
        'UPDATE lottery_entries SET date=?, amount=?, tickets=?, winning_tickets=? WHERE id=?',
        (
            data['date'],
            float(data['amount']),
            int(data['tickets']) if data.get('tickets') not in (None, '') else None,
            int(data['winning_tickets']) if data.get('winning_tickets') not in (None, '') else None,
            id,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/lottery/<int:id>', methods=['DELETE'])
def delete_lottery_entry(id):
    conn = get_db()
    conn.execute('DELETE FROM lottery_entries WHERE id=?', (id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/lottery/overview', methods=['GET'])
def lottery_overview():
    conn = get_db()
    rows = conn.execute('SELECT * FROM lottery_entries ORDER BY week_number').fetchall()
    conn.close()
    amounts = [r['amount'] for r in rows]
    weeks = len(amounts)
    total = sum(amounts)

    resolved = _with_effective_tickets(rows)
    win_pcts = [r['win_pct'] for r in resolved if r['win_pct'] is not None]
    avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else None
    current_tickets = resolved[-1]['effective_tickets'] if resolved else None

    return jsonify({
        'weeks': weeks,
        'total': total,
        'biggest': max(amounts) if amounts else 0,
        'average': total / weeks if weeks else 0,
        'avg_win_pct': avg_win_pct,
        'current_tickets': current_tickets,
    })


def cointracking_call(method, extra=None):
    if not COINTRACKING_KEY or not COINTRACKING_SECRET:
        return None, 'CoinTracking API-nøkkel er ikke konfigurert'
    params = {'method': method, 'nonce': str(int(time.time() * 1000))}
    if extra:
        params.update(extra)
    post_data = urllib.parse.urlencode(params)
    sign = hmac.new(COINTRACKING_SECRET.encode(), post_data.encode(), hashlib.sha512).hexdigest()
    headers = {
        'Key': COINTRACKING_KEY,
        'Sign': sign,
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    try:
        r = requests.post('https://cointracking.info/api/v1/', data=post_data, headers=headers, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        return None, str(e)
    if payload.get('success') != 1:
        return None, payload.get('error') or 'Ukjent feil fra CoinTracking'
    return payload, None


@app.route('/api/crypto', methods=['GET'])
def get_crypto():
    force = request.args.get('refresh') == '1'
    now = time.time()
    age = (now - _ct_cache['ts']) if _ct_cache['ts'] else None

    should_fetch = _ct_cache['data'] is None or (age is not None and age > CT_CACHE_TTL)
    if force and (age is None or age > CT_MIN_FORCE_GAP):
        should_fetch = True

    if should_fetch:
        payload, err = cointracking_call('getBalance')
        gains_payload, gains_err = cointracking_call('getGains') if payload else (None, None)
        if payload:
            gains = (gains_payload or {}).get('gains', {})
            details = payload.get('details', {})
            coins = []
            for sym, c in details.items():
                value = float(c.get('value_fiat') or 0)
                if abs(value) < 1:
                    continue
                g = gains.get(sym, {})
                coins.append({
                    'coin': sym,
                    'amount': float(c.get('amount') or 0),
                    'price': float(c.get('price_fiat') or 0),
                    'value': value,
                    'change24h': float(c.get('change24h') or 0),
                    'change7d': float(c.get('change7d') or 0),
                    'change30d': float(c.get('change30d') or 0),
                    'cost': float(g.get('cost') or 0) if g else None,
                    'unrealized': float(g.get('unrealized') or 0) if g else None,
                    'realized': float(g.get('realized') or 0) if g else None,
                })
            coins.sort(key=lambda c: -c['value'])
            account_ccy = payload.get('account_currency', 'USD')
            fx_to_nok = fetch_fx_rate(account_ccy, 'NOK')
            total_value = float(payload.get('summary', {}).get('value_fiat') or 0)

            total_unrealized = None
            total_realized = None
            if gains:
                total_unrealized = sum(float(g.get('unrealized') or 0) for g in gains.values())
                total_realized = sum(float(g.get('realized') or 0) for g in gains.values())

            _ct_cache['data'] = {
                'account_currency': account_ccy,
                'total_value': total_value,
                'total_value_nok': total_value * fx_to_nok,
                'total_unrealized_nok': total_unrealized * fx_to_nok if total_unrealized is not None else None,
                'total_realized_nok': total_realized * fx_to_nok if total_realized is not None else None,
                'gains_error': gains_err,
                'fx_to_nok': fx_to_nok,
                'coins': coins,
            }
            _ct_cache['ts'] = now
            _ct_cache['error'] = None
        else:
            _ct_cache['error'] = err

    if _ct_cache['data'] is None:
        return jsonify({'error': _ct_cache['error'] or 'Ingen data ennå'}), 502

    return jsonify({
        **_ct_cache['data'],
        'fetched_at': _ct_cache['ts'],
        'stale_error': _ct_cache['error'],
    })


_ct_hist_cache = {'data': None, 'ts': 0, 'error': None}
CT_HIST_CACHE_TTL = 21600  # historical portfolio value barely changes; refresh every 6h at most


def downsample(points, max_points=300):
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    indices = sorted(set(int(i * step) for i in range(max_points)))
    if indices[-1] != len(points) - 1:
        indices.append(len(points) - 1)
    return [points[i] for i in indices]


@app.route('/api/crypto/history-chart', methods=['GET'])
def crypto_history_chart():
    force = request.args.get('refresh') == '1'
    now = time.time()
    age = (now - _ct_hist_cache['ts']) if _ct_hist_cache['ts'] else None

    should_fetch = _ct_hist_cache['data'] is None or (age is not None and age > CT_HIST_CACHE_TTL)
    if force and (age is None or age > CT_MIN_FORCE_GAP):
        should_fetch = True

    if should_fetch:
        payload, err = cointracking_call('getHistoricalSummary')
        if payload:
            coins_hist = payload.get('historical', {}).get('Coins', {})
            account_ccy = payload.get('display_currency', 'USD')
            fx_to_nok = fetch_fx_rate(account_ccy, 'NOK')
            points = sorted(
                ({'ts': int(ts), 'value_nok': float(v) * fx_to_nok} for ts, v in coins_hist.items()),
                key=lambda p: p['ts']
            )
            points = downsample(points, 300)
            _ct_hist_cache['data'] = {'points': points, 'account_currency': account_ccy}
            _ct_hist_cache['ts'] = now
            _ct_hist_cache['error'] = None
        else:
            _ct_hist_cache['error'] = err

    if _ct_hist_cache['data'] is None:
        return jsonify({'error': _ct_hist_cache['error'] or 'Ingen data ennå'}), 502

    return jsonify({
        **_ct_hist_cache['data'],
        'fetched_at': _ct_hist_cache['ts'],
        'stale_error': _ct_hist_cache['error'],
    })


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', debug=True, port=5002)
