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
    """Возвращает множество марок из таблиц с MARK NAME."""
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


def nearest_letter(letter_axes, x, y):
    best = None; best_d = 1e9
    for lx, items in letter_axes:
        if abs(lx - x) > 600: continue
        for letter, ly in items:
            d = abs(ly - y) + abs(lx - x) * 0.2
            if d < best_d:
                best_d = d; best = letter
    return best


def nearest_number(number_axes, x, y):
    best = None; best_d = 1e9
    for ny, items in number_axes:
        if abs(ny - y) > 600: continue
        for digit, nx in items:
            d = abs(nx - x) + abs(ny - y) * 0.2
            if d < best_d:
                best_d = d; best = digit
    return best


def build_result_table(records):
    df = pd.DataFrame(records)
    if df.empty: return df

    def sort_letters(lst):
        return sorted(set(lst), key=letter_sort_key)
    def sort_numbers(lst):
        def k(x):
            try: return int(x)
            except: return 999
        return sorted(set(lst), key=k)
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

        letters = sort_letters([l for l in group['Ось-буква'] if l != '?'])
        let_str = f"{letters[0]}-{letters[-1]}" if len(letters) > 1 else (letters[0] if letters else "?")

        numbers = sort_numbers([n for n in group['Ось-цифра'] if n != '?'])
        num_str = f"{numbers[0]}-{numbers[-1]}" if len(numbers) > 1 else (numbers[0] if numbers else "?")

        elevs = sort_elevs([e for e in group['Отм.'] if e != '?'])
        elev_str = f"от {elevs[0]} до {elevs[-1]}" if len(elevs) > 1 else (elevs[0] if elevs else "?")

        summary = f"{let_str}/{num_str} в отм. {elev_str}"

        for i, (_, r) in enumerate(group.iterrows()):
            rows.append({
                'Марка': mark,
                'Найдено, шт': count if i == 0 else '',
                'Место': f"{r['Ось-буква']}/{r['Ось-цифра']} (лист {r['Лист']}) в отм. {r['Отм.']}",
                'Сводка (от-до)': summary if i == 0 else '',
            })
    return pd.DataFrame(rows)


# ---------- Разбиение PDF на части ----------
def split_pdf(pdf_path, chunk_size=12):
    """Разбивает PDF. Возвращает (всего_страниц, [(start, end, path), ...])."""
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


# ---------- Основная обработка по частям ----------
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
                            del tables
                            gc.collect()
                            continue
                        del tables

                        words_raw = page.extract_words()
                        if not words_raw:
                            gc.collect()
                            continue

                        words = [(fix_enc(w['text']), w['x0'], w['top']) for w in words_raw]
                        del words_raw

                        page_elev = get_header_elev(words)
                        letter_axes = find_letter_axes(words)
                        number_axes = find_number_axes(words)

                        for t, x, y in words:
                            if not mark_re.match(t):
                                continue
                            let = nearest_letter(letter_axes, x, y)
                            num = nearest_number(number_axes, x, y)
                            candidates.append({
                                'Лист': global_num, 'Марка': t,
                                'Ось-буква': let or '?', 'Ось-цифра': num or '?',
                                'Отм.': page_elev or '?',
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
