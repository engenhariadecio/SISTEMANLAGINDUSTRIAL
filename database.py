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
    # Foto do material (guardada no próprio banco; idempotente, não apaga dados)
    cur.execute('ALTER TABLE materiais ADD COLUMN IF NOT EXISTS imagem BYTEA')
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
    # Coluna que registra quem fez a movimentação (idempotente, não apaga dados)
    cur.execute('ALTER TABLE movimentacoes ADD COLUMN IF NOT EXISTS usuario TEXT')
    # Usuários do sistema (senha sempre criptografada)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id         SERIAL PRIMARY KEY,
            usuario    TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            nome       TEXT NOT NULL,
            is_admin   BOOLEAN NOT NULL DEFAULT FALSE,
            ativo      BOOLEAN NOT NULL DEFAULT TRUE,
            criado_em  TEXT
        )
    ''')
    # Coluna de perfil: 'admin', 'operador' ou 'visualizador' (idempotente)
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS perfil TEXT NOT NULL DEFAULT 'operador'")
    cur.execute("UPDATE usuarios SET perfil='admin' WHERE is_admin=TRUE AND perfil <> 'admin'")
    conn.commit()
    conn.close()
    print("✅ Banco PostgreSQL inicializado!")

if __name__ == '__main__':
    init_db()
