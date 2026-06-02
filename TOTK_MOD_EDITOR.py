#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOTK MOD EDITOR – Outil de modding tout-en-un pour Nintendo Switch
Version 4.1 – Code complet corrigé
"""

import sys, os, io, struct, re, fnmatch, tempfile, shutil, zipfile, tarfile, difflib
from pathlib import Path
from io import BytesIO

# Dépendances externes
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTabWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QToolBar, QStatusBar, QProgressDialog, QMenu, QHeaderView, QComboBox,
    QStyle, QAbstractItemView, QAction, QDialog, QCheckBox,
    QPlainTextEdit, QScrollArea
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import (
    QFont, QColor, QPalette, QTextCharFormat, QSyntaxHighlighter,
    QTextCursor, QPixmap, QImage
)

import py7zr
import zstandard as zstd

# ─── Configuration des jeux ──────────────────────────────────
class GameConfig:
    def __init__(self, name, lbl1_slots=101, hash_mult=0x492, align=16, langs=None):
        self.name = name
        self.lbl1_slots = lbl1_slots
        self.hash_mult = hash_mult
        self.align = align
        self.langs = langs or ["USen"]

GAMES = {
    "TotK": GameConfig("Tears of the Kingdom", 101, 0x492, 16,
                       ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh","TWzh"]),
    "BotW": GameConfig("Breath of the Wild", 101, 0x492, 16,
                       ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh"]),
    "LA": GameConfig("Link's Awakening", 101, 0x492, 16,
                     ["USen","EUfr","EUde","EUes","EUit","JPja"]),
}
current_game = GAMES["TotK"]

# ─── Dictionnaire Zstd ──────────────────────────────────────
_zstd_dict = None

def set_zstd_dict(path):
    global _zstd_dict
    with open(path, 'rb') as f:
        _zstd_dict = zstd.ZstdCompressionDict(f.read())

def decompress_zs(data):
    dctx = zstd.ZstdDecompressor(dict_data=_zstd_dict) if _zstd_dict else zstd.ZstdDecompressor()
    for method in [dctx.decompress,
                   lambda d: dctx.decompress(d, max_output_size=200_000_000),
                   lambda d: dctx.stream_reader(BytesIO(d)).read()]:
        try:
            return method(data)
        except:
            pass
    return data

def compress_zs(data):
    cctx = zstd.ZstdCompressor(dict_data=_zstd_dict) if _zstd_dict else zstd.ZstdCompressor()
    return cctx.compress(data)

# ─── Module SARC ────────────────────────────────────────────
class SarcReader:
    def __init__(self, data):
        self.data = data
        self.stream = BytesIO(data)
        self.files = {}
        self._parse()

    def _parse(self):
        s = self.stream
        if s.read(4) != b'SARC':
            raise ValueError("Not a SARC archive")
        s.read(2)  # header_size
        bom = struct.unpack('<H', s.read(2))[0]
        if bom not in (0xFFFE, 0xFEFF):
            raise ValueError(f"Bad BOM: {hex(bom)}")
        s.read(4)  # file_size
        data_offset = struct.unpack('<I', s.read(4))[0]
        s.read(4)  # version + reserved

        if s.read(4) != b'SFAT':
            raise ValueError("SFAT not found")
        pos = s.tell()
        sfat_hdr = struct.unpack('<H', s.read(2))[0]
        file_cnt = struct.unpack('<H', s.read(2))[0]
        if sfat_hdr < 8 or file_cnt > 100000:
            s.seek(pos)
            sfat_hdr = struct.unpack('<I', s.read(4))[0]
            file_cnt = struct.unpack('<I', s.read(4))[0]
        s.read(4)  # hash multiplier

        entries = []
        for _ in range(file_cnt):
            s.read(4)  # name_hash
            name_info = struct.unpack('<I', s.read(4))[0]
            file_start = struct.unpack('<I', s.read(4))[0]
            file_end = struct.unpack('<I', s.read(4))[0]
            name_off = (name_info & 0xFFFF) * 4
            entries.append((name_off, file_start + data_offset, file_end + data_offset))

        if s.read(4) != b'SFNT':
            raise ValueError("SFNT not found")
        sfnt_size = struct.unpack('<I', s.read(4))[0]
        sfnt_base = s.tell()

        for name_off, start, end in entries:
            s.seek(sfnt_base + name_off)
            name_bytes = b''
            while True:
                b = s.read(1)
                if not b or b == b'\x00':
                    break
                name_bytes += b
            name = name_bytes.decode('utf-8', errors='replace')
            if start <= end <= len(self.data):
                self.files[name] = self.data[start:end]

    def list_files(self):
        return list(self.files.keys())

    def get_file(self, name):
        return self.files.get(name, b'')


class SarcWriter:
    def __init__(self):
        self.entries = []  # list of (name, data)

    def add_file(self, name, data):
        self.entries.append((name, data))

    @staticmethod
    def _hash(name, mult=0x65):
        h = 0
        for c in name.encode('utf-8'):
            h = (h * mult + c) & 0xFFFFFFFF
        return h

    def save(self):
        self.entries.sort(key=lambda x: self._hash(x[0]))
        fc = len(self.entries)

        name_offsets = {}
        name_block = BytesIO()
        for name, _ in self.entries:
            if name not in name_offsets:
                name_offsets[name] = name_block.tell() // 4
                enc = name.encode('utf-8') + b'\x00'
                name_block.write(enc)
                pad = (4 - (name_block.tell() % 4)) % 4
                if pad:
                    name_block.write(b'\x00' * pad)
        name_data = name_block.getvalue()

        data_positions = []
        data_block = BytesIO()
        for _, data in self.entries:
            start = data_block.tell()
            data_block.write(data)
            end = data_block.tell()
            data_positions.append((start, end))
            pad = (4 - (end % 4)) % 4
            if pad:
                data_block.write(b'\x00' * pad)
        data_data = data_block.getvalue()

        sfat_size = 12 + fc * 16
        sfnt_size = 8 + len(name_data)
        data_offset = 0x14 + sfat_size + sfnt_size
        data_offset = (data_offset + 0xFF) & ~0xFF
        total_size = data_offset + len(data_data)

        out = BytesIO()
        out.write(b'SARC')
        out.write(struct.pack('<H', 0x14))
        out.write(struct.pack('<H', 0xFFFE))
        out.write(struct.pack('<I', total_size))
        out.write(struct.pack('<I', data_offset))
        out.write(struct.pack('<H', 0x0100))
        out.write(struct.pack('<H', 0x0000))

        out.write(b'SFAT')
        out.write(struct.pack('<H', sfat_size))
        out.write(struct.pack('<H', fc))
        out.write(struct.pack('<I', 0x65))

        for i, (name, _) in enumerate(self.entries):
            s, e = data_positions[i]
            out.write(struct.pack('<I', self._hash(name)))
            out.write(struct.pack('<I', (name_offsets[name] & 0xFFFF) | 0x01000000))
            out.write(struct.pack('<I', s))
            out.write(struct.pack('<I', e))

        out.write(b'SFNT')
        out.write(struct.pack('<I', sfnt_size))
        out.write(name_data)

        cur = out.tell()
        if cur < data_offset:
            out.write(b'\x00' * (data_offset - cur))
        out.write(data_data)
        return out.getvalue()


# ─── Module MSBT ────────────────────────────────────────────
class MsbtParser:
    MAGIC = b'MsgStdBn'

    def __init__(self, data, game_cfg=None):
        self.raw = data
        self.game = game_cfg or current_game
        self.labels = []
        self.texts = {}
        self._parse()

    def _parse(self):
        s = BytesIO(self.raw)
        if s.read(8) != self.MAGIC:
            raise ValueError("Not a MSBT file")
        bom = s.read(2)
        self.enc = 'utf-16-be' if bom == b'\xFE\xFF' else 'utf-16-le'
        s.read(2)
        section_count = struct.unpack('<H', s.read(2))[0]
        s.read(2)
        s.read(4)
        s.read(10)

        lbl_map = {}
        all_texts = []

        for _ in range(section_count):
            pos = s.tell()
            align = (self.game.align - (pos % self.game.align)) % self.game.align
            if align:
                s.read(align)
            magic = s.read(4)
            if not magic:
                break
            sec_size = struct.unpack('<I', s.read(4))[0]
            s.read(8)
            sec_start = s.tell()

            if magic == b'LBL1':
                lbl_map = self._read_lbl1(s, sec_start, sec_size)
            elif magic == b'TXT2':
                all_texts = self._read_txt2(s, sec_start, sec_size)
            else:
                s.read(sec_size)
            s.seek(sec_start + sec_size)

        for idx, label in sorted(lbl_map.items()):
            self.labels.append(label)
            self.texts[label] = all_texts[idx] if idx < len(all_texts) else ''

    def _read_lbl1(self, s, sec_start, sec_size):
        num_slots = struct.unpack('<I', s.read(4))[0]
        slots = []
        for _ in range(num_slots):
            count = struct.unpack('<I', s.read(4))[0]
            offset = struct.unpack('<I', s.read(4))[0]
            slots.append((count, offset))
        base = sec_start + 4 + num_slots * 8
        result = {}
        for count, offset in slots:
            if count == 0:
                continue
            s.seek(base + offset)
            for _ in range(count):
                llen = struct.unpack('B', s.read(1))[0]
                label = s.read(llen).decode('utf-8', errors='replace')
                idx = struct.unpack('<I', s.read(4))[0]
                result[idx] = label
        return result

    def _read_txt2(self, s, sec_start, sec_size):
        num = struct.unpack('<I', s.read(4))[0]
        offsets = [struct.unpack('<I', s.read(4))[0] for _ in range(num)]
        base = sec_start + 4 + num * 4
        texts = []
        for off in offsets:
            s.seek(base + off)
            chars = []
            while True:
                raw2 = s.read(2)
                if len(raw2) < 2:
                    break
                cp = struct.unpack('<H', raw2)[0]
                if cp == 0:
                    break
                if cp == 0x000E:
                    grp = struct.unpack('<H', s.read(2))[0]
                    typ = struct.unpack('<H', s.read(2))[0]
                    dsz = struct.unpack('<H', s.read(2))[0]
                    dat = s.read(dsz)
                    chars.append(f'<tag grp={grp} typ={typ} data={dat.hex()}>')
                elif cp == 0x000F:
                    chars.append('</tag>')
                else:
                    try:
                        chars.append(chr(cp))
                    except:
                        chars.append(f'<{cp:04X}>')
            texts.append(''.join(chars))
        return texts

    def to_txt(self):
        lines = []
        for label in self.labels:
            lines.append(f'[{label}]')
            lines.append(self.texts.get(label, ''))
            lines.append('---')
        return '\n'.join(lines)

    def from_txt(self, txt):
        current = None
        buf = []
        for line in txt.splitlines():
            if line.startswith('[') and line.endswith(']') and len(line) > 2:
                if current is not None and current in self.texts:
                    self.texts[current] = '\n'.join(buf)
                current = line[1:-1]
                buf = []
            elif line == '---':
                if current is not None and current in self.texts:
                    self.texts[current] = '\n'.join(buf)
                current = None
                buf = []
            else:
                if current is not None:
                    buf.append(line)
        if current is not None and current in self.texts:
            self.texts[current] = '\n'.join(buf)

    def save(self):
        cfg = self.game
        out = BytesIO()
        out.write(self.MAGIC)
        out.write(b'\xFF\xFE')
        out.write(b'\x00\x00')
        out.write(struct.pack('<H', 2))  # LBL1 + TXT2
        out.write(b'\x00\x00')
        size_pos = out.tell()
        out.write(struct.pack('<I', 0))
        out.write(b'\x00' * 10)

        def align():
            pos = out.tell()
            pad = (cfg.align - (pos % cfg.align)) % cfg.align
            if pad:
                out.write(b'\x00' * pad)

        def write_section(magic, body):
            align()
            out.write(magic)
            out.write(struct.pack('<I', len(body)))
            out.write(b'\x00' * 8)
            out.write(body)

        # LBL1
        lbl_body = BytesIO()
        slots = [[] for _ in range(cfg.lbl1_slots)]
        for idx, label in enumerate(self.labels):
            h = 0
            for c in label.encode('utf-8'):
                h = (h * cfg.hash_mult + c) & 0xFFFFFFFF
            slots[h % cfg.lbl1_slots].append((label, idx))

        lbl_body.write(struct.pack('<I', cfg.lbl1_slots))
        lbl_block = BytesIO()
        for slot in slots:
            lbl_body.write(struct.pack('<I', len(slot)))
            lbl_body.write(struct.pack('<I', lbl_block.tell()))
            for label, idx in slot:
                enc = label.encode('utf-8')
                lbl_block.write(struct.pack('B', len(enc)))
                lbl_block.write(enc)
                lbl_block.write(struct.pack('<I', idx))
        lbl_body.write(lbl_block.getvalue())
        write_section(b'LBL1', lbl_body.getvalue())

        # TXT2
        strings = []
        for label in self.labels:
            text = self.texts.get(label, '')
            strings.append(self._encode_text(text))

        txt_body = BytesIO()
        txt_body.write(struct.pack('<I', len(strings)))
        cur_off = 0
        for enc in strings:
            txt_body.write(struct.pack('<I', cur_off))
            cur_off += len(enc)
        for enc in strings:
            txt_body.write(enc)
        write_section(b'TXT2', txt_body.getvalue())

        total = out.tell()
        out.seek(size_pos)
        out.write(struct.pack('<I', total))
        return out.getvalue()

    def _encode_text(self, text):
        out = BytesIO()
        i = 0
        while i < len(text):
            if text[i:i+5] == '<tag ':
                end = text.find('>', i)
                if end != -1:
                    try:
                        parts = {}
                        for tok in text[i+5:end].split():
                            k, v = tok.split('=', 1)
                            parts[k] = v
                        grp = int(parts.get('grp', '0'))
                        typ = int(parts.get('typ', '0'))
                        dat = bytes.fromhex(parts.get('data', ''))
                        out.write(struct.pack('<H', 0x000E))
                        out.write(struct.pack('<H', grp))
                        out.write(struct.pack('<H', typ))
                        out.write(struct.pack('<H', len(dat)))
                        out.write(dat)
                    except:
                        pass
                    i = end + 1
                    continue
            if text[i:i+6] == '</tag>':
                out.write(struct.pack('<H', 0x000F))
                i += 6
                continue
            out.write(struct.pack('<H', ord(text[i])))
            i += 1
        out.write(b'\x00\x00')
        return out.getvalue()

# ─── Utilitaires fichiers/archives ──────────────────────────
def read_file(path):
    with open(path, 'rb') as f:
        return f.read()

def archive_list(path):
    ext = Path(path).suffix.lower()
    try:
        if ext == '.zip':
            with zipfile.ZipFile(path) as z:
                return [i.filename for i in z.infolist() if not i.is_dir()]
        elif ext == '.7z':
            with py7zr.SevenZipFile(path, 'r') as sz:
                return sz.getnames()
        elif ext in ('.tar', '.gz', '.bz2', '.xz'):
            with tarfile.open(path) as t:
                return [m.name for m in t.getmembers() if m.isfile()]
        elif ext == '.sarc':
            return SarcReader(read_file(path)).list_files()
        elif ext == '.zs':
            dec = decompress_zs(read_file(path))
            if dec[:4] == b'SARC':
                return SarcReader(dec).list_files()
            return [Path(path).stem]
    except:
        return []

def archive_extract(arc_path, internal):
    ext = Path(arc_path).suffix.lower()
    if ext == '.zip':
        with zipfile.ZipFile(arc_path) as z:
            return z.read(internal)
    elif ext == '.7z':
        with py7zr.SevenZipFile(arc_path, 'r') as sz:
            return sz.read([internal])[internal].read()
    elif ext in ('.tar', '.gz', '.bz2', '.xz'):
        with tarfile.open(arc_path) as t:
            return t.extractfile(t.getmember(internal)).read()
    elif ext == '.sarc':
        return SarcReader(read_file(arc_path)).get_file(internal)
    elif ext == '.zs':
        dec = decompress_zs(read_file(arc_path))
        if dec[:4] == b'SARC':
            return SarcReader(dec).get_file(internal)
        return dec
    return b''

def archive_update(arc_path, internal, new_data):
    ext = Path(arc_path).suffix.lower()
    if ext == '.zip':
        tmp = tempfile.mkdtemp()
        try:
            with zipfile.ZipFile(arc_path) as z:
                z.extractall(tmp)
            dst = os.path.join(tmp, internal)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as f:
                f.write(new_data)
            base = arc_path[:-4]
            if os.path.exists(arc_path):
                os.remove(arc_path)
            shutil.make_archive(base, 'zip', tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    elif ext == '.sarc':
        arc = SarcReader(read_file(arc_path))
        w = SarcWriter()
        for n in arc.list_files():
            w.add_file(n, new_data if n == internal else arc.get_file(n))
        with open(arc_path, 'wb') as f:
            f.write(w.save())
    elif ext == '.zs':
        dec = decompress_zs(read_file(arc_path))
        if dec[:4] == b'SARC':
            arc = SarcReader(dec)
            w = SarcWriter()
            for n in arc.list_files():
                w.add_file(n, new_data if n == internal else arc.get_file(n))
            with open(arc_path, 'wb') as f:
                f.write(compress_zs(w.save()))
        else:
            with open(arc_path, 'wb') as f:
                f.write(compress_zs(new_data))

# ─── Détection de type et vue hex ──────────────────────────
def is_text(data):
    if not data:
        return False
    sample = data[:4096]
    if b'\x00' in sample:
        return False
    ctrl = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 13))
    return (ctrl / len(sample)) <= 0.05

def build_hex_view(data, max_bytes=65536):
    lines = []
    hdr = f"{'OFFSET':>10}  {'00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F':49}  ASCII"
    lines.append(hdr)
    lines.append('─' * len(hdr))
    shown = data[:max_bytes]
    for i in range(0, len(shown), 16):
        chunk = shown[i:i+16]
        left = ' '.join(f'{b:02X}' for b in chunk[:8])
        right = ' '.join(f'{b:02X}' for b in chunk[8:])
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'0x{i:08X}  {left:<23}  {right:<23}  {asc}')
    if len(data) > max_bytes:
        lines.append(f'... ({len(data):,} octets total)')
    return '\n'.join(lines)

def decode_file(raw, hint_ext=''):
    is_z = False
    if raw[:4] == b'\x28\xB5\x2F\xFD':
        try:
            raw = decompress_zs(raw)
            is_z = True
        except:
            pass
    ext = hint_ext.lower()
    if ext == '.msbt' or raw[:8] == b'MsgStdBn':
        try:
            msbt = MsbtParser(raw)
            return 'msbt', msbt.to_txt(), raw, is_z, msbt
        except:
            pass
    if is_text(raw):
        try:
            return 'text', raw.decode('utf-8'), raw, is_z, None
        except:
            pass
        try:
            return 'text', raw.decode('utf-16'), raw, is_z, None
        except:
            pass
    return 'hex', build_hex_view(raw), raw, is_z, None

# ─── Coloration syntaxique ─────────────────────────────────
class HexHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        fmt_off = QTextCharFormat()
        fmt_off.setForeground(QColor('#569CD6'))
        fmt_hex = QTextCharFormat()
        fmt_hex.setForeground(QColor('#CE9178'))
        fmt_asc = QTextCharFormat()
        fmt_asc.setForeground(QColor('#4EC9B0'))
        if text.startswith('0x'):
            self.setFormat(0, 10, fmt_off)
            self.setFormat(12, 49, fmt_hex)
            if len(text) > 63:
                self.setFormat(63, len(text) - 63, fmt_asc)

class MsbtHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        fmt_lbl = QTextCharFormat()
        fmt_lbl.setForeground(QColor('#DCDCAA'))
        fmt_lbl.setFontWeight(QFont.Bold)
        fmt_sep = QTextCharFormat()
        fmt_sep.setForeground(QColor('#555555'))
        fmt_tag = QTextCharFormat()
        fmt_tag.setForeground(QColor('#C586C0'))
        if text.startswith('[') and text.endswith(']'):
            self.setFormat(0, len(text), fmt_lbl)
        elif text == '---':
            self.setFormat(0, len(text), fmt_sep)
        else:
            for m in re.finditer(r'</?tag[^>]*>', text):
                self.setFormat(m.start(), m.end() - m.start(), fmt_tag)

# ─── Dialogue recherche/remplacement (corrigé) ─────────────
class FindReplaceDialog(QDialog):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self._matches = []
        self._cur = -1
        self.setWindowTitle("Recherche & Remplacement")
        self.setMinimumWidth(520)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Rechercher :"))
        self.e_find = QLineEdit()
        self.e_find.textChanged.connect(self._refresh)
        lay.addWidget(self.e_find)

        lay.addWidget(QLabel("Remplacer par :"))
        self.e_repl = QLineEdit()
        lay.addWidget(self.e_repl)

        r3 = QHBoxLayout()
        self.chk_case = QCheckBox("Casse exacte")
        self.chk_word = QCheckBox("Mot entier")
        self.chk_regex = QCheckBox("Regex")
        self.lbl_count = QLabel("0 résultat(s)")
        self.lbl_count.setStyleSheet("color:#569CD6;")
        for w in (self.chk_case, self.chk_word, self.chk_regex):
            w.stateChanged.connect(self._refresh)
            r3.addWidget(w)
        r3.addStretch()
        r3.addWidget(self.lbl_count)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        for label, slot in [
            ("◀", self._prev), ("▶", self._next),
            ("Remplacer", self._replace_one),
            ("Tout remplacer", self._replace_all),
            ("Fermer", self.close),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            r4.addWidget(b)
        lay.addLayout(r4)

    def _pattern(self):
        pat = self.e_find.text()
        if not pat:
            return None
        if not self.chk_regex.isChecked():
            pat = re.escape(pat)
        if self.chk_word.isChecked():
            pat = r'\b' + pat + r'\b'
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        try:
            return re.compile(pat, flags)
        except:
            return None

    def _refresh(self):
        # Effacer l'ancienne surbrillance
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.Start)
        cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cur.setCharFormat(QTextCharFormat())
        self.editor.setTextCursor(cur)

        self._matches = []
        rx = self._pattern()
        if not rx:
            self.lbl_count.setText("0 résultat(s)")
            return
        text = self.editor.toPlainText()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor('#613214'))
        fmt.setForeground(QColor('#ffffff'))
        for m in rx.finditer(text):
            self._matches.append((m.start(), m.end()))
            c = self.editor.textCursor()
            c.setPosition(m.start())
            c.setPosition(m.end(), QTextCursor.KeepAnchor)
            c.setCharFormat(fmt)
        self.lbl_count.setText(f"{len(self._matches)} résultat(s)")
        self._cur = -1

    def _go(self, idx):
        if not self._matches:
            return
        self._cur = idx % len(self._matches)
        s, e = self._matches[self._cur]
        fmt = QTextCharFormat()
        fmt.setBackground(QColor('#D4A017'))
        fmt.setForeground(QColor('#000000'))
        c = self.editor.textCursor()
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        c.setCharFormat(fmt)
        self.editor.setTextCursor(c)
        self.editor.ensureCursorVisible()

    def _next(self):
        self._refresh()
        self._go(self._cur + 1)

    def _prev(self):
        self._refresh()
        self._go(self._cur - 1)

    def _replace_one(self):
        c = self.editor.textCursor()
        if c.hasSelection():
            c.insertText(self.e_repl.text())
        self._next()

    def _replace_all(self):
        rx = self._pattern()
        if not rx:
            return
        txt = rx.sub(self.e_repl.text(), self.editor.toPlainText())
        self.editor.setPlainText(txt)
        self._refresh()

# ─── Dialogue de comparaison ────────────────────────────────
class CompareDialog(QDialog):
    def __init__(self, left_path, right_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparaison de MSBT")
        self.resize(1200, 700)
        layout = QVBoxLayout(self)

        self.left_edit = QTextEdit()
        self.right_edit = QTextEdit()
        self.left_edit.setReadOnly(True)
        self.right_edit.setReadOnly(True)
        self.left_edit.setFont(QFont("Consolas", 10))
        self.right_edit.setFont(QFont("Consolas", 10))

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.left_edit)
        splitter.addWidget(self.right_edit)
        layout.addWidget(splitter)

        try:
            raw_left = read_file(left_path)
            raw_right = read_file(right_path)
            msbt_left = MsbtParser(raw_left)
            msbt_right = MsbtParser(raw_right)
            left_txt = msbt_left.to_txt()
            right_txt = msbt_right.to_txt()
            self.left_edit.setPlainText(left_txt)
            self.right_edit.setPlainText(right_txt)
            self._highlight_diffs(left_txt, right_txt)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))

    def _highlight_diffs(self, left_text, right_text):
        differ = difflib.Differ()
        diff = list(differ.compare(left_text.splitlines(), right_text.splitlines()))
        left_html = []
        right_html = []
        for line in diff:
            if line.startswith('  '):
                left_html.append(line[2:])
                right_html.append(line[2:])
            elif line.startswith('- '):
                left_html.append(f'<span style="background:#ffcccc">{line[2:]}</span>')
                right_html.append('')
            elif line.startswith('+ '):
                left_html.append('')
                right_html.append(f'<span style="background:#ccffcc">{line[2:]}</span>')
        self.left_edit.setHtml('<br>'.join(left_html))
        self.right_edit.setHtml('<br>'.join(right_html))

# ─── Onglet éditeur ─────────────────────────────────────────
class EditorTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.arc_path = None
        self.arc_int = None
        self.file_path = None
        self.raw = b''
        self.mode = 'hex'
        self.is_zstd = False
        self.msbt = None
        self.highlighter = None
        self._editing = False
        self._original_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)

        self.lbl_info = QLabel("—")
        self.lbl_info.setStyleSheet("background:#252526; color:#888; padding:2px 8px; font-size:11px;")
        layout.addWidget(self.lbl_info)

        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setFont(QFont("Consolas", 10))
        layout.addWidget(self.editor)

        bar = QHBoxLayout()
        bar.setContentsMargins(4,3,4,3)
        self.btn_edit = QPushButton("✏️ Éditer")
        self.btn_edit.clicked.connect(self._toggle_edit)
        self.btn_hex = QPushButton("🔢 Hex")
        self.btn_hex.clicked.connect(self._toggle_hex)
        self.btn_save = QPushButton("💾 Sauver")
        self.btn_save.clicked.connect(self._save)
        self.btn_save.setEnabled(False)
        self.btn_saveas = QPushButton("📤 Sous…")
        self.btn_saveas.clicked.connect(self._save_as)
        self.btn_find = QPushButton("🔍 Rechercher…")
        self.btn_find.clicked.connect(lambda: FindReplaceDialog(self.editor, self).show())
        for w in (self.btn_edit, self.btn_hex, self.btn_save, self.btn_saveas, self.btn_find):
            bar.addWidget(w)
        btn_w = QWidget()
        btn_w.setLayout(bar)
        btn_w.setStyleSheet("background:#252526; border-top:1px solid #333;")
        layout.addWidget(btn_w)

        self._image_widget = None

    def load_direct(self, path):
        self.file_path = path
        self.arc_path = None
        self.arc_int = None
        try:
            raw = read_file(path)
        except Exception as e:
            self.editor.setPlainText(f"Erreur lecture : {e}")
            return
        self._display(raw, Path(path).suffix)

    def load_from_archive(self, arc_path, internal):
        self.arc_path = arc_path
        self.arc_int = internal
        self.file_path = None
        try:
            raw = archive_extract(arc_path, internal)
        except Exception as e:
            self.editor.setPlainText(f"Erreur extraction : {e}")
            return
        self._display(raw, Path(internal).suffix)

    def _display(self, raw, ext=''):
        if self._image_widget:
            self.layout().replaceWidget(self._image_widget, self.editor)
            self._image_widget.deleteLater()
            self._image_widget = None
            self.editor.show()

        if ext.lower() in ('.png', '.jpg', '.jpeg', '.dds'):
            try:
                pix = QPixmap()
                if ext.lower() == '.dds':
                    try:
                        from PIL import Image
                        img = Image.open(BytesIO(raw))
                        img = img.convert("RGBA")
                        qimg = QImage(img.tobytes("raw","RGBA"), img.width, img.height, QImage.Format_RGBA8888)
                        pix = QPixmap.fromImage(qimg)
                    except ImportError:
                        pix.loadFromData(raw)
                else:
                    pix.loadFromData(raw)
                if not pix.isNull():
                    scroll = QScrollArea()
                    lbl = QLabel()
                    lbl.setPixmap(pix)
                    scroll.setWidget(lbl)
                    self.layout().replaceWidget(self.editor, scroll)
                    self.editor.hide()
                    self._image_widget = scroll
                    self.mode = 'image'
                    self.lbl_info.setText(f"  🖼 {os.path.basename(self.file_path or self.arc_int or '?')}  │  Image")
                    return
            except:
                pass

        mode, txt, raw_dec, is_z, msbt = decode_file(raw, ext)
        self.raw = raw_dec
        self.mode = mode
        self.is_zstd = is_z
        self.msbt = msbt
        self._editing = False
        self.editor.setReadOnly(True)
        self.btn_edit.setText("✏️ Éditer")
        self.btn_save.setEnabled(False)

        if self.highlighter:
            self.highlighter.setDocument(None)
        if mode == 'hex':
            self.highlighter = HexHighlighter(self.editor.document())
        elif mode == 'msbt':
            self.highlighter = MsbtHighlighter(self.editor.document())
        else:
            self.highlighter = None

        self.editor.setPlainText(txt)
        self._original_text = txt

        name = self.arc_int or (os.path.basename(self.file_path) if self.file_path else '?')
        modes = {'msbt': 'MSBT', 'text': 'Texte', 'hex': 'Binaire/Hex', 'image': 'Image'}
        zinfo = ' 🗜 zstd' if is_z else ''
        self.lbl_info.setText(f"  {name}  │  {modes.get(mode, mode)}  │  {len(raw_dec):,} o{zinfo}")

    def _toggle_edit(self):
        if self.mode == 'hex':
            return
        self._editing = not self._editing
        self.editor.setReadOnly(not self._editing)
        self.btn_edit.setText("🔒 Lecture seule" if self._editing else "✏️ Éditer")
        self.btn_save.setEnabled(self._editing)

    def _toggle_hex(self):
        if self.mode != 'hex':
            if self.highlighter:
                self.highlighter.setDocument(None)
            self.highlighter = HexHighlighter(self.editor.document())
            self.editor.setPlainText(build_hex_view(self.raw))
            self.btn_hex.setText("📝 Normal")
            self._prev_mode = self.mode
            self.mode = 'hex'
        else:
            self.mode = getattr(self, '_prev_mode', 'text')
            self._display(self.raw, Path(self.arc_int or self.file_path or '').suffix)
            self.btn_hex.setText("🔢 Hex")

    def _build_output(self):
        txt = self.editor.toPlainText()
        if self.mode == 'msbt' and self.msbt:
            self.msbt.from_txt(txt)
            data = self.msbt.save()
        elif self.mode == 'text':
            data = txt.encode('utf-8')
        else:
            data = self.raw
        if self.is_zstd:
            data = compress_zs(data)
        return data

    def _save(self):
        try:
            data = self._build_output()
            if self.arc_path and self.arc_int:
                archive_update(self.arc_path, self.arc_int, data)
                self._show_status("✅ Sauvegardé dans l'archive")
                self._original_text = self.editor.toPlainText()
            elif self.file_path:
                with open(self.file_path, 'wb') as f:
                    f.write(data)
                self._show_status("✅ Fichier sauvegardé")
                self._original_text = self.editor.toPlainText()
        except Exception as e:
            QMessageBox.critical(self, "Erreur sauvegarde", str(e))

    def _save_as(self):
        name = os.path.basename(self.arc_int or self.file_path or 'fichier')
        dest, _ = QFileDialog.getSaveFileName(self, "Enregistrer sous…", name)
        if not dest:
            return
        try:
            with open(dest, 'wb') as f:
                f.write(self._build_output())
            self._show_status(f"✅ Enregistré : {dest}")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))

    def is_modified(self):
        if self.mode in ('msbt', 'text'):
            return self.editor.toPlainText() != self._original_text
        return False

    def prompt_save(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Modifications non sauvegardées")
        box.setText(f"Voulez-vous enregistrer les modifications de '{self.tab_name()}' ?")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        return box.exec_()

    def tab_name(self):
        return os.path.basename(self.arc_int or self.file_path or "sans nom")

    def _show_status(self, msg):
        win = self.window()
        if hasattr(win, 'statusBar'):
            win.statusBar().showMessage(msg)

# ─── Arbre de fichiers (corrigé) ────────────────────────────
class FileTree(QTreeWidget):
    open_file = pyqtSignal(str)
    open_intern = pyqtSignal(str, str)

    ARCHIVE_EXT = {'.zip', '.7z', '.tar', '.gz', '.bz2', '.xz', '.sarc', '.zs'}
    EXT_ICON = {
        '.sarc': '📦', '.zs': '🗜', '.msbt': '📝',
        '.zip': '📦', '.7z': '📦', '.tar': '📦',
        '.txt': '📄', '.yaml': '📋', '.json': '📋', '.xml': '📋',
        '.png': '🖼', '.dds': '🖼', '.jpg': '🖼'
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Nom", "Taille"])
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)
        self.itemDoubleClicked.connect(self._on_dclick)
        # Connexion des deux types d'expansion
        self.itemExpanded.connect(self._on_expand)
        self.itemExpanded.connect(self._on_expand_archive_folder)
        self.root_path = ''

    def set_root(self, path):
        self.clear()
        self.root_path = path
        self._populate(self.invisibleRootItem(), path)

    def load_single_archive(self, path):
        self.clear()
        self.root_path = os.path.dirname(path)
        self._add_file_item(self.invisibleRootItem(), os.path.basename(path), path)

    def _populate(self, parent, path):
        try:
            entries = sorted(os.listdir(path), key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        except PermissionError:
            return
        for name in entries:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                item = QTreeWidgetItem(parent, [f"📁 {name}", ""])
                item.setData(0, Qt.UserRole, ('dir', full))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                QTreeWidgetItem(item, ["…"])
            else:
                self._add_file_item(parent, name, full)

    def _add_file_item(self, parent, name, full):
        ext = Path(name).suffix.lower()
        icon = self.EXT_ICON.get(ext, '📄')
        size = os.path.getsize(full) if os.path.exists(full) else 0
        item = QTreeWidgetItem(parent, [f"{icon} {name}", self._fmt_size(size)])
        item.setData(0, Qt.UserRole, ('file', full))
        if ext in self.ARCHIVE_EXT:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            QTreeWidgetItem(item, ["…"])

    def _on_expand(self, item):
        # Expansion des dossiers réels et des archives racines
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            kind, path = item.data(0, Qt.UserRole)
            if kind == 'dir':
                self._populate(item, path)
            elif kind == 'file':
                self._load_archive_children(item, path)

    def _on_expand_archive_folder(self, item):
        # Expansion des sous-dossiers d'archive
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            kind, _ = item.data(0, Qt.UserRole)
            if kind == 'archive_folder':
                item.takeChildren()
                # Trouver l'archive parente
                parent = item.parent()
                while parent:
                    pdata = parent.data(0, Qt.UserRole)
                    if pdata and pdata[0] == 'file':
                        arc_path = pdata[1]
                        internal_dir = self._get_internal_dir(item)
                        self._load_archive_subfolder(item, arc_path, internal_dir)
                        break
                    parent = parent.parent()

    def _load_archive_children(self, parent, arc_path):
        try:
            files = archive_list(arc_path)
            tree = {}
            for f in files:
                parts = Path(f).parts
                current = tree
                for i, part in enumerate(parts):
                    if part not in current:
                        is_file = (i == len(parts) - 1)
                        current[part] = {'__file__': f if is_file else None, '__children__': {}}
                    if is_file:
                        current[part]['__file__'] = f
                    current = current[part]['__children__']

            def fill(parent_item, d):
                for name, info in sorted(d.items()):
                    children = info['__children__']
                    fname = info['__file__']
                    if fname is not None:
                        ext = Path(name).suffix.lower()
                        icon = self.EXT_ICON.get(ext, '📄')
                        child = QTreeWidgetItem(parent_item, [f"{icon} {name}", ""])
                        child.setData(0, Qt.UserRole, ('archive_file', arc_path, fname))
                        child.setToolTip(0, fname)
                    else:
                        folder_item = QTreeWidgetItem(parent_item, [f"📁 {name}", ""])
                        folder_item.setData(0, Qt.UserRole, ('archive_folder', arc_path, None))
                        folder_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                        QTreeWidgetItem(folder_item, ["…"])
                        fill(folder_item, children)
            fill(parent, tree)

        except Exception as e:
            QTreeWidgetItem(parent, [f"⚠ {e}", ""])

    def _get_internal_dir(self, item):
        parts = []
        while item:
            data = item.data(0, Qt.UserRole)
            if data and data[0] == 'archive_folder':
                parts.append(item.text(0).replace('📁 ', ''))
            elif data and data[0] == 'file':
                break
            item = item.parent()
        return '/'.join(reversed(parts)) + '/'

    def _load_archive_subfolder(self, parent_item, arc_path, internal_dir):
        try:
            files = archive_list(arc_path)
            sub_files = [f for f in files if f.startswith(internal_dir)]
            tree = {}
            for f in sub_files:
                rel = os.path.relpath(f, internal_dir)
                parts = Path(rel).parts
                current = tree
                for i, part in enumerate(parts):
                    if part not in current:
                        is_file = (i == len(parts) - 1)
                        current[part] = {'__file__': f if is_file else None, '__children__': {}}
                    if is_file:
                        current[part]['__file__'] = f
                    current = current[part]['__children__']

            def fill(parent_item, d):
                for name, info in sorted(d.items()):
                    children = info['__children__']
                    fname = info['__file__']
                    if fname is not None:
                        ext = Path(name).suffix.lower()
                        icon = self.EXT_ICON.get(ext, '📄')
                        child = QTreeWidgetItem(parent_item, [f"{icon} {name}", ""])
                        child.setData(0, Qt.UserRole, ('archive_file', arc_path, fname))
                        child.setToolTip(0, fname)
                    else:
                        folder_item = QTreeWidgetItem(parent_item, [f"📁 {name}", ""])
                        folder_item.setData(0, Qt.UserRole, ('archive_folder', arc_path, None))
                        folder_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                        QTreeWidgetItem(folder_item, ["…"])
                        fill(folder_item, children)
            fill(parent_item, tree)

        except Exception as e:
            QTreeWidgetItem(parent_item, [f"⚠ {e}", ""])

    def _on_dclick(self, item, _col):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == 'file':
            ext = Path(data[1]).suffix.lower()
            if ext not in self.ARCHIVE_EXT:
                self.open_file.emit(data[1])
        elif data[0] == 'archive_file':
            _, arc_path, internal = data
            self.open_intern.emit(arc_path, internal)

    def _ctx_menu(self, pos):
        items = self.selectedItems()
        if not items:
            return
        menu = QMenu(self)
        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if data[0] == 'file':
            menu.addAction("Ouvrir").triggered.connect(lambda: self.open_file.emit(data[1]))
            menu.addAction("Extraire vers…").triggered.connect(lambda: self._extract_direct(data[1]))
            if Path(data[1]).suffix.lower() == '.msbt':
                menu.addAction("Comparer avec…").triggered.connect(lambda: self._compare_file(data[1]))
        elif data[0] == 'archive_file':
            _, arc, internal = data
            menu.addAction("Ouvrir").triggered.connect(lambda: self.open_intern.emit(arc, internal))
            menu.addAction("Extraire vers…").triggered.connect(lambda: self._extract_internal(arc, internal))
            if Path(internal).suffix.lower() == '.msbt':
                menu.addAction("📤 Exporter TXT…").triggered.connect(lambda: self._export_msbt(arc, internal))
                menu.addAction("Comparer avec…").triggered.connect(lambda: self._compare_archive_file(arc, internal))
        if len(items) > 1:
            msbt_items = [i for i in items if i.data(0, Qt.UserRole) and i.data(0, Qt.UserRole)[0] == 'archive_file' and Path(i.data(0, Qt.UserRole)[2]).suffix.lower() == '.msbt']
            if msbt_items:
                menu.addAction(f"📤 Exporter {len(msbt_items)} MSBT → TXT…").triggered.connect(lambda: self._batch_export_items(msbt_items))
        menu.exec_(self.viewport().mapToGlobal(pos))

    def _compare_file(self, path):
        other, _ = QFileDialog.getOpenFileName(self, "Choisir un autre fichier MSBT", filter="*.msbt")
        if other:
            dlg = CompareDialog(path, other, self)
            dlg.exec_()

    def _compare_archive_file(self, arc_path, internal):
        tmp = tempfile.mkdtemp()
        tmp_file = os.path.join(tmp, os.path.basename(internal))
        try:
            data = archive_extract(arc_path, internal)
            with open(tmp_file, 'wb') as f:
                f.write(data)
            self._compare_file(tmp_file)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _extract_direct(self, path):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(path))
        if dest:
            shutil.copy2(path, dest)

    def _extract_internal(self, arc, internal):
        dest, _ = QFileDialog.getSaveFileName(self, "Extraire sous…", os.path.basename(internal))
        if dest:
            try:
                data = archive_extract(arc, internal)
                with open(dest, 'wb') as f:
                    f.write(data)
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _export_msbt(self, arc, internal):
        try:
            raw = archive_extract(arc, internal)
            msbt = MsbtParser(raw)
            dest, _ = QFileDialog.getSaveFileName(self, "Exporter TXT…", Path(internal).stem + '.txt', "*.txt")
            if dest:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
        except Exception as e:
            QMessageBox.critical(self, "Erreur", str(e))

    def _batch_export_items(self, items):
        dest_dir = QFileDialog.getExistingDirectory(self, "Dossier destination")
        if not dest_dir:
            return
        done = 0
        for item in items:
            _, arc, internal = item.data(0, Qt.UserRole)
            try:
                raw = archive_extract(arc, internal)
                msbt = MsbtParser(raw)
                out = os.path.join(dest_dir, Path(internal).stem + '.txt')
                with open(out, 'w', encoding='utf-8') as f:
                    f.write(msbt.to_txt())
                done += 1
            except:
                pass
        QMessageBox.information(self, "Export", f"{done} fichier(s) exporté(s).")

    @staticmethod
    def _fmt_size(sz):
        for u in ('o', 'Ko', 'Mo', 'Go'):
            if sz < 1024:
                return f"{sz:.0f} {u}"
            sz /= 1024
        return f"{sz:.1f} To"

# ─── Style sombre ───────────────────────────────────────────
STYLE = """
QMainWindow, QWidget    { background:#1e1e1e; color:#d4d4d4; }
QMenuBar                { background:#2d2d2d; color:#ccc; }
QMenuBar::item:selected { background:#094771; }
QMenu                   { background:#2d2d2d; color:#ccc; border:1px solid #555; }
QMenu::item:selected    { background:#094771; }
QToolBar                { background:#2d2d2d; border:none; padding:3px; spacing:4px; }
QStatusBar              { background:#007ACC; color:#fff; font-size:11px; }
QSplitter::handle       { background:#333; width:3px; }

QTreeWidget             { background:#252526; border:none; color:#d4d4d4; }
QTreeWidget::item       { padding:2px 4px; }
QTreeWidget::item:selected  { background:#094771; color:#fff; }
QTreeWidget::item:hover     { background:#2a2d2e; }
QHeaderView::section    { background:#2d2d2d; color:#888; border:none; border-right:1px solid #3a3a3a; padding:3px 6px; font-size:11px; }

QTextEdit               { background:#1e1e1e; color:#d4d4d4; border:none; font-family:'Cascadia Code','Consolas',monospace; font-size:11px; selection-background-color:#264F78; }
QLineEdit               { background:#3c3c3c; color:#d4d4d4; border:1px solid #555; padding:3px 6px; border-radius:3px; }
QLineEdit:focus         { border-color:#007ACC; }
QPushButton             { background:#3a3a3a; color:#d4d4d4; border:1px solid #555; padding:3px 10px; border-radius:3px; }
QPushButton:hover       { background:#094771; border-color:#007ACC; }
QPushButton:pressed     { background:#005a9e; }
QPushButton:disabled    { color:#555; background:#2a2a2a; }

QTabWidget::pane        { border:1px solid #333; }
QTabBar::tab            { background:#2d2d2d; color:#888; padding:5px 14px; border:none; }
QTabBar::tab:selected   { background:#1e1e1e; color:#d4d4d4; border-bottom:2px solid #007ACC; }
QTabBar::tab:hover      { color:#ccc; }

QComboBox               { background:#3c3c3c; color:#d4d4d4; border:1px solid #555; padding:2px 6px; border-radius:3px; }
QComboBox QAbstractItemView { background:#2d2d2d; color:#d4d4d4; selection-background-color:#094771; }
QScrollBar:vertical     { background:#252526; width:10px; border:none; }
QScrollBar::handle:vertical { background:#424242; border-radius:5px; min-height:20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""

# ─── Fenêtre principale ─────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TOTK MOD EDITOR")
        self.setGeometry(80, 80, 1400, 860)
        self.setStyleSheet(STYLE)
        self._build_toolbar()
        self._build_central()
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.statusBar().showMessage("Prêt — Ctrl+O dossier, Ctrl+F fichier")

    def _build_toolbar(self):
        tb = self.addToolBar("Principal")
        tb.setMovable(False)

        tb.addWidget(QLabel(" Jeu : "))
        self.combo_game = QComboBox()
        self.combo_game.addItems(list(GAMES.keys()))
        self.combo_game.currentTextChanged.connect(self._change_game)
        tb.addWidget(self.combo_game)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dossier : "))
        self.e_root = QLineEdit()
        self.e_root.setMinimumWidth(260)
        self.e_root.setReadOnly(True)
        tb.addWidget(self.e_root)
        btn_folder = QPushButton("📁 Dossier")
        btn_folder.clicked.connect(self._open_folder)
        tb.addWidget(btn_folder)
        btn_file = QPushButton("📄 Fichier")
        btn_file.clicked.connect(self._open_file)
        tb.addWidget(btn_file)
        tb.addSeparator()

        tb.addWidget(QLabel(" Dict Zstd : "))
        self.e_dict = QLineEdit()
        self.e_dict.setMaximumWidth(180)
        self.e_dict.setReadOnly(True)
        self.e_dict.setPlaceholderText("(optionnel)")
        tb.addWidget(self.e_dict)
        btn_dict = QPushButton("Charger .dict")
        btn_dict.clicked.connect(self._load_dict)
        tb.addWidget(btn_dict)
        tb.addSeparator()

        btn_exp_lot = QPushButton("📤 Export MSBT→TXT (lot)")
        btn_exp_lot.clicked.connect(self._batch_export)
        tb.addWidget(btn_exp_lot)
        btn_imp_lot = QPushButton("📥 Import TXT→MSBT (lot)")
        btn_imp_lot.clicked.connect(self._batch_import)
        tb.addWidget(btn_imp_lot)

        mb = self.menuBar()
        mf = mb.addMenu("Fichier")
        a_folder = QAction("Ouvrir dossier…", self)
        a_folder.setShortcut("Ctrl+O")
        a_folder.triggered.connect(self._open_folder)
        mf.addAction(a_folder)
        a_file = QAction("Ouvrir fichier…", self)
        a_file.setShortcut("Ctrl+F")
        a_file.triggered.connect(self._open_file)
        mf.addAction(a_file)
        mf.addSeparator()
        a_quit = QAction("Quitter", self)
        a_quit.setShortcut("Ctrl+Q")
        a_quit.triggered.connect(self.close)
        mf.addAction(a_quit)

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self.tree = FileTree()
        self.tree.open_file.connect(self._open_tab_direct)
        self.tree.open_intern.connect(self._open_tab_intern)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        splitter.addWidget(self.tabs)

        splitter.setSizes([320, 1080])

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choisir le dossier ROMFS")
        if path:
            self.e_root.setText(path)
            self.tree.set_root(path)
            self.statusBar().showMessage(f"Dossier : {path}")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un fichier",
                                              filter="Tous fichiers (*);;SARC (*.sarc);;ZS (*.zs);;MSBT (*.msbt)")
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext in FileTree.ARCHIVE_EXT:
            self.e_root.setText(os.path.dirname(path))
            self.tree.load_single_archive(path)
            self.statusBar().showMessage(f"Archive : {path}")
        else:
            self._open_tab_direct(path)

    def _open_tab_direct(self, path):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.file_path == path:
                self.tabs.setCurrentIndex(i)
                return
        tab = EditorTab()
        tab.load_direct(path)
        self.tabs.addTab(tab, os.path.basename(path))
        self.tabs.setCurrentWidget(tab)

    def _open_tab_intern(self, arc_path, internal):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, EditorTab) and tab.arc_path == arc_path and tab.arc_int == internal:
                self.tabs.setCurrentIndex(i)
                return
        tab = EditorTab()
        tab.load_from_archive(arc_path, internal)
        self.tabs.addTab(tab, os.path.basename(internal))
        self.tabs.setCurrentWidget(tab)

    def _close_tab(self, idx):
        tab = self.tabs.widget(idx)
        if isinstance(tab, EditorTab) and tab.is_modified():
            ret = tab.prompt_save()
            if ret == QMessageBox.Save:
                tab._save()
            elif ret == QMessageBox.Cancel:
                return
        self.tabs.removeTab(idx)
        tab.deleteLater()

    def _change_game(self, name):
        global current_game
        if name in GAMES:
            current_game = GAMES[name]
            self.statusBar().showMessage(f"Config jeu : {GAMES[name].name}")

    def _load_dict(self):
        path, _ = QFileDialog.getOpenFileName(self, "Dictionnaire Zstd", "", "*.dict *.zsdic")
        if path:
            try:
                set_zstd_dict(path)
                self.e_dict.setText(os.path.basename(path))
                self.statusBar().showMessage(f"Dictionnaire chargé : {path}")
            except Exception as e:
                QMessageBox.critical(self, "Erreur dict", str(e))

    def _batch_export(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur", "Ouvrir d'abord un dossier.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Dossier destination TXT")
        if not dest:
            return
        prog = QProgressDialog("Export en cours…", "Annuler", 0, 0, self)
        prog.setWindowModality(Qt.WindowModal)
        prog.show()
        done, errs = 0, []
        for dirpath, _, files in os.walk(root):
            if prog.wasCanceled():
                break
            for fname in files:
                if prog.wasCanceled():
                    break
                fpath = os.path.join(dirpath, fname)
                ext = Path(fname).suffix.lower()
                prog.setLabelText(fname)
                QApplication.processEvents()
                if ext == '.msbt':
                    try:
                        raw = read_file(fpath)
                        if raw[:4] == b'\x28\xB5\x2F\xFD':
                            raw = decompress_zs(raw)
                        msbt = MsbtParser(raw)
                        rel = os.path.relpath(fpath, root)
                        out = os.path.join(dest, rel + '.txt')
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, 'w', encoding='utf-8') as f:
                            f.write(msbt.to_txt())
                        done += 1
                    except Exception as e:
                        errs.append(f"{fname}: {e}")
                elif ext in ('.sarc', '.zs'):
                    try:
                        raw = read_file(fpath)
                        if ext == '.zs':
                            raw = decompress_zs(raw)
                        if raw[:4] != b'SARC':
                            continue
                        sarc = SarcReader(raw)
                        for iname in sarc.list_files():
                            if Path(iname).suffix.lower() != '.msbt':
                                continue
                            try:
                                msbt = MsbtParser(sarc.get_file(iname))
                                rel = os.path.relpath(fpath, root)
                                out = os.path.join(dest, rel, iname + '.txt')
                                os.makedirs(os.path.dirname(out), exist_ok=True)
                                with open(out, 'w', encoding='utf-8') as f:
                                    f.write(msbt.to_txt())
                                done += 1
                            except Exception as e:
                                errs.append(f"{iname}: {e}")
                    except Exception as e:
                        errs.append(f"{fname}: {e}")
        prog.close()
        msg = f"Export terminé : {done} fichier(s)."
        if errs:
            msg += f"\n\nErreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Export par lot", msg)

    def _batch_import(self):
        root = self.e_root.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "Erreur", "Ouvrir d'abord un dossier.")
            return
        src = QFileDialog.getExistingDirectory(self, "Dossier source TXT")
        if not src:
            return
        prog = QProgressDialog("Import en cours…", "Annuler", 0, 0, self)
        prog.setWindowModality(Qt.WindowModal)
        prog.show()
        done, errs = 0, []
        for dirpath, _, files in os.walk(src):
            if prog.wasCanceled():
                break
            for fname in files:
                if prog.wasCanceled():
                    break
                if not fname.endswith('.txt'):
                    continue
                txt_path = os.path.join(dirpath, fname)
                rel_txt = os.path.relpath(txt_path, src)
                rel_orig = rel_txt[:-4] if rel_txt.endswith('.txt') else rel_txt
                orig = os.path.join(root, rel_orig)
                prog.setLabelText(fname)
                QApplication.processEvents()
                if os.path.isfile(orig) and orig.lower().endswith('.msbt'):
                    try:
                        raw = read_file(orig)
                        is_z = raw[:4] == b'\x28\xB5\x2F\xFD'
                        if is_z:
                            raw = decompress_zs(raw)
                        msbt = MsbtParser(raw)
                        with open(txt_path, 'r', encoding='utf-8') as f:
                            msbt.from_txt(f.read())
                        out = msbt.save()
                        if is_z:
                            out = compress_zs(out)
                        with open(orig, 'wb') as f:
                            f.write(out)
                        done += 1
                    except Exception as e:
                        errs.append(f"{rel_orig}: {e}")
                else:
                    parts = Path(rel_orig).parts
                    for i in range(len(parts) - 1, 0, -1):
                        arc_rel = os.path.join(*parts[:i])
                        int_name = '/'.join(parts[i:])
                        arc_path = os.path.join(root, arc_rel)
                        if os.path.isfile(arc_path) and Path(arc_path).suffix.lower() in ('.sarc', '.zs'):
                            try:
                                iraw = archive_extract(arc_path, int_name)
                                msbt = MsbtParser(iraw)
                                with open(txt_path, 'r', encoding='utf-8') as f:
                                    msbt.from_txt(f.read())
                                archive_update(arc_path, int_name, msbt.save())
                                done += 1
                            except Exception as e:
                                errs.append(f"{rel_orig}: {e}")
                            break
        prog.close()
        msg = f"Import terminé : {done} fichier(s) mis à jour."
        if errs:
            msg += f"\n\nErreurs :\n" + '\n'.join(errs[:15])
        QMessageBox.information(self, "Import par lot", msg)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())