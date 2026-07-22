import os
import io
import base64
import csv
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
import barcode as python_barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageChops

app = Flask(__name__)
# Sem segredos fracos no código. Se a variável não existir no ambiente,
# usa um valor aleatório (que ninguém conhece), em vez de um padrão público.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
# Mantém o login por 7 dias (evita deslogar ao fechar o navegador).
app.permanent_session_lifetime = timedelta(days=7)
DATABASE_URL = os.environ.get('DATABASE_URL', '')

APP_USUARIO = os.environ.get('APP_USUARIO', 'admin')
APP_SENHA   = os.environ.get('APP_SENHA') or secrets.token_urlsafe(24)

# Fuso horário do Brasil (São Paulo). Usar em vez de datetime.now(),
# que no servidor do Railway retorna UTC.
TZ_BR = ZoneInfo('America/Sao_Paulo')

def agora_br():
    return datetime.now(TZ_BR)

# ──────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def query(sql, params=None, fetchone=False, fetchall=False, commit=False):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        if commit:
            conn.commit()
            return None
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
    except Exception as e:
        if commit:
            conn.rollback()
        raise e
    finally:
        conn.close()

# ──────────────────────────────────────────────
# Barcode – 300 DPI, auto-crop
# ──────────────────────────────────────────────
def gerar_barcode_base64(codigo):
    try:
        writer_options = {
            'module_width':  0.3,
            'module_height': 10.0,
            'font_size':     0,
            'text_distance': 0,
            'quiet_zone':    2.0,
            'dpi':           300,
            'write_text':    False,
        }
        code128 = python_barcode.get('code128', str(codigo),
                                     writer=ImageWriter())
        buf = io.BytesIO()
        code128.write(buf, options=writer_options)
        buf.seek(0)

        img = Image.open(buf).convert('RGB')
        bg   = Image.new('RGB', img.size, (255, 255, 255))
        diff = ImageChops.difference(img, bg)
        bbox = diff.getbbox()
        if bbox:
            img = img.crop(bbox)

        razao = img.width / img.height
        nova_altura = 85
        nova_largura = int(nova_altura * razao)
        img = img.resize((nova_largura, nova_altura), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        return base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        app.logger.error(f'Barcode error: {e}')
        return None

# ──────────────────────────────────────────────
# Saldo
# ──────────────────────────────────────────────
def calcular_saldo(codigo):
    row = query(
        """SELECT
             COALESCE(SUM(CASE WHEN tipo='ENTRADA' THEN quantidade ELSE 0 END),0)
           - COALESCE(SUM(CASE WHEN tipo='SAIDA'   THEN quantidade ELSE 0 END),0)
             AS saldo
           FROM movimentacoes WHERE codigo=%s""",
        (codigo,), fetchone=True
    )
    return float(row['saldo']) if row else 0.0

# ──────────────────────────────────────────────
# Formatar data_hora (datetime ou string)
# ──────────────────────────────────────────────
def fmt_dt(dh):
    if dh is None:
        return ''
    if hasattr(dh, 'strftime'):
        return dh.strftime('%d/%m/%Y %H:%M')
    return str(dh)[:16]

# ──────────────────────────────────────────────
# Imagens de materiais
# ──────────────────────────────────────────────
def processar_imagem(file):
    """Recebe o arquivo enviado, redimensiona (máx. 800px) e devolve os bytes
    em JPEG. Retorna None se não houver arquivo ou se falhar."""
    if not file or file.filename == '':
        return None
    try:
        img = Image.open(file.stream)
        img = img.convert('RGB')
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=82, optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"[imagem] falha ao processar: {e}", flush=True)
        return None


@app.route('/material_imagem/<codigo>')
def material_imagem(codigo):
    row = query('SELECT imagem FROM materiais WHERE codigo=%s',
                (codigo.strip().upper(),), fetchone=True)
    if row and row['imagem']:
        return Response(bytes(row['imagem']), mimetype='image/jpeg')
    # Placeholder para não quebrar a tag <img>
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">'
           '<rect width="80" height="80" fill="#EEF1F6"/>'
           '<text x="40" y="44" font-size="9" fill="#8B94A3" text-anchor="middle">sem foto</text>'
           '</svg>')
    return Response(svg, mimetype='image/svg+xml')


# ──────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────
@app.before_request
def verificar_login():
    rotas_liberadas = ['login', 'static', 'print_etiqueta']
    if request.endpoint not in rotas_liberadas and 'usuario' not in session:
        return redirect(url_for('login'))
    # Perfil "visualizador": só pode ver o saldo (dashboard) e exportá-lo.
    if session.get('perfil') == 'visualizador':
        permitido = set(rotas_liberadas) | {'index', 'exportar_saldo', 'logout', 'material_imagem'}
        if request.endpoint not in permitido:
            flash('Seu perfil permite apenas visualizar o saldo.', 'warning')
            return redirect(url_for('index'))

def seed_admin():
    """Cria o primeiro admin a partir de APP_USUARIO/APP_SENHA, se não houver
    nenhum usuário ainda. Assim sempre existe uma forma de entrar."""
    try:
        total = query('SELECT COUNT(*) AS n FROM usuarios', fetchone=True)
        if total and total['n'] == 0:
            usuario = os.environ.get('APP_USUARIO', 'admin').strip().lower()
            senha = os.environ.get('APP_SENHA')
            if senha:
                query(
                    'INSERT INTO usuarios (usuario, senha_hash, nome, is_admin, ativo, criado_em, perfil) '
                    "VALUES (%s, %s, %s, TRUE, TRUE, %s, 'admin') ON CONFLICT (usuario) DO NOTHING",
                    (usuario, generate_password_hash(senha), 'Administrador',
                     agora_br().strftime('%Y-%m-%d %H:%M:%S')), commit=True
                )
                print(f"[seed] Admin inicial criado: usuario '{usuario}'", flush=True)
    except Exception as e:
        print(f"[seed] Falha ao criar admin inicial: {e}", flush=True)


@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip().lower()
        senha   = request.form.get('senha', '')
        u = query('SELECT * FROM usuarios WHERE usuario=%s AND ativo=TRUE',
                  (usuario,), fetchone=True)
        if u and check_password_hash(u['senha_hash'], senha):
            perfil = u.get('perfil') or ('admin' if u['is_admin'] else 'operador')
            session.permanent = True
            session['usuario']  = u['usuario']
            session['nome']     = u['nome']
            session['perfil']   = perfil
            session['is_admin'] = (perfil == 'admin')
            return redirect(url_for('index'))
        erro = 'Usuário ou senha inválidos.'
    return render_template('login.html', erro=erro)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ──────────────────────────────────────────────
# Usuários (somente administradores)
# ──────────────────────────────────────────────
@app.route('/usuarios', methods=['GET', 'POST'])
def usuarios():
    if not session.get('is_admin'):
        flash('Acesso restrito a administradores.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        usuario  = request.form.get('usuario', '').strip().lower()
        nome     = request.form.get('nome', '').strip()
        senha    = request.form.get('senha', '')
        perfil   = request.form.get('perfil', 'operador')
        if perfil not in ('admin', 'operador', 'visualizador'):
            perfil = 'operador'
        is_admin = (perfil == 'admin')
        if not usuario or not nome or not senha:
            flash('❌ Preencha usuário, nome e senha.', 'danger')
        elif len(senha) < 6:
            flash('❌ A senha deve ter pelo menos 6 caracteres.', 'danger')
        elif query('SELECT 1 FROM usuarios WHERE usuario=%s', (usuario,), fetchone=True):
            flash(f'❌ O usuário "{usuario}" já existe.', 'danger')
        else:
            query(
                'INSERT INTO usuarios (usuario, senha_hash, nome, is_admin, ativo, criado_em, perfil) '
                'VALUES (%s, %s, %s, %s, TRUE, %s, %s)',
                (usuario, generate_password_hash(senha), nome, is_admin,
                 agora_br().strftime('%Y-%m-%d %H:%M:%S'), perfil), commit=True
            )
            flash(f'✅ Usuário "{usuario}" criado com sucesso.', 'success')
        return redirect(url_for('usuarios'))

    lista = query('SELECT id, usuario, nome, is_admin, ativo, criado_em, perfil '
                  'FROM usuarios ORDER BY usuario', fetchall=True)
    return render_template('usuarios.html', usuarios=lista)


@app.route('/usuarios/toggle/<int:uid>')
def usuarios_toggle(uid):
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    u = query('SELECT * FROM usuarios WHERE id=%s', (uid,), fetchone=True)
    if u:
        if u['usuario'] == session.get('usuario'):
            flash('❌ Você não pode desativar a sua própria conta.', 'danger')
        else:
            query('UPDATE usuarios SET ativo = NOT ativo WHERE id=%s', (uid,), commit=True)
            flash('✅ Status do usuário atualizado.', 'success')
    return redirect(url_for('usuarios'))


@app.route('/usuarios/senha/<int:uid>', methods=['POST'])
def usuarios_senha(uid):
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    nova = request.form.get('nova_senha', '')
    if len(nova) < 6:
        flash('❌ A nova senha deve ter pelo menos 6 caracteres.', 'danger')
    else:
        query('UPDATE usuarios SET senha_hash=%s WHERE id=%s',
              (generate_password_hash(nova), uid), commit=True)
        flash('✅ Senha redefinida.', 'success')
    return redirect(url_for('usuarios'))

# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────
@app.route('/')
def index():
    materiais = query('SELECT id, codigo, descricao, unidade, (imagem IS NOT NULL) AS tem_imagem '
                      'FROM materiais ORDER BY descricao', fetchall=True)
    saldo = []
    for m in materiais:
        s = calcular_saldo(m['codigo'])
        saldo.append({**m, 'saldo': s})
    total_itens     = len(saldo)
    total_com_saldo = sum(1 for i in saldo if i['saldo'] > 0)
    total_zerados   = sum(1 for i in saldo if i['saldo'] <= 0)
    agora = agora_br().strftime('%d/%m/%Y %H:%M')
    return render_template('index.html',
                           saldo=saldo,
                           total_itens=total_itens,
                           total_com_saldo=total_com_saldo,
                           total_zerados=total_zerados,
                           agora=agora)

# ──────────────────────────────────────────────
# Materiais
# ──────────────────────────────────────────────
@app.route('/materiais', methods=['GET', 'POST'])
def materiais():
    if request.method == 'POST':
        acao = request.form.get('acao')
        if acao == 'cadastrar':
            codigo    = request.form['codigo'].strip().upper()
            descricao = request.form['descricao'].strip().upper()
            unidade   = request.form['unidade'].strip().upper()
            try:
                query(
                    'INSERT INTO materiais (codigo,descricao,unidade) VALUES (%s,%s,%s)',
                    (codigo, descricao, unidade), commit=True
                )
                # Foto opcional no cadastro
                dados = processar_imagem(request.files.get('imagem'))
                if dados:
                    query('UPDATE materiais SET imagem=%s WHERE codigo=%s',
                          (psycopg2.Binary(dados), codigo), commit=True)
                flash(f'✅ Material {codigo} cadastrado!', 'success')
            except Exception:
                flash(f'❌ Código {codigo} já existe ou erro ao cadastrar.', 'danger')
        elif acao == 'imagem':
            codigo = request.form['codigo'].strip().upper()
            dados = processar_imagem(request.files.get('imagem'))
            if dados:
                query('UPDATE materiais SET imagem=%s WHERE codigo=%s',
                      (psycopg2.Binary(dados), codigo), commit=True)
                flash(f'📷 Foto do material {codigo} atualizada.', 'success')
            else:
                flash('❌ Não foi possível ler a imagem enviada.', 'danger')
        elif acao == 'remover_imagem':
            codigo = request.form['codigo'].strip().upper()
            query('UPDATE materiais SET imagem=NULL WHERE codigo=%s', (codigo,), commit=True)
            flash(f'🗑️ Foto do material {codigo} removida.', 'warning')
        elif acao == 'excluir':
            codigo = request.form['codigo'].strip().upper()
            try:
                query('DELETE FROM materiais WHERE codigo=%s', (codigo,), commit=True)
                flash(f'🗑️ Material {codigo} excluído.', 'warning')
            except Exception:
                flash(f'❌ Erro ao excluir {codigo}.', 'danger')
        return redirect(url_for('materiais'))
    lista = query('SELECT id, codigo, descricao, unidade, (imagem IS NOT NULL) AS tem_imagem '
                  'FROM materiais ORDER BY descricao', fetchall=True)
    return render_template('materiais.html', lista=lista)

# ──────────────────────────────────────────────
# Importar CSV
# ──────────────────────────────────────────────
@app.route('/importar_csv', methods=['POST'])
def importar_csv():
    f = request.files.get('arquivo_csv')
    if not f:
        flash('❌ Nenhum arquivo enviado.', 'danger')
        return redirect(url_for('materiais'))
    raw = f.read()
    texto = None
    for enc in ('utf-8-sig', 'latin-1', 'cp1252'):
        try:
            texto = raw.decode(enc)
            break
        except Exception:
            continue
    if texto is None:
        flash('❌ Encoding não reconhecido.', 'danger')
        return redirect(url_for('materiais'))
    delim = ';' if ';' in texto.splitlines()[0] else ','
    reader = csv.DictReader(io.StringIO(texto), delimiter=delim)
    inseridos = ignorados = 0
    erros = []
    for i, row in enumerate(reader, 1):
        try:
            codigo    = row.get('codigo', '').strip().upper()
            descricao = row.get('descricao', '').strip().upper()
            unidade   = row.get('unidade', 'UN').strip().upper()
            if not codigo:
                continue
            query(
                'INSERT INTO materiais (codigo,descricao,unidade) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                (codigo, descricao, unidade), commit=True
            )
            inseridos += 1
        except Exception as e:
            ignorados += 1
            if len(erros) < 5:
                erros.append(f'Linha {i}: {e}')
    msg = f'✅ {inseridos} inseridos, {ignorados} ignorados.'
    if erros:
        msg += ' Erros: ' + ' | '.join(erros)
    flash(msg, 'success' if not erros else 'warning')
    return redirect(url_for('materiais'))

# ──────────────────────────────────────────────
# Entrada
# ──────────────────────────────────────────────
@app.route('/entrada', methods=['GET', 'POST'])
def entrada():
    material    = None
    barcode_img = None
    agora      = agora_br().strftime('%d/%m/%Y %H:%M')
    codigo_pre = request.args.get('codigo', '')
    if codigo_pre:
        material    = query('SELECT * FROM materiais WHERE codigo=%s',
                            (codigo_pre.upper(),), fetchone=True)
        barcode_img = gerar_barcode_base64(codigo_pre.upper()) if material else None
    if request.method == 'POST':
        codigo     = request.form['codigo'].strip().upper()
        quantidade = request.form['quantidade'].strip()
        observacao = request.form.get('observacao', '').strip()
        try:
            qty = float(quantidade)
            if qty <= 0:
                raise ValueError
        except ValueError:
            flash('❌ Quantidade inválida.', 'danger')
            return redirect(url_for('entrada'))
        mat = query('SELECT * FROM materiais WHERE codigo=%s', (codigo,), fetchone=True)
        if not mat:
            flash(f'❌ Código {codigo} não encontrado.', 'danger')
            return redirect(url_for('entrada'))
        query(
            'INSERT INTO movimentacoes (codigo,tipo,quantidade,data_hora,observacao,usuario) VALUES (%s,%s,%s,%s,%s,%s)',
            (codigo, 'ENTRADA', qty, agora_br().strftime('%Y-%m-%d %H:%M:%S'), observacao, session.get('nome') or session.get('usuario')), commit=True
        )
        flash(f'✅ Entrada de {qty} {mat["unidade"]} registrada para {codigo}.', 'success')
        material    = mat
        barcode_img = gerar_barcode_base64(codigo)
    return render_template('entrada.html',
                           material=material,
                           barcode_img=barcode_img,
                           agora=agora,
                           codigo_pre=codigo_pre)

# ──────────────────────────────────────────────
# Imprimir etiqueta
# ──────────────────────────────────────────────
@app.route('/imprimir_etiqueta', methods=['GET'])
def imprimir_etiqueta():
    codigo      = request.args.get('codigo', '')
    material    = None
    barcode_img = None
    agora = agora_br().strftime('%d/%m/%Y %H:%M')
    if codigo:
        material    = query('SELECT * FROM materiais WHERE codigo=%s',
                            (codigo.upper(),), fetchone=True)
        barcode_img = gerar_barcode_base64(codigo.upper()) if material else None
    return render_template('imprimir_etiqueta.html',
                           material=material,
                           barcode_img=barcode_img,
                           agora=agora)

# ──────────────────────────────────────────────
# Print popup – sem login obrigatório
# ──────────────────────────────────────────────
@app.route('/print/<codigo>')
def print_etiqueta(codigo):
    material = query('SELECT * FROM materiais WHERE codigo=%s',
                     (codigo.upper(),), fetchone=True)
    if not material:
        return (f"<h3 style='font-family:sans-serif;padding:20px;color:red;'>"
                f"Código {codigo} não encontrado.</h3>"), 404
    barcode_img = gerar_barcode_base64(codigo.upper())
    agora_str   = agora_br().strftime('%d/%m/%Y %H:%M')
    return render_template('etiqueta_print.html',
                           material=material,
                           barcode_img=barcode_img,
                           agora=agora_str)

# ──────────────────────────────────────────────
# Saída
# ──────────────────────────────────────────────
@app.route('/saida', methods=['GET', 'POST'])
def saida():
    agora = agora_br().strftime('%d/%m/%Y %H:%M')
    if request.method == 'POST':
        codigo     = request.form['codigo'].strip().upper()
        quantidade = request.form['quantidade'].strip()
        observacao = request.form.get('observacao', '').strip()
        try:
            qty = float(quantidade)
            if qty <= 0:
                raise ValueError
        except ValueError:
            flash('❌ Quantidade inválida.', 'danger')
            return redirect(url_for('saida'))
        mat = query('SELECT * FROM materiais WHERE codigo=%s', (codigo,), fetchone=True)
        if not mat:
            flash(f'❌ Código {codigo} não encontrado.', 'danger')
            return redirect(url_for('saida'))
        saldo = calcular_saldo(codigo)
        if qty > saldo:
            flash(f'❌ Saldo insuficiente. Saldo atual: {saldo} {mat["unidade"]}.', 'danger')
            return redirect(url_for('saida'))
        query(
            'INSERT INTO movimentacoes (codigo,tipo,quantidade,data_hora,observacao,usuario) VALUES (%s,%s,%s,%s,%s,%s)',
            (codigo, 'SAIDA', qty, agora_br().strftime('%Y-%m-%d %H:%M:%S'), observacao, session.get('nome') or session.get('usuario')), commit=True
        )
        flash(f'✅ Saída de {qty} {mat["unidade"]} registrada para {codigo}.', 'success')
        return redirect(url_for('saida'))
    codigo_pre = request.args.get('codigo', '')
    return render_template('saida.html', agora=agora, codigo_pre=codigo_pre)

# ──────────────────────────────────────────────
# Histórico
# ──────────────────────────────────────────────
@app.route('/historico')
def historico():
    codigo = request.args.get('codigo', '').strip().upper()
    tipo   = request.args.get('tipo', '').strip().upper()
    sql    = """SELECT m.data_hora, m.tipo, m.codigo, mat.descricao, mat.unidade,
                       m.quantidade, m.observacao, m.usuario
                FROM movimentacoes m
                LEFT JOIN materiais mat ON mat.codigo = m.codigo
                WHERE 1=1"""
    params = []
    if codigo:
        sql += ' AND m.codigo=%s'
        params.append(codigo)
    if tipo in ('ENTRADA', 'SAIDA'):
        sql += ' AND m.tipo=%s'
        params.append(tipo)
    sql += ' ORDER BY m.data_hora DESC LIMIT 500'
    movs = query(sql, params, fetchall=True)
    agora = agora_br().strftime('%d/%m/%Y %H:%M')
    return render_template('historico.html', movs=movs, agora=agora,
                           filtro_codigo=codigo, filtro_tipo=tipo)

# ──────────────────────────────────────────────
# Exportar saldo CSV  ← CORRIGIDO
# ──────────────────────────────────────────────
@app.route('/exportar_saldo')
def exportar_saldo():
    materiais = query('SELECT codigo, descricao, unidade FROM materiais ORDER BY descricao', fetchall=True)

    output = io.StringIO()
    output.write('Codigo;Descricao;Unidade;Saldo\n')
    for m in (materiais or []):
        s = calcular_saldo(m['codigo'])
        s_fmt = str(int(s)) if s == int(s) else f'{s:.2f}'
        output.write(f'{m["codigo"]};{m["descricao"]};{m["unidade"]};{s_fmt}\n')

    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=saldo_estoque.csv'}
    )

# ──────────────────────────────────────────────
# Exportar histórico CSV  ← CORRIGIDO
# ──────────────────────────────────────────────
@app.route('/exportar_historico')
def exportar_historico():
    movs = query(
        """SELECT m.data_hora, m.tipo, m.codigo, mat.descricao, mat.unidade,
                  m.quantidade, m.observacao
           FROM movimentacoes m
           LEFT JOIN materiais mat ON mat.codigo = m.codigo
           ORDER BY m.data_hora DESC""",
        fetchall=True
    )

    output = io.StringIO()
    output.write('Data/Hora;Tipo;Codigo;Descricao;Unidade;Quantidade;Observacao\n')
    for mv in (movs or []):
        linha = (
            f'{fmt_dt(mv["data_hora"])};'
            f'{mv["tipo"]};'
            f'{mv["codigo"]};'
            f'{mv.get("descricao") or ""};'
            f'{mv.get("unidade") or ""};'
            f'{mv["quantidade"]};'
            f'{mv.get("observacao") or ""}\n'
        )
        output.write(linha)

    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=historico.csv'}
    )

# ──────────────────────────────────────────────
# API AJAX
# ──────────────────────────────────────────────
@app.route('/api/material/<codigo>')
def api_material(codigo):
    mat = query('SELECT * FROM materiais WHERE codigo=%s',
                (codigo.upper(),), fetchone=True)
    if not mat:
        return jsonify({'erro': 'Não encontrado'}), 404
    saldo = calcular_saldo(codigo.upper())
    return jsonify({**dict(mat), 'saldo': saldo})

# ──────────────────────────────────────────────
# Coletor mobile
# ──────────────────────────────────────────────
@app.route('/coletor')
def coletor():
    return render_template('coletor.html')

# ──────────────────────────────────────────────
# Init & run
# ──────────────────────────────────────────────
# Cria as tabelas assim que o app sobe (gunicorn/Railway e execução local).
# Idempotente: o database.py usa CREATE TABLE IF NOT EXISTS.
from database import init_db
try:
    init_db()
except Exception as e:
    print(f"[startup] Falha ao inicializar o banco: {e}", flush=True)

# Cria o administrador inicial (a partir de APP_USUARIO/APP_SENHA) se ainda
# não houver nenhum usuário cadastrado.
try:
    seed_admin()
except Exception as e:
    print(f"[startup] seed_admin falhou: {e}", flush=True)

# Migração única: só age se a variável OLD_DATABASE_URL estiver definida.
# Copia materiais/movimentacoes do banco antigo. Remova a variável depois.
from migracao import migrar_dados_iniciais
try:
    migrar_dados_iniciais()
except Exception as e:
    print(f"[startup] migracao falhou: {e}", flush=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
