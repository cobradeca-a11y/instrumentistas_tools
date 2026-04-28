#!/usr/bin/env python3
"""
calibrate.py — Mesclagem e calibração do protocolo CXD+T90

Funções principais:

  merge(pipeline_json, editor_json, saida_json)
    → Une JSON do pipeline com JSON humano do editor
    → Calcula ground_truth
    → Salva JSON mesclado

  calibrate(json_paths)
    → Lê JSONs mesclados
    → Gera relatório de calibração

  report(json_paths)
    → Relatório resumido

  debug(merged_json)
    → Gera CSV em calibration/debug_<slug>.csv

Uso:
  python calibrate.py merge scores/json/ainda-uma-vez_pipeline.json scores/json/ainda-uma-vez.json scores/merged/ainda-uma-vez_merged.json
  python calibrate.py calibrate scores/merged/ainda-uma-vez_merged.json
  python calibrate.py report scores/merged/ainda-uma-vez_merged.json
  python calibrate.py debug scores/merged/ainda-uma-vez_merged.json
"""

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


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
# UTILITÁRIOS
# ════════════════════════════════════════════════════════════════

def utc_iso():
    return datetime.now(timezone.utc).isoformat()


def utc_label():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v, default=None):
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


# ════════════════════════════════════════════════════════════════
# CLASSIFICAÇÃO
# ════════════════════════════════════════════════════════════════

def classify_correction(p_syl, h_syl, p_chord, h_chord, p_rule):
    """
    Classifica o tipo de correção comparando protocolo e humano.
    """
    p_syl   = (p_syl   or '').strip()
    h_syl   = (h_syl   or '').strip()
    p_chord = (p_chord or '').strip()
    h_chord = (h_chord or '').strip()

    syl_ok   = p_syl == h_syl
    chord_ok = p_chord == h_chord

    if syl_ok and chord_ok:
        return 'correct'

    if not h_chord and p_chord:
        return 'extra_chord'

    if h_chord and not p_chord:
        return 'missing_chord'

    if not chord_ok:
        return 'wrong_chord'

    if p_rule == 'T90.2_MELISMA':
        return 'melisma_undetected'

    if p_rule == 'T90.3_PAUSA':
        return 'pause_undetected'

    return 'wrong_syllable'


def compute_ground_truth(protocol_block, human_block, ts):
    """
    Calcula ground_truth comparando protocol e human.
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

    if human_block.get('validated_by') == 'editor-v4' and not protocol_block.get('cx'):
        return {
            'status':          'human_origin',
            'correct':         None,
            'delta_cx':        None,
            'correction_type': 'human_origin',
            'rule_changed':    None,
            'implied_rule':    'HUMAN',
            'merged_at':       ts,
        }

    p_syl  = protocol_block.get('syllable')
    h_syl  = human_block.get('syllable')
    p_cx   = protocol_block.get('syllable_cx')
    if p_cx is None:
        p_cx = protocol_block.get('float_cx')

    h_cx = human_block.get('syllable_cx')
    if h_cx is None:
        h_cx = human_block.get('cx_display')

    p_rule = protocol_block.get('rule', '')

    ct = classify_correction(p_syl, h_syl, None, None, p_rule)

    # Protocolo e humano sem sílaba não é acerto musical validável.
    if ct == 'correct' and not p_syl and not h_syl:
        ct = 'unvalidated'

    correct = (ct == 'correct')

    delta_cx = (
        round(abs(float(h_cx) - float(p_cx)), 3)
        if h_cx is not None and p_cx is not None
        else None
    )

    implied_rule = 'T90' if h_syl else 'VAZIO'
    rule_changed = (implied_rule != p_rule)

    return {
        'status':          'correct' if correct else ('unvalidated' if ct == 'unvalidated' else 'corrected'),
        'correct':         correct,
        'delta_cx':        delta_cx,
        'correction_type': ct,
        'rule_changed':    rule_changed,
        'implied_rule':    implied_rule,
        'merged_at':       ts,
    }


# ════════════════════════════════════════════════════════════════
# EXTRAÇÃO LINEAR
# ════════════════════════════════════════════════════════════════

def _iter_chords_by_measure(data):
    """
    Retorna acordes preservando seção, linha, compasso e ordem.
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
                        'measure': measure,
                        'chord': chord,
                    })

    return items


def _chord_ratio(chord):
    """
    Melhor posição normalizada disponível dentro do compasso.
    """
    if chord.get('beat_ratio') is not None:
        return _safe_float(chord.get('beat_ratio'), 0.0)

    human = chord.get('human') or {}
    if human.get('cx_display') is not None:
        return _safe_float(human.get('cx_display'), 0.0)

    protocol = chord.get('protocol') or {}
    if protocol.get('float_cx') is not None:
        return _safe_float(protocol.get('float_cx'), 0.0)

    if protocol.get('syllable_cx') is not None:
        return _safe_float(protocol.get('syllable_cx'), 0.0)

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
    Indexa acordes humanos por compasso + símbolo.
    Preserva múltiplas ocorrências.
    """
    by_n_sym = {}
    global_order = 0

    for item in _iter_chords_by_measure(editor):
        chord = item['chord']
        sym = (chord.get('symbol') or '').strip()
        n = item['n']

        if not sym:
            continue

        global_order += 1

        entry = {
            'key':          (item['sec_i'], item['line_i'], item['measure_i'], item['chord_i']),
            'n':            n,
            'sym':          sym,
            'beat':         chord.get('beat', 1),
            'beat_ratio':   _chord_ratio(chord),
            'beat_bi':      _chord_beat_bi(chord),
            'global_order': global_order,
            'chord':        chord,
            'human':        chord.get('human') or {},
            'used':         False,
        }

        by_n_sym.setdefault((n, sym), []).append(entry)

    return by_n_sym


def _build_editor_sequence(editor):
    """
    Lista linear de acordes humanos na ordem musical.
    """
    seq = []
    global_order = 0

    for item in _iter_chords_by_measure(editor):
        chord = item['chord']
        sym = (chord.get('symbol') or '').strip()

        if not sym:
            continue

        global_order += 1

        seq.append({
            'global_order': global_order,
            'n':            item['n'],
            'sym':          sym,
            'beat':         chord.get('beat', 1),
            'beat_ratio':   _chord_ratio(chord),
            'beat_bi':      _chord_beat_bi(chord),
            'chord':        chord,
            'human':        chord.get('human') or {},
            'used':         False,
        })

    return seq

def _build_pipeline_sequence(pipeline):
    """
    Lista linear de acordes do pipeline na ordem musical.
    """
    seq = []
    global_order = 0

    for item in _iter_chords_by_measure(pipeline):
        chord = item['chord']
        sym = (chord.get('symbol') or '').strip()

        if not sym:
            continue

        global_order += 1

        seq.append({
            'global_order': global_order,
            'n': item['n'],
            'sym': sym,
            'beat': chord.get('beat', 1),
            'beat_ratio': _chord_ratio(chord),
            'beat_bi': _chord_beat_bi(chord),
            'chord': chord,
        })

    return seq


def _sequence_alignment_map(pipeline_seq, editor_seq):
    """
    Alinha pipeline e editor por sequência de símbolos.
    Permite acordes extras no editor ou ausentes no pipeline.
    Retorna: {pipeline_global_order: editor_entry}
    """
    n = len(pipeline_seq)
    m = len(editor_seq)

    dp = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(n):
        for j in range(m):
            if pipeline_seq[i]['sym'] == editor_seq[j]['sym']:
                dp[i + 1][j + 1] = dp[i][j] + 1
            else:
                dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])

    mapping = {}
    i, j = n, m

    while i > 0 and j > 0:
        if pipeline_seq[i - 1]['sym'] == editor_seq[j - 1]['sym']:
            p = pipeline_seq[i - 1]
            e = editor_seq[j - 1]
            mapping[p['global_order']] = e
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1

    return mapping

def _mark_editor_used(editor_index, editor_seq, matched_chord):
    """
    Marca como usado em todos os índices o mesmo objeto de acorde humano.
    """
    if matched_chord is None:
        return

    for entries in editor_index.values():
        for entry in entries:
            if entry.get('chord') is matched_chord:
                entry['used'] = True

    for entry in editor_seq:
        if entry.get('chord') is matched_chord:
            entry['used'] = True


# ════════════════════════════════════════════════════════════════
# MATCHING
# ════════════════════════════════════════════════════════════════

def _score_match(p_chord, e):
    p_ratio = _chord_ratio(p_chord)
    p_bi = _chord_beat_bi(p_chord)
    p_beat = p_chord.get('beat', 1)

    e_ratio = e.get('beat_ratio', 0.0)
    e_bi = e.get('beat_bi')
    e_beat = e.get('beat', 1)

    ratio_delta = abs(_safe_float(p_ratio, 0.0) - _safe_float(e_ratio, 0.0))

    if p_bi is not None and e_bi is not None:
        bi_delta = abs(int(p_bi) - int(e_bi))
    else:
        bi_delta = 99

    beat_delta = abs(_safe_float(p_beat, 0.0) - _safe_float(e_beat, 0.0))

    return ratio_delta, bi_delta, beat_delta


def _find_best_editor_match(editor_index, n, sym, p_chord, window=2):
    """
    Busca por:
      1. mesmo compasso
      2. compasso próximo
      3. mesmo símbolo
      4. menor distância rítmica
    """
    offsets = [0]
    for i in range(1, window + 1):
        offsets.append(-i)
        offsets.append(i)

    best = None
    best_method = None
    best_score = None

    for offset in offsets:
        target_n = n + offset
        candidates = editor_index.get((target_n, sym), [])

        for c in candidates:
            if c.get('used'):
                continue

            ratio_delta, bi_delta, beat_delta = _score_match(p_chord, c)
            measure_delta = abs(offset)
            score = (measure_delta, ratio_delta, bi_delta, beat_delta)

            if best_score is None or score < best_score:
                best = c
                best_score = score

                prefix = 'n_sym' if offset == 0 else 'near_measure'

                if ratio_delta <= 0.26:
                    suffix = 'ratio'
                elif bi_delta != 99 and bi_delta <= 2:
                    suffix = 'bi'
                else:
                    suffix = 'loose'

                best_method = f'{prefix}_{suffix}'

    if not best:
        return None, None

    measure_delta, ratio_delta, bi_delta, beat_delta = best_score

    # Bloqueia casamento frouxo demais.
    if best_method.endswith('_loose'):
        if ratio_delta > 0.38:
            return None, None

        if measure_delta > 1 and bi_delta > 2:
            return None, None

    return best, best_method


def _find_sequence_match(editor_seq, p_chord, p_order, window=8):
    """
    Fallback por sequência global.
    Mais conservador:
    - não cruza VAZIO por sequência
    - não aceita seq_loose
    - exige proximidade rítmica mínima
    """
    sym = (p_chord.get('symbol') or '').strip()
    protocol = p_chord.get('protocol') or {}
    p_rule = protocol.get('rule')
    p_syl = (protocol.get('syllable') or '').strip()

    # VAZIO por sequência gera muito falso positivo.
    if p_rule == 'VAZIO':
        return None, None

    candidates = []

    for e in editor_seq:
        if e.get('used'):
            continue

        if e.get('sym') != sym:
            continue

        order_delta = abs(e.get('global_order', 0) - p_order)

        if order_delta > window:
            continue

        ratio_delta, bi_delta, beat_delta = _score_match(p_chord, e)

        human = e.get('human') or {}
        h_syl = (human.get('syllable') or '').strip()

        # Se os dois têm sílaba e são muito diferentes, só aceita se o ritmo for forte.
        both_have_syl = bool(p_syl and h_syl)
        syllable_same = p_syl == h_syl

        if both_have_syl and not syllable_same:
            if ratio_delta > 0.18:
                continue

        score = (order_delta, ratio_delta, bi_delta, beat_delta)
        candidates.append((score, e))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0])
    best_score, best = candidates[0]
    order_delta, ratio_delta, bi_delta, beat_delta = best_score

    if ratio_delta <= 0.22:
        method = 'seq_ratio'
    elif bi_delta != 99 and bi_delta <= 1:
        method = 'seq_bi'
    else:
        return None, None

    return best, method

# ════════════════════════════════════════════════════════════════
# MERGE
# ════════════════════════════════════════════════════════════════

def merge(pipeline_path, editor_path, output_path):
    """
    Une pipeline + editor.
    """
    print(f"\nMesclando:")
    print(f"  pipeline: {pipeline_path}")
    print(f"  editor:   {editor_path}")
    print(f"  saída:    {output_path}")

    with open(pipeline_path, encoding='utf-8') as f:
        pipeline = json.load(f)

    with open(editor_path, encoding='utf-8') as f:
        editor = json.load(f)

    ts = utc_iso()

    editor_index = _build_editor_index(editor)
    editor_seq = _build_editor_sequence(editor)
    pipeline_seq = _build_pipeline_sequence(pipeline)
    alignment_map = _sequence_alignment_map(pipeline_seq, editor_seq)
    pipeline_order = 0

    merged = dict(pipeline)
    merged['merged_at'] = ts
    merged['merge_sources'] = {
        'pipeline': pipeline_path,
        'editor':   editor_path,
    }

    stats = Counter()

    for sec in merged.get('sections', []):
        for line in sec.get('lines', []):
            for measure in line.get('measures', []):
                n = measure.get('n', 0)

                for chord in measure.get('chords', []):
                    sym = (chord.get('symbol') or '').strip()
                    p_beat = chord.get('beat', 1)
                    pipeline_order += 1

                    match, match_method = _find_best_editor_match(
                        editor_index,
                        n,
                        sym,
                        chord,
                        window=2,
                    )

                    # Se o match geométrico falhou ou foi frouxo, tenta alinhamento musical global.
                    if not match or (match_method and match_method.endswith('_loose')):
                        aligned = alignment_map.get(pipeline_order)

                        if aligned and not aligned.get('used'):
                            protocol = chord.get('protocol') or {}
                            p_rule = protocol.get('rule')
                            p_syl = (protocol.get('syllable') or '').strip()

                            human = aligned.get('human') or {}
                            h_syl = (human.get('syllable') or '').strip()

                            ratio_delta, bi_delta, beat_delta = _score_match(chord, aligned)

                            accept_align = True

                            # Não usar alinhamento global para VAZIO:
                            # se o pipeline não achou sílaba, não deve casar com sílaba humana por sequência.
                            if p_rule == 'VAZIO':
                                accept_align = False

                            # Se sílabas são diferentes, exigir proximidade rítmica forte.
                            if p_syl and h_syl and p_syl != h_syl:
                                if ratio_delta > 0.18 and bi_delta > 1 and beat_delta > 0:
                                    accept_align = False

                            # Bloqueio geral de alinhamento distante.
                            if ratio_delta > 0.35 and bi_delta > 2:
                                accept_align = False

                            if accept_align:
                                match = aligned
                                match_method = 'seq_align'

                    # Último fallback conservador por sequência local.
                    if not match:
                        match, match_method = _find_sequence_match(
                            editor_seq,
                            chord,
                            pipeline_order,
                            window=8,
                        )

                    chord['_match_method'] = match_method

                    if match:
                        _mark_editor_used(editor_index, editor_seq, match.get('chord'))

                        editor_chord = match['chord']
                        e_human = editor_chord.get('human') or {}

                        chord['human'] = {
                            'syllable':      e_human.get('syllable') or editor_chord.get('protocol', {}).get('syllable'),
                            'syllable_bi':   e_human.get('syllable_bi'),
                            'syllable_cx':   e_human.get('syllable_cx'),
                            'cx_display':    e_human.get('cx_display'),
                            'beat_bi':       e_human.get('beat_bi'),
                            'is_rest':       e_human.get('is_rest', False),
                            'rhythm':        e_human.get('rhythm', 'nota'),
                            'validated_by':  e_human.get('validated_by', 'editor'),
                            'validated_at':  e_human.get('validated_at', ts),
                            'note':          e_human.get('note', ''),
                            '_match':        match_method,
                            '_editor_n':     match.get('n'),
                            '_global_order': match.get('global_order'),
                        }

                        e_beat = editor_chord.get('beat', p_beat)

                        chord['_beat_delta'] = abs(
                            _safe_float(p_beat, 0.0) - _safe_float(e_beat, 0.0)
                        )

                        chord['_ratio_delta'] = abs(
                            _chord_ratio(chord) - _safe_float(match.get('beat_ratio'), 0.0)
                        )
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

    missing_human = []

    for entry in editor_seq:
        if not entry.get('used'):
            ch = entry['chord']

            missing_human.append({
                'n':            entry['n'],
                'symbol':       entry['sym'],
                'beat':         ch.get('beat'),
                'beat_ratio':   _chord_ratio(ch),
                'global_order': entry.get('global_order'),
                'human':        ch.get('human'),
                'ground_truth': {
                    'status':          'corrected',
                    'correct':         False,
                    'delta_cx':        None,
                    'correction_type': 'missing_chord',
                    'rule_changed':    None,
                    'implied_rule':    'HUMAN',
                    'merged_at':       ts,
                },
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
# CALIBRAÇÃO
# ════════════════════════════════════════════════════════════════

def calibrate(json_paths):
    correction_counts = Counter()
    rule_errors = Counter()
    delta_cx_vals = []
    beat_deltas = []
    match_methods = Counter()

    total_pipeline_chords = 0
    total_missing_human = 0
    total_validated = 0
    total_correct = 0
    total_human_origin = 0
    total_unvalidated = 0

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        for sec in data.get('sections', []):
            for line in sec.get('lines', []):
                for measure in line.get('measures', []):
                    for chord in measure.get('chords', []):
                        total_pipeline_chords += 1

                        mm = chord.get('_match_method')
                        if mm:
                            match_methods[mm] += 1

                        bd = chord.get('_beat_delta')
                        if bd is not None:
                            beat_deltas.append(bd)

                        gt = chord.get('ground_truth', {})
                        ct = gt.get('correction_type', 'unvalidated')

                        if ct == 'unvalidated':
                            total_unvalidated += 1
                            continue

                        if ct == 'human_origin':
                            total_human_origin += 1
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

        for mh in data.get('_missing_human_chords', []):
            total_missing_human += 1
            correction_counts['missing_chord'] += 1

    sep = '═' * 62

    out = [
        sep,
        f"RELATÓRIO DE CALIBRAÇÃO — {len(json_paths)} louvor(es)",
        f"Gerado em: {utc_label()}",
        sep,
        '',
        f"Acordes do pipeline:       {total_pipeline_chords}",
        f"Acordes humanos faltantes: {total_missing_human}",
        f"Cifrados pelo humano:      {total_human_origin}  (sem comparação — origem humana)",
        f"Não validados:             {total_unvalidated}",
        f"Validados com merge:       {total_validated}",
        f"Corretos:                  {total_correct}"
        + (f"  ({round(100 * total_correct / total_validated)}%)" if total_validated else ''),
        '',
    ]

    if total_validated == 0 and total_missing_human == 0:
        out += [
            '⚠️  Nenhum acorde com ground_truth validado encontrado.',
            '   Execute primeiro: python calibrate.py merge <pipeline.json> <editor.json> <saida.json>',
            '',
        ]
    else:
        out += ['DISTRIBUIÇÃO POR TIPO DE CORREÇÃO:', '']

        denom = max(total_validated + total_missing_human, 1)

        for ct, n in correction_counts.most_common():
            pct = round(100 * n / denom)
            descr = CORRECTION_TYPES.get(ct, '')
            out.append(f"  {str(ct):<25} {n:>4}  ({pct:>3}%)  {descr}")

        if rule_errors:
            out += ['', 'ERROS POR REGRA ORIGINAL:', '']
            for key, n in rule_errors.most_common():
                out.append(f"  {key:<40} {n:>4} vezes")

        out += ['', '─' * 62, 'SUGESTÕES DE RECALIBRAÇÃO', '─' * 62, '']

        missing = correction_counts.get('missing_chord', 0)
        if missing > 0:
            out += [
                f"missing_chord: {missing} casos",
                "  → pipeline não detectou acorde existente no editor ou merge ainda não cruzou",
                "  → verificar janela de acordes, numeração de compassos e sequência global",
                '',
            ]

        melisma = correction_counts.get('melisma_undetected', 0)
        if melisma > 0:
            pct = round(100 * melisma / denom)
            out += [
                f"melisma_undetected: {melisma} casos ({pct}%)",
                "  → considerar reduzir MELISMA_THRESH",
                "  → sugestão inicial: testar 20px",
                '',
            ]

    if beat_deltas:
        avg_bd = sum(beat_deltas) / len(beat_deltas)
        zero = beat_deltas.count(0)

        out += ['', '─' * 62, 'BEAT DELTA (protocolo vs humano)', '─' * 62, '']
        out.append(f"  Total cruzados:     {len(beat_deltas)}")
        out.append(f"  Beat correto (Δ=0): {zero} ({round(100 * zero / len(beat_deltas))}%)")
        out.append(f"  Δ médio:            {avg_bd:.2f} tempos")

        if avg_bd > 1.0:
            out.append("  → P2: considerar refinamento do cálculo de beat")
        else:
            out.append("  → beat dentro do aceitável (< 1 tempo)")

    if match_methods:
        out += ['', 'MÉTODOS DE CRUZAMENTO NO MERGE:', '']
        for mm, n in match_methods.most_common():
            out.append(f"  {mm:<18} {n:>4} acordes cruzados")

    out.append(sep)

    report_text = '\n'.join(out)
    print(report_text)
    return report_text


# ════════════════════════════════════════════════════════════════
# REPORT
# ════════════════════════════════════════════════════════════════

def report(json_paths):
    total = correct = human = unval = missing = 0

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        for sec in data.get('sections', []):
            for line in sec.get('lines', []):
                for measure in line.get('measures', []):
                    for chord in measure.get('chords', []):
                        total += 1
                        ct = chord.get('ground_truth', {}).get('correction_type', '')

                        if ct == 'correct':
                            correct += 1
                        elif ct == 'human_origin':
                            human += 1
                        elif ct == 'unvalidated':
                            unval += 1

        missing += len(data.get('_missing_human_chords', []))

    validated = total - human - unval
    pct = round(100 * correct / validated) if validated > 0 else 0

    print(f"\n{'─' * 40}")
    print(f"RELATÓRIO — {len(json_paths)} arquivo(s)")
    print(f"{'─' * 40}")
    print(f"Acordes pipeline:     {total}")
    print(f"Missing humanos:      {missing}")
    print(f"Origem humana:        {human}")
    print(f"Não revisados:        {unval}")
    print(f"Validados:            {validated}")
    print(f"Corretos:             {correct}  ({pct}%)")
    print(f"{'─' * 40}\n")


# ════════════════════════════════════════════════════════════════
# DEBUG CSV
# ════════════════════════════════════════════════════════════════

def debug(json_path):
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    out_dir = Path('calibration')
    out_dir.mkdir(exist_ok=True)

    slug = Path(json_path).stem.replace('_merged', '')
    out_path = out_dir / f"debug_{slug}.csv"

    rows = []

    for sec in data.get('sections', []):
        for line in sec.get('lines', []):
            for measure in line.get('measures', []):
                n = measure.get('n')
                local_n = measure.get('local_n')
                system = measure.get('system')
                page = measure.get('page')

                for chord in measure.get('chords', []):
                    gt = chord.get('ground_truth', {}) or {}
                    human = chord.get('human') or {}
                    protocol = chord.get('protocol') or {}

                    rows.append({
                        'source':               'pipeline',
                        'page':                 page,
                        'system':               system,
                        'measure_n':            n,
                        'local_n':              local_n,
                        'symbol':               chord.get('symbol'),
                        'correction_type':      gt.get('correction_type'),
                        'correct':              gt.get('correct'),
                        'status':               gt.get('status'),
                        'rule':                 protocol.get('rule'),
                        'match_method':         chord.get('_match_method'),
                        'beat_pipeline':        chord.get('beat'),
                        'beat_human':           human.get('beat_bi'),
                        'beat_delta':           chord.get('_beat_delta'),
                        'ratio_pipeline':       chord.get('beat_ratio'),
                        'ratio_delta':          chord.get('_ratio_delta'),
                        'protocol_syllable':    protocol.get('syllable'),
                        'human_syllable':       human.get('syllable'),
                        'protocol_cx':          protocol.get('cx'),
                        'protocol_syllable_cx': protocol.get('syllable_cx'),
                        'human_cx_display':     human.get('cx_display'),
                        'human_syllable_cx':    human.get('syllable_cx'),
                        'nota_anchor':          protocol.get('nota_anchor'),
                    })

    for mh in data.get('_missing_human_chords', []):
        human = mh.get('human') or {}
        gt = mh.get('ground_truth') or {}

        rows.append({
            'source':               'missing_human',
            'page':                 None,
            'system':               None,
            'measure_n':            mh.get('n'),
            'local_n':              None,
            'symbol':               mh.get('symbol'),
            'correction_type':      gt.get('correction_type'),
            'correct':              gt.get('correct'),
            'status':               gt.get('status'),
            'rule':                 None,
            'match_method':         None,
            'beat_pipeline':        None,
            'beat_human':           human.get('beat_bi'),
            'beat_delta':           None,
            'ratio_pipeline':       None,
            'ratio_delta':          None,
            'protocol_syllable':    None,
            'human_syllable':       human.get('syllable'),
            'protocol_cx':          None,
            'protocol_syllable_cx': None,
            'human_cx_display':     human.get('cx_display'),
            'human_syllable_cx':    human.get('syllable_cx'),
            'nota_anchor':          None,
        })

    fieldnames = [
        'source',
        'page',
        'system',
        'measure_n',
        'local_n',
        'symbol',
        'correction_type',
        'correct',
        'status',
        'rule',
        'match_method',
        'beat_pipeline',
        'beat_human',
        'beat_delta',
        'ratio_pipeline',
        'ratio_delta',
        'protocol_syllable',
        'human_syllable',
        'protocol_cx',
        'protocol_syllable_cx',
        'human_cx_display',
        'human_syllable_cx',
        'nota_anchor',
    ]

    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Debug gerado: {out_path}")
    print(f"Linhas: {len(rows)}")


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def compare_sequence(pipeline_path, editor_path):
    """
    Gera CSV lado a lado:
    pipeline ordenado vs editor humano ordenado.
    """
    with open(pipeline_path, encoding='utf-8') as f:
        pipeline = json.load(f)

    with open(editor_path, encoding='utf-8') as f:
        editor = json.load(f)

    def flat(data, source):
        rows = []
        order = 0

        for item in _iter_chords_by_measure(data):
            chord = item['chord']
            sym = (chord.get('symbol') or '').strip()
            if not sym:
                continue

            order += 1
            protocol = chord.get('protocol') or {}
            human = chord.get('human') or {}

            rows.append({
                'order': order,
                'source': source,
                'n': item['n'],
                'symbol': sym,
                'beat': chord.get('beat'),
                'beat_ratio': chord.get('beat_ratio'),
                'protocol_syllable': protocol.get('syllable'),
                'human_syllable': human.get('syllable'),
            })

        return rows

    p_rows = flat(pipeline, 'pipeline')
    h_rows = flat(editor, 'human')

    out_dir = Path('calibration')
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / 'compare_sequence.csv'

    max_len = max(len(p_rows), len(h_rows))
    rows = []

    for i in range(max_len):
        p = p_rows[i] if i < len(p_rows) else {}
        h = h_rows[i] if i < len(h_rows) else {}

        rows.append({
            'order': i + 1,

            'pipeline_symbol': p.get('symbol'),
            'pipeline_n': p.get('n'),
            'pipeline_beat': p.get('beat'),
            'pipeline_ratio': p.get('beat_ratio'),
            'pipeline_syllable': p.get('protocol_syllable'),

            'human_symbol': h.get('symbol'),
            'human_n': h.get('n'),
            'human_beat': h.get('beat'),
            'human_ratio': h.get('beat_ratio'),
            'human_syllable': h.get('human_syllable'),

            'same_symbol': p.get('symbol') == h.get('symbol'),
        })

    fieldnames = [
        'order',
        'pipeline_symbol',
        'pipeline_n',
        'pipeline_beat',
        'pipeline_ratio',
        'pipeline_syllable',
        'human_symbol',
        'human_n',
        'human_beat',
        'human_ratio',
        'human_syllable',
        'same_symbol',
    ]

    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Comparação gerada: {out_path}")
    print(f"Pipeline: {len(p_rows)} acordes")
    print(f"Human:    {len(h_rows)} acordes")

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == 'merge':
        if len(sys.argv) < 5:
            print("Uso: python calibrate.py merge <pipeline.json> <editor.json> <saida.json>")
            sys.exit(1)

        merge(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == 'calibrate':
        if len(sys.argv) < 3:
            print("Uso: python calibrate.py calibrate <arquivo1.json> [arquivo2.json ...]")
            sys.exit(1)

        calibrate(sys.argv[2:])

    elif cmd == 'report':
        if len(sys.argv) < 3:
            print("Uso: python calibrate.py report <arquivo1.json> [arquivo2.json ...]")
            sys.exit(1)

        report(sys.argv[2:])

    elif cmd == 'debug':
        if len(sys.argv) < 3:
            print("Uso: python calibrate.py debug <merged.json>")
            sys.exit(1)

        debug(sys.argv[2])

    elif cmd == 'compare':
        if len(sys.argv) < 4:
            print("Uso: python calibrate.py compare <pipeline.json> <editor.json>")
            sys.exit(1)

        compare_sequence(sys.argv[2], sys.argv[3])

    else:
        print(f"Comando desconhecido: {cmd}")
        print("Comandos disponíveis: merge, calibrate, report, debug")
        sys.exit(1)


if __name__ == '__main__':
    main()