#!/usr/bin/env python3
"""
servidor.py — Instrumentistas Tools
Backend Flask que integra editor, pipeline e calibração.

Uso:
  python3 servidor.py

Depois abra o browser em:
  http://localhost:5000
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

# ════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO DE PASTAS
# ════════════════════════════════════════════════════════════════

BASE_DIR    = Path(__file__).parent
SCORES_DIR  = BASE_DIR / 'scores'
PDF_DIR     = SCORES_DIR / 'pdf'
JSON_DIR    = SCORES_DIR / 'json'
MERGED_DIR  = SCORES_DIR / 'merged'
META_DIR    = SCORES_DIR / 'meta'
CALIB_DIR   = BASE_DIR / 'calibration'
STATIC_DIR  = BASE_DIR / 'static'

for d in [PDF_DIR, JSON_DIR, MERGED_DIR, META_DIR, CALIB_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR))


# ════════════════════════════════════════════════════════════════
# ROTAS PRINCIPAIS
# ════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Página principal — lista de louvores."""
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/editor')
def editor():
    """Editor de cifra."""
    return send_from_directory(BASE_DIR, 'editor_v4.html')


@app.route('/upload')
def upload_page():
    """Tela de upload de partitura."""
    return send_from_directory(BASE_DIR, 'upload.html')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


# ════════════════════════════════════════════════════════════════
# API — LOUVORES
# ════════════════════════════════════════════════════════════════

@app.route('/api/louvores', methods=['GET'])
def listar_louvores():
    """
    Lista todos os louvores no acervo.
    Retorna informações básicas de cada um.
    """
    louvores = []

    for json_file in sorted(JSON_DIR.glob('*.json')):
        try:
            with open(json_file, encoding='utf-8') as f:
                data = json.load(f)

            # Contar acordes e status
            total = corretos = humanos = 0
            for sec in data.get('sections', []):
                for line in sec.get('lines', []):
                    for measure in line.get('measures', []):
                        for chord in measure.get('chords', []):
                            total += 1
                            ct = chord.get('ground_truth', {}).get('correction_type', '')
                            if ct == 'correct':       corretos += 1
                            elif ct == 'human_origin': humanos += 1

            # Verificar se tem PDF e versão mesclada
            slug     = json_file.stem
            has_pdf    = (PDF_DIR / f"{slug}.pdf").exists()
            has_merged = (MERGED_DIR / f"{slug}_merged.json").exists()

            pct = round(100 * corretos / (total - humanos)) \
                  if (total - humanos) > 0 else None

            louvores.append({
                'slug':       slug,
                'title':      data.get('title', slug),
                'composer':   data.get('composer', ''),
                'key':        data.get('key', ''),
                'bpm':        data.get('bpm', ''),
                'meter':      data.get('meter', '4/4'),
                'total_chords': total,
                'human_chords': humanos,
                'correct_pct':  pct,
                'has_pdf':      has_pdf,
                'has_merged':   has_merged,
                'modified':     datetime.fromtimestamp(
                    json_file.stat().st_mtime
                ).strftime('%Y-%m-%d %H:%M'),
            })
        except Exception as e:
            print(f"  [AVISO] Erro ao ler {json_file.name}: {e}")

    return jsonify(louvores)


@app.route('/api/louvores/<slug>', methods=['GET'])
def carregar_louvor(slug):
    """Carrega o JSON de um louvor pelo slug."""
    # Prioriza versão mesclada se existir
    merged = MERGED_DIR / f"{slug}_merged.json"
    normal = JSON_DIR   / f"{slug}.json"

    path = merged if merged.exists() else normal
    if not path.exists():
        return jsonify({'erro': 'Louvor não encontrado'}), 404

    with open(path, encoding='utf-8') as f:
        return jsonify(json.load(f))

@app.route('/api/editor/<slug>', methods=['GET'])
def carregar_louvor_editor(slug):
    """
    Carrega exclusivamente o JSON humano salvo pelo editor.
    Não usa merged.
    Não usa pipeline.
    Usado pelo editor_v4.html.
    """
    path = JSON_DIR / f"{slug}.json"

    if not path.exists():
        return jsonify({
            'erro': f'JSON humano não encontrado: {slug}.json',
            'path': str(path)
        }), 404

    with open(path, encoding='utf-8') as f:
        return jsonify(json.load(f))

@app.route('/api/louvores/<slug>', methods=['POST'])
def salvar_louvor(slug):
    """
    Salva o JSON do editor e dispara merge + calibração automaticamente.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'erro': 'JSON inválido'}), 400

        ts = datetime.now(timezone.utc).isoformat()

        # 1. Salvar JSON do editor
        editor_path = JSON_DIR / f"{slug}.json"
        with open(editor_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        result = {
            'slug':      slug,
            'saved_at':  ts,
            'editor':    str(editor_path),
            'merged':    None,
            'calibrated': False,
            'report':    None,
        }

        # 2. Se existe JSON do pipeline, fazer merge automático
        pipeline_path = JSON_DIR / f"{slug}_pipeline.json"
        if pipeline_path.exists():
            merged_path = MERGED_DIR / f"{slug}_merged.json"
            try:
                from calibrate import merge, calibrate
                merge(str(pipeline_path), str(editor_path), str(merged_path))
                result['merged'] = str(merged_path)

                # 3. Rodar calibração em todos os arquivos mesclados
                all_merged = list(MERGED_DIR.glob('*_merged.json'))
                if all_merged:
                    report_path = CALIB_DIR / f"relatorio_{datetime.now().strftime('%Y-%m-%d')}.txt"
                    import io
                    from contextlib import redirect_stdout
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        calibrate([str(p) for p in all_merged])
                    report_text = buf.getvalue()
                    with open(report_path, 'w', encoding='utf-8') as f:
                        f.write(report_text)
                    result['calibrated'] = True
                    result['report']     = report_text

            except Exception as e:
                print(f"  [AVISO] Merge/calibração: {e}")
                result['merge_error'] = str(e)
        else:
            result['aviso'] = (
                f"JSON do pipeline não encontrado em {pipeline_path.name}. "
                f"Execute o pipeline sobre o PDF para habilitar a calibração automática."
            )

        return jsonify(result)

    except Exception as e:
        return jsonify({'erro': str(e)}), 500



@app.route('/api/meta/<slug>', methods=['POST'])
def salvar_meta(slug):
    """Salva metadados simples para o pipeline_v6. Não usa LINES_MAP."""
    try:
        data = request.get_json() or {}
        meta = {
            'title':    data.get('title', slug),
            'composer': data.get('composer', ''),
            'key':      data.get('key', ''),
            'key_sig':  data.get('key_sig', None),
            'bpm':      data.get('bpm', ''),
            'meter':    data.get('meter', '4/4'),
            'clef':     data.get('clef', 'treble'),
        }
        meta_path = META_DIR / f"{slug}_meta.json"
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return jsonify({'slug': slug, 'saved': str(meta_path), 'meta': meta})
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# ════════════════════════════════════════════════════════════════
# API — PIPELINE
# ════════════════════════════════════════════════════════════════

@app.route('/api/pipeline/<slug>', methods=['POST'])
def rodar_pipeline(slug):
    """
    Roda sempre o pipeline_v6 sobre o PDF do louvor.
    Não usa LINES_MAP.
    O PDF deve estar em scores/pdf/<slug>.pdf
    """
    pdf_path   = PDF_DIR / f"{slug}.pdf"
    out_path   = JSON_DIR / f"{slug}_pipeline.json"

    if not pdf_path.exists():
        return jsonify({
            'erro': f"PDF não encontrado: {pdf_path.name}",
            'instrucao': f"Coloque o PDF em scores/pdf/{slug}.pdf"
        }), 404

    try:
        # Importar e rodar o pipeline v6 (sem LINES_MAP)
        sys.path.insert(0, str(BASE_DIR))
        import pipeline_v6 as pipeline

        # Meta: lê primeiro o arquivo salvo pela tela de upload.
        # Se não existir, tenta ler o JSON do editor.
        meta = {}

        meta_json = META_DIR / f"{slug}_meta.json"
        if meta_json.exists():
            with open(meta_json, encoding='utf-8') as f:
                meta = json.load(f)

        editor_json = JSON_DIR / f"{slug}.json"
        if editor_json.exists() and not meta:
            with open(editor_json, encoding='utf-8') as f:
                ej = json.load(f)
            meta = {
                'title':    ej.get('title', slug),
                'composer': ej.get('composer', ''),
                'key':      ej.get('key', ''),
                'key_sig':  ej.get('key_sig', None),
                'bpm':      ej.get('bpm', ''),
                'meter':    ej.get('meter', '4/4'),
                'clef':     ej.get('clef', 'treble'),
            }

        output, audit = pipeline.run(str(pdf_path), meta)

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        total  = len(audit)
        by_rule = {}
        for r in audit:
            regra = r['regra']
            by_rule[regra] = by_rule.get(regra, 0) + 1

        return jsonify({
            'slug':       slug,
            'output':     str(out_path),
            'total':      total,
            'by_rule':    by_rule,
        })

    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# ════════════════════════════════════════════════════════════════
# API — UPLOAD DE PDF
# ════════════════════════════════════════════════════════════════

@app.route('/api/lines/<slug>', methods=['POST'])
def salvar_lines(slug):
    """Rota antiga desativada. Esta versão não usa LINES_MAP."""
    return jsonify({'erro': 'LINES_MAP foi removido. Esta versão usa somente pipeline_v6.'}), 410


@app.route('/api/upload/<slug>', methods=['POST'])
def upload_pdf(slug):
    """Upload de PDF para scores/pdf/<slug>.pdf"""
    if 'pdf' not in request.files:
        return jsonify({'erro': 'Nenhum arquivo enviado'}), 400

    file = request.files['pdf']
    if not file.filename.endswith('.pdf'):
        return jsonify({'erro': 'Somente arquivos .pdf são aceitos'}), 400

    dest = PDF_DIR / f"{slug}.pdf"
    file.save(str(dest))

    return jsonify({
        'slug':    slug,
        'saved':   str(dest),
        'size_kb': round(dest.stat().st_size / 1024, 1),
    })


# ════════════════════════════════════════════════════════════════
# API — CALIBRAÇÃO
# ════════════════════════════════════════════════════════════════

@app.route('/api/mesclar/<slug>', methods=['POST'])
def mesclar_louvor(slug):
    """Mescla o JSON do editor com o JSON do pipeline e salva o merged."""
    editor_path   = JSON_DIR   / f"{slug}.json"
    pipeline_path = JSON_DIR   / f"{slug}_pipeline.json"
    merged_path   = MERGED_DIR / f"{slug}_merged.json"

    if not editor_path.exists():
        return jsonify({'erro': f"JSON do editor não encontrado: {slug}.json"}), 404
    if not pipeline_path.exists():
        return jsonify({'erro': f"JSON do pipeline não encontrado: {slug}_pipeline.json"}), 404

    try:
        from calibrate import merge
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            merged, stats = merge(str(pipeline_path), str(editor_path), str(merged_path))

        total   = sum(stats.values())
        correct = stats.get('correct', 0)
        pct     = round(100 * correct / total) if total > 0 else 0

        return jsonify({'slug': slug, 'merged': str(merged_path),
                        'total': total, 'correct': correct, 'pct': pct})
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@app.route('/api/calibrar', methods=['GET'])
def calibrar():
    """Roda calibração sobre todos os arquivos mesclados."""
    all_merged = list(MERGED_DIR.glob('*_merged.json'))
    if not all_merged:
        return jsonify({'aviso': 'Nenhum arquivo mesclado encontrado.'}), 200

    import io
    from contextlib import redirect_stdout
    from calibrate import calibrate

    buf = io.StringIO()
    with redirect_stdout(buf):
        calibrate([str(p) for p in all_merged])
    report_text = buf.getvalue()

    report_path = CALIB_DIR / f"relatorio_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    return jsonify({
        'arquivos': len(all_merged),
        'report':   report_text,
        'saved':    str(report_path),
    })


@app.route('/api/relatorios', methods=['GET'])
def listar_relatorios():
    """Lista os relatórios de calibração gerados."""
    relatorios = []
    for f in sorted(CALIB_DIR.glob('*.txt'), reverse=True):
        relatorios.append({
            'nome':     f.name,
            'data':     datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M'),
            'tamanho':  f.stat().st_size,
        })
    return jsonify(relatorios)


# ════════════════════════════════════════════════════════════════
# INICIALIZAÇÃO
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("""
╔══════════════════════════════════════════════════╗
║          INSTRUMENTISTAS TOOLS v1.0              ║
╠══════════════════════════════════════════════════╣
║  Editor + Pipeline v6 + Calibração               ║
╚══════════════════════════════════════════════════╝

Pastas:
  scores/pdf/     → coloque os PDFs das partituras aqui
  scores/meta/    → metadados do upload
  scores/json/    → JSONs do editor e do pipeline
  scores/merged/  → JSONs após mesclagem
  calibration/    → relatórios de calibração

Abra o browser em:
  http://localhost:5000

Pressione Ctrl+C para parar.
""")
    app.run(host='0.0.0.0', port=5000, debug=False)
