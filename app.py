import sqlite3
import os
import datetime
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'finance.db')


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
    ''')
    conn.commit()
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


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', debug=True, port=5002)
