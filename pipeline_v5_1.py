"""
PIPELINE CXD+T90 v5.1
Gera JSON conforme score_schema_v1.json

Novidades em relação à v5:
- Estrutura do acorde separada em blocos: protocol / human / ground_truth
- ground_truth preenchido automaticamente quando human está presente
- Módulo calibrate() para análise acumulada de correções
- Metadados de geração: pipeline_version, generated_at
"""

import pdfplumber, re, json
from datetime import datetime, timezone

PIPELINE_VERSION = "v5.1"

# ════════════════════════════════════════════════════════════════
# CONSTANTES
# ════════════════════════════════════════════════════════════════

FIGURE_BASE = {
    'w': ('semibreve',       4.0, False),
    '˙': ('mínima',          2.0, False),
    'œ': ('semínima',        1.0, False),
    '∑': ('pausa_semibreve', 4.0, True),
    'Ó': ('pausa_mínima',    2.0, True),
    'Œ': ('pausa_semínima',  1.0, True),
    '‚': ('pausa_colcheia',  0.5, True),
}
NOTE_HEADS  = {k for k,(_, _, r) in FIGURE_BASE.items() if not r}
PAUSE_CHARS = {k for k,(_, _, r) in FIGURE_BASE.items() if r}
FLAG_CHARS  = {'‰'}
DOT_CHARS   = {'™'}
MELISMA_THRESH = 25.0

NOTE_MIDI_BASE = {'Dó':0,'Ré':2,'Mi':4,'Fá':5,'Sol':7,'Lá':9,'Si':11}

KEY_SIG_FLATS = {
    -1:{'Si':('Sib',-1)},
    -2:{'Si':('Sib',-1),'Mi':('Mib',-1)},
    -3:{'Si':('Sib',-1),'Mi':('Mib',-1),'Lá':('Láb',-1)},
    -4:{'Si':('Sib',-1),'Mi':('Mib',-1),'Lá':('Láb',-1),'Ré':('Réb',-1)},
    -5:{'Si':('Sib',-1),'Mi':('Mib',-1),'Lá':('Láb',-1),'Ré':('Réb',-1),'Sol':('Solb',-1)},
}
KEY_SIG_SHARPS = {
    1:{'Fá':('Fá#',1)},
    2:{'Fá':('Fá#',1),'Dó':('Dó#',1)},
    3:{'Fá':('Fá#',1),'Dó':('Dó#',1),'Sol':('Sol#',1)},
}

def normalize_chord(t):
    return (t.replace('©','#').replace('‹','m')
             .replace('„ˆˆ','add').replace('Œ„Š','maj')
             .replace('¨','b'))


# ════════════════════════════════════════════════════════════════
# GROUND TRUTH — cálculo automático no salvamento
# ════════════════════════════════════════════════════════════════

def compute_ground_truth(protocol_block, human_block):
    """
    Calcula o bloco ground_truth comparando protocol e human.
    Chamado toda vez que um louvor é salvo no editor.

    protocol_block: dict com cx, rule, syllable, syllable_cx
    human_block:    dict com syllable, syllable_cx, cx_display | None

    Retorna: dict ground_truth
    """
    if human_block is None:
        return {"status": "unvalidated", "correct": None,
                "delta_cx": None, "correction_type": None,
                "rule_changed": None, "implied_rule": None}

    p_syl = protocol_block.get("syllable")
    h_syl = human_block.get("syllable")
    p_cx  = protocol_block.get("syllable_cx") or protocol_block.get("float_cx")
    h_cx  = human_block.get("syllable_cx") or human_block.get("cx_display")
    p_rule = protocol_block.get("rule")

    correct = (p_syl == h_syl) if (p_syl is not None and h_syl is not None) else None
    delta_cx = round(abs(h_cx - p_cx), 1) if (h_cx is not None and p_cx is not None) else None

    # Classificar tipo de correção
    correction_type = _classify_correction(p_syl, h_syl, p_cx, h_cx, p_rule, correct)

    # Inferir regra implícita na decisão humana
    implied_rule = _infer_rule(h_syl, h_cx, p_rule)

    rule_changed = (implied_rule != p_rule) if implied_rule else None

    status = "correct" if correct else "corrected"

    return {
        "status":          status,
        "correct":         correct,
        "delta_cx":        delta_cx,
        "correction_type": correction_type,
        "rule_changed":    rule_changed,
        "implied_rule":    implied_rule,
    }


def _classify_correction(p_syl, h_syl, p_cx, h_cx, p_rule, correct):
    """Classifica o tipo de correção automaticamente."""
    if correct:
        return "correct"

    # Sem sílaba no protocolo, humano adicionou → protocolo perdeu o acorde
    if p_syl is None and h_syl is not None:
        if p_rule == "VAZIO":
            return "missing_chord"
        if p_rule in ("T90.3_PAUSA",):
            return "pause_undetected"
        return "missing_chord"

    # Humano removeu o acorde → era falso positivo
    if p_syl is not None and h_syl is None:
        return "extra_chord"

    # Mesma sílaba mas posição diferente
    if p_syl == h_syl and p_cx and h_cx and abs(h_cx - p_cx) > 5:
        return "anchor_shift"

    # Sílabas diferentes
    if p_syl != h_syl:
        if p_rule == "T90.2_MELISMA":
            return "melisma_undetected"
        if p_rule == "T90.3_PAUSA":
            return "pause_undetected"
        return "wrong_syllable"

    return "anchor_shift"


def _infer_rule(h_syl, h_cx, p_rule):
    """Infere qual regra a decisão humana implica."""
    if h_syl is None:
        return "VAZIO"
    # Se humano posicionou uma sílaba, assume T90 (âncora válida)
    return "T90"


# ════════════════════════════════════════════════════════════════
# CALIBRAÇÃO — análise acumulada de ground_truth
# ════════════════════════════════════════════════════════════════

def calibrate(json_paths):
    """
    Lê múltiplos arquivos JSON validados e gera relatório de calibração.
    Indica onde o protocolo sistematicamente erra e o que ajustar.

    json_paths: lista de caminhos para arquivos .json validados
    """
    from collections import Counter, defaultdict

    correction_counts  = Counter()
    rule_errors        = Counter()   # regra que gerou erro → tipo de correção
    delta_cx_vals      = []          # deltas de anchor_shift para recalibrar CHAR_SCALE
    melisma_dists      = []          # distâncias nos casos de melisma_undetected
    total_chords       = 0
    total_validated    = 0
    total_correct      = 0

    for path in json_paths:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        for sec in data.get('sections', []):
            for line in sec.get('lines', []):
                for measure in line.get('measures', []):
                    for chord in measure.get('chords', []):
                        total_chords += 1
                        gt = chord.get('ground_truth')
                        if not gt or gt['status'] == 'unvalidated':
                            continue

                        total_validated += 1
                        ct = gt.get('correction_type')
                        correction_counts[ct] += 1

                        if gt.get('correct'):
                            total_correct += 1
                        else:
                            p_rule = chord['protocol'].get('rule', '?')
                            rule_errors[f"{p_rule} → {ct}"] += 1

                            if ct == 'anchor_shift' and gt.get('delta_cx'):
                                delta_cx_vals.append(gt['delta_cx'])

    # Relatório
    lines_out = [
        "═" * 60,
        f"RELATÓRIO DE CALIBRAÇÃO — {len(json_paths)} louvor(es)",
        "═" * 60,
        "",
        f"Total de acordes:   {total_chords}",
        f"Validados:          {total_validated} ({100*total_validated//total_chords if total_chords else 0}%)",
        f"Corretos:           {total_correct} ({100*total_correct//total_validated if total_validated else 0}%)",
        "",
        "TIPOS DE CORREÇÃO:",
    ]
    for ct, n in correction_counts.most_common():
        pct = 100 * n // total_validated if total_validated else 0
        lines_out.append(f"  {str(ct):<25} {n:>4}  ({pct}%)")

    if rule_errors:
        lines_out += ["", "ERROS POR REGRA ORIGINAL:"]
        for key, n in rule_errors.most_common():
            lines_out.append(f"  {key:<35} {n:>4}")

    if delta_cx_vals:
        avg = sum(delta_cx_vals) / len(delta_cx_vals)
        lines_out += [
            "",
            "ANCHOR SHIFT — sugestão de recalibração:",
            f"  Delta médio:  {avg:.1f} px",
            f"  Se avg > 6px: considerar ajustar CHAR_SCALE ou MELISMA_THRESH",
        ]

    lines_out += ["", "═" * 60]
    return "\n".join(lines_out)


# ════════════════════════════════════════════════════════════════
# EXTRAÇÃO POR FONTE
# ════════════════════════════════════════════════════════════════

def extract_by_font(chars_list):
    chars = [c for c in chars_list
             if not (c.get('text') == '_' and 'Opus' in c.get('fontname',''))]
    chord_chars = [c for c in chars if 'OpusChords' in c.get('fontname','')]
    lyric_chars = [c for c in chars if 'Times'      in c.get('fontname','')]

    def group(cl, x_tol=3):
        ld = {}
        for c in cl:
            ld.setdefault(round(c['top']/3)*3, []).append(c)
        words = []
        for tk in sorted(ld):
            cur = []
            for c in sorted(ld[tk], key=lambda c: c['x0']):
                if not cur or c['x0']-cur[-1]['x1'] <= x_tol:
                    cur.append(c)
                else:
                    t = ''.join(ch['text'] for ch in cur).strip()
                    if t:
                        words.append({'text':t,'x0':cur[0]['x0'],'x1':cur[-1]['x1'],
                                      'top':cur[0]['top'],'cx':(cur[0]['x0']+cur[-1]['x1'])/2})
                    cur = [c]
            if cur:
                t = ''.join(ch['text'] for ch in cur).strip()
                if t:
                    words.append({'text':t,'x0':cur[0]['x0'],'x1':cur[-1]['x1'],
                                  'top':cur[0]['top'],'cx':(cur[0]['x0']+cur[-1]['x1'])/2})
        return words

    return group(chord_chars), group(lyric_chars)


# ════════════════════════════════════════════════════════════════
# INFRA: sistemas, barras, clave, metro, alturas, figura
# ════════════════════════════════════════════════════════════════

def detect_systems(lines_list):
    h = [l for l in lines_list if abs(l['y0']-l['y1'])<0.5 and abs(l['x1']-l['x0'])>400]
    h_ys = sorted(set(round(l['y0'],1) for l in h))
    sis = []
    for i in range(0, len(h_ys), 5):
        g = h_ys[i:i+5]
        if len(g) != 5:
            continue
        xe = [l['x0'] for l in h if abs(l['y0']-g[0])<0.5]
        xd = [l['x1'] for l in h if abs(l['y0']-g[0])<0.5]
        if not xe or not xd:
            continue
        sis.append({"id":len(sis)+1,"y_top":g[0],"y_bot":g[-1],
                    "x_esq":min(xe),"x_dir":max(xd),
                    "lines_y":sorted(g,reverse=True)})
    return sis

def detect_barlines(lines_list, sis):
    v = [l for l in lines_list if abs(l['x0']-l['x1'])<0.5]
    b = [l for l in v if 16<=abs(l['y1']-l['y0'])<=18
         and (abs(l['y0']-sis['y_top'])<2 or abs(l['y1']-sis['y_top'])<2)]
    bx     = sorted(set(round(l['x0'],1) for l in b))
    bounds = sorted(set([sis['x_esq']]+bx))
    return [{"n":i+1,"x0":bounds[i],"x1":bounds[i+1]}
            for i in range(len(bounds)-1) if bounds[i+1]-bounds[i]>=10]

def detect_clef_and_key(chars_list, sis):
    y_top = sis['y_top']
    y_bot = sis['y_bot']
    ctx = [c for c in chars_list
           if 'OpusStd' in c.get('fontname','')
           and c['text'] in {'&','?','b','#'}
           and c['x0'] < 100]
    claves = [c for c in ctx if c['text'] in {'&','?'}]
    clef = None
    if claves:
        clave = min(claves, key=lambda c: abs(c['top']-y_top))
        if abs(clave['top']-y_top) < 30:
            clef = 'treble' if clave['text']=='&' else 'bass'
    arm_chars = [c for c in ctx if c['text'] in {'b','#'}
                 and y_top-5 <= c['top'] <= y_bot+90]
    bemois_cx = sorted(set(round(c['x0'],0) for c in arm_chars if c['text']=='b'))
    sust_cx   = sorted(set(round(c['x0'],0) for c in arm_chars if c['text']=='#'))
    if not arm_chars and clef is None:
        key_sig = None
    else:
        key_sig = -len(bemois_cx) if bemois_cx else len(sust_cx)
    return clef, key_sig

def detect_meter(chars_list, sis):
    ctx = [c for c in chars_list
           if 'OpusStd' in c.get('fontname','')
           and c['text'] in {'4','3','2','6','9','%'}
           and c['x0'] < 110
           and sis['y_top']-5 <= c['top'] <= sis['y_bot']+40]
    numeros = sorted([c for c in ctx if c['text'].isdigit()], key=lambda c: c['top'])
    if len(numeros) >= 2:
        return int(numeros[0]['text']), int(numeros[1]['text'])
    elif len(numeros) == 1:
        return int(numeros[0]['text']), 4
    if any(c['text']=='%' for c in ctx):
        return 2, 2
    return None, None

def build_pitch_table(staff_ys, clef='treble', key_sig=-3):
    ys   = sorted(staff_ys, reverse=True)
    step = (ys[0]-ys[4])/4
    half = step/2
    ks   = (KEY_SIG_FLATS if key_sig<0 else KEY_SIG_SHARPS).get(key_sig, {})
    if clef == 'treble':
        BASE = [
            (0,'Mi',0,4),(1,'Fá',0,4),(2,'Sol',0,4),(3,'Lá',0,4),(4,'Si',0,4),
            (5,'Dó',0,5),(6,'Ré',0,5),(7,'Mi',0,5),(8,'Fá',0,5),
            (-1,'Ré',0,4),(-2,'Dó',0,4),(-3,'Si',0,3),(-4,'Lá',0,3),(-5,'Sol',0,3),
            (9,'Sol',0,5),(10,'Lá',0,5),(11,'Si',0,5),(12,'Dó',0,6),(13,'Ré',0,6),
        ]
    else:
        BASE = [
            (0,'Sol',0,2),(1,'Lá',0,2),(2,'Si',0,2),(3,'Dó',0,3),(4,'Ré',0,3),
            (5,'Mi',0,3),(6,'Fá',0,3),(7,'Sol',0,3),(8,'Lá',0,3),
            (-1,'Fá',0,2),(-2,'Mi',0,2),(-3,'Ré',0,2),(-4,'Dó',0,2),(-5,'Si',0,1),
            (9,'Si',0,3),(10,'Dó',0,4),(11,'Ré',0,4),(12,'Mi',0,4),(13,'Fá',0,4),
        ]
    table = {}
    for offset,nome,acc,oitava in BASE:
        y = ys[0]-offset*half
        if nome in ks:
            nome_alt,d = ks[nome]
            table[y] = (f"{nome_alt}{oitava}", 12*(oitava+1)+NOTE_MIDI_BASE[nome]+acc+d)
        else:
            table[y] = (f"{nome}{oitava}", 12*(oitava+1)+NOTE_MIDI_BASE[nome]+acc)
    return table, half

def note_from_top(top, table, half):
    closest = min(table.keys(), key=lambda y: abs(y-top))
    return table[closest] if abs(closest-top)<=half*1.6 else (None,None)

def rhythmic_figure(char, cx, top, all_chars):
    if char not in FIGURE_BASE: return None
    name, beats, is_rest = FIGURE_BASE[char]
    if char not in NOTE_HEADS or char in ('w','˙'):
        return {'figure':name,'beats':beats,'flags':0,'dots':0,'is_rest':is_rest}
    flags = len(set(round(c['cx'],0) for c in all_chars
                    if c['text'] in FLAG_CHARS and abs(c['cx']-cx)<=6 and abs(c['top']-top)<=20))
    dots  = len([c for c in all_chars if c['text'] in DOT_CHARS
                 and 0<c['cx']-cx<=12 and abs(c['top']-top)<=6])
    b = beats/(2**flags)*(1.5 if dots else 1)
    names = {0:'semínima',1:'colcheia',2:'semicolcheia',3:'fusa'}
    return {'figure':names.get(flags,f'œ/{2**flags}'),'beats':round(b,4),
            'flags':flags,'dots':dots,'is_rest':is_rest}


# ════════════════════════════════════════════════════════════════
# T90 — MAPEAMENTO TEMPORAL
# ════════════════════════════════════════════════════════════════

def t90_full(chord_cx, compassos, notas_comp_map, silas_por_comp,
             silas_linha_anterior=None, meter=4):

    comp = next((c for c in compassos if c['x0']<=chord_cx<=c['x1']),None)
    if not comp:
        comp = min(compassos,key=lambda c:min(abs(chord_cx-c['x0']),abs(chord_cx-c['x1'])))

    notas_c    = notas_comp_map.get(comp['n'],[])
    nota_anchor = min(notas_c,key=lambda n:abs(n['cx']-chord_cx)) if notas_c else None
    nota_str   = (f"{nota_anchor['text']}@{nota_anchor['cx']:.0f}"
                  if nota_anchor else None)

    larg  = comp['x1']-comp['x0']
    ratio = max(0,min(1,(chord_cx-comp['x0'])/larg)) if larg>0 else 0
    beat  = min(int(ratio*meter)+1, meter)
    silas_c = silas_por_comp.get(comp['n'],[])

    # T90.3 — nota âncora é pausa
    if nota_anchor and nota_anchor['text'] in PAUSE_CHARS:
        silas_depois = [s for s in silas_c if s['cx'] > chord_cx]
        next_syl = min(silas_depois,key=lambda s:s['cx']) if silas_depois else None
        return {"measure":comp['n'],"beat":beat,"ratio":round(ratio,3),
                "nota_anchor":nota_str,
                "syllable":None,"syllable_cx":None,
                "float_cx":chord_cx,
                "next_syllable":next_syl['text'] if next_syl else None,
                "next_syllable_cx":round(next_syl['cx'],1) if next_syl else None,
                "rule":"T90.3_PAUSA"}

    # T90 / T90.2
    if nota_anchor and silas_c:
        dist_min = min(abs(s['cx']-nota_anchor['cx']) for s in silas_c)
        if dist_min > MELISMA_THRESH:
            silas_antes = [s for s in silas_c if s['cx'] < nota_anchor['cx']-5]
            if not silas_antes:
                for pn in range(comp['n']-1,0,-1):
                    p = silas_por_comp.get(pn,[])
                    if p: silas_antes=p; break
            if not silas_antes and silas_linha_anterior:
                silas_antes = silas_linha_anterior
            target = max(silas_antes,key=lambda s:s['cx']) if silas_antes else None
            return {"measure":comp['n'],"beat":beat,"ratio":round(ratio,3),
                    "nota_anchor":nota_str,
                    "syllable":target['text'] if target else None,
                    "syllable_cx":round(target['cx'],1) if target else None,
                    "float_cx":None,"next_syllable":None,"next_syllable_cx":None,
                    "rule":"T90.2_MELISMA"}
        else:
            target = min(silas_c,key=lambda s:abs(s['cx']-nota_anchor['cx']))
            return {"measure":comp['n'],"beat":beat,"ratio":round(ratio,3),
                    "nota_anchor":nota_str,
                    "syllable":target['text'],"syllable_cx":round(target['cx'],1),
                    "float_cx":None,"next_syllable":None,"next_syllable_cx":None,
                    "rule":"T90"}
    elif silas_c:
        target = min(silas_c,key=lambda s:abs(s['cx']-chord_cx))
        return {"measure":comp['n'],"beat":beat,"ratio":round(ratio,3),
                "nota_anchor":None,
                "syllable":target['text'],"syllable_cx":round(target['cx'],1),
                "float_cx":None,"next_syllable":None,"next_syllable_cx":None,
                "rule":"V90"}
    else:
        return {"measure":comp['n'],"beat":beat,"ratio":round(ratio,3),
                "nota_anchor":nota_str,
                "syllable":None,"syllable_cx":None,"float_cx":chord_cx,
                "next_syllable":None,"next_syllable_cx":None,
                "rule":"VAZIO"}


# ════════════════════════════════════════════════════════════════
# ANACRUSE
# ════════════════════════════════════════════════════════════════

def detect_anacrusis_v2(compassos, notas_comp_map, silas_por_comp, comps_com_acorde):
    anacrusis = set()
    for comp in compassos:
        silas_c = silas_por_comp.get(comp['n'],[])
        notas_c = notas_comp_map.get(comp['n'],[])
        if silas_c and notas_c and comp['n'] not in comps_com_acorde:
            anacrusis.add(comp['n'])
    return anacrusis


# ════════════════════════════════════════════════════════════════
# PIPELINE PRINCIPAL
# ════════════════════════════════════════════════════════════════

def run(pdf_path, lines_map, meta):
    """
    Executa o pipeline e retorna JSON conforme score_schema_v1.

    Estrutura de cada acorde no JSON de saída:
    {
      "symbol": "Fm",
      "beat": 1,
      "beat_ratio": 0.099,
      "protocol": { cx, rule, syllable, syllable_cx, float_cx,
                    next_syllable, nota_anchor },
      "human": null,          ← preenchido pelo editor
      "ground_truth": {       ← calculado no salvamento
          "status": "unvalidated", ...
      }
    }
    """
    output = {
        "schema_version":   "1.0",
        "title":            meta.get("title",""),
        "composer":         meta.get("composer",""),
        "key":              meta.get("key",""),
        "key_sig":          meta.get("key_sig", -3),
        "bpm":              meta.get("bpm",""),
        "meter":            meta.get("meter","4/4"),
        "clef":             meta.get("clef","treble"),
        "pipeline_version": PIPELINE_VERSION,
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "sections":         [],
    }

    audit_rows          = []
    cur_section         = None
    section_obj         = None
    inherited           = {"meter":4,"clef":"treble","key_sig":-3}
    silas_linha_anterior = []

    with pdfplumber.open(pdf_path) as pdf:
        for pg_idx in range(len(pdf.pages)):
            page    = pdf.pages[pg_idx]
            lines_l = page.lines
            chars_l = page.chars

            sistemas = detect_systems(lines_l)
            chords_all, lyrics_all = extract_by_font(chars_l)

            for c in chars_l:
                if 'OpusStd' in c.get('fontname',''):
                    c['cx'] = (c['x0']+c['x1'])/2

            for (pg,sis_idx,cmin,cmax,lmin,lmax,sec,sub) in \
                    [l for l in lines_map if l[0]==pg_idx]:

                if sis_idx >= len(sistemas):
                    print(f"  [AVISO] sis_idx={sis_idx} inexistente — pulando {sec}/{sub}")
                    continue

                sis       = sistemas[sis_idx]
                compassos = detect_barlines(lines_l, sis)

                clef_det, key_sig_det = detect_clef_and_key(chars_l, sis)
                meter_n, _            = detect_meter(chars_l, sis)

                if clef_det    is not None: inherited['clef']    = clef_det
                if key_sig_det is not None: inherited['key_sig'] = key_sig_det
                if meter_n     is not None: inherited['meter']   = meter_n

                clef    = inherited['clef']
                key_sig = inherited['key_sig']
                meter   = inherited['meter']

                pitch_table, half = build_pitch_table(sis['lines_y'], clef, key_sig)
                line3_y = (sis['lines_y'][2] if len(sis['lines_y'])>=3
                           else (sis['y_top']+sis['y_bot'])/2)

                opus_chars = [c for c in chars_l
                              if 'OpusStd' in c.get('fontname','')
                              and sis['y_top']-25 <= c['top'] <= sis['y_bot']+60]

                notas_comp_map = {}
                for comp in compassos:
                    nc = [c for c in opus_chars
                          if c['text'] in NOTE_HEADS|PAUSE_CHARS
                          and comp['x0']<=c['cx']<=comp['x1']
                          and c['top'] < line3_y+2]
                    dedup = {}
                    for c in sorted(nc, key=lambda x:x['cx']):
                        k = round(c['cx'],0)
                        if k not in dedup: dedup[k] = c
                    notas_comp_map[comp['n']] = list(dedup.values())

                acordes = sorted([w for w in chords_all if cmin<=w['top']<=cmax],
                                  key=lambda x:x['cx'])
                silas = sorted([w for w in lyrics_all
                                 if lmin and lmin<=w['top']<=lmax
                                 and not re.match(r'^\d+[\.\)]',w['text'])
                                 and w['text'] not in ('Coro','D.S. al Coda','To Coda')],
                                key=lambda x:x['cx']) if lmin else []

                silas_por_comp = {}
                for comp in compassos:
                    silas_por_comp[comp['n']] = [
                        s for s in silas if comp['x0']<=s['cx']<=comp['x1']
                    ]

                comps_com_acorde = set()
                for a in acordes:
                    for comp in compassos:
                        if comp['x0']<=a['cx']<=comp['x1']:
                            comps_com_acorde.add(comp['n'])

                anacrusis_ns = detect_anacrusis_v2(
                    compassos, notas_comp_map, silas_por_comp, comps_com_acorde)

                # Seção
                if sec != cur_section:
                    if section_obj: output['sections'].append(section_obj)
                    section_obj = {'id':sec,'label':sec.replace('_',' ').title(),'lines':[]}
                    cur_section = sec

                line_obj = {'id':sub,'measures':[]}

                for comp in compassos:
                    measure_obj = {
                        'n':         comp['n'],
                        'x0':        round(comp['x0'],1),
                        'x1':        round(comp['x1'],1),
                        'chords':    [],
                        'notes':     [],
                        'syllables': [],
                        'beats_used':0.0,
                    }

                    # Todos os acordes do compasso
                    ac_in = [a for a in acordes if comp['x0']<=a['cx']<=comp['x1']]
                    for ac in ac_in:
                        r = t90_full(ac['cx'], compassos, notas_comp_map,
                                     silas_por_comp, silas_linha_anterior, meter)

                        # Bloco protocol — imutável
                        protocol_block = {
                            "cx":               round(ac['cx'],1),
                            "rule":             r['rule'],
                            "syllable":         r['syllable'],
                            "syllable_cx":      r['syllable_cx'],
                            "float_cx":         r.get('float_cx'),
                            "next_syllable":    r.get('next_syllable'),
                            "next_syllable_cx": r.get('next_syllable_cx'),
                            "nota_anchor":      r.get('nota_anchor'),
                        }

                        # Bloco ground_truth — calculado com human=None (unvalidated)
                        gt_block = compute_ground_truth(protocol_block, None)

                        chord_obj = {
                            "symbol":       normalize_chord(ac['text']),
                            "beat":         r['beat'],
                            "beat_ratio":   r['ratio'],
                            "protocol":     protocol_block,
                            "human":        None,
                            "ground_truth": gt_block,
                        }
                        measure_obj['chords'].append(chord_obj)

                        audit_rows.append({
                            'sec':sec,'sub':sub,
                            'acorde':normalize_chord(ac['text']),
                            'beat':r['beat'],'regra':r['rule'],
                            'silaba':r['syllable'] or r.get('next_syllable') or '—',
                            'nota':r.get('nota_anchor') or '—',
                        })

                    # Notas
                    for c in notas_comp_map.get(comp['n'],[]):
                        nome,mid = note_from_top(c['top'],pitch_table,half)
                        fig = rhythmic_figure(c['text'],c['cx'],c['top'],opus_chars)
                        if not fig: continue
                        measure_obj['notes'].append({
                            'cx':round(c['cx'],1),'note':nome,'midi':mid,
                            'figure':fig['figure'],'beats':fig['beats'],
                            'is_rest':fig['is_rest'],'flags':fig.get('flags',0),
                        })
                        if not fig['is_rest']: measure_obj['beats_used']+=fig['beats']

                    # Sílabas
                    is_anacrusis = comp['n'] in anacrusis_ns
                    for s in silas_por_comp.get(comp['n'],[]):
                        measure_obj['syllables'].append({
                            'text':s['text'],'cx':round(s['cx'],1),
                            'anacrusis':is_anacrusis,
                        })

                    line_obj['measures'].append(measure_obj)

                section_obj['lines'].append(line_obj)
                if silas: silas_linha_anterior = silas

    if section_obj: output['sections'].append(section_obj)
    return output, audit_rows


# ════════════════════════════════════════════════════════════════
# EXECUÇÃO
# ════════════════════════════════════════════════════════════════

PDF_PATH = "/mnt/user-data/uploads/Ainda_Uma_Vez_-_Coro.pdf"

LINES = [
    (0,0,127,129,None,None, "intro",   "instrumental_1"),
    (0,1,215,217,None,None, "intro",   "instrumental_2"),
    (0,2,304,306,347, 349,  "verso_1", "L1"),
    (0,3,392,394,438, 440,  "verso_1", "L2"),
    (0,4,480,482,527, 530,  "refrao",  "L1"),
    (0,5,651,653,702, 704,  "refrao",  "L2"),
    (1,0, 74, 76,128, 130,  "verso_2", "L1"),
    (1,1,164,166,217, 219,  "verso_2", "L2"),
    (1,2,265,267,314, 316,  "refrao",  "L3"),
    (1,3,418,420,469, 471,  "refrao",  "L4"),
    (1,6,588,590,None,None, "coda",    "instrumental"),
    (1,7,733,735,None,None, "final",   "instrumental"),
]

META = {
    "title":    "Ainda Uma Vez",
    "composer": "Francisco Jose Barbosa Condi",
    "key":      "Cm",
    "key_sig":  -3,
    "bpm":      "75-85",
    "meter":    "4/4",
    "clef":     "treble",
}

if __name__ == "__main__":
    output, audit = run(PDF_PATH, LINES, META)

    # Auditoria
    print("=== AUDITORIA CXD+T90 v5.1 ===\n")
    cur = None
    for r in audit:
        k = r['sec']+r['sub']
        if k!=cur: print(f"\n  ── {r['sec']} {r['sub']} ──"); cur=k
        flag = {"T90":"✅","T90.2_MELISMA":"🎵","T90.3_PAUSA":"⏸",
                "V90":"◻","VAZIO":"⚠️"}.get(r['regra'],"?")
        print(f"  {flag} {r['acorde']:<8} t{r['beat']} | {r['nota']:<14} → '{r['silaba']}' [{r['regra']}]")

    from collections import Counter
    contagem = Counter(r['regra'] for r in audit)
    total = len(audit)
    print(f"\n{'═'*60}")
    for regra,n in sorted(contagem.items()):
        print(f"  {regra:<22} {n:>3}  ({100*n//total if total else 0}%)")
    print(f"  {'TOTAL':<22} {total:>3}")
    print(f"{'═'*60}")

    # Salvar JSON
    out_path = "/home/claude/Ainda_Uma_Vez_v5_1.json"
    with open(out_path,'w',encoding='utf-8') as f:
        json.dump(output,f,ensure_ascii=False,indent=2)
    print(f"\nJSON salvo: {out_path}")

    # Validar estrutura de um acorde de exemplo
    print("\n=== ESTRUTURA DE UM ACORDE (exemplo) ===")
    for sec in output['sections']:
        for line in sec['lines']:
            for measure in line['measures']:
                if measure['chords'] and measure['syllables']:
                    print(json.dumps(measure['chords'][0], ensure_ascii=False, indent=2))
                    import sys; sys.exit(0)
