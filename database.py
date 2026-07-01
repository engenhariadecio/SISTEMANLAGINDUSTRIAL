import psycopg2
import os

DATABASE_URL = os.environ.get('DATABASE_URL')

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS materiais (
            id        SERIAL PRIMARY KEY,
            codigo    TEXT UNIQUE NOT NULL,
            descricao TEXT NOT NULL,
            unidade   TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id         SERIAL PRIMARY KEY,
            codigo     TEXT NOT NULL,
            tipo       TEXT NOT NULL,
            quantidade REAL NOT NULL,
            data_hora  TEXT NOT NULL,
            observacao TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ Banco PostgreSQL inicializado!")

if __name__ == '__main__':
    init_db()
