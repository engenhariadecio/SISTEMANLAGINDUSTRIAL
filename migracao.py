"""
Migração de dados única — Depósito NLAG.

Roda na subida do app. Só faz alguma coisa se a variável de ambiente
OLD_DATABASE_URL estiver definida (a URL pública do Postgres ANTIGO).

Copia as tabelas `materiais` e `movimentacoes` do banco antigo para o
banco atual (DATABASE_URL). O saldo NÃO precisa ser copiado: ele é
calculado a partir das movimentações.

É seguro rodar mais de uma vez:
  - materiais: usa ON CONFLICT (codigo) DO NOTHING, então não duplica.
  - movimentacoes: só copia se a tabela de destino estiver vazia.

Depois que a migração terminar, REMOVA a variável OLD_DATABASE_URL no
Railway (por segurança e para não tentar copiar de novo).
"""
import os
import psycopg2
import psycopg2.extras


def migrar_dados_iniciais():
    old_url = os.environ.get('OLD_DATABASE_URL', '').strip()
    new_url = os.environ.get('DATABASE_URL', '').strip()

    if not old_url:
        return  # Nada a migrar — operação normal do app.

    if not new_url:
        print("[migracao] DATABASE_URL ausente; nada a fazer.", flush=True)
        return

    try:
        src = psycopg2.connect(old_url)
        dst = psycopg2.connect(new_url)
    except Exception as e:
        print(f"[migracao] Falha ao conectar aos bancos: {e}", flush=True)
        return

    try:
        sc = src.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        dc = dst.cursor()

        # 1) Materiais (codigo é UNIQUE -> ON CONFLICT evita duplicar)
        sc.execute("SELECT codigo, descricao, unidade FROM materiais")
        materiais = sc.fetchall()
        for m in materiais:
            dc.execute(
                "INSERT INTO materiais (codigo, descricao, unidade) "
                "VALUES (%s, %s, %s) ON CONFLICT (codigo) DO NOTHING",
                (m['codigo'], m['descricao'], m['unidade'])
            )

        # 2) Movimentacoes (sem chave única -> só copia se o destino estiver
        #    vazio, para não duplicar caso o app reinicie com a variável setada)
        dc.execute("SELECT COUNT(*) FROM movimentacoes")
        destino_vazio = dc.fetchone()[0] == 0
        movs_copiadas = 0
        if destino_vazio:
            sc.execute(
                "SELECT codigo, tipo, quantidade, data_hora, observacao "
                "FROM movimentacoes"
            )
            for mv in sc.fetchall():
                dc.execute(
                    "INSERT INTO movimentacoes "
                    "(codigo, tipo, quantidade, data_hora, observacao) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (mv['codigo'], mv['tipo'], mv['quantidade'],
                     mv['data_hora'], mv['observacao'])
                )
                movs_copiadas += 1
        else:
            print("[migracao] movimentacoes ja possui dados no destino; "
                  "pulei essa parte para nao duplicar.", flush=True)

        dst.commit()
        print(f"[migracao] OK! {len(materiais)} materiais processados, "
              f"{movs_copiadas} movimentacoes copiadas. "
              f"Pode remover a variavel OLD_DATABASE_URL agora.", flush=True)
    except Exception as e:
        dst.rollback()
        print(f"[migracao] Erro durante a copia: {e}", flush=True)
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
