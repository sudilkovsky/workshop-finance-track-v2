#!/usr/bin/env python3
"""Local SQLite expense editor. Run: python3 app.py"""
import argparse
import datetime
import json
import math
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'finance.sqlite'
DEFAULT_CATEGORIES = ('Продукти', 'Кафе та ресторани', 'Транспорт',
                      'Житло та комунальні', 'Здоров’я', 'Покупки',
                      'Розваги', 'Освіта', 'Інше')


def connect():
    conn = sqlite3.connect(f'{DB.as_uri()}?mode=rwc', uri=True)
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def initialize():
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
        )''')
        # Seed only once: later renames or deactivation must survive restarts.
        if not conn.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
            conn.executemany('INSERT INTO categories (name, sort_order) VALUES (?, ?)',
                             [(name, order) for order, name in enumerate(DEFAULT_CATEGORIES)])
        conn.execute('''CREATE TABLE IF NOT EXISTS expenses (
            spent_on TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            payment_method TEXT NOT NULL
        )''')
        conn.execute('SELECT spent_on, amount, description, payment_method FROM expenses LIMIT 0')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(expenses)')}
        if 'transaction_type' not in columns:
            conn.execute("""ALTER TABLE expenses ADD COLUMN transaction_type TEXT NOT NULL
                DEFAULT 'expense' CHECK (transaction_type IN ('expense', 'income', 'refund'))""")
        conn.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK (id=1), amount REAL NOT NULL CHECK (amount>=0))')
        conn.execute('INSERT OR IGNORE INTO budget VALUES (1, 10000)')
        if 'category_id' not in columns:
            conn.execute('ALTER TABLE expenses ADD COLUMN category_id INTEGER REFERENCES categories(id)')
        conn.execute('CREATE INDEX IF NOT EXISTS expenses_category_id ON expenses(category_id)')
        if 'budget_group' not in columns:
            conn.execute("""ALTER TABLE expenses ADD COLUMN budget_group TEXT
                CHECK (budget_group IN ('Fixed', 'Investments', 'Savings', 'Rich life'))""")


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Некоректні дані.')
    date = datetime.date.fromisoformat(str(data.get('spent_on', ''))).isoformat()
    amount = float(data.get('amount', ''))
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('Сума має бути більшою за нуль.')
    values = [str(data.get(key, '')).strip() for key in ('description', 'payment_method')]
    if not all(values):
        raise ValueError('Заповніть опис і спосіб оплати.')
    kind = data.get('transaction_type', 'expense')
    if kind not in ('expense', 'income', 'refund'):
        raise ValueError('Оберіть тип транзакції.')
    category = data.get('category_id')
    if kind != 'expense' and category in ('', None):
        return [date, amount, *values, kind, None]
    if isinstance(category, bool) or not str(category).isascii() or not str(category).isdigit():
        raise ValueError('Оберіть категорію зі списку.')
    return [date, amount, *values, kind, int(category)]


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/':
            body = (ROOT / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/budget':
            with connect() as conn:
                amount = conn.execute('SELECT amount FROM budget WHERE id=1').fetchone()[0]
            self.reply(200, {'amount': amount})
        elif self.path == '/api/categories':
            with connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('SELECT * FROM categories ORDER BY sort_order, id').fetchall()
            self.reply(200, [dict(row) for row in rows])
        elif self.path == '/api/expenses':
            with connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''SELECT e.rowid AS id, e.*, c.name AS category_name
                    FROM expenses e LEFT JOIN categories c ON c.id = e.category_id
                    ORDER BY e.spent_on DESC, e.rowid DESC''').fetchall()
            self.reply(200, [dict(row) for row in rows])
        else:
            self.reply(404, {'error': 'Не знайдено.'})

    def update_budget(self):
        if self.command != 'PUT':
            return self.reply(405, {'error': 'Метод не підтримується.'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 16384:
                raise ValueError
            value = json.loads(self.rfile.read(length))['amount']
            amount = float(value)
            if isinstance(value, bool) or not math.isfinite(amount) or amount < 0:
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            return self.reply(400, {'error': 'Вкажіть бюджет від 0 грн.'})
        with connect() as conn:
            conn.execute('UPDATE budget SET amount=? WHERE id=1', [amount])
        self.reply(200, {'amount': amount})

    def mutate(self):
        # Only accept same-origin JSON requests from the local interface.
        if self.headers.get('Origin') not in (None, f'http://{self.headers.get("Host")}'):
            return self.reply(403, {'error': 'Доступ заборонено.'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.reply(415, {'error': 'Очікується JSON.'})
        try:
            if self.path == '/api/budget':
                return self.update_budget()
            parts = self.path.strip('/').split('/')
            if parts[:2] != ['api', 'expenses'] or len(parts) not in (2, 3):
                return self.reply(404, {'error': 'Не знайдено.'})
            creating = self.command == 'POST' and len(parts) == 2
            if not creating and (len(parts) != 3 or self.command not in ('PUT', 'DELETE')):
                return self.reply(405, {'error': 'Метод не підтримується.'})
            row_id = None if creating else int(parts[2])
            values = None
            budget_group = None
            if self.command != 'DELETE':
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16384:
                    raise ValueError('Некоректний розмір запиту.')
                data = json.loads(self.rfile.read(length))
                values = validate(data)
                budget_group = data.get('budget_group')
                if budget_group not in (None, '', 'Fixed', 'Investments', 'Savings', 'Rich life'):
                    return self.reply(400, {'error': 'Оберіть групу бюджету зі списку.'})
            with connect() as conn:
                if values is not None and values[-1] is not None:
                    category = conn.execute('SELECT is_active FROM categories WHERE id=?', [values[-1]]).fetchone()
                    previous = None if creating else conn.execute('SELECT category_id FROM expenses WHERE rowid=?', [row_id]).fetchone()
                    if not category or (not category[0] and (not previous or previous[0] != values[-1])):
                        raise ValueError('Оберіть доступну категорію зі списку.')
                if creating:
                    conn.execute('INSERT INTO expenses (spent_on, amount, description, payment_method, transaction_type, category_id, budget_group) VALUES (?, ?, ?, ?, ?, ?, ?)', [*values, budget_group or None])
                else:
                    if self.command == 'PUT':
                        cursor = conn.execute('UPDATE expenses SET spent_on=?, amount=?, description=?, payment_method=?, transaction_type=?, category_id=? WHERE rowid=?', [*values, row_id])
                        if 'budget_group' in data:
                            conn.execute('UPDATE expenses SET budget_group=? WHERE rowid=?', [budget_group or None, row_id])
                    else:
                        cursor = conn.execute('DELETE FROM expenses WHERE rowid=?', [row_id])
                    if cursor.rowcount == 0:
                        return self.reply(404, {'error': 'Запис уже видалено.'})
            self.reply(201 if creating else 200, {'ok': True})
        except (ValueError, TypeError, OverflowError):
            self.reply(400, {'error': 'Перевірте дату, додатну суму, тип транзакції та доступність категорії.'})
        except sqlite3.Error:
            self.reply(500, {'error': 'Не вдалося зберегти зміни в базі.'})

    do_POST = do_PUT = do_DELETE = mutate


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--db', type=Path, default=DB)
    args = parser.parse_args()
    DB = args.db.resolve()
    initialize()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Витрати: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
