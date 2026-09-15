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
    try:
        return page.extract_tables()
    except Exception:
        return []


def page_has_mark_name(tables):
    for t in tables:
        if not t or not t[0]: continue
        h = [(x or "").strip().upper() for x in t[0]]
        if "MARK NAME" in h:
            return True
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
    """Отметка из заголовка (y < 50). Fallback."""
    header = " ".join(t for t, x, y in words if y < 50)
    m = re.search(r'(?:отм|elev)\.?\s*([+-]?\d+[.,]\d{2,3})', header, re.IGNORECASE)
    if m: return m.group(1).replace(',', '.')
    elevs = re.findall(r'[+-]\d+[.,]\d{2,3}', header)
    return elevs[0].replace(',', '.') if elevs else None


def find_near_elev(words, mark_x, mark_y):
    """НОВОЕ: ищем отметку прямо под маркой: X±60, Y чуть ниже."""
    best = None
    best_d = 1e9
    for t, x, y in words:
        if not re.fullmatch(r'[+-]?\d+[.,]\d{2,3}', t):
            continue
        if abs(x - mark_x) > 60:
            continue
        if not (mark_y + 2 < y < mark_y + 80):
            continue
        d = abs(x - mark_x) + (y - mark_y)
        if d < best_d:
            best_d = d
            best = t.replace(',', '.')
    return best


def is_node_page(words):
    """НОВОЕ: страница-узел по заголовку."""
    header = " ".join(t for t, x, y in words if y < 100)
    return "Узел" in header or "Detail" in header


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


def find_letter_axes(words):
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


def find_number_axes(words):
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
        if len(clean) < 3: continue
        xs = [i[1] for i in clean]
        if max(xs) - min(xs) < 100: continue
        diffs = [xs[i+1] - xs[i] for i in range(len(xs)-1)]
        if not diffs: continue
        if max(diffs) - min(diffs) > max(diffs) * 0.5: continue
        axes.append((sum(i[2] for i in clean)/len(clean),
                     [(t, x) for t, x, _ in clean]))
    return axes


# ---------- НОВОЕ: ось-диапазон ----------
def find_axis_letter(letter_axes, x, y):
    """Возвращает 'E' или 'D-E' — буква или диапазон между осями."""
    candidates = []
    for lx, items in letter_axes:
        if abs(lx - x) > 500:  # та же зона плана
            continue
        for letter, ly in items:
            candidates.append((letter, ly))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1])

    above = [c for c in candidates if c[1] < y]
    below = [c for c in candidates if c[1] >= y]

    if not above and not below:
        return None
    if not above:
        return below[0][0]
    if not below:
        return above[-1][0]

    a = max(above, key=lambda c: c[1])   # ближайшая сверху
    b = min(below, key=lambda c: c[1])   # ближайшая снизу
    d_a, d_b = y - a[1], b[1] - y
    ratio = (d_a / d_b) if d_b > 0 else 0
    if d_a > d_b:
        ratio = (d_b / d_a) if d_a > 0 else 0

    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    letters = sorted([a[0], b[0]], key=letter_sort_key)
    return f"{letters[0]}-{letters[1]}"


def find_axis_number(number_axes, x, y):
    """Возвращает '1' или '1-2' — цифра или диапазон."""
    candidates = []
    for ny, items in number_axes:
        if abs(ny - y) > 500:
            continue
        for num, nx in items:
            candidates.append((num, nx))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1])

    above = [c for c in candidates if c[1] < x]   # слева
    below = [c for c in candidates if c[1] >= x]  # справа

    if not above and not below:
        return None
    if not above:
        return below[0][0]
    if not below:
        return above[-1][0]

    a = max(above, key=lambda c: c[1])
    b = min(below, key=lambda c: c[1])
    d_a, d_b = x - a[1], b[1] - x
    ratio = (d_a / d_b) if d_b > 0 else 0
    if d_a > d_b:
        ratio = (d_b / d_a) if d_a > 0 else 0

    if ratio < 0.3:
        return a[0] if d_a < d_b else b[0]
    try:
        nums = sorted([a[0], b[0]], key=lambda n: int(n))
        return f"{nums[0]}-{nums[1]}"
    except Exception:
        return f"{a[0]}-{b[0]}"


# ---------- Итоговая таблица ----------
def build_result_table(records):
    df = pd.DataFrame(records)
    if df.empty: return df

    def extract_letters(val):
        if not val or val == '?' or 'узел' in val:
            return []
        return [p.strip() for p in val.split('-') if p.strip()]

    def extract_numbers(val):
        if not val or val == '?' or 'узел' in val:
            return []
        return [p.strip() for p in val.split('-') if p.strip()]

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

        # Сводка: только по не-узлам
        plan_group = group[~group['Ось-буква'].astype(str).str.contains('узел', na=False)]
        if plan_group.empty:
            plan_group = group

        all_letters = set()
        for v in plan_group['Ось-буква']:
            all_letters.update(extract_letters(str(v)))
        sorted_letters = sorted(all_letters, key=letter_sort_key)
        if len(sorted_letters) > 1:
            let_str = f"{sorted_letters[0]}-{sorted_letters[-1]}"
        elif sorted_letters:
            let_str = sorted_letters[0]
        else:
            let_str = "?"

        all_numbers = set()
        for v in plan_group['Ось-цифра']:
            all_numbers.update(extract_numbers(str(v)))
        def num_key(n):
            try: return int(n)
            except: return 999
        sorted_numbers = sorted(all_numbers, key=num_key)
        if len(sorted_numbers) > 1:
            num_str = f"{sorted_numbers[0]}-{sorted_numbers[-1]}"
        elif sorted_numbers:
            num_str = sorted_numbers[0]
        else:
            num_str = "?"

        elevs = sort_elevs([e for e in plan_group['Отм.'] if e != '?'])
        if len(elevs) > 1:
            elev_str = f"от {elevs[0]} до {elevs[-1]}"
        elif elevs:
            elev_str = elevs[0]
        else:
            elev_str = "?"

        summary = f"{let_str}/{num_str} в отм. {elev_str}"

        for i, (_, r) in enumerate(group.iterrows()):
            # Место
            if 'узел' in str(r['Ось-буква']):
                place = f"узел (лист {r['Лист']})"
            else:
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
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        part_path = f"{pdf_path}.part_{start+1}_{end}.pdf"
        with open(part_path, "wb") as f:
            writer.write(f)
        parts.append((start + 1, end, part_path))
    return total, parts


# ---------- Основная обработка ----------
def process_pdf_by_chunks(pdf_path, status_slot=None):
    total, parts = split_pdf(pdf_path, chunk_size=12)
    marks_set = set()
    candidates = []
    skipped = []

    mark_re = re.compile(r'^[A-ZА-Я]{1,4}-?\d{1,4}$')

    for idx, (start, end, part_path) in enumerate(parts):
        if status_slot is not None:
            status_slot.info(f"⏳ Обрабатываю листы {start}–{end} из {total} (часть {idx+1}/{len(parts)})…")
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
                        if not words_raw:
                            gc.collect(); continue

                        words = [(fix_enc(w['text']), w['x0'], w['top']) for w in words_raw]
                        del words_raw

                        page_elev = get_header_elev(words)
                        letter_axes = find_letter_axes(words)
                        number_axes = find_number_axes(words)
                        node = is_node_page(words)

                        for t, x, y in words:
                            if not mark_re.match(t): continue

                            if node:
                                let = 'узел'
                                num = 'узел'
                                elev = page_elev or '?'
                            else:
                                let = find_axis_letter(letter_axes, x, y) or '?'
                                num = find_axis_number(number_axes, x, y) or '?'
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
