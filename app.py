import streamlit as st
st.set_page_config(page_title="Парсер марок из PDF", layout="wide")

import pdfplumber
import re
import pandas as pd
import tempfile
import gc
import os
from pypdf import PdfReader, PdfWriter


# ---------- Утилиты ----------
def fix_enc(s):
    if s is None: return ""
    try: return s.encode('latin-1').decode('cp1251')
    except: return s


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


LETTER_ORDER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ"
def letter_sort_key(l):
    idx = LETTER_ORDER.find(l.upper())
    return idx if idx >= 0 else 999


# ---------- Тип страницы и обозначение разреза ----------
def parse_section_designation(header_text):
    m = re.search(r'([A-ZА-Яa-zа-я0-9]+)-([A-ZА-Яa-zа-я0-9]+)\s*\(\s*(\d+)\s*\)', header_text)
    if m:
        return m.group(1).upper(), m.group(2).upper(), m.group(3)
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


# ---------- Планы: буквы в колонке, цифры в ряду ----------
def find_letter_axes_plan(words):
    items = [(t, x, y) for t, x, y in words if re.fullmatch(r'[A-ZА-Я]', t)]
    axes = []
    for g in cluster_by(items, 1, 12):
        if len(g) < 4: continue
        xs = [i[1] for i in g]; ys = [i[2] for i in g]
        if max(xs) - min(xs) > 20: continue
        if max(ys) - min(ys) < 80: continue
        gs = sorted(g, key=lambda i: i[2])
        clean = []
        for t, x, y in gs:
            if not clean or y - clean[-1][2] > 20:
                clean.append((t, x, y))
        if len(clean) < 4: continue
        axes.append((sum(xs)/len(xs), [(t, y) for t, _, y in clean]))
    return axes


def filter_digits_row(items):
    """Оставляем только чистую последовательность 1,2,3... с равным шагом."""
    items = sorted(items, key=lambda i: i[1])
    if not items: return []
    result = [items[0]]
    for i in range(1, len(items)):
        prev = result[-1]; curr = items[i]
        try:
            prev_n = int(prev[0]); curr_n = int(curr[0])
        except ValueError:
            break
        if curr_n != prev_n + 1: break
        if len(result) >= 2:
            d_prev = result[-1][1] - result[-2][1]
            d_curr = curr[1] - result[-1][1]
            if abs(d_prev - d_curr) > d_prev * 0.3: break
        result.append(curr)
    return result if len(result) >= 2 else []


def find_number_axes_plan(words):
    items = [(t, x, y) for t, x, y in words if re.fullmatch(r'\d{1,2}', t)]
    axes = []
    for g in cluster_by(items, 2, 6):
        if len(g) < 3: continue
        g = [i for i in g if not (i[1] > 440 and i[2] > 950)]
        gs = sorted(g, key=lambda i: i[1])
        clean = []
        for t, x, y in gs:
            if not clean or x - clean[-1][1] > 15:
                clean.append((t, x, y))
        filtered = filter_digits_row([(t, x) for t, x, _ in clean])
        if len(filtered) < 2: continue
        axes.append((sum(y for _, _, y in clean)/len(clean), filtered))
    return axes


# ---------- Разрезы: буквы в нижнем ряду ----------
def find_letters_in_section(words, whitelist_letters, page_height):
    items = [(t, x, y) for t, x, y in words
             if t in whitelist_letters and y > page_height * 0.7]
    if not items: return []
    # только одиночные буквы
    items = [i for i in items if re.fullmatch(r'[A-ZА-Я]', i[0])]
    # группируем по Y
    items.sort(key=lambda i: i[2])
    row = []
    prev_y = None
    for t, x, y in items:
        if prev_y is None or abs(y - prev_y) < 30:
            row.append((t, x, y))
            prev_y = y
        else:
            break
    return row


# ---------- Осевые вычисления ----------
def find_axis_letter_in_cols(letter_axes, x, y, whitelist_letters):
    if not letter_axes: return None
    nearest_col = min(letter_axes, key=lambda la: abs(la[0] - x))
    col_x, col_items = nearest_col
    if abs(col_x - x) > 400: return None
    items = sorted([c for c in col_items if c[0] in whitelist_letters], key=lambda c: c[1])
    if len(items) < 2: return None
    above = [c for c in items if c[1] < y]
    below = [c for c in items if c[1] >= y]
    if not above and not below: return None
    if not above: return below[0][0]
    if not below: return above[-1][0]
    a = above[-1]; b = below[0]
    d_a, d_b = y - a[1], b[1] - y
    ratio = min(d_a, d_b) / max(d_a, d_b) if max(d_a, d_b) > 0 else 1
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    letters = sorted([a[0], b[0]], key=letter_sort_key)
    return f"{letters[0]}-{letters[1]}"


def find_axis_number_in_rows(number_axes, x, y, whitelist_numbers):
    if not number_axes: return None
    nearest_row = min(number_axes, key=lambda na: abs(na[0] - y))
    row_y, row_items = nearest_row
    if abs(row_y - y) > 400: return None
    items = sorted([c for c in row_items if c[0] in whitelist_numbers], key=lambda c: c[1])
    if len(items) < 2: return None
    left = [c for c in items if c[1] < x]
    right = [c for c in items if c[1] >= x]
    if not left and not right: return None
    if not left: return right[0][0]
    if not right: return left[-1][0]
    a = left[-1]; b = right[0]
    d_a, d_b = x - a[1], b[1] - x
    ratio = min(d_a, d_b) / max(d_a, d_b) if max(d_a, d_b) > 0 else 1
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    try:
        nums = sorted([a[0], b[0]], key=lambda n: int(n))
        return f"{nums[0]}-{nums[1]}"
    except Exception:
        return f"{a[0]}-{b[0]}"


def find_axis_letter_in_section(letters_row, x, whitelist_letters):
    items = sorted([(t, x) for t, x, y in letters_row if t in whitelist_letters],
                   key=lambda i: i[1])
    if len(items) < 1: return None
    if len(items) == 1: return items[0][0]
    left = [c for c in items if c[1] < x]
    right = [c for c in items if c[1] >= x]
    if not left and not right: return None
    if not left: return right[0][0]
    if not right: return left[-1][0]
    a = left[-1]; b = right[0]
    d_a, d_b = x - a[1], b[1] - x
    ratio = min(d_a, d_b) / max(d_a, d_b) if max(d_a, d_b) > 0 else 1
    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    letters = sorted([a[0], b[0]], key=letter_sort_key)
    return f"{letters[0]}-{letters[1]}"


def get_section_number(section_tuple, whitelist_numbers):
    if not section_tuple: return None
    a, b, _ = section_tuple
    if a == b and a in whitelist_numbers:
        return a
    if a.isdigit() and b.isdigit() and a in whitelist_numbers and b in whitelist_numbers:
        nums = sorted([a, b], key=int)
        return f"{nums[0]}-{nums[1]}"
    return None


# ---------- Итоговая таблица ----------
def build_result_table(records):
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
        sorted_letters = sorted(all_letters, key=letter_sort_key)
        let_str = (f"{sorted_letters[0]}-{sorted_letters[-1]}" if len(sorted_letters) > 1
                   else (sorted_letters[0] if sorted_letters else "?"))

        all_numbers = set()
        for v in plan_group['Ось-цифра']: all_numbers.update(extract_numbers(v))
        def num_key(n):
            try: return int(n)
            except: return 999
        sorted_numbers = sorted(all_numbers, key=num_key)
        num_str = (f"{sorted_numbers[0]}-{sorted_numbers[-1]}" if len(sorted_numbers) > 1
                   else (sorted_numbers[0] if sorted_numbers else "?"))

        elevs = sort_elevs([e for e in plan_group['Отм.'] if e != '?'])
        elev_str = (f"от {elevs[0]} до {elevs[-1]}" if len(elevs) > 1
                    else (elevs[0] if elevs else "?"))

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
def process_pdf_by_chunks(pdf_path, status_slot=None):
    total, parts = split_pdf(pdf_path, chunk_size=12)
    whitelist_letters = set()
    whitelist_numbers = set()
    marks_set = set()
    skipped = []

    # ---- Фаза 1: whitelist осей + марки ----
    for idx, (start, end, part_path) in enumerate(parts):
        if status_slot:
            status_slot.info(f"⏳ Фаза 1/2 — анализ листов {start}–{end} из {total}")
        try:
            with pdfplumber.open(part_path) as pdf:
                for page in pdf.pages:
                    try:
                        tables = get_page_tables(page)
                        if page_has_mark_name(tables):
                            marks_set |= extract_marks_from_tables(tables)
                            del tables; gc.collect(); continue
                        del tables

                        words_raw = page.extract_words()
                        if not words_raw: continue
                        words = [(fix_enc(w['text']), w['x0'], w['top']) for w in words_raw]

                        header = " ".join(t for t, x, y in words if y < 150)
                        ptype = detect_page_type(header)

                        if ptype == "plan":
                            for _, col in find_letter_axes_plan(words):
                                for letter, _ in col: whitelist_letters.add(letter)
                            for _, row in find_number_axes_plan(words):
                                for num, _ in row: whitelist_numbers.add(num)

                        del words
                    except Exception: pass
                    gc.collect()
        except Exception as e:
            skipped.append(f"часть {start}-{end}: {e}")
        gc.collect()

    # Fallback если планы не нашлись
    if not whitelist_letters:
        whitelist_letters = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if not whitelist_numbers:
        whitelist_numbers = set(str(i) for i in range(1, 30))

    if status_slot:
        status_slot.info(f"⏳ Оси проекта: буквы = {sorted(whitelist_letters)}, цифры = {sorted(whitelist_numbers)}")

    # ---- Фаза 2: обработка ----
    candidates = []
    mark_re = re.compile(r'^[A-ZА-Я]{1,4}-?\d{1,4}$')

    for idx, (start, end, part_path) in enumerate(parts):
        if status_slot:
            status_slot.info(f"⏳ Фаза 2/2 — обработка листов {start}–{end} из {total}")
        try:
            with pdfplumber.open(part_path) as pdf:
                for local_idx, page in enumerate(pdf.pages):
                    global_num = start + local_idx
                    try:
                        tables = get_page_tables(page)
                        if page_has_mark_name(tables):
                            del tables; gc.collect(); continue
                        del tables

                        words_raw = page.extract_words()
                        if not words_raw: continue
                        words = [(fix_enc(w['text']), w['x0'], w['top']) for w in words_raw]
                        del words_raw

                        header = " ".join(t for t, x, y in words if y < 150)
                        ptype = detect_page_type(header)
                        page_elev = get_header_elev(words)
                        page_height = page.height

                        if ptype == "node":
                            for t, x, y in words:
                                if not mark_re.match(t): continue
                                candidates.append({
                                    'Лист': global_num, 'Марка': t,
                                    'Ось-буква': 'узел', 'Ось-цифра': 'узел',
                                    'Отм.': page_elev or '?',
                                })
                            del words; gc.collect(); continue

                        if ptype == "section":
                            section = parse_section_designation(header)
                            letters_row = find_letters_in_section(words, whitelist_letters, page_height)
                            num_from_section = get_section_number(section, whitelist_numbers)

                            for t, x, y in words:
                                if not mark_re.match(t): continue
                                let = find_axis_letter_in_section(letters_row, x, whitelist_letters) or '?'
                                num = num_from_section or '?'
                                elev = find_near_elev(words, x, y) or page_elev or '?'
                                candidates.append({
                                    'Лист': global_num, 'Марка': t,
                                    'Ось-буква': let, 'Ось-цифра': num,
                                    'Отм.': elev,
                                })
                            del words; gc.collect(); continue

                        # план / unknown
                        letter_axes = find_letter_axes_plan(words)
                        number_axes = find_number_axes_plan(words)

                        for t, x, y in words:
                            if not mark_re.match(t): continue
                            let = find_axis_letter_in_cols(letter_axes, x, y, whitelist_letters) or '?'
                            num = find_axis_number_in_rows(number_axes, x, y, whitelist_numbers) or '?'
                            elev = find_near_elev(words, x, y) or page_elev or '?'
                            candidates.append({
                                'Лист': global_num, 'Марка': t,
                                'Ось-буква': let, 'Ось-цифра': num,
                                'Отм.': elev,
                            })
                        del words
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
        return None, "Не найдено таблиц с MARK NAME.", skipped

    records = [r for r in candidates if r['Марка'] in marks_set]
    if not records:
        return None, "Марки из ведомости не найдены на чертежах.", skipped

    result = build_result_table(records)
    msg = f"Готово. Листов: {total}, марок в ведомости: {len(marks_set)}, вхождений: {len(records)}."
    return result, msg, skipped


# ---------- UI ----------
st.title("🏗️ Парсер марок из PDF-чертежей")
st.write(
    "Загрузите PDF с ведомостью марок и чертежами. "
    "Обработка идёт частями по 12 листов — **не закрывайте вкладку**."
)

uploaded = st.file_uploader("Выберите PDF-файл", type=["pdf"])

if uploaded is not None:
    if st.button("▶ Обработать", type="primary"):
        status = st.empty()
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                tmp.write(uploaded.read())
                pdf_path = tmp.name
            xlsx_path = pdf_path.replace('.pdf', '.xlsx')

            status.info("⏳ Начинаю обработку…")
            result, msg, skipped = process_pdf_by_chunks(pdf_path, status)
            status.empty()

            if result is None:
                st.error(msg)
            else:
                st.success(msg)
                if skipped:
                    st.warning("Пропущенные листы: " + "; ".join(skipped[:10]))
                result.to_excel(xlsx_path, index=False)
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
