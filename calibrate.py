#!/usr/bin/env python3
"""
calibrate.py — Mesclagem e calibração do protocolo CXD+T90

Três funções principais:

  merge(pipeline_json, editor_json)
    → Une o JSON do pipeline (tem 'protocol') com o JSON do editor (tem 'human')
    → Calcula 'ground_truth' completo para cada acorde
    → Salva um único JSON mesclado

  calibrate(json_paths)
    → Lê múltiplos JSONs mesclados
    → Gera relatório de onde o protocolo erra e o que ajustar

  report(json_paths)
    → Versão resumida do relatório para leitura rápida

Uso:
  python3 calibrate.py merge pipeline.json editor.json saida.json
  python3 calibrate.py calibrate louvor1.json louvor2.json ...
  python3 calibrate.py report louvor1.json louvor2.json ...
"""

import json
import sys
from collections import Counter
from datetime import datetime


# ════════════════════════════════════════════════════════════════
# TIPOS DE CORREÇÃO
# ════════════════════════════════════════════════════════════════

CORRECTION_TYPES = {
    'correct':             'protocolo acertou — acorde e sílaba corretos',
    'anchor_shift':        'acorde correto, sílaba deslocada horizontalmente',
    'wrong_syllable':      'acorde correto, sílaba errada',
    'wrong_chord':         'sílaba correta, acorde errado',
    'missing_chord':       'protocolo não detectou acorde que existe',
    'extra_chord':         'protocolo detectou acorde que não existe',
    'beat_wrong':          'acorde no tempo errado',
    'melisma_undetected':  'protocolo não detectou melisma',
    'pause_undetected':    'protocolo não detectou pausa melódica',
    'human_origin':        'cifrado do zero pelo humano (sem pipeline)',
    'unvalidated':         'ainda não revisado',
}


# ════════════════════════════════════════════════════════════════
# MESCLAGEM: pipeline + editor → ground_truth completo
# ════════════════════════════════════════════════════════════════

def classify_correction(p_syl, h_syl, p_chord, h_chord, p_rule):
    """
    Dado o que o protocolo decidiu e o que o humano decidiu,
    classifica o tipo de correção.
    """
    p_syl   = (p_syl   or '').strip()
    h_syl   = (h_syl   or '').strip()
    p_chord = (p_chord or '').strip()
    h_chord = (h_chord or '').strip()

    syl_ok   = p_syl   == h_syl
    chord_ok = p_chord == h_chord

    if syl_ok and chord_ok:
        return 'correct'

    if not h_chord and p_chord:
        return 'extra_chord'
    if h_chord and not p_chord:
        return 'missing_chord'

    if not chord_ok:
        return 'wrong_chord'

    # Mesmo acorde, sílabas diferentes
    if p_rule == 'T90.2_MELISMA':
        return 'melisma_undetected'
    if p_rule == 'T90.3_PAUSA':
        return 'pause_undetected'
    return 'wrong_syllable'


def compute_ground_truth(protocol_block, human_block, ts):
    """
    Calcula o bloco ground_truth completo comparando protocol e human.
    """
    if not human_block:
        return {
            'status':          'unvalidated',
            'correct':         None,
            'delta_cx':        None,
            'correction_type': 'unvalidated',
            'rule_changed':    None,
            'implied_rule':    None,
            'merged_at':       ts,
        }

    if human_block.get('validated_by') == 'editor-v4' and \
       not protocol_block.get('cx'):
        # Origem humana — sem pipeline para comparar
        return {
            'status':          'human_origin',
            'correct':         None,
            'delta_cx':        None,
            'correction_type': 'human_origin',
            'rule_changed':    None,
            'implied_rule':    'HUMAN',
            'merged_at':       ts,
        }

    p_syl   = protocol_block.get('syllable')
    h_syl   = human_block.get('syllable')
    p_cx    = protocol_block.get('syllable_cx') or protocol_block.get('float_cx')
    h_cx    = human_block.get('syllable_cx') or human_block.get('cx_display')
    p_rule  = protocol_block.get('rule', '')

    correct  = (p_syl == h_syl) if (p_syl is not None and h_syl is not None) else False
    delta_cx = round(abs(h_cx - p_cx), 1) if (h_cx and p_cx) else None

    ct           = classify_correction(p_syl, h_syl, None, None, p_rule)
    implied_rule = 'T90' if h_syl else 'VAZIO'
    rule_changed = (implied_rule != p_rule)

    return {
        'status':          'correct' if correct else 'corrected',
        'correct':         correct,
        'delta_cx':        delta_cx,
        'correction_type': ct,
        'rule_changed':    rule_changed,
        'implied_rule':    implied_rule,
        'merged_at':       ts,
    }


def _iter_chords_by_measure(data):
    """
    Retorna lista plana de acordes preservando seção, linha, compasso e ordem.
    """
    items = []
    for sec_i, sec in enumerate(data.get('sections', [])):
        for line_i, line in enumerate(sec.get('lines', [])):
            for measure_i, measure in enumerate(line.get('measures', [])):
                n = measure.get('n', 0)
                for chord_i, chord in enumerate(measure.get('chords', [])):
                    items.append({
                        'sec_i': sec_i,
                        'line_i': line_i,
                        'measure_i': measure_i,
                        'n': n,
                        'chord_i': chord_i,
                        'chord': chord,
                    })
    return items


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _chord_ratio(chord):
    """
    Melhor posição normalizada disponível do acorde dentro do compasso.
    """
    if chord.get('beat_ratio') is not None:
        return _safe_float(chord.get('beat_ratio'), 0.0)

    human = chord.get('human') or {}
    if human.get('cx_display') is not None:
        return _safe_float(human.get('cx_display'), 0.0)

    protocol = chord.get('protocol') or {}
    if protocol.get('float_cx') is not None:
        return _safe_float(protocol.get('float_cx'), 0.0)

    return 0.0


def _chord_beat_bi(chord):
    human = chord.get('human') or {}
    protocol = chord.get('protocol') or {}

    if human.get('beat_bi') is not None:
        return human.get('beat_bi')

    if chord.get('bi') is not None:
        return chord.get('bi')

    if protocol.get('bi') is not None:
        return protocol.get('bi')

    return None


def _build_editor_index(editor):
    """
    Indexa acordes humanos por compasso e símbolo, preservando múltiplas ocorrências.
    """
    by_n_sym = {}

    for item in _iter_chords_by_measure(editor):
        chord = item['chord']
        sym = (chord.get('symbol') or '').strip()
        n = item['n']

        if not sym:
            continue

        human = chord.get('human') or {}
        entry = {
            'key': (item['sec_i'], item['line_i'], item['measure_i'], item['chord_i']),
            'n': n,
            'sym': sym,
            'beat': chord.get('beat', 1),
            'beat_ratio': _chord_ratio(chord),
            'beat_bi': _chord_beat_bi(chord),
            'chord': chord,
            'human': human,
            'used': False,
        }

        by_n_sym.setdefault((n, sym), []).append(entry)

    return by_n_sym


def _pick_best_editor_match(candidates, p_chord):
    """
    Escolhe o melhor acorde humano para um acorde do pipeline.
    Critério:
    1. não usado ainda
    2. menor distância de beat_ratio
    3. menor distância de beat_bi, quando existir
    4. menor distância de beat
    """
    available = [c for c in candidates if not c.get('used')]
    if not available:
        return None, None

    p_ratio = _chord_ratio(p_chord)
    p_bi = _chord_beat_bi(p_chord)
    p_beat = p_chord.get('beat', 1)

    def score(c):
        c_ratio = c.get('beat_ratio', 0.0)
        c_bi = c.get('beat_bi')
        c_beat = c.get('beat', 1)

        ratio_delta = abs(_safe_float(p_ratio, 0.0) - _safe_float(c_ratio, 0.0))

        if p_bi is not None and c_bi is not None:
            bi_delta = abs(int(p_bi) - int(c_bi))
        else:
            bi_delta = 99

        beat_delta = abs(_safe_float(p_beat, 0.0) - _safe_float(c_beat, 0.0))

        return (ratio_delta, bi_delta, beat_delta)

    best = min(available, key=score)
    ratio_delta, bi_delta, beat_delta = score(best)

    if ratio_delta <= 0.26:
        method = 'n_sym_ratio'
    elif bi_delta != 99 and bi_delta <= 2:
        method = 'n_sym_bi'
    else:
        method = 'n_sym_loose'

    return best, method

def _find_best_editor_match(editor_index, n, sym, p_chord, window=2):
    """
    Procura o melhor acorde humano:
    1. mesmo compasso
    2. compassos próximos: n-1, n+1, n-2, n+2
    3. mesmo símbolo
    4. menor distância de beat_ratio / beat_bi / beat
    """
    offsets = [0]

    for i in range(1, window + 1):
        offsets.append(-i)
        offsets.append(i)

    best = None
    best_method = None
    best_score = None

    p_ratio = _chord_ratio(p_chord)
    p_bi = _chord_beat_bi(p_chord)
    p_beat = p_chord.get('beat', 1)

    for offset in offsets:
        target_n = n + offset
        candidates = editor_index.get((target_n, sym), [])

        for c in candidates:
            if c.get('used'):
                continue

            c_ratio = c.get('beat_ratio', 0.0)
            c_bi = c.get('beat_bi')
            c_beat = c.get('beat', 1)

            ratio_delta = abs(_safe_float(p_ratio, 0.0) - _safe_float(c_ratio, 0.0))

            if p_bi is not None and c_bi is not None:
                bi_delta = abs(int(p_bi) - int(c_bi))
            else:
                bi_delta = 99

            beat_delta = abs(_safe_float(p_beat, 0.0) - _safe_float(c_beat, 0.0))
            measure_delta = abs(offset)

            score = (measure_delta, ratio_delta, bi_delta, beat_delta)

            if best_score is None or score < best_score:
                best = c
                best_score = score

                if offset == 0:
                    prefix = 'n_sym'
                else:
                    prefix = 'near_measure'

                if ratio_delta <= 0.26:
                    suffix = 'ratio'
                elif bi_delta != 99 and bi_delta <= 2:
                    suffix = 'bi'
                else:
                    suffix = 'loose'

                best_method = f'{prefix}_{suffix}'

    return best, best_method

def merge(pipeline_path, editor_path, output_path):
    """
    Une o JSON do pipeline com o JSON do editor.

    Nova estratégia:
    1. Indexa todos os acordes humanos por (número do compasso, símbolo)
    2. Preserva múltiplas ocorrências do mesmo acorde
    3. Para cada acorde do pipeline, escolhe o humano mais próximo por beat_ratio
    4. Não reutiliza o mesmo acorde humano em dois acordes do pipeline
    5. Calcula ground_truth
    """
    print(f"\nMesclando:")
    print(f"  pipeline: {pipeline_path}")
    print(f"  editor:   {editor_path}")
    print(f"  saída:    {output_path}")

    with open(pipeline_path, encoding='utf-8') as f:
        pipeline = json.load(f)
    with open(editor_path, encoding='utf-8') as f:
        editor = json.load(f)

    ts = datetime.utcnow().isoformat() + 'Z'

    editor_index = _build_editor_index(editor)

    merged = dict(pipeline)
    merged['merged_at'] = ts
    merged['merge_sources'] = {
        'pipeline': pipeline_path,
        'editor': editor_path,
    }

    stats = Counter()

    for sec in merged.get('sections', []):
        for line in sec.get('lines', []):
            for measure in line.get('measures', []):
                n = measure.get('n', 0)

                for chord in measure.get('chords', []):
                    sym = (chord.get('symbol') or '').strip()
                    p_beat = chord.get('beat', 1)

                    match, match_method = _find_best_editor_match(
    			editor_index,
    			n,
    			sym,
    			chord,
    			window=2
		    )

                    chord['_match_method'] = match_method

                    if match:
                        match['used'] = True
                        editor_chord = match['chord']
                        e_human = editor_chord.get('human') or {}

                        chord['human'] = {
                            'syllable': e_human.get('syllable') or editor_chord.get('protocol', {}).get('syllable'),
                            'syllable_bi': e_human.get('syllable_bi'),
                            'syllable_cx': e_human.get('syllable_cx'),
                            'cx_display': e_human.get('cx_display'),
                            'beat_bi': e_human.get('beat_bi'),
                            'is_rest': e_human.get('is_rest', False),
                            'rhythm': e_human.get('rhythm', 'nota'),
                            'validated_by': e_human.get('validated_by', 'editor'),
                            'validated_at': e_human.get('validated_at', ts),
                            'note': e_human.get('note', ''),
                            '_match': match_method,
                        }

                        e_beat = editor_chord.get('beat', p_beat)
                        chord['_beat_delta'] = abs(_safe_float(p_beat, 0.0) - _safe_float(e_beat, 0.0))
                        chord['_ratio_delta'] = abs(_chord_ratio(chord) - match.get('beat_ratio', 0.0))
                    else:
                        chord['human'] = None
                        chord['_beat_delta'] = None
                        chord['_ratio_delta'] = None

                    chord['ground_truth'] = compute_ground_truth(
                        chord.get('protocol', {}),
                        chord.get('human'),
                        ts,
                    )

                    stats[chord['ground_truth']['correction_type']] += 1

    # Acrescentar acordes humanos que não apareceram no pipeline como missing_chord.
    # Mantém o merged baseado no pipeline, mas registra diagnóstico global.
    missing_human = []
    for entries in editor_index.values():
        for entry in entries:
            if not entry.get('used'):
                ch = entry['chord']
                missing_human.append({
                    'n': entry['n'],
                    'symbol': entry['sym'],
                    'beat': ch.get('beat'),
                    'beat_ratio': _chord_ratio(ch),
                    'human': ch.get('human'),
                    'ground_truth': {
                        'status': 'corrected',
                        'correct': False,
                        'delta_cx': None,
                        'correction_type': 'missing_chord',
                        'rule_changed': None,
                        'implied_rule': 'HUMAN',
                        'merged_at': ts,
                    }
                })
                stats['missing_chord'] += 1

    merged['_missing_human_chords'] = missing_human

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    total = sum(stats.values())
    correct = stats.get('correct', 0)
    pct = round(100 * correct / total) if total else 0

    print(f"\n✅ Mesclagem concluída")
    print(f"   {total} eventos processados")
    print(f"   {correct}/{total} corretos ({pct}%)")
    print(f"   missing_human_chords: {len(missing_human)}")
    print(f"\n   Distribuição:")
    for ct, n in stats.most_common():
        print(f"     {str(ct):<25} {n:>4}")

    return merged, stats

# ════════════════════════════════════════════════════════════════
# CALIBRAÇÃO: lê JSONs mesclados e sugere ajustes no pipeline
# ════════════════════════════════════════════════════════════════

def calibrate(json_paths):
    """
    Lê múltiplos JSONs mesclados e gera relatório de calibração.
    Indica onde o protocolo erra sistematicamente e o que ajustar.
    """
    correction_counts = Counter()
    rule_errors       = Counter()
    delta_cx_vals     = []
    melisma_cases     = []
    pause_cases       = []

    total_chords    = 0
    total_validated = 0
    total_correct   = 0
    total_human     = 0
    beat_deltas     = []
    match_methods   = Counter()

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        for sec in data.get('sections', []):
            for line in sec.get('lines', []):
                for measure in line.get('measures', []):
                    for chord in measure.get('chords', []):
                        total_chords += 1

                        # Registrar método de cruzamento
                        mm = chord.get('_match_method')
                        if mm: match_methods[mm] += 1

                        # Registrar beat_delta
                        bd = chord.get('_beat_delta')
                        if bd is not None: beat_deltas.append(bd)

                        gt = chord.get('ground_truth', {})
                        ct = gt.get('correction_type', 'unvalidated')

                        if ct == 'unvalidated':
                            continue
                        if ct == 'human_origin':
                            total_human += 1
                            continue

                        total_validated += 1
                        correction_counts[ct] += 1

                        if gt.get('correct'):
                            total_correct += 1
                        else:
                            p_rule = chord.get('protocol', {}).get('rule', '?')
                            rule_errors[f"{p_rule} → {ct}"] += 1

                            if ct == 'anchor_shift' and gt.get('delta_cx'):
                                delta_cx_vals.append(gt['delta_cx'])
                            if ct == 'melisma_undetected':
                                melisma_cases.append(chord)
                            if ct == 'pause_undetected':
                                pause_cases.append(chord)

    # ── Relatório ──────────────────────────────────────────────
    sep = '═' * 62
    out = [
        sep,
        f"RELATÓRIO DE CALIBRAÇÃO — {len(json_paths)} louvor(es)",
        f"Gerado em: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        sep,
        '',
        f"Total de acordes:      {total_chords}",
        f"Cifrados pelo humano:  {total_human}  (sem comparação — origem humana)",
        f"Validados (com merge): {total_validated}",
        f"Corretos:              {total_correct}"
        + (f"  ({round(100*total_correct/total_validated)}%)" if total_validated else ''),
        '',
    ]

    if total_validated == 0:
        out += [
            '⚠️  Nenhum acorde com ground_truth validado encontrado.',
            '   Execute primeiro: python3 calibrate.py merge <pipeline.json> <editor.json> <saida.json>',
            '',
        ]
    else:
        out += ['DISTRIBUIÇÃO POR TIPO DE CORREÇÃO:', '']
        for ct, n in correction_counts.most_common():
            pct   = round(100 * n / total_validated)
            descr = CORRECTION_TYPES.get(ct, '')
            out.append(f"  {str(ct):<25} {n:>4}  ({pct:>3}%)  {descr}")

        if rule_errors:
            out += ['', 'ERROS POR REGRA ORIGINAL:', '']
            for key, n in rule_errors.most_common():
                out.append(f"  {key:<40} {n:>4} vezes")

        # Sugestões de recalibração
        out += ['', '─' * 62, 'SUGESTÕES DE RECALIBRAÇÃO', '─' * 62, '']

        if delta_cx_vals:
            avg = sum(delta_cx_vals) / len(delta_cx_vals)
            mx  = max(delta_cx_vals)
            out += [
                f"anchor_shift: {len(delta_cx_vals)} casos",
                f"  delta_cx médio: {avg:.1f}px  máximo: {mx:.1f}px",
            ]
            if avg > 6:
                out.append(f"  → considerar ajustar CHAR_SCALE no pipeline")
            else:
                out.append(f"  → delta dentro do aceitável (< 6px)")
            out.append('')

        melisma_pct = correction_counts.get('melisma_undetected', 0)
        if melisma_pct > 0:
            pct = round(100 * melisma_pct / total_validated)
            out += [
                f"melisma_undetected: {melisma_pct} casos ({pct}%)",
                f"  → considerar reduzir MELISMA_THRESH (atual: 25px)",
                f"  → sugestão: testar com 20px",
                '',
            ]

        pause_pct = correction_counts.get('pause_undetected', 0)
        if pause_pct > 0:
            pct = round(100 * pause_pct / total_validated)
            out += [
                f"pause_undetected: {pause_pct} casos ({pct}%)",
                f"  → revisar critério T90.3 no pipeline",
                '',
            ]

        missing = correction_counts.get('missing_chord', 0)
        if missing > 0:
            out += [
                f"missing_chord: {missing} casos",
                f"  → acordes que o pipeline não detectou",
                f"  → verificar janelas cmin/cmax no LINES_MAP",
                '',
            ]

    # Beat delta
    if beat_deltas:
        avg_bd = sum(beat_deltas) / len(beat_deltas)
        zero   = beat_deltas.count(0)
        out += ['', '─'*62, 'BEAT DELTA (protocolo vs humano)', '─'*62, '']
        out.append(f"  Total cruzados:    {len(beat_deltas)}")
        out.append(f"  Beat correto (Δ=0): {zero} ({round(100*zero/len(beat_deltas))}%)")
        out.append(f"  Δ médio:           {avg_bd:.2f} tempos")
        if avg_bd > 1.0:
            out.append(f"  → P2: considerar refinamento do cálculo de beat")
        else:
            out.append(f"  → beat dentro do aceitável (< 1 tempo)")

    # Métodos de cruzamento
    if match_methods:
        out += ['', 'MÉTODOS DE CRUZAMENTO NO MERGE:', '']
        for mm, n in match_methods.most_common():
            out.append(f"  {mm:<15} {n:>4} acordes cruzados")

    out.append(sep)
    report_text = '\n'.join(out)
    print(report_text)
    return report_text


# ════════════════════════════════════════════════════════════════
# RELATÓRIO RÁPIDO
# ════════════════════════════════════════════════════════════════

def report(json_paths):
    """Versão resumida — só os números principais."""
    total = correct = human = unval = 0

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        for sec in data.get('sections', []):
            for line in sec.get('lines', []):
                for measure in line.get('measures', []):
                    for chord in measure.get('chords', []):
                        total += 1
                        ct = chord.get('ground_truth', {}).get('correction_type', '')
                        if ct == 'correct':      correct += 1
                        elif ct == 'human_origin': human += 1
                        elif ct == 'unvalidated':  unval += 1

    validated = total - human - unval
    pct = round(100 * correct / validated) if validated > 0 else 0

    print(f"\n{'─'*40}")
    print(f"RELATÓRIO — {len(json_paths)} arquivo(s)")
    print(f"{'─'*40}")
    print(f"Acordes total:      {total}")
    print(f"Origem humana:      {human}")
    print(f"Não revisados:      {unval}")
    print(f"Validados:          {validated}")
    print(f"Corretos:           {correct}  ({pct}%)")
    print(f"{'─'*40}\n")


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == 'merge':
        if len(sys.argv) < 5:
            print("Uso: python3 calibrate.py merge <pipeline.json> <editor.json> <saida.json>")
            sys.exit(1)
        merge(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == 'calibrate':
        if len(sys.argv) < 3:
            print("Uso: python3 calibrate.py calibrate <arquivo1.json> [arquivo2.json ...]")
            sys.exit(1)
        calibrate(sys.argv[2:])

    elif cmd == 'report':
        if len(sys.argv) < 3:
            print("Uso: python3 calibrate.py report <arquivo1.json> [arquivo2.json ...]")
            sys.exit(1)
        report(sys.argv[2:])

    else:
        print(f"Comando desconhecido: {cmd}")
        print("Comandos disponíveis: merge, calibrate, report")
        sys.exit(1)


if __name__ == '__main__':
    main()
