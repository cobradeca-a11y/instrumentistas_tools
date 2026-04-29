#!/usr/bin/env python3
"""
calibrate.py — Mesclagem, coverage, review e merge guard do protocolo CXD+T90.

Comandos:
  python calibrate.py merge <pipeline.json> <editor.json> <saida.json>
  python calibrate.py calibrate <merged.json>
  python calibrate.py coverage <merged.json>
  python calibrate.py debug <merged.json>
  python calibrate.py review <merged.json>
  python calibrate.py compare <pipeline.json> <editor.json>
  python calibrate.py report <merged.json>
"""

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CORRECTION_TYPES = {
    'correct': 'protocolo acertou — acorde e sílaba corretos',
    'anchor_shift': 'acorde correto, sílaba deslocada horizontalmente',
    'wrong_syllable': 'acorde correto, sílaba errada',
    'wrong_chord': 'sílaba correta, acorde errado',
    'missing_chord': 'protocolo não detectou acorde que existe',
    'extra_chord': 'protocolo detectou acorde que não existe',
    'beat_wrong': 'acorde no tempo errado',
    'melisma_undetected': 'protocolo não detectou melisma',
    'pause_undetected': 'protocolo não detectou pausa melódica',
    'human_origin': 'cifrado do zero pelo humano',
    'presence_only': 'acorde cruzado por presença, sem validação musical',
    'unvalidated_alignment_conflict': 'cruzamento fraco: símbolo bate, mas sílaba diverge; não validar como erro musical',
    'unvalidated': 'ainda não revisado',
}


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


def classify_correction(p_syl, h_syl, p_chord, h_chord, p_rule):
    p_syl = (p_syl or '').strip()
    h_syl = (h_syl or '').strip()
    p_chord = (p_chord or '').strip()
    h_chord = (h_chord or '').strip()

    syl_ok = p_syl == h_syl
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
    if not human_block:
        return {
            'status': 'unvalidated',
            'correct': None,
            'delta_cx': None,
            'correction_type': 'unvalidated',
            'rule_changed': None,
            'implied_rule': None,
            'merged_at': ts,
        }

    if human_block.get('_presence_only'):
        return {
            'status': 'presence_only',
            'correct': None,
            'delta_cx': None,
            'correction_type': 'unvalidated',
            'rule_changed': None,
            'implied_rule': 'PRESENCE',
            'merged_at': ts,
        }

    if human_block.get('validated_by') == 'editor-v4' and not protocol_block.get('cx'):
        return {
            'status': 'human_origin',
            'correct': None,
            'delta_cx': None,
            'correction_type': 'human_origin',
            'rule_changed': None,
            'implied_rule': 'HUMAN',
            'merged_at': ts,
        }

    p_syl = protocol_block.get('syllable')
    h_syl = human_block.get('syllable')

    p_cx = protocol_block.get('syllable_cx')
    if p_cx is None:
        p_cx = protocol_block.get('float_cx')

    h_cx = human_block.get('syllable_cx')
    if h_cx is None:
        h_cx = human_block.get('cx_display')

    p_rule = protocol_block.get('rule', '')

    ct = classify_correction(p_syl, h_syl, None, None, p_rule)

    if ct == 'correct' and not p_syl and not h_syl:
        ct = 'unvalidated'

    correct = ct == 'correct'

    delta_cx = (
        round(abs(float(h_cx) - float(p_cx)), 3)
        if h_cx is not None and p_cx is not None
        else None
    )

    implied_rule = 'T90' if h_syl else 'VAZIO'
    rule_changed = implied_rule != p_rule

    return {
        'status': 'correct' if correct else ('unvalidated' if ct == 'unvalidated' else 'corrected'),
        'correct': correct,
        'delta_cx': delta_cx,
        'correction_type': ct,
        'rule_changed': rule_changed,
        'implied_rule': implied_rule,
        'merged_at': ts,
    }


def _iter_chords_by_measure(data):
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
            'key': (item['sec_i'], item['line_i'], item['measure_i'], item['chord_i']),
            'n': n,
            'sym': sym,
            'beat': chord.get('beat', 1),
            'beat_ratio': _chord_ratio(chord),
            'beat_bi': _chord_beat_bi(chord),
            'global_order': global_order,
            'chord': chord,
            'human': chord.get('human') or {},
            'used': False,
        }

        by_n_sym.setdefault((n, sym), []).append(entry)

    return by_n_sym


def _build_editor_sequence(editor):
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
            'n': item['n'],
            'sym': sym,
            'beat': chord.get('beat', 1),
            'beat_ratio': _chord_ratio(chord),
            'beat_bi': _chord_beat_bi(chord),
            'chord': chord,
            'human': chord.get('human') or {},
            'used': False,
        })

    return seq


def _build_pipeline_sequence(pipeline):
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
    if matched_chord is None:
        return

    for entries in editor_index.values():
        for entry in entries:
            if entry.get('chord') is matched_chord:
                entry['used'] = True

    for entry in editor_seq:
        if entry.get('chord') is matched_chord:
            entry['used'] = True


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

    if best_method.endswith('_loose'):
        if measure_delta == 0:
            return best, 'n_sym_presence'

        if ratio_delta > 0.38:
            return None, None

        if measure_delta > 1 and bi_delta > 2:
            return None, None

    return best, best_method


def _find_sequence_match(editor_seq, p_chord, p_order, window=8):
    sym = (p_chord.get('symbol') or '').strip()
    protocol = p_chord.get('protocol') or {}
    p_rule = protocol.get('rule')
    p_syl = (protocol.get('syllable') or '').strip()

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

        if p_syl and h_syl and p_syl != h_syl:
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
        return best, 'seq_ratio'

    if bi_delta != 99 and bi_delta <= 1:
        return best, 'seq_bi'

    return None, None


def _find_presence_match(editor_seq, p_chord, p_order, alignment_map=None, window=18):
    sym = (p_chord.get('symbol') or '').strip()

    if alignment_map:
        aligned = alignment_map.get(p_order)

        if aligned and not aligned.get('used') and aligned.get('sym') == sym:
            return aligned, 'presence_align'

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
        score = (order_delta, ratio_delta, bi_delta, beat_delta)
        candidates.append((score, e))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0])
    score, best = candidates[0]

    return best, 'presence_seq'


def _is_alignment_conflict(chord):
    """
    Evita que cruzamento fraco vire erro musical real.
    Se o símbolo bate, mas a sílaba diverge em match fraco,
    marca como não validado, não como erro musical.
    """
    gt = chord.get('ground_truth') or {}
    ct = gt.get('correction_type')

    if ct not in {'wrong_syllable', 'melisma_undetected'}:
        return False

    method = chord.get('_match_method') or ''
    weak_methods = {
        'seq_align',
        'seq_ratio',
        'near_measure_ratio',
        'near_measure_loose',
        'n_sym_ratio',
    }

    if method not in weak_methods:
        return False

    protocol = chord.get('protocol') or {}
    human = chord.get('human') or {}

    p_syl = (protocol.get('syllable') or '').strip()
    h_syl = (human.get('syllable') or '').strip()

    delta_cx = gt.get('delta_cx')
    ratio_delta = chord.get('_ratio_delta')
    beat_delta = chord.get('_beat_delta')

    delta_cx = _safe_float(delta_cx, None)
    ratio_delta = _safe_float(ratio_delta, None)
    beat_delta = _safe_float(beat_delta, None)

    # Melisma só é erro real se o match for confiável.
    # Se o pipeline não tinha sílaba anterior disponível,
    # mas o merge trouxe uma sílaba humana de outro lugar,
    # isso é conflito de alinhamento, não erro musical.
    if ct == 'melisma_undetected':
        melisma_debug = protocol.get('melisma_debug') or {}
        reason = melisma_debug.get('reason')

        if reason == 'no_previous_syllable_available' and h_syl:
            return True

        if p_syl != h_syl:
            if method in {'seq_align', 'seq_ratio', 'near_measure_ratio', 'near_measure_loose'}:
                return True

            if ratio_delta is not None and ratio_delta >= 0.18:
                return True

            if delta_cx is not None and delta_cx >= 18:
                return True

        return False

    # wrong_syllable precisa ter sílabas reais nos dois lados.
    if not p_syl or not h_syl:
        return False

    if p_syl == h_syl:
        return False

    # conflito forte: sílaba diferente e afastamento horizontal relevante
    if delta_cx is not None and delta_cx >= 18:
        return True

    # conflito médio: match por sequência/medida com ratio afastado
    if ratio_delta is not None and ratio_delta >= 0.18:
        return True

    # casos de seq_align com sílaba completamente diferente, mesmo beat bom
    if method in {'seq_align', 'seq_ratio'} and p_syl != h_syl:
        return True

    # near_measure_loose nunca deve virar erro musical validado quando a sílaba diverge
    if method == 'near_measure_loose':
        return True

    return False


def _downgrade_alignment_conflict(chord, ts):
    gt = chord.get('ground_truth') or {}

    gt.update({
        'status': 'unvalidated',
        'correct': None,
        'correction_type': 'unvalidated_alignment_conflict',
        'rule_changed': None,
        'implied_rule': 'ALIGNMENT_CONFLICT',
        'merged_at': ts,
    })

    chord['ground_truth'] = gt
    chord['_alignment_conflict'] = True



def _should_force_presence_only(chord, match, match_method):
    """
    Mantém coverage estrutural, mas impede match fraco de virar validação musical.
    Se símbolo cruza por método fraco e a sílaba diverge, o match vira presence_only.
    """
    if not match:
        return False

    method = match_method or ''

    weak_methods = {
        'seq_align',
        'seq_ratio',
        'near_measure_ratio',
        'near_measure_loose',
        'n_sym_ratio',
    }

    if method not in weak_methods:
        return False

    protocol = chord.get('protocol') or {}
    editor_chord = match.get('chord') or {}
    e_human = editor_chord.get('human') or {}

    p_syl = (protocol.get('syllable') or '').strip()
    h_syl = (e_human.get('syllable') or editor_chord.get('protocol', {}).get('syllable') or '').strip()

    # VAZIO vazio-vazio é estrutural, não validação musical.
    if (protocol.get('rule') or '') == 'VAZIO' and not p_syl and not h_syl:
        return True

    # Melisma sem sílaba anterior disponível: human veio do alinhamento.
    melisma_debug = protocol.get('melisma_debug') or {}
    if (protocol.get('rule') or '') == 'T90.2_MELISMA':
        if melisma_debug.get('reason') == 'no_previous_syllable_available' and h_syl:
            return True

    # Sílaba diferente em método fraco: mantém cruzamento, mas só presença.
    if p_syl != h_syl:
        return True

    return False

def merge(pipeline_path, editor_path, output_path):
    print("\nMesclando:")
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
                    pipeline_order += 1

                    match, match_method = _find_best_editor_match(
                        editor_index,
                        n,
                        sym,
                        chord,
                        window=2,
                    )

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

                            if p_rule == 'VAZIO':
                                accept_align = False

                            if p_syl and h_syl and p_syl != h_syl:
                                if ratio_delta > 0.18 and bi_delta > 1 and beat_delta > 0:
                                    accept_align = False

                            if ratio_delta > 0.35 and bi_delta > 2:
                                accept_align = False

                            if accept_align:
                                match = aligned
                                match_method = 'seq_align'

                    if not match:
                        match, match_method = _find_sequence_match(
                            editor_seq,
                            chord,
                            pipeline_order,
                            window=8,
                        )

                    if not match:
                        match, match_method = _find_presence_match(
                            editor_seq,
                            chord,
                            pipeline_order,
                            alignment_map=alignment_map,
                            window=18,
                        )

                    chord['_match_method'] = match_method
                    chord['_alignment_conflict'] = False

                    if match:
                        _mark_editor_used(editor_index, editor_seq, match.get('chord'))

                        editor_chord = match['chord']
                        e_human = editor_chord.get('human') or {}

                        forced_presence = _should_force_presence_only(chord, match, match_method)

                        presence_only = forced_presence or match_method in {
                            'n_sym_presence',
                            'presence_seq',
                            'presence_align',
                        }

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
                            '_editor_n': match.get('n'),
                            '_global_order': match.get('global_order'),
                            '_presence_only': presence_only,
                            '_forced_presence': forced_presence,
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

                    if _is_alignment_conflict(chord):
                        _downgrade_alignment_conflict(chord, ts)

                    stats[chord['ground_truth']['correction_type']] += 1

    missing_human = []

    for entry in editor_seq:
        if not entry.get('used'):
            ch = entry['chord']

            missing_human.append({
                'n': entry['n'],
                'symbol': entry['sym'],
                'beat': ch.get('beat'),
                'beat_ratio': _chord_ratio(ch),
                'global_order': entry.get('global_order'),
                'human': ch.get('human'),
                'ground_truth': {
                    'status': 'corrected',
                    'correct': False,
                    'delta_cx': None,
                    'correction_type': 'missing_chord',
                    'rule_changed': None,
                    'implied_rule': 'HUMAN',
                    'merged_at': ts,
                },
            })

            stats['missing_chord'] += 1

    merged['_missing_human_chords'] = missing_human

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    total = sum(stats.values())
    correct = stats.get('correct', 0)
    pct = round(100 * correct / total) if total else 0

    print("\n✅ Mesclagem concluída")
    print(f"   {total} eventos processados")
    print(f"   {correct}/{total} corretos ({pct}%)")
    print(f"   missing_human_chords: {len(missing_human)}")
    print("\n   Distribuição:")

    for ct, n in stats.most_common():
        print(f"     {str(ct):<35} {n:>4}")

    return merged, stats


def _collect_metrics(data):
    metrics = {
        'pipeline_total': 0,
        'structural_crossed': 0,
        'presence_only': 0,
        'forced_presence': 0,
        'musical_crossed': 0,
        'validated': 0,
        'unvalidated': 0,
        'alignment_conflict': 0,
        'correct': 0,
        'wrong_syllable': 0,
        'melisma': 0,
        'pause': 0,
        'human_origin': 0,
        'missing_human': len(data.get('_missing_human_chords', [])),
        'beat_all': [],
        'beat_musical': [],
        'match_methods': Counter(),
        'corrections': Counter(),
        'rule_errors': Counter(),
    }

    for sec in data.get('sections', []):
        for line in sec.get('lines', []):
            for measure in line.get('measures', []):
                for chord in measure.get('chords', []):
                    metrics['pipeline_total'] += 1

                    mm = chord.get('_match_method')
                    human = chord.get('human') or {}
                    gt = chord.get('ground_truth', {}) or {}
                    ct = gt.get('correction_type', 'unvalidated')

                    if mm:
                        metrics['structural_crossed'] += 1
                        metrics['match_methods'][mm] += 1

                    bd = chord.get('_beat_delta')
                    if bd is not None:
                        metrics['beat_all'].append(bd)

                    is_presence = bool(human.get('_presence_only'))
                    if human.get('_forced_presence'):
                        metrics['forced_presence'] += 1

                    protocol = chord.get('protocol') or {}
                    p_syl = (protocol.get('syllable') or '').strip()
                    h_syl = (human.get('syllable') or '').strip()
                    rule = protocol.get('rule') or ''
                    empty_match = rule == 'VAZIO' and not p_syl and not h_syl

                    if is_presence:
                        metrics['presence_only'] += 1
                    elif mm and not empty_match:
                        metrics['musical_crossed'] += 1
                        if bd is not None:
                            metrics['beat_musical'].append(bd)

                    if ct in {'unvalidated', 'unvalidated_alignment_conflict'}:
                        metrics['unvalidated'] += 1

                        if ct == 'unvalidated_alignment_conflict':
                            metrics['alignment_conflict'] += 1
                            metrics['corrections'][ct] += 1

                        continue

                    if ct == 'human_origin':
                        metrics['human_origin'] += 1
                        continue

                    metrics['validated'] += 1
                    metrics['corrections'][ct] += 1

                    if ct == 'correct':
                        metrics['correct'] += 1
                    elif ct == 'wrong_syllable':
                        metrics['wrong_syllable'] += 1
                    elif ct == 'melisma_undetected':
                        metrics['melisma'] += 1
                    elif ct == 'pause_undetected':
                        metrics['pause'] += 1

                    if not gt.get('correct'):
                        p_rule = chord.get('protocol', {}).get('rule', '?')
                        metrics['rule_errors'][f"{p_rule} → {ct}"] += 1

    metrics['corrections']['missing_chord'] += metrics['missing_human']

    return metrics


def calibrate(json_paths):
    total = Counter()
    match_methods = Counter()
    corrections = Counter()
    rule_errors = Counter()
    beat_all = []
    beat_musical = []

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        m = _collect_metrics(data)

        for k, v in m.items():
            if isinstance(v, int):
                total[k] += v

        match_methods.update(m['match_methods'])
        corrections.update(m['corrections'])
        rule_errors.update(m['rule_errors'])
        beat_all.extend(m['beat_all'])
        beat_musical.extend(m['beat_musical'])

    sep = '═' * 62

    human_est = total['structural_crossed'] + total['missing_human']
    musical_crossed = total['musical_crossed']
    presence_only = total['presence_only']

    out = [
        sep,
        f"RELATÓRIO DE CALIBRAÇÃO — {len(json_paths)} louvor(es)",
        f"Gerado em: {utc_label()}",
        sep,
        '',
        'COBERTURA ESTRUTURAL',
        f"  Acordes pipeline:        {total['pipeline_total']}",
        f"  Acordes humanos estim.:  {human_est}",
        f"  Presença cruzada:        {total['structural_crossed']}",
        f"  Presence-only:           {presence_only}",
        f"  Forced-presence:         {total['forced_presence']}",
        f"  Cruzamento musical:      {musical_crossed}",
        f"  Humanos faltantes:       {total['missing_human']}",
    ]

    if human_est:
        out.append(f"  Cobertura vs humano:     {round(100 * total['structural_crossed'] / human_est)}%")

    if total['pipeline_total']:
        out.append(f"  Pipeline cruzado:        {round(100 * total['structural_crossed'] / total['pipeline_total'])}%")

    out += [
        '',
        'VALIDAÇÃO MUSICAL REAL',
        f"  Validados:               {total['validated']}",
        f"  Não validados:           {total['unvalidated']}",
        f"  Conflitos de alinhamento:{total['alignment_conflict']:>8}",
        f"  Sílabas corretas:        {total['correct']}",
        f"  Sílabas erradas:         {total['wrong_syllable']}",
        f"  Melismas não detectados: {total['melisma']}",
        f"  Pausas não detectadas:   {total['pause']}",
    ]

    if total['validated']:
        out.append(f"  Precisão validada:       {round(100 * total['correct'] / total['validated'])}%")

    out += ['', 'DISTRIBUIÇÃO POR TIPO DE CORREÇÃO:', '']

    denom = max(total['validated'] + total['missing_human'] + total['alignment_conflict'], 1)

    for ct, n in corrections.most_common():
        pct = round(100 * n / denom)
        descr = CORRECTION_TYPES.get(ct, '')
        out.append(f"  {str(ct):<35} {n:>4}  ({pct:>3}%)  {descr}")

    if rule_errors:
        out += ['', 'ERROS POR REGRA ORIGINAL:', '']
        for key, n in rule_errors.most_common():
            out.append(f"  {key:<40} {n:>4} vezes")

    out += ['', '─' * 62, 'BEAT', '─' * 62, '']

    if beat_all:
        avg_all = sum(beat_all) / len(beat_all)
        zero_all = beat_all.count(0)
        out.append("  Estrutural:")
        out.append(f"    Cruzados com beat:       {len(beat_all)}")
        out.append(f"    Beat correto Δ=0:        {zero_all} ({round(100 * zero_all / len(beat_all))}%)")
        out.append(f"    Δ médio:                 {avg_all:.2f}")

    if beat_musical:
        avg_mus = sum(beat_musical) / len(beat_musical)
        zero_mus = beat_musical.count(0)
        out.append("  Musical validável:")
        out.append(f"    Cruzados com beat:       {len(beat_musical)}")
        out.append(f"    Beat correto Δ=0:        {zero_mus} ({round(100 * zero_mus / len(beat_musical))}%)")
        out.append(f"    Δ médio:                 {avg_mus:.2f}")

    if match_methods:
        out += ['', 'MÉTODOS DE CRUZAMENTO NO MERGE:', '']
        for mm, n in match_methods.most_common():
            out.append(f"  {mm:<20} {n:>4} acordes cruzados")

    out += [
        '',
        '─' * 62,
        'PRÓXIMO ALVO',
        '─' * 62,
        '',
        '  → wrong_syllable agora conta só match forte',
        '  → alignment_conflict indica pareamento fraco, não erro musical',
        '  → próximo ajuste real deve vir do debug dos validados restantes',
        sep,
    ]

    report_text = '\n'.join(out)
    print(report_text)
    return report_text


def coverage(json_path):
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    m = _collect_metrics(data)

    human_total = m['structural_crossed'] + m['missing_human']

    print("\n" + "═" * 62)
    print("RELATÓRIO DE COBERTURA")
    print("═" * 62)

    print("\nCOBERTURA ESTRUTURAL")
    print(f"  Acordes pipeline:        {m['pipeline_total']}")
    print(f"  Acordes humanos estim.:  {human_total}")
    print(f"  Presença cruzada:        {m['structural_crossed']}")
    print(f"  Presence-only:           {m['presence_only']}")
    print(f"  Forced-presence:         {m['forced_presence']}")
    print(f"  Cruzamento musical:      {m['musical_crossed']}")
    print(f"  Humanos faltantes:       {m['missing_human']}")

    if human_total:
        print(f"  Cobertura vs humano:     {round(100 * m['structural_crossed'] / human_total)}%")

    if m['pipeline_total']:
        print(f"  Pipeline cruzado:        {round(100 * m['structural_crossed'] / m['pipeline_total'])}%")

    print("\nVALIDAÇÃO MUSICAL REAL")
    print(f"  Validados:               {m['validated']}")
    print(f"  Não validados:           {m['unvalidated']}")
    print(f"  Conflitos alinhamento:   {m['alignment_conflict']}")
    print(f"  Sílabas corretas:        {m['correct']}")
    print(f"  Sílabas erradas:         {m['wrong_syllable']}")
    print(f"  Melismas não detectados: {m['melisma']}")
    print(f"  Pausas não detectadas:   {m['pause']}")

    if m['validated']:
        print(f"  Precisão validada:       {round(100 * m['correct'] / m['validated'])}%")

    print("\nBEAT")
    if m['beat_all']:
        beat_avg = sum(m['beat_all']) / len(m['beat_all'])
        beat_zero = m['beat_all'].count(0)
        print("  Estrutural:")
        print(f"    Cruzados com beat:       {len(m['beat_all'])}")
        print(f"    Beat correto Δ=0:        {beat_zero} ({round(100 * beat_zero / len(m['beat_all']))}%)")
        print(f"    Δ médio:                 {beat_avg:.2f}")

    if m['beat_musical']:
        beat_avg = sum(m['beat_musical']) / len(m['beat_musical'])
        beat_zero = m['beat_musical'].count(0)
        print("  Musical validável:")
        print(f"    Cruzados com beat:       {len(m['beat_musical'])}")
        print(f"    Beat correto Δ=0:        {beat_zero} ({round(100 * beat_zero / len(m['beat_musical']))}%)")
        print(f"    Δ médio:                 {beat_avg:.2f}")

    if m['match_methods']:
        print("\nMÉTODOS DE CRUZAMENTO")
        for k, v in m['match_methods'].most_common():
            print(f"  {k:<20} {v}")

    print("\nPRÓXIMO ALVO")
    print("  → coverage está bom; próximo ajuste é nos validados restantes")
    print("  → alignment_conflict não entra como precisão musical")
    print("═" * 62 + "\n")


def report(json_paths):
    for path in json_paths:
        calibrate([path])


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
                        'source': 'pipeline',
                        'page': page,
                        'system': system,
                        'measure_n': n,
                        'local_n': local_n,
                        'symbol': chord.get('symbol'),
                        'correction_type': gt.get('correction_type'),
                        'correct': gt.get('correct'),
                        'status': gt.get('status'),
                        'presence_only': human.get('_presence_only'),
                        'forced_presence': human.get('_forced_presence'),
                        'alignment_conflict': chord.get('_alignment_conflict'),
                        'rule': protocol.get('rule'),
                        'match_method': chord.get('_match_method'),
                        'beat_pipeline': chord.get('beat'),
                        'beat_human': human.get('beat_bi'),
                        'beat_delta': chord.get('_beat_delta'),
                        'ratio_pipeline': chord.get('beat_ratio'),
                        'ratio_delta': chord.get('_ratio_delta'),
                        'protocol_syllable': protocol.get('syllable'),
                        'human_syllable': human.get('syllable'),
                        'protocol_cx': protocol.get('cx'),
                        'protocol_syllable_cx': protocol.get('syllable_cx'),
                        'human_cx_display': human.get('cx_display'),
                        'human_syllable_cx': human.get('syllable_cx'),
                        'nota_anchor': protocol.get('nota_anchor'),
                    })

    for mh in data.get('_missing_human_chords', []):
        human = mh.get('human') or {}
        gt = mh.get('ground_truth') or {}

        rows.append({
            'source': 'missing_human',
            'page': None,
            'system': None,
            'measure_n': mh.get('n'),
            'local_n': None,
            'symbol': mh.get('symbol'),
            'correction_type': gt.get('correction_type'),
            'correct': gt.get('correct'),
            'status': gt.get('status'),
            'presence_only': None,
            'forced_presence': None,
            'alignment_conflict': None,
            'rule': None,
            'match_method': None,
            'beat_pipeline': None,
            'beat_human': human.get('beat_bi'),
            'beat_delta': None,
            'ratio_pipeline': None,
            'ratio_delta': None,
            'protocol_syllable': None,
            'human_syllable': human.get('syllable'),
            'protocol_cx': None,
            'protocol_syllable_cx': None,
            'human_cx_display': human.get('cx_display'),
            'human_syllable_cx': human.get('syllable_cx'),
            'nota_anchor': None,
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
        'presence_only',
        'forced_presence',
        'alignment_conflict',
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




def _review_kind(row):
    """
    Classifica linha do review em tipo acionável.
    """
    rule = row.get('rule') or ''
    ct = row.get('correction_type') or ''
    p_syl = (row.get('protocol_syllable') or '').strip()
    h_syl = (row.get('human_syllable') or '').strip()
    match_method = row.get('match_method') or ''
    presence_only = bool(row.get('presence_only'))
    alignment_conflict = bool(row.get('alignment_conflict'))

    if presence_only:
        return 'forced_presence' if row.get('forced_presence') else 'presence_only'

    if rule == 'VAZIO' and not p_syl and not h_syl:
        return 'empty_match'

    if rule == 'T90.2_MELISMA' and not p_syl and h_syl:
        # Sem sílaba anterior no audit, o human veio de alinhamento fraco.
        # Não é melisma real; é conflito do merge.
        return 'alignment_conflict'

    if ct == 'correct':
        return 'confirmed_correct'

    if alignment_conflict or ct == 'unvalidated_alignment_conflict':
        return 'alignment_conflict'

    if ct in {'wrong_syllable', 'melisma_undetected', 'pause_undetected'}:
        return 'possible_pipeline_error'

    if match_method:
        return 'needs_review'

    return 'ignore'


def _review_action(kind):
    actions = {
        'empty_match': 'ignorar',
        'presence_only': 'ignorar_coverage',
        'forced_presence': 'manter_como_presenca',
        'possible_real_melisma': 'investigar_pipeline_melisma',
        'confirmed_correct': 'manter',
        'alignment_conflict': 'melhorar_merge',
        'possible_pipeline_error': 'investigar_pipeline',
        'needs_review': 'revisar_manual',
        'ignore': 'ignorar',
    }
    return actions.get(kind, 'revisar_manual')

def review(json_path):
    """
    Gera CSV de revisão humana focado em decisões acionáveis.
    Não altera o JSON.
    """
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    out_dir = Path('calibration')
    out_dir.mkdir(exist_ok=True)

    slug = Path(json_path).stem.replace('_merged', '')
    out_path = out_dir / f"review_{slug}.csv"

    rows = []

    for sec in data.get('sections', []):
        for line in sec.get('lines', []):
            for measure in line.get('measures', []):
                page = measure.get('page')
                system = measure.get('system')
                measure_n = measure.get('n')
                local_n = measure.get('local_n')

                for chord in measure.get('chords', []):
                    gt = chord.get('ground_truth') or {}
                    human = chord.get('human') or {}
                    protocol = chord.get('protocol') or {}

                    match_method = chord.get('_match_method')
                    presence_only = bool(human.get('_presence_only'))
                    forced_presence = bool(human.get('_forced_presence'))
                    alignment_conflict = bool(chord.get('_alignment_conflict'))

                    p_syl = protocol.get('syllable')
                    h_syl = human.get('syllable')

                    melisma_debug = protocol.get('melisma_debug') or {}

                    base = {
                        'melisma_reason': melisma_debug.get('reason'),
                        'melisma_used_source': melisma_debug.get('used_source'),
                        'page': page,
                        'system': system,
                        'measure_n': measure_n,
                        'local_n': local_n,
                        'symbol': chord.get('symbol'),
                        'status': gt.get('status'),
                        'correction_type': gt.get('correction_type'),
                        'correct': gt.get('correct'),
                        'match_method': match_method,
                        'presence_only': presence_only,
                        'forced_presence': forced_presence,
                        'alignment_conflict': alignment_conflict,
                        'rule': protocol.get('rule'),
                        'beat_pipeline': chord.get('beat'),
                        'beat_human': human.get('beat_bi'),
                        'beat_delta': chord.get('_beat_delta'),
                        'ratio_pipeline': chord.get('beat_ratio'),
                        'ratio_delta': chord.get('_ratio_delta'),
                        'protocol_syllable': p_syl,
                        'human_syllable': h_syl,
                        'same_syllable': (p_syl or '').strip() == (h_syl or '').strip(),
                        'protocol_cx': protocol.get('cx'),
                        'protocol_syllable_cx': protocol.get('syllable_cx'),
                        'human_cx_display': human.get('cx_display'),
                        'human_syllable_cx': human.get('syllable_cx'),
                        'delta_cx': gt.get('delta_cx'),
                        'nota_anchor': protocol.get('nota_anchor'),
                    }

                    kind = _review_kind(base)
                    action = _review_action(kind)

                    include = kind not in {'presence_only', 'ignore'}

                    # forced_presence entra no CSV para mostrar o que o guard protegeu.
                    if kind == 'forced_presence':
                        include = True

                    # empty_match entra no CSV, mas já classificado como ignorável.
                    if kind == 'empty_match':
                        include = True

                    if not include:
                        continue

                    base['review_kind'] = kind
                    base['suggested_action'] = action
                    base['review_decision'] = ''
                    base['review_note'] = ''

                    rows.append(base)

    kind_order = {
        'possible_real_melisma': 0,
        'possible_pipeline_error': 1,
        'confirmed_correct': 2,
        'alignment_conflict': 3,
        'forced_presence': 4,
        'empty_match': 5,
        'needs_review': 5,
    }

    rows.sort(key=lambda r: (
        kind_order.get(r.get('review_kind'), 99),
        r.get('page') if r.get('page') is not None else 999,
        r.get('system') if r.get('system') is not None else 999,
        r.get('measure_n') if r.get('measure_n') is not None else 999,
        str(r.get('symbol') or ''),
    ))

    fieldnames = [
        'review_kind',
        'suggested_action',
        'melisma_reason',
        'melisma_used_source',
        'page',
        'system',
        'measure_n',
        'local_n',
        'symbol',
        'status',
        'correction_type',
        'correct',
        'match_method',
        'presence_only',
        'forced_presence',
        'alignment_conflict',
        'rule',
        'beat_pipeline',
        'beat_human',
        'beat_delta',
        'ratio_pipeline',
        'ratio_delta',
        'protocol_syllable',
        'human_syllable',
        'same_syllable',
        'protocol_cx',
        'protocol_syllable_cx',
        'human_cx_display',
        'human_syllable_cx',
        'delta_cx',
        'nota_anchor',
        'review_decision',
        'review_note',
    ]

    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    by_kind = Counter(r['review_kind'] for r in rows)
    by_type = Counter(r['correction_type'] for r in rows)
    by_method = Counter(r['match_method'] for r in rows)

    print(f"Review gerado: {out_path}")
    print(f"Linhas: {len(rows)}")

    if by_kind:
        print("\nPor review_kind:")
        for k, v in by_kind.most_common():
            print(f"  {str(k):<28} {v}")

    if by_type:
        print("\nPor correction_type:")
        for k, v in by_type.most_common():
            print(f"  {str(k):<35} {v}")

    if by_method:
        print("\nPor match_method:")
        for k, v in by_method.most_common():
            print(f"  {str(k):<20} {v}")

def compare_sequence(pipeline_path, editor_path):
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

    elif cmd == 'coverage':
        if len(sys.argv) < 3:
            print("Uso: python calibrate.py coverage <merged.json>")
            sys.exit(1)

        coverage(sys.argv[2])

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

    elif cmd == 'review':
        if len(sys.argv) < 3:
            print("Uso: python calibrate.py review <merged.json>")
            sys.exit(1)

        review(sys.argv[2])

    elif cmd == 'compare':
        if len(sys.argv) < 4:
            print("Uso: python calibrate.py compare <pipeline.json> <editor.json>")
            sys.exit(1)

        compare_sequence(sys.argv[2], sys.argv[3])

    else:
        print(f"Comando desconhecido: {cmd}")
        print("Comandos disponíveis: merge, calibrate, coverage, report, debug, review, compare")
        sys.exit(1)


if __name__ == '__main__':
    main()
