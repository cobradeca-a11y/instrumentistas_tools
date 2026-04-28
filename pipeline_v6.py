#!/usr/bin/env python3
"""
PIPELINE CXD+T90 v6
Extração automática sem LINES_MAP.

P1 — Detecta sistemas, acordes e letra automaticamente.
P2 — Calcula beat pela nota âncora relativa às barras.
P3 — Aceita OpusStd e Maestro para notas.
P4 — Não nomeia seções; o editor faz isso.
P5 — Audit completo:
     - chords
     - systems
     - qtd_acordes
     - qtd_silabas
     - qtd_compassos
     - acordes_nao_atribuidos
P6 — Atribuição segura de acordes ao compasso:
     - assign_chords_to_measures()
     - aceita acorde próximo da borda do compasso por margem
"""

import json
import pdfplumber
import re
from datetime import datetime, timezone
from collections import Counter

PIPELINE_VERSION = "v6"


# ════════════════════════════════════════════════════════════════
# CONSTANTES
# ════════════════════════════════════════════════════════════════

FIGURE_BASE = {
    'w': ('semibreve',       4.0, False),
    '˙': ('mínima',          2.0, False),
    'ú': ('mínima',          2.0, False),
    'œ': ('semínima',        1.0, False),
    '∑': ('pausa_semibreve', 4.0, True),
    'Ó': ('pausa_mínima',    2.0, True),
    'Œ': ('pausa_semínima',  1.0, True),
    '‚': ('pausa_colcheia',  0.5, True),
}

NOTE_HEADS = {k for k, (_, _, r) in FIGURE_BASE.items() if not r}
PAUSE_CHARS = {k for k, (_, _, r) in FIGURE_BASE.items() if r}
FLAG_CHARS = {'‰'}
DOT_CHARS = {'™'}

NOTE_MIDI_BASE = {
    'Dó': 0,
    'Ré': 2,
    'Mi': 4,
    'Fá': 5,
    'Sol': 7,
    'Lá': 9,
    'Si': 11,
}

KEY_SIG_FLATS = {
    -1: {'Si': ('Sib', -1)},
    -2: {'Si': ('Sib', -1), 'Mi': ('Mib', -1)},
    -3: {'Si': ('Sib', -1), 'Mi': ('Mib', -1), 'Lá': ('Láb', -1)},
    -4: {'Si': ('Sib', -1), 'Mi': ('Mib', -1), 'Lá': ('Láb', -1), 'Ré': ('Réb', -1)},
    -5: {'Si': ('Sib', -1), 'Mi': ('Mib', -1), 'Lá': ('Láb', -1), 'Ré': ('Réb', -1), 'Sol': ('Solb', -1)},
}

KEY_SIG_SHARPS = {
    1: {'Fá': ('Fá#', 1)},
    2: {'Fá': ('Fá#', 1), 'Dó': ('Dó#', 1)},
    3: {'Fá': ('Fá#', 1), 'Dó': ('Dó#', 1), 'Sol': ('Sol#', 1)},
}

SKIP_TEXTS = {
    'Coro',
    'coro',
    '(Ageu)',
    'Ageu 2.6-9',
    'Arr. Vocal:',
    'Arr. Cordas:',
    'Arr. de Cordas:',
    'To Coda',
    'D.S. al Coda',
    'D.C. al Coda',
    'Fine',
    'DEPARTAMENTO DE LOUVOR',
}

SKIP_PATTERNS = [
    r'^\d+$',
    r'^= \d',
    r'^q\s*=',
    r'^Sérgio',
    r'^Luciana',
    r'^Francisco',
    r'^Felipe',
    r'^Daniel',
    r'^Fábio',
]


# ════════════════════════════════════════════════════════════════
# UTILITÁRIOS
# ════════════════════════════════════════════════════════════════

def should_skip(text):
    text = text.strip()

    if not text:
        return True

    if text in SKIP_TEXTS:
        return True

    if any(text.startswith(p) for p in [
        'Arr.',
        'DEPART',
        'Sérgio',
        'Luciana',
        'Francisco',
        'Felipe',
        'Daniel',
        'Fábio',
    ]):
        return True

    for pat in SKIP_PATTERNS:
        if re.match(pat, text):
            return True

    return False


def normalize_chord(t):
    return (
        t.replace('©', '#')
         .replace('‹', 'm')
         .replace('„ˆˆ', 'add')
         .replace('Œ„Š', 'maj')
         .replace('¨', 'b')
    )


def is_note_font(fontname):
    return 'OpusStd' in fontname or 'Maestro' in fontname


# ════════════════════════════════════════════════════════════════
# SISTEMAS
# ════════════════════════════════════════════════════════════════

def detect_systems_auto(page):
    """
    Detecta sistemas de pauta e calcula janelas automáticas.

    Janela de acordes: y_top - 80 até y_top + 5
    Janela de letra:   y_bot + 2 até y_bot + 150
    """
    h = [
        l for l in page.lines
        if abs(l['y0'] - l['y1']) < 0.5
        and abs(l['x1'] - l['x0']) > 400
    ]

    h_ys = sorted(set(round(l['y0'], 1) for l in h))

    sistemas = []

    for i in range(0, len(h_ys), 5):
        g = h_ys[i:i + 5]

        if len(g) != 5:
            continue

        xe = [l['x0'] for l in h if abs(l['y0'] - g[0]) < 0.5]
        xd = [l['x1'] for l in h if abs(l['y0'] - g[0]) < 0.5]

        if not xe or not xd:
            continue

        sistemas.append({
            'id': len(sistemas),
            'y_top': g[0],
            'y_bot': g[-1],
            'lines_y': sorted(g, reverse=True),
            'x_esq': min(xe),
            'x_dir': max(xd),
        })

    for sis in sistemas:
        sis['ac_y_min'] = sis['y_top'] - 80
        sis['ac_y_max'] = sis['y_top'] + 5
        sis['ly_y_min'] = sis['y_bot'] + 2
        sis['ly_y_max'] = sis['y_bot'] + 150

    return sistemas


def has_lyrics(sis, lyric_tokens):
    return any(
        t for t in lyric_tokens
        if sis['ly_y_min'] <= t['top'] <= sis['ly_y_max']
        and not should_skip(t['text'])
    )


# ════════════════════════════════════════════════════════════════
# TOKENS
# ════════════════════════════════════════════════════════════════

def group_tokens(chars, x_tol=3):
    chars = [
        c for c in chars
        if not (c.get('text') == '_' and 'Opus' in c.get('fontname', ''))
    ]

    ld = {}

    for c in chars:
        ld.setdefault(round(c['top'] / 3) * 3, []).append(c)

    words = []

    for tk in sorted(ld):
        cur = []

        for c in sorted(ld[tk], key=lambda c: c['x0']):
            if not cur or c['x0'] - cur[-1]['x1'] <= x_tol:
                cur.append(c)
            else:
                t = ''.join(ch['text'] for ch in cur).strip()

                if t:
                    words.append({
                        'text': t,
                        'top': cur[0]['top'],
                        'x0': cur[0]['x0'],
                        'x1': cur[-1]['x1'],
                        'cx': (cur[0]['x0'] + cur[-1]['x1']) / 2,
                    })

                cur = [c]

        if cur:
            t = ''.join(ch['text'] for ch in cur).strip()

            if t:
                words.append({
                    'text': t,
                    'top': cur[0]['top'],
                    'x0': cur[0]['x0'],
                    'x1': cur[-1]['x1'],
                    'cx': (cur[0]['x0'] + cur[-1]['x1']) / 2,
                })

    return words


# ════════════════════════════════════════════════════════════════
# CLAVE / ARMADURA / METRO
# ════════════════════════════════════════════════════════════════

def detect_clef_and_key(chars, sis):
    ctx = [
        c for c in chars
        if is_note_font(c.get('fontname', ''))
        and c['text'] in {'&', '?', 'b', '#'}
        and c['x0'] < 100
    ]

    claves = [c for c in ctx if c['text'] in {'&', '?'}]
    clef = None

    if claves:
        best = min(claves, key=lambda c: abs(c['top'] - sis['y_top']))

        if abs(best['top'] - sis['y_top']) < 30:
            clef = 'treble' if best['text'] == '&' else 'bass'

    arm = [
        c for c in ctx
        if c['text'] in {'b', '#'}
        and sis['y_top'] - 5 <= c['top'] <= sis['y_bot'] + 90
    ]

    bx = sorted(set(round(c['x0'], 0) for c in arm if c['text'] == 'b'))
    sx = sorted(set(round(c['x0'], 0) for c in arm if c['text'] == '#'))

    key_sig = -len(bx) if bx else len(sx) if sx else None

    return clef, key_sig


def detect_meter(chars, sis):
    ctx = [
        c for c in chars
        if is_note_font(c.get('fontname', ''))
        and c['text'] in {'4', '3', '2', '6', '9', '%', 'c', 'C'}
        and c['x0'] < 110
        and sis['y_top'] - 5 <= c['top'] <= sis['y_bot'] + 40
    ]

    nums = sorted([c for c in ctx if c['text'].isdigit()], key=lambda c: c['top'])

    if len(nums) >= 2:
        return int(nums[0]['text']), int(nums[1]['text'])

    if len(nums) == 1:
        return int(nums[0]['text']), 4

    if any(c['text'] in {'%', 'c', 'C'} for c in ctx):
        return 2, 2

    return None, None


# ════════════════════════════════════════════════════════════════
# BARRAS DE COMPASSO
# ════════════════════════════════════════════════════════════════

def detect_barlines(lines, sis):
    v = [l for l in lines if abs(l['x0'] - l['x1']) < 0.5]

    b = [
        l for l in v
        if 14 <= abs(l['y1'] - l['y0']) <= 20
        and (
            abs(l['y0'] - sis['y_top']) < 3
            or abs(l['y1'] - sis['y_top']) < 3
        )
    ]

    bx = sorted(set(round(l['x0'], 1) for l in b))
    bounds = sorted(set([sis['x_esq']] + bx))

    return [
        {
            "n": i + 1,
            "x0": bounds[i],
            "x1": bounds[i + 1],
        }
        for i in range(len(bounds) - 1)
        if bounds[i + 1] - bounds[i] >= 10
    ]


# ════════════════════════════════════════════════════════════════
# ATRIBUIÇÃO DE ACORDES AO COMPASSO
# ════════════════════════════════════════════════════════════════

def assign_chords_to_measures(acordes_sis, compassos, margin=8):
    """
    Atribui cada acorde ao compasso correto.
    Se o acorde cair pouco fora da borda de um compasso,
    atribui ao compasso mais próximo.
    """
    assigned = {comp['n']: [] for comp in compassos}
    unassigned = []

    for a in acordes_sis:
        cx = a['cx']
        found = None

        for comp in compassos:
            if comp['x0'] <= cx <= comp['x1']:
                found = comp
                break

        if not found:
            candidates = []

            for comp in compassos:
                dist = min(abs(cx - comp['x0']), abs(cx - comp['x1']))

                if dist <= margin:
                    candidates.append((dist, comp))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                found = candidates[0][1]

        if found:
            assigned[found['n']].append(a)
        else:
            unassigned.append({
                "text": a["text"],
                "cx": round(a["cx"], 1),
                "top": round(a["top"], 1),
            })

    return assigned, unassigned


# ════════════════════════════════════════════════════════════════
# ALTURAS
# ════════════════════════════════════════════════════════════════

def build_pitch_table(staff_ys, clef='treble', key_sig=-3):
    ys = sorted(staff_ys, reverse=True)
    step = (ys[0] - ys[4]) / 4
    half = step / 2

    ks = (KEY_SIG_FLATS if key_sig < 0 else KEY_SIG_SHARPS).get(key_sig, {})

    if clef == 'treble':
        base = [
            (0, 'Mi', 0, 4), (1, 'Fá', 0, 4), (2, 'Sol', 0, 4),
            (3, 'Lá', 0, 4), (4, 'Si', 0, 4), (5, 'Dó', 0, 5),
            (6, 'Ré', 0, 5), (7, 'Mi', 0, 5), (8, 'Fá', 0, 5),
            (-1, 'Ré', 0, 4), (-2, 'Dó', 0, 4), (-3, 'Si', 0, 3),
            (-4, 'Lá', 0, 3), (-5, 'Sol', 0, 3),
            (9, 'Sol', 0, 5), (10, 'Lá', 0, 5), (11, 'Si', 0, 5),
            (12, 'Dó', 0, 6), (13, 'Ré', 0, 6),
        ]
    else:
        base = [
            (0, 'Sol', 0, 2), (1, 'Lá', 0, 2), (2, 'Si', 0, 2),
            (3, 'Dó', 0, 3), (4, 'Ré', 0, 3), (5, 'Mi', 0, 3),
            (6, 'Fá', 0, 3), (7, 'Sol', 0, 3), (8, 'Lá', 0, 3),
            (-1, 'Fá', 0, 2), (-2, 'Mi', 0, 2), (-3, 'Ré', 0, 2),
            (-4, 'Dó', 0, 2), (-5, 'Si', 0, 1),
            (9, 'Si', 0, 3), (10, 'Dó', 0, 4), (11, 'Ré', 0, 4),
            (12, 'Mi', 0, 4), (13, 'Fá', 0, 4),
        ]

    table = {}

    for offset, nome, acc, oitava in base:
        y = ys[0] - offset * half

        if nome in ks:
            nome_alt, d = ks[nome]
            table[y] = (
                f"{nome_alt}{oitava}",
                12 * (oitava + 1) + NOTE_MIDI_BASE[nome] + acc + d,
            )
        else:
            table[y] = (
                f"{nome}{oitava}",
                12 * (oitava + 1) + NOTE_MIDI_BASE[nome] + acc,
            )

    return table, half


def note_from_top(top, table, half):
    closest = min(table.keys(), key=lambda y: abs(y - top))

    if abs(closest - top) <= half * 1.6:
        return table[closest]

    return None, None


# ════════════════════════════════════════════════════════════════
# FIGURA RÍTMICA
# ════════════════════════════════════════════════════════════════

def rhythmic_figure(char, cx, top, all_chars):
    if char not in FIGURE_BASE:
        return None

    name, beats, is_rest = FIGURE_BASE[char]

    if char not in NOTE_HEADS or char in ('w', '˙', 'ú'):
        return {
            'figure': name,
            'beats': beats,
            'flags': 0,
            'dots': 0,
            'is_rest': is_rest,
        }

    flags = len(set(
        round(c['cx'], 0)
        for c in all_chars
        if c['text'] in FLAG_CHARS
        and abs(c['cx'] - cx) <= 6
        and abs(c['top'] - top) <= 20
    ))

    dots = len([
        c for c in all_chars
        if c['text'] in DOT_CHARS
        and 0 < c['cx'] - cx <= 12
        and abs(c['top'] - top) <= 6
    ])

    b = beats / (2 ** flags) * (1.5 if dots else 1)

    names = {
        0: 'semínima',
        1: 'colcheia',
        2: 'semicolcheia',
        3: 'fusa',
    }

    return {
        'figure': names.get(flags, f'œ/{2 ** flags}'),
        'beats': round(b, 4),
        'flags': flags,
        'dots': dots,
        'is_rest': is_rest,
    }


# ════════════════════════════════════════════════════════════════
# BEAT
# ════════════════════════════════════════════════════════════════

def calc_beat_by_anchor(nota_anchor_cx, comp, meter):
    larg = comp['x1'] - comp['x0']

    if larg <= 0:
        return 1, 0.0

    ratio = max(0, min(1, (nota_anchor_cx - comp['x0']) / larg))
    beat = min(int(ratio * meter) + 1, meter)

    return beat, round(ratio, 3)


# ════════════════════════════════════════════════════════════════
# T90
# ════════════════════════════════════════════════════════════════

MELISMA_THRESH = 25.0


def pick_syllable_for_chord(chord_cx, anchor_cx, silas_c, max_left=8, max_right=42):
    if not silas_c:
        return None

    ref_cx = anchor_cx if anchor_cx is not None else chord_cx

    window = [
        s for s in silas_c
        if (ref_cx - max_left) <= s['cx'] <= (ref_cx + max_right)
    ]

    if window:
        rightish = [s for s in window if s['cx'] >= ref_cx - max_left]

        if rightish:
            return min(rightish, key=lambda s: abs(s['cx'] - ref_cx))

        return min(window, key=lambda s: abs(s['cx'] - ref_cx))

    after = [s for s in silas_c if s['cx'] >= chord_cx - max_left]

    if after:
        return min(after, key=lambda s: s['cx'])

    return min(silas_c, key=lambda s: abs(s['cx'] - ref_cx))


def is_probable_melisma(anchor_cx, silas_c, threshold=MELISMA_THRESH):
    if anchor_cx is None or not silas_c:
        return False

    dist_min = min(abs(s['cx'] - anchor_cx) for s in silas_c)

    return dist_min > threshold


def t90_full(chord_cx, compassos, notas_comp_map, silas_por_comp,
             silas_linha_anterior=None, meter=4):

    comp = next((c for c in compassos if c['x0'] <= chord_cx <= c['x1']), None)

    if not comp:
        comp = min(
            compassos,
            key=lambda c: min(abs(chord_cx - c['x0']), abs(chord_cx - c['x1']))
        )

    notas_c = notas_comp_map.get(comp['n'], [])
    nota_anchor = min(notas_c, key=lambda n: abs(n['cx'] - chord_cx)) if notas_c else None
    nota_str = f"{nota_anchor['text']}@{nota_anchor['cx']:.0f}" if nota_anchor else None

    if nota_anchor:
        beat, ratio = calc_beat_by_anchor(nota_anchor['cx'], comp, meter)
    else:
        larg = comp['x1'] - comp['x0']
        ratio = max(0, min(1, (chord_cx - comp['x0']) / larg)) if larg > 0 else 0
        beat = min(int(ratio * meter) + 1, meter)

    silas_c = silas_por_comp.get(comp['n'], [])

    if nota_anchor and nota_anchor['text'] in PAUSE_CHARS:
        silas_depois = [s for s in silas_c if s['cx'] > chord_cx]
        next_syl = min(silas_depois, key=lambda s: s['cx']) if silas_depois else None

        return {
            "measure": comp['n'],
            "beat": beat,
            "ratio": ratio,
            "nota_anchor": nota_str,
            "syllable": None,
            "syllable_cx": None,
            "float_cx": chord_cx,
            "next_syllable": next_syl['text'] if next_syl else None,
            "next_syllable_cx": round(next_syl['cx'], 1) if next_syl else None,
            "rule": "T90.3_PAUSA",
        }

    if nota_anchor and silas_c:
        if is_probable_melisma(nota_anchor['cx'], silas_c):
            silas_antes = [s for s in silas_c if s['cx'] < nota_anchor['cx'] - 5]

            if not silas_antes:
                for pn in range(comp['n'] - 1, 0, -1):
                    p = silas_por_comp.get(pn, [])

                    if p:
                        silas_antes = p
                        break

            if not silas_antes and silas_linha_anterior:
                silas_antes = silas_linha_anterior

            target = max(silas_antes, key=lambda s: s['cx']) if silas_antes else None

            return {
                "measure": comp['n'],
                "beat": beat,
                "ratio": ratio,
                "nota_anchor": nota_str,
                "syllable": target['text'] if target else None,
                "syllable_cx": round(target['cx'], 1) if target else None,
                "float_cx": None,
                "next_syllable": None,
                "next_syllable_cx": None,
                "rule": "T90.2_MELISMA",
            }

        target = pick_syllable_for_chord(chord_cx, nota_anchor['cx'], silas_c)

        return {
            "measure": comp['n'],
            "beat": beat,
            "ratio": ratio,
            "nota_anchor": nota_str,
            "syllable": target['text'] if target else None,
            "syllable_cx": round(target['cx'], 1) if target else None,
            "float_cx": None,
            "next_syllable": None,
            "next_syllable_cx": None,
            "rule": "T90",
        }

    if silas_c:
        target = pick_syllable_for_chord(chord_cx, None, silas_c)

        return {
            "measure": comp['n'],
            "beat": beat,
            "ratio": ratio,
            "nota_anchor": None,
            "syllable": target['text'] if target else None,
            "syllable_cx": round(target['cx'], 1) if target else None,
            "float_cx": None,
            "next_syllable": None,
            "next_syllable_cx": None,
            "rule": "V90",
        }

    return {
        "measure": comp['n'],
        "beat": beat,
        "ratio": ratio,
        "nota_anchor": nota_str,
        "syllable": None,
        "syllable_cx": None,
        "float_cx": chord_cx,
        "next_syllable": None,
        "next_syllable_cx": None,
        "rule": "VAZIO",
    }


def detect_anacrusis_v2(compassos, notas_comp_map, silas_por_comp, comps_com_acorde):
    anacrusis = set()

    for comp in compassos:
        silas_c = silas_por_comp.get(comp['n'], [])
        notas_c = notas_comp_map.get(comp['n'], [])

        if silas_c and notas_c and comp['n'] not in comps_com_acorde:
            anacrusis.add(comp['n'])

    return anacrusis


# ════════════════════════════════════════════════════════════════
# FORMA
# ════════════════════════════════════════════════════════════════

FORM_LABELS = {
    'To Coda': 'to_coda',
    'D.S. al Coda': 'ds_al_coda',
    'D.C. al Coda': 'dc_al_coda',
    'Fine': 'fine',
    'Coda': 'coda_start',
}


def detect_form_marks(lyric_tokens, opus_chars, sistemas):
    marks = []

    for t in lyric_tokens:
        for label, tipo in FORM_LABELS.items():
            if label in t['text']:
                marks.append({
                    'type': tipo,
                    'cx': t['cx'],
                    'top': t['top'],
                    'label': t['text'],
                })

    for c in opus_chars:
        if c['text'] == '%':
            marks.append({
                'type': 'segno',
                'cx': (c['x0'] + c['x1']) / 2,
                'top': c['top'],
                'label': '⊕',
            })

    return marks


# ════════════════════════════════════════════════════════════════
# PIPELINE PRINCIPAL
# ════════════════════════════════════════════════════════════════

def run(pdf_path, meta=None):
    meta = meta or {}
    ts = datetime.now(timezone.utc).isoformat()

    output = {
        "schema_version": "1.0",
        "title": meta.get("title", ""),
        "composer": meta.get("composer", ""),
        "key": meta.get("key", ""),
        "key_sig": meta.get("key_sig", None),
        "bpm": meta.get("bpm", ""),
        "meter": meta.get("meter", "4/4"),
        "clef": meta.get("clef", "treble"),
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": ts,
        "form": [],
        "sections": [{
            "id": "linear",
            "label": "Linear",
            "clef": meta.get("clef", "treble"),
            "key_sig": meta.get("key_sig", None),
            "meter": meta.get("meter", "4/4"),
            "lines": [],
        }],
    }

    audit_rows = []
    system_audit = []

    inherited = {
        "meter": 4,
        "clef": "treble",
        "key_sig": None,
    }

    silas_linha_anterior = []
    system_global = 0
    measure_global = 0
    all_form_marks = []

    with pdfplumber.open(pdf_path) as pdf:
        for pg_idx, page in enumerate(pdf.pages):
            chars = page.chars

            for c in chars:
                if (
                    is_note_font(c.get('fontname', ''))
                    or 'OpusChords' in c.get('fontname', '')
                    or 'Times' in c.get('fontname', '')
                ):
                    c['cx'] = (c['x0'] + c['x1']) / 2

            sistemas = detect_systems_auto(page)

            chord_chars = [c for c in chars if 'OpusChords' in c.get('fontname', '')]
            lyric_chars = [c for c in chars if 'Times' in c.get('fontname', '')]
            opus_chars = [c for c in chars if is_note_font(c.get('fontname', ''))]

            chord_tokens = group_tokens(chord_chars)
            lyric_tokens = group_tokens(lyric_chars)

            pg_marks = detect_form_marks(lyric_tokens, opus_chars, sistemas)

            for m in pg_marks:
                m['page'] = pg_idx

            all_form_marks.extend(pg_marks)

            for sis in sistemas:
                system_global += 1

                acordes_sis = sorted(
                    [
                        t for t in chord_tokens
                        if sis['ac_y_min'] <= t['top'] <= sis['ac_y_max']
                    ],
                    key=lambda x: x['cx'],
                )

                silas_sis = sorted(
                    [
                        t for t in lyric_tokens
                        if sis['ly_y_min'] <= t['top'] <= sis['ly_y_max']
                        and not should_skip(t['text'])
                    ],
                    key=lambda x: x['cx'],
                )

                sys_audit = {
                    "pg": pg_idx,
                    "sis": sis["id"],
                    "system_global": system_global,
                    "y_top": sis["y_top"],
                    "y_bot": sis["y_bot"],
                    "ac_y_min": sis["ac_y_min"],
                    "ac_y_max": sis["ac_y_max"],
                    "ly_y_min": sis["ly_y_min"],
                    "ly_y_max": sis["ly_y_max"],
                    "qtd_acordes": len(acordes_sis),
                    "qtd_silabas": len(silas_sis),
                    "qtd_compassos": 0,
                    "acordes": [a["text"] for a in acordes_sis],
                    "silabas": [s["text"] for s in silas_sis[:30]],
                    "acordes_nao_atribuidos": [],
                    "qtd_acordes_nao_atribuidos": 0,
                }

                system_audit.append(sys_audit)

                if not silas_sis and not acordes_sis:
                    continue

                clef_det, key_sig_det = detect_clef_and_key(chars, sis)
                meter_n, _ = detect_meter(chars, sis)

                if clef_det is not None:
                    inherited['clef'] = clef_det

                if key_sig_det is not None:
                    inherited['key_sig'] = key_sig_det

                if meter_n is not None:
                    inherited['meter'] = meter_n

                clef = inherited['clef']
                key_sig = inherited['key_sig'] or 0
                meter = inherited['meter']

                pitch_table, half = build_pitch_table(sis['lines_y'], clef, key_sig)

                line3_y = (
                    sis['lines_y'][2]
                    if len(sis['lines_y']) >= 3
                    else (sis['y_top'] + sis['y_bot']) / 2
                )

                opus_sis = [
                    c for c in opus_chars
                    if sis['y_top'] - 25 <= c['top'] <= sis['y_bot'] + 60
                ]

                compassos = detect_barlines(page.lines, sis)
                sys_audit["qtd_compassos"] = len(compassos)

                acordes_por_comp, acordes_nao_atribuidos = assign_chords_to_measures(
                    acordes_sis,
                    compassos,
                    margin=8,
                )

                sys_audit["acordes_nao_atribuidos"] = acordes_nao_atribuidos
                sys_audit["qtd_acordes_nao_atribuidos"] = len(acordes_nao_atribuidos)

                notas_comp_map = {}

                for comp in compassos:
                    nc = [
                        c for c in opus_sis
                        if c['text'] in NOTE_HEADS | PAUSE_CHARS
                        and comp['x0'] <= c['cx'] <= comp['x1']
                        and c['top'] < line3_y + 2
                    ]

                    dedup = {}

                    for c in sorted(nc, key=lambda x: x['cx']):
                        k = round(c['cx'], 0)

                        if k not in dedup:
                            dedup[k] = c

                    notas_comp_map[comp['n']] = list(dedup.values())

                silas_por_comp = {}

                for comp in compassos:
                    silas_por_comp[comp['n']] = [
                        s for s in silas_sis
                        if comp['x0'] <= s['cx'] <= comp['x1']
                    ]

                comps_com_acorde = {
                    comp_n for comp_n, arr in acordes_por_comp.items()
                    if arr
                }

                anacrusis_ns = detect_anacrusis_v2(
                    compassos,
                    notas_comp_map,
                    silas_por_comp,
                    comps_com_acorde,
                )

                line_measures = []

                for comp in compassos:
                    measure_global += 1

                    m_obj = {
                        'n': measure_global,
                        'local_n': comp['n'],
                        'system': system_global,
                        'page': pg_idx,
                        'x0': round(comp['x0'], 1),
                        'x1': round(comp['x1'], 1),
                        'meter': f"{meter}/4",
                        'chords': [],
                        'notes': [],
                        'syllables': [],
                        'beats_used': 0.0,
                    }

                    ac_in = acordes_por_comp.get(comp['n'], [])

                    for ac in ac_in:
                        r = t90_full(
                            ac['cx'],
                            compassos,
                            notas_comp_map,
                            silas_por_comp,
                            silas_linha_anterior,
                            meter,
                        )

                        protocol_block = {
                            "cx": round(ac['cx'], 1),
                            "rule": r['rule'],
                            "syllable": r['syllable'],
                            "syllable_cx": r['syllable_cx'],
                            "float_cx": r.get('float_cx'),
                            "next_syllable": r.get('next_syllable'),
                            "next_syllable_cx": r.get('next_syllable_cx'),
                            "nota_anchor": r.get('nota_anchor'),
                        }

                        m_obj['chords'].append({
                            "symbol": normalize_chord(ac['text']),
                            "beat": r['beat'],
                            "beat_ratio": r['ratio'],
                            "bi": None,
                            "protocol": protocol_block,
                            "human": None,
                            "ground_truth": {
                                "status": "unvalidated",
                                "correct": None,
                                "delta_cx": None,
                                "correction_type": None,
                                "rule_changed": None,
                                "implied_rule": None,
                            },
                        })

                        audit_rows.append({
                            'pg': pg_idx,
                            'sis': sis['id'],
                            'system_global': system_global,
                            'comp_n': measure_global,
                            'local_n': comp['n'],
                            'acorde': normalize_chord(ac['text']),
                            'beat': r['beat'],
                            'ratio': r['ratio'],
                            'regra': r['rule'],
                            'silaba': r['syllable'] or r.get('next_syllable') or '—',
                            'nota': r.get('nota_anchor') or '—',
                        })

                    for c in notas_comp_map.get(comp['n'], []):
                        nome, mid = note_from_top(c['top'], pitch_table, half)
                        fig = rhythmic_figure(c['text'], c['cx'], c['top'], opus_sis)

                        if not fig:
                            continue

                        m_obj['notes'].append({
                            'cx': round(c['cx'], 1),
                            'note': nome,
                            'midi': mid,
                            'figure': fig['figure'],
                            'beats': fig['beats'],
                            'is_rest': fig['is_rest'],
                            'flags': fig.get('flags', 0),
                        })

                        if not fig['is_rest']:
                            m_obj['beats_used'] += fig['beats']

                    is_anacrusis = comp['n'] in anacrusis_ns

                    for s in silas_por_comp.get(comp['n'], []):
                        m_obj['syllables'].append({
                            'text': s['text'],
                            'cx': round(s['cx'], 1),
                            'anacrusis': is_anacrusis,
                        })

                    line_measures.append(m_obj)

                output['sections'][0]['lines'].append({
                    'id': f"pg{pg_idx}_sis{sis['id']}",
                    'page': pg_idx,
                    'system': sis['id'],
                    'system_global': system_global,
                    'measures': line_measures,
                })

                if silas_sis:
                    silas_linha_anterior = silas_sis

    output['form'] = all_form_marks

    return output, {
        "chords": audit_rows,
        "systems": system_audit,
    }


# ════════════════════════════════════════════════════════════════
# EXECUÇÃO DIRETA
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    configs = [
        (
            '/mnt/user-data/uploads/Ainda_Uma_Vez_-_Coro.pdf',
            {
                'title': 'Ainda Uma Vez',
                'composer': 'Francisco Jose Barbosa Condi',
                'key': 'Cm',
                'key_sig': -3,
                'bpm': '75-85',
            },
        ),
        (
            '/mnt/user-data/uploads/Cordeiro_Eterno_-_Coro.pdf',
            {
                'title': 'Cordeiro Eterno',
                'composer': 'Sérgio Paulo Ferreira de Matos',
                'key': 'E',
                'key_sig': 4,
                'bpm': '80-90',
            },
        ),
        (
            '/mnt/user-data/uploads/Quando_Buscamos_-_Coro.pdf',
            {
                'title': 'Quando Buscamos',
                'composer': 'Luciana Monteiro Carneiro Simora',
                'key': 'D',
                'key_sig': 2,
                'bpm': '60-70',
            },
        ),
    ]

    for pdf_path, meta in configs:
        nome = meta['title']

        print(f"\n{'=' * 55}")
        print(f"Processando: {nome}")
        print(f"{'=' * 55}")

        output, audit = run(pdf_path, meta)

        audit_chords = audit.get("chords", audit) if isinstance(audit, dict) else audit

        total_comp = sum(
            len(l['measures'])
            for s in output['sections']
            for l in s['lines']
        )

        total_ch = sum(
            len(m['chords'])
            for s in output['sections']
            for l in s['lines']
            for m in l['measures']
        )

        total_syl = sum(
            len(m['syllables'])
            for s in output['sections']
            for l in s['lines']
            for m in l['measures']
        )

        by_rule = Counter(r['regra'] for r in audit_chords)

        print(f"  Linhas detectadas: {sum(len(s['lines']) for s in output['sections'])}")
        print(f"  Compassos: {total_comp}")
        print(f"  Acordes:   {total_ch}")
        print(f"  Sílabas:   {total_syl}")
        print(f"  Marcações: {len(output['form'])}")
        print(f"  Sistemas auditados: {len(audit.get('systems', [])) if isinstance(audit, dict) else 0}")
        print(f"  Por regra:")

        for regra, n in by_rule.most_common():
            pct = round(100 * n / total_ch) if total_ch else 0
            print(f"    {regra:<22} {n:>4}  ({pct}%)")

        slug = nome.lower().replace(' ', '-').replace('_', '-')
        out_path = f"/home/claude/{slug}_v6.json"

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"  → {out_path}")