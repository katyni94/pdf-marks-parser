import streamlit as st
st.set_page_config(page_title="Парсер марок из PDF", layout="wide")

import pdfplumber
import re
import pandas as pd
import tempfile
import gc
import os
from pypdf import PdfReader, PdfWriter

st.caption("Версия 8.0 — fallback на диапазон осей и отметок")


# ---------- Утилиты ----------
def fix_enc(s):
    if s is None: return ""
    try: return s.encode('latin-1').decode('cp1251')
    except: return s


def normalize_text(text):
    if not text: return ""
    for ch in ['–', '—', '−', '‐', '‑', '‒', '―', '﹣', '－']:
        text = text.replace(ch, '-')
    return text.strip()


def collapse_repeats(text):
    if not text: return text
    if 2 <= len(text) <= 4 and len(set(text)) == 1:
        return text[0]
    return text


def elev_to_float(s):
    try: return float(s.replace(',', '.').replace('+', ''))
    except: return None


def get_page_tables(page):
    try: return page.extract_tables()
    except Exception: return []


def page_has_mark_name(tables):
    for t in tables:
        if not t or not t[0]: continue
        h = [(x or "").strip().upper() for x in t[0]]
        if "MARK NAME" in h: return True
    return False


def extract_marks_from_tables(tables):
    found = set()
    for t in tables:
        if not t or not t[0]: continue
        h = [(x or "").strip().upper() for x in t[0]]
        if "MARK NAME" not in h: continue
        cm = h.index("MARK NAME")
        for row in t[2:]:
            if row and len(row) > cm and row[cm]:
                found.add(row[cm].strip())
    return found


# ---------- Нечёткое сопоставление марки ----------
def build_mark_lookup(marks_set):
    marks_by_len = {}
    all_marks = set()
    for m in marks_set:
        mu = m.upper()
        marks_by_len.setdefault(len(mu), set()).add(mu)
        all_marks.add(mu)
    return marks_by_len, all_marks


def match_mark(text, marks_by_len, all_marks):
    if not text: return None
    t = normalize_text(text).upper()
    if not t: return None

    if t in all_marks: return t

    t_rev = t[::-1]
    if t_rev in all_marks: return t_rev

    for L in sorted(marks_by_len.keys(), reverse=True):
        if L >= len(t): continue
        prefix = t[:L]
        if prefix in marks_by_len[L]:
            return prefix

    for L in sorted(marks_by_len.keys(), reverse=True):
        if L >= len(t_rev): continue
        prefix = t_rev[:L]
        if prefix in marks_by_len[L]:
            return prefix

    return None


# ---------- Отметки ----------
def get_header_elev(words):
    header = " ".join(t for t, x, y in words if y < 50)
    m = re.search(r'(?:отм|elev)\.?\s*([+-]?\d+[.,]\d{2,3})', header, re.IGNORECASE)
    if m: return m.group(1).replace(',', '.')
    elevs = re.findall(r'[+-]\d+[.,]\d{2,3}', header)
    return elevs[0].replace(',', '.') if elevs else None


def find_near_elev(words, mark_x, mark_y):
    best = None; best_d = 1e9
    for t, x, y in words:
        if not re.fullmatch(r'[+-]?\d+[.,]\d{2,3}', t): continue
        if abs(x - mark_x) > 120: continue
        if not (mark_y + 2 < y < mark_y + 120): continue
        d = abs(x - mark_x) * 0.5 + (y - mark_y)
        if d < best_d:
            best_d = d; best = t.replace(',', '.')
    return best


def cluster_by(items, axis_idx, tol):
    s = sorted(items, key=lambda i: i[axis_idx])
    groups = []
    for it in s:
        v = it[axis_idx]
        if groups and v - groups[-1][0][axis_idx] <= tol:
            groups[-1].append(it)
        else:
            groups.append([it])
    return groups


# ---------- Склейка разбитых слов ----------
def merge_adjacent_words(words_raw, gap_max=12.0, y_tol=2.5):
    if not words_raw:
        return []
    items = sorted(words_raw, key=lambda w: (round(w['top'], 1), w['x0']))
    merged = []
    cur = dict(items[0])
    for w in items[1:]:
        same_line = abs(w['top'] - cur['top']) < y_tol
        gap = w['x0'] - cur['x1']
        if same_line and -0.5 <= gap <= gap_max:
            cur['text'] = cur['text'] + w['text']
            cur['x1'] = w['x1']
        else:
            merged.append((fix_enc(cur['text']), cur['x0'], cur['top']))
            cur = dict(w)
    merged.append((fix_enc(cur['text']), cur['x0'], cur['top']))
    return [(collapse_repeats(t), x, y) for t, x, y in merged]


# ---------- Тип страницы ----------
def parse_section_designation(header_text):
    m = re.search(r"([A-ZА-Я0-9\'/]+)-([A-ZА-Я0-9\'/]+)\s*\(\s*(\d+)\s*\)", header_text.upper())
    if m: return m.group(1), m.group(2), m.group(3)
    return None


def detect_page_type(header_text):
    if parse_section_designation(header_text):
        return "section"
    h = header_text.lower()
    if "узел" in h or "detail" in h:
        return "node"
    if "разрез" in h:
        return "section"
    if "план" in h or "схема расположения" in h or "layout" in h:
        return "plan"
    return "unknown"


# ---------- Оси ----------
def find_letter_axes_plan(words, wl_letters):
    items = [(t, x, y) for t, x, y in words if t in wl_letters]
    axes = []
    for g in cluster_by(items, 1, 12):
        if len(g) < 3: continue
        xs = [i[1] for i in g]; ys = [i[2] for i in g]
        if max(xs) - min(xs) > 20: continue
        if max(ys) - min(ys) < 60: continue
        gs = sorted(g, key=lambda i: i[2])
        clean = []
        for t, x, y in gs:
            if not clean or y - clean[-1][2] > 20:
                clean.append((t, x, y))
        if len(clean) < 2: continue
        axes.append((sum(xs)/len(xs), [(t, y) for t, _, y in clean]))
    return axes


def find_number_axes_plan(words, wl_numbers):
    items = [(t, x, y) for t, x, y in words if t in wl_numbers]
    axes = []
    for g in cluster_by(items, 2, 6):
        if len(g) < 2: continue
        g = [i for i in g if not (i[1] > 440 and i[2] > 950)]
        gs = sorted(g, key=lambda i: i[1])
        clean = []
        for t, x, y in gs:
            if not clean or x - clean[-1][1] > 15:
                clean.append((t, x, y))
        if len(clean) < 2: continue
        xs = [i[1] for i in clean]
        if max(xs) - min(xs) < 100: continue
        axes.append((sum(i[2] for i in clean)/len(clean),
                     [(t, x) for t, x, _ in clean]))
    return axes


def find_letters_in_section(words, wl_letters, page_height):
    items = [(t, x, y) for t, x, y in words
             if t in wl_letters and y > page_height * 0.6]
    if not items: return []
    items.sort(key=lambda i: i[2])
    row = []
    prev_y = None
    for t, x, y in items:
        if prev_y is None or abs(y - prev_y) < 40:
            row.append((t, x, y))
            prev_y = y
        else:
            break
    return row


def nearest_in_col(letter_axes, x, y):
    if not letter_axes: return None
    nearest_col = min(letter_axes, key=lambda la: abs(la[0] - x))
    col_x, col_items = nearest_col
    if abs(col_x - x) > 500: return None
    items = sorted(col_items, key=lambda c: c[1])
    if not items: return None
    above = [c for c in items if c[1] < y]
    below = [c for c in items if c[1] >= y]
    if not above and not below: return None
    if not above: return below[0][0]
    if not below: return above[-1][0]
    a = above[-1]; b = below[0]
    d_a, d_b = y - a[1], b[1] - y
    max_d = max(d_a, d_b)
    if max_d == 0: return a[0]
    ratio = min(d_a, d_b) / max_d
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    return [a[0], b[0]]


def nearest_in_row(number_axes, x, y):
    if not number_axes: return None
    nearest_row = min(number_axes, key=lambda na: abs(na[0] - y))
    row_y, row_items = nearest_row
    if abs(row_y - y) > 500: return None
    items = sorted(row_items, key=lambda c: c[1])
    if not items: return None
    left = [c for c in items if c[1] < x]
    right = [c for c in items if c[1] >= x]
    if not left and not right: return None
    if not left: return right[0][0]
    if not right: return left[-1][0]
    a = left[-1]; b = right[0]
    d_a, d_b = x - a[1], b[1] - x
    max_d = max(d_a, d_b)
    if max_d == 0: return a[0]
    ratio = min(d_a, d_b) / max_d
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    return [a[0], b[0]]


def nearest_in_letters_row(letters_row, x):
    items = sorted([(t, x_) for t, x_, _ in letters_row], key=lambda i: i[1])
    if not items: return None
    if len(items) == 1: return items[0][0]
    left = [c for c in items if c[1] < x]
    right = [c for c in items if c[1] >= x]
    if not left and not right: return None
    if not left: return right[0][0]
    if not right: return left[-1][0]
    a = left[-1]; b = right[0]
    d_a, d_b = x - a[1], b[1] - x
    max_d = max(d_a, d_b)
    if max_d == 0: return a[0]
    ratio = min(d_a, d_b) / max_d
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    return [a[0], b[0]]


def combine_letters(val, letter_order):
    if val is None: return '?'
    if isinstance(val, list):
        items = sorted(set(val), key=lambda l: letter_order.get(l, 999))
        if len(items) == 1: return items[0]
        return f"{items[0]}-{items[-1]}"
    return val


def combine_numbers(val, number_order):
    if val is None: return '?'
    if isinstance(val, list):
        items = sorted(set(val), key=lambda n: number_order.get(n, 999))
        if len(items) == 1: return items[0]
        return f"{items[0]}-{items[-1]}"
    return val


# ---------- Итоговая таблица (с fallback) ----------
def build_result_table(records, letter_order, number_order,
                       whitelist_letters, whitelist_numbers, all_elevs):
    df = pd.DataFrame(records)
    if df.empty: return df

    def extract_letters(val):
        if not val or val == '?' or 'узел' in str(val): return []
        return [p.strip() for p in str(val).split('-') if p.strip()]

    def extract_numbers(val):
        if not val or val == '?' or 'узел' in str(val): return []
        return [p.strip() for p in str(val).split('-') if p.strip()]

    def sort_elevs(lst):
        def k(x):
            try: return float(x.replace(',', '.').replace('+', ''))
            except: return 99999
        return sorted(set(lst), key=k)

    # Заготовки fallback
    fallback_letters = (f"{whitelist_letters[0]}-{whitelist_letters[-1]}"
                        if len(whitelist_letters) > 1
                        else (whitelist_letters[0] if whitelist_letters else "?"))
    fallback_numbers = (f"{whitelist_numbers[0]}-{whitelist_numbers[-1]}"
                        if len(whitelist_numbers) > 1
                        else (whitelist_numbers[0] if whitelist_numbers else "?"))

    sorted_all_elevs = sort_elevs(all_elevs)
    if len(sorted_all_elevs) > 1:
        fallback_elev = f"от {sorted_all_elevs[0]} до {sorted_all_elevs[-1]}"
    elif sorted_all_elevs:
        fallback_elev = sorted_all_elevs[0]
    else:
        fallback_elev = "?"

    rows = []
    def mark_sort_key(m):
        parts = re.match(r'([A-ZА-Я]+)-?(\d+)', m)
        if parts: return (parts.group(1), int(parts.group(2)))
        return (m, 0)

    for mark in sorted(df['Марка'].unique(), key=mark_sort_key):
        group = df[df['Марка'] == mark]
        count = len(group)
        plan_group = group[~group['Ось-буква'].astype(str).str.contains('узел', na=False)]
        if plan_group.empty: plan_group = group

        all_letters = set()
        for v in plan_group['Ось-буква']: all_letters.update(extract_letters(v))
        sorted_letters = sorted(all_letters, key=lambda l: letter_order.get(l, 999))
        if sorted_letters:
            let_str = (f"{sorted_letters[0]}-{sorted_letters[-1]}" if len(sorted_letters) > 1
                       else sorted_letters[0])
        else:
            let_str = fallback_letters  # FALLBACK

        all_numbers = set()
        for v in plan_group['Ось-цифра']: all_numbers.update(extract_numbers(v))
        sorted_numbers = sorted(all_numbers, key=lambda n: number_order.get(n, 999))
        if sorted_numbers:
            num_str = (f"{sorted_numbers[0]}-{sorted_numbers[-1]}" if len(sorted_numbers) > 1
                       else sorted_numbers[0])
        else:
            num_str = fallback_numbers  # FALLBACK

        elevs = sort_elevs([e for e in plan_group['Отм.'] if e != '?'])
        if len(elevs) > 1:
            elev_str = f"от {elevs[0]} до {elevs[-1]}"
        elif elevs:
            elev_str = elevs[0]
        else:
            elev_str = fallback_elev  # FALLBACK

        summary = f"{let_str}/{num_str} в отм. {elev_str}"

        for i, (_, r) in enumerate(group.iterrows()):
            place = f"{r['Ось-буква']}/{r['Ось-цифра']} (лист {r['Лист']}) в отм. {r['Отм.']}"
            rows.append({
                'Марка': mark,
                'Найдено, шт': count if i == 0 else '',
                'Место': place,
                'Сводка (от-до)': summary if i == 0 else '',
            })
    return pd.DataFrame(rows)


# ---------- Разбиение PDF ----------
def split_pdf(pdf_path, chunk_size=12):
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    parts = []
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        writer = PdfWriter()
        for i in range(start, end): writer.add_page(reader.pages[i])
        part_path = f"{pdf_path}.part_{start+1}_{end}.pdf"
        with open(part_path, "wb") as f: writer.write(f)
        parts.append((start + 1, end, part_path))
    return total, parts


# ---------- Основная обработка ----------
def process_pdf_by_chunks(pdf_path, whitelist_letters, whitelist_numbers, status_slot=None):
    letter_order = {l: i for i, l in enumerate(whitelist_letters)}
    number_order = {n: i for i, n in enumerate(whitelist_numbers)}
    wl_letters_set = set(whitelist_letters)
    wl_numbers_set = set(whitelist_numbers)

    total, parts = split_pdf(pdf_path, chunk_size=12)
    marks_set = set()
    skipped = []
    candidates = []
    all_found_texts = set()
    all_elevs = set()  # для fallback по отметкам

    for idx, (start, end, part_path) in enumerate(parts):
        if status_slot:
            status_slot.info(f"⏳ Обрабатываю листы {start}–{end} из {total} (часть {idx+1}/{len(parts)})")
        try:
            with pdfplumber.open(part_path) as pdf:
                for local_idx, page in enumerate(pdf.pages):
                    global_num = start + local_idx
                    try:
                        tables = get_page_tables(page)
                        if page_has_mark_name(tables):
                            marks_set |= extract_marks_from_tables(tables)
                            del tables; gc.collect(); continue
                        del tables

                        words_raw = page.extract_words()
                        if not words_raw: continue

                        words_for_axes = [(fix_enc(collapse_repeats(w['text'])), w['x0'], w['top'])
                                          for w in words_raw]
                        words = merge_adjacent_words(words_raw, gap_max=12.0, y_tol=2.5)
                        del words_raw

                        for t, x, y in words:
                            if re.match(r'^[A-ZА-Я0-9\-/]{3,}$', t):
                                all_found_texts.add(t)
                            # собираем все отметки для fallback
                            if re.fullmatch(r'[+-]?\d+[.,]\d{2,3}', t):
                                all_elevs.add(t.replace(',', '.'))

                        header = " ".join(t for t, x, y in words_for_axes if y < 150)
                        ptype = detect_page_type(header)
                        page_elev = get_header_elev(words_for_axes)
                        if page_elev:
                            all_elevs.add(page_elev)
                        page_height = page.height

                        if not marks_set:
                            del words, words_for_axes; gc.collect(); continue

                        marks_by_len, all_marks = build_mark_lookup(marks_set)

                        if ptype == "node":
                            for t, x, y in words:
                                m = match_mark(t, marks_by_len, all_marks)
                                if not m: continue
                                candidates.append({
                                    'Лист': global_num, 'Марка': m,
                                    'Ось-буква': 'узел', 'Ось-цифра': 'узел',
                                    'Отм.': page_elev or '?',
                                })
                            del words, words_for_axes; gc.collect(); continue

                        if ptype == "section":
                            section = parse_section_designation(header)
                            letters_row = find_letters_in_section(words_for_axes, wl_letters_set, page_height)
                            num_default = None
                            if section:
                                a, b, _ = section
                                if a == b and a in wl_numbers_set:
                                    num_default = a
                                elif a in wl_numbers_set and b in wl_numbers_set:
                                    nums = sorted([a, b], key=lambda n: number_order.get(n, 999))
                                    num_default = f"{nums[0]}-{nums[1]}"

                            for t, x, y in words:
                                m = match_mark(t, marks_by_len, all_marks)
                                if not m: continue
                                let_raw = nearest_in_letters_row(letters_row, x)
                                let = combine_letters(let_raw, letter_order) if let_raw else '?'
                                num = num_default or '?'
                                elev = find_near_elev(words, x, y) or page_elev or '?'
                                candidates.append({
                                    'Лист': global_num, 'Марка': m,
                                    'Ось-буква': let, 'Ось-цифра': num,
                                    'Отм.': elev,
                                })
                            del words, words_for_axes; gc.collect(); continue

                        # план / unknown
                        letter_axes = find_letter_axes_plan(words_for_axes, wl_letters_set)
                        number_axes = find_number_axes_plan(words_for_axes, wl_numbers_set)

                        for t, x, y in words:
                            m = match_mark(t, marks_by_len, all_marks)
                            if not m: continue
                            let_raw = nearest_in_col(letter_axes, x, y)
                            num_raw = nearest_in_row(number_axes, x, y)
                            let = combine_letters(let_raw, letter_order) if let_raw else '?'
                            num = combine_numbers(num_raw, number_order) if num_raw else '?'
                            elev = find_near_elev(words, x, y) or page_elev or '?'
                            candidates.append({
                                'Лист': global_num, 'Марка': m,
                                'Ось-буква': let, 'Ось-цифра': num,
                                'Отм.': elev,
                            })
                        del words, words_for_axes
                    except Exception as e:
                        skipped.append(f"лист {global_num}: {e}")
                    gc.collect()
        except Exception as e:
            skipped.append(f"часть {start}-{end}: {e}")
        finally:
            try: os.unlink(part_path)
            except: pass
        gc.collect()

    if not marks_set:
        return None, "Не найдено таблиц с MARK NAME.", skipped, set(), pd.DataFrame(), all_found_texts

    records = candidates
    if not records:
        return None, "Марки из ведомости не найдены на чертежах.", skipped, marks_set, pd.DataFrame(), all_found_texts

    result = build_result_table(records, letter_order, number_order,
                                whitelist_letters, whitelist_numbers, all_elevs)
    found_marks = set(r['Марка'] for r in records)
    missing = marks_set - found_marks
    missing_df = pd.DataFrame({'Пропавшие марки': sorted(missing)}) if missing else pd.DataFrame()

    msg = (f"Готово. Листов: {total}, марок в ведомости: {len(marks_set)}, "
           f"найдено марок: {len(found_marks)}, вхождений: {len(records)}.")
    return result, msg, skipped, missing, missing_df, all_found_texts


# ---------- UI ----------
st.title("🏗️ Парсер марок из PDF-чертежей")
st.write(
    "Загрузите PDF с ведомостью марок и чертежами. "
    "**Впишите оси проекта** — парсер найдёт только их."
)

col1, col2 = st.columns(2)
with col1:
    letters_input = st.text_input("Буквенные оси (через запятую)", value="A, B, C, D, E, F")
with col2:
    numbers_input = st.text_input("Цифровые оси (через запятую)", value="1, 2, 3")

whitelist_letters = [x.strip().upper() for x in re.split(r'[,\n;]+', letters_input) if x.strip()]
whitelist_numbers = [x.strip().upper() for x in re.split(r'[,\n;]+', numbers_input) if x.strip()]

uploaded = st.file_uploader("Выберите PDF-файл", type=["pdf"])

# ---------- ДИАГНОСТИКА ----------
st.markdown("---")
st.subheader("🔎 Диагностика")

diag_mark = st.text_input("Найти текст (подстрока)", value="B-106")
diag_page = st.number_input("Номер листа", min_value=1, max_value=300, value=25, step=1)
diag_filter = st.text_input("Фильтр слов на листе (подстрока, пусто = все)", value="B-10")

col_d1, col_d2 = st.columns(2)
search_clicked = col_d1.button("🔎 Найти по всем листам")
show_clicked = col_d2.button("🖨 Показать лист")

if search_clicked or show_clicked:
    if uploaded is None:
        st.warning("Сначала загрузи PDF.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            tmp.write(uploaded.read())
            pdf_path = tmp.name

        with pdfplumber.open(pdf_path) as pdf:
            if search_clicked:
                target = normalize_text(diag_mark.strip()).upper()
                st.write(f"**Поиск подстроки '{target}':**")
                found_count = 0
                for i, page in enumerate(pdf.pages):
                    words_raw = page.extract_words()
                    if not words_raw: continue
                    raw_hits = []
                    for w in words_raw:
                        t = fix_enc(w['text'])
                        if target in normalize_text(t).upper():
                            raw_hits.append(t)
                    merged = merge_adjacent_words(words_raw, gap_max=12.0, y_tol=2.5)
                    merged_hits = [t for t, x, y in merged
                                   if target in normalize_text(t).upper()]
                    if raw_hits or merged_hits:
                        found_count += 1
                        st.write(f"• **Лист {i+1}**: сырых={len(raw_hits)}, склеенных={len(merged_hits)}")
                        if raw_hits:
                            st.write(f"    сырые: {raw_hits[:5]}")
                        if merged_hits:
                            st.write(f"    склеенные: {merged_hits[:5]}")
                if found_count == 0:
                    st.warning(f"'{target}' нигде не найдено.")

            if show_clicked:
                idx = diag_page - 1
                if 0 <= idx < len(pdf.pages):
                    page = pdf.pages[idx]
                    words_raw = page.extract_words()
                    st.write(f"**Лист {diag_page}: {len(words_raw)} слов.**")

                    all_words = []
                    for w in words_raw:
                        t = fix_enc(w['text'])
                        all_words.append((t, round(w['x0']), round(w['top'])))

                    if diag_filter.strip():
                        flt = diag_filter.strip().upper()
                        all_words = [x for x in all_words if flt in x[0].upper()]

                    all_words.sort(key=lambda x: x[0].upper())

                    st.write(f"**Найдено слов по фильтру: {len(all_words)}** (первые 300):")
                    lines = [f"{t}\t(x={x}, y={y})" for t, x, y in all_words[:300]]
                    st.text("\n".join(lines) if lines else "(пусто)")
                else:
                    st.error(f"Листа {diag_page} нет в файле.")

# ---------- ОСНОВНАЯ ОБРАБОТКА ----------
st.markdown("---")

if uploaded is not None:
    if not whitelist_letters or not whitelist_numbers:
        st.warning("Заполните оба поля с осями.")
    else:
        if st.button("▶ Обработать", type="primary"):
            status = st.empty()
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                    tmp.write(uploaded.read())
                    pdf_path = tmp.name
                xlsx_path = pdf_path.replace('.pdf', '.xlsx')

                status.info("⏳ Начинаю обработку…")
                result, msg, skipped, missing, missing_df, all_found = process_pdf_by_chunks(
                    pdf_path, whitelist_letters, whitelist_numbers, status)
                status.empty()

                if result is None:
                    st.error(msg)
                else:
                    st.success(msg)
                    if skipped:
                        st.warning("Пропущенные листы: " + "; ".join(skipped[:10]))
                    if missing:
                        st.warning(
                            f"**Не найдено: {len(missing)} марок.** "
                            f"Первые 30: " + ", ".join(sorted(missing)[:30])
                        )
                    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
                        result.to_excel(writer, sheet_name='Марки', index=False)
                        if not missing_df.empty:
                            missing_df.to_excel(writer, sheet_name='Не найдено', index=False)
                        if all_found:
                            pd.DataFrame({'Найдено в PDF': sorted(all_found)}).to_excel(
                                writer, sheet_name='Все тексты марок', index=False)
                    with open(xlsx_path, "rb") as f:
                        st.download_button(
                            label="⬇️ Скачать Excel",
                            data=f.read(),
                            file_name="marks_result.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
            except Exception as e:
                status.empty()
                st.error(f"Ошибка обработки: {e}")
