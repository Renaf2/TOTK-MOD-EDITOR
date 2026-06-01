#!/usr/bin/env python3
import sys, os, io, struct, fnmatch, tempfile, shutil, zipfile, tarfile
from pathlib import Path
from io import BytesIO
import yaml

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTabWidget, QPlainTextEdit,
    QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QToolBar, QStatusBar, QProgressDialog, QMenu, QHeaderView, QComboBox, QStyle
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette

import py7zr
import zstandard as zstd

# --------------------------------------------------------------------
zstd_dictionary = None
def set_zstd_dictionary(path):
    global zstd_dictionary
    with open(path,'rb') as f: zstd_dictionary = zstd.ZstdCompressionDict(f.read())
def decompress_zs(data):
    if zstd_dictionary: dctx = zstd.ZstdDecompressor(dict_data=zstd_dictionary)
    else: dctx = zstd.ZstdDecompressor()
    for method in [dctx.decompress, lambda d: dctx.decompress(d, max_output_size=100_000_000),
                   lambda d: dctx.stream_reader(BytesIO(d)).read()]:
        try: return method(data)
        except: pass
    return data
def compress_zs(data):
    if zstd_dictionary: cctx = zstd.ZstdCompressor(dict_data=zstd_dictionary)
    else: cctx = zstd.ZstdCompressor()
    return cctx.compress(data)

# --------------------------------------------------------------------
class GameConfig:
    def __init__(self, name="TotK", endian='<', align_bytes=16, section_padding=b'\x00', lbl1_num_slots=101, hash_mult=0x65, language_order=[]):
        self.name=name; self.endian=endian; self.align_bytes=align_bytes; self.section_padding=section_padding
        self.lbl1_num_slots=lbl1_num_slots; self.hash_mult=hash_mult; self.language_order=language_order
GAMES = {
    "TotK": GameConfig("Tears of the Kingdom", '<', 16, b'\x00', 101, 0x65, ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh"]),
    "BotW": GameConfig("Breath of the Wild", '<', 16, b'\x00', 101, 0x65, ["USen","EUfr","EUde","EUes","EUit","JPja","KRko","CNzh"]),
    "Link's Awakening": GameConfig("Link's Awakening", '<', 16, b'\x00', 101, 0x65, ["USen","EUfr","EUde","EUes","EUit","JPja"]),
}
current_game_config = GAMES["TotK"]

def is_probably_text(data):
    if not data: return False
    sample = data[:4096]; control=0
    for b in sample:
        if b==0: return False
        if b<0x20 and b not in (9,10,13): control+=1
    return (control/len(sample))<=0.1

# --------------------------------------------------------------------
class SarcReader:
    def __init__(self, data): self.data=data; self.stream=BytesIO(data); self.files={}; self._parse()
    def _parse(self):
        magic=self.stream.read(4)
        if magic!=b'SARC': raise ValueError("Not a SARC")
        header_size=struct.unpack('<H', self.stream.read(2))[0]
        bom=struct.unpack('<H', self.stream.read(2))[0]
        if bom not in (0xFFFE,0xFEFF): raise ValueError(f"BOM: {hex(bom)}")
        file_size=struct.unpack('<I', self.stream.read(4))[0]
        data_offset=struct.unpack('<I', self.stream.read(4))[0]
        self.stream.read(4)
        magic2=self.stream.read(4)
        if magic2!=b'SFAT': raise ValueError("SFAT not found")
        pos_backup=self.stream.tell()
        sfat_size=struct.unpack('<I', self.stream.read(4))[0]
        file_count=struct.unpack('<I', self.stream.read(4))[0]
        if sfat_size<12 or file_count>100000:
            self.stream.seek(pos_backup)
            sfat_size=struct.unpack('<H', self.stream.read(2))[0]
            file_count=struct.unpack('<H', self.stream.read(2))[0]
        hash_multiplier=struct.unpack('<I', self.stream.read(4))[0]
        entries=[]
        for _ in range(file_count):
            name_hash=struct.unpack('<I', self.stream.read(4))[0]
            name_info=struct.unpack('<I', self.stream.read(4))[0]
            file_start=struct.unpack('<I', self.stream.read(4))[0]
            file_end=struct.unpack('<I', self.stream.read(4))[0]
            name_offset=(name_info & 0xFFFF)*4
            entries.append((name_offset, file_start+data_offset, file_end+data_offset))
        magic3=self.stream.read(4)
        if magic3!=b'SFNT': raise ValueError("SFNT not found")
        sfnt_size=struct.unpack('<I', self.stream.read(4))[0]
        sfnt_start=self.stream.tell()
        name_block=self.stream.read(sfnt_size)
        for name_offset,start,end in entries:
            self.stream.seek(sfnt_start+name_offset)
            name_bytes=b''
            while True:
                b=self.stream.read(1)
                if not b or b==b'\x00': break
                name_bytes+=b
            name=name_bytes.decode('utf-8',errors='replace')
            if start<=end<=len(self.data): self.files[name]=self.data[start:end]
    def list_files(self): return list(self.files.keys())
    def get_file(self,name): return self.files.get(name)

class SarcWriter:
    def __init__(self): self.files=[]
    def add_file(self,name,data): self.files.append((name,data))
    def _hash(self,name,mult=0x65):
        res=0
        for c in name.encode(): res=(res*mult+c)&0xFFFFFFFF
        return res
    def save(self):
        self.files.sort(key=lambda x: self._hash(x[0]))
        out=BytesIO(); fc=len(self.files)
        name_offsets={}; name_block=BytesIO()
        for name,_ in self.files:
            if name not in name_offsets:
                name_offsets[name]=name_block.tell()//4
                enc=name.encode()+b'\x00'
                name_block.write(enc)
                pad=(4-(name_block.tell()%4))%4
                name_block.write(b'\x00'*pad)
        name_data=name_block.getvalue()
        data_positions=[]; data_block=BytesIO()
        for _,data in self.files:
            start=data_block.tell()
            data_block.write(data)
            end=data_block.tell()
            data_positions.append((start,end))
            pad=(4-(end%4))%4
            if pad: data_block.write(b'\x00'*pad)
        data_data=data_block.getvalue()
        sfat_size=12+fc*16; sfnt_size=8+len(name_data)
        data_offset=0x14+sfat_size+sfnt_size
        data_offset=(data_offset+0xFF)&~0xFF
        total_size=data_offset+len(data_data)
        out.write(b'SARC')
        out.write(struct.pack('<H',0x14)); out.write(struct.pack('<H',0xFFFE))
        out.write(struct.pack('<I',total_size)); out.write(struct.pack('<I',data_offset))
        out.write(struct.pack('<H',0x0100)); out.write(struct.pack('<H',0))
        out.write(b'SFAT'); out.write(struct.pack('<I',sfat_size)); out.write(struct.pack('<I',fc))
        out.write(struct.pack('<I',0x65))
        for i,(name,data) in enumerate(self.files):
            s,e=data_positions[i]
            out.write(struct.pack('<I',self._hash(name)))
            out.write(struct.pack('<I',(name_offsets[name]&0xFFFF)|0x01000000))
            out.write(struct.pack('<I',s)); out.write(struct.pack('<I',e))
        out.write(b'SFNT'); out.write(struct.pack('<I',sfnt_size))
        out.write(name_data)
        cur=out.tell()
        if cur<data_offset: out.write(b'\x00'*(data_offset-cur))
        out.write(data_data)
        return out.getvalue()

# --------------------------------------------------------------------
class MsbtParser:
    def __init__(self, data, game_config=None):
        self.data=data; self.stream=BytesIO(data); self.entries={}; self.labels_order=[]; self.languages=[]
        self.game=game_config or current_game_config
        self._parse()
    def _parse(self):
        stream=self.stream
        magic=stream.read(8)
        if magic!=b'MsgStdBn': raise ValueError("Not MSBT")
        stream.read(2); stream.read(2)
        section_count=struct.unpack('<H', stream.read(2))[0]
        stream.read(2); stream.read(4); stream.read(10)
        labels=[]; texts_by_lang=[]; lang_names=[]
        for _ in range(section_count):
            pos=stream.tell()
            align=(self.game.align_bytes - (pos % self.game.align_bytes)) % self.game.align_bytes
            if align: stream.read(align)
            sec_magic=stream.read(4)
            if not sec_magic: break
            sec_size=struct.unpack('<I', stream.read(4))[0]
            stream.read(8); sec_start=stream.tell()
            if sec_magic==b'LBL1':
                num_slots=struct.unpack('<I', stream.read(4))[0]
                for _ in range(num_slots): stream.read(8)
                while stream.tell()<sec_start+sec_size:
                    length=struct.unpack('B', stream.read(1))[0]
                    if length==0: break
                    name=stream.read(length).decode('utf-8',errors='replace')
                    index=struct.unpack('<I', stream.read(4))[0]
                    labels.append((index,name))
                labels.sort(key=lambda x:x[0])
                self.labels_order=[lbl for _,lbl in labels]
            elif sec_magic==b'ATR1':
                num_langs=struct.unpack('<I', stream.read(4))[0]
                lang_offsets=[struct.unpack('<I', stream.read(4))[0] for _ in range(num_langs)]
                for off in lang_offsets:
                    stream.seek(sec_start+off)
                    lang_bytes=b''
                    while True:
                        b=stream.read(1)
                        if not b or b==0: break
                        lang_bytes+=b
                    lang_names.append(lang_bytes.decode('utf-8',errors='replace'))
                self.languages=lang_names
            elif sec_magic==b'TXT2':
                num_strings=struct.unpack('<I', stream.read(4))[0]
                offsets=[struct.unpack('<I', stream.read(4))[0] for _ in range(num_strings)]
                texts=[]
                for off in offsets:
                    stream.seek(sec_start+4+num_strings*4+off)
                    chars=[]
                    while True:
                        raw=stream.read(2)
                        if len(raw)<2: break
                        cp=struct.unpack('<H',raw)[0]
                        if cp==0: break
                        try: chars.append(chr(cp))
                        except: chars.append(f'<{cp:04X}>')
                    texts.append(''.join(chars))
                texts_by_lang.append(texts)
            stream.seek(sec_start+sec_size)
        if not lang_names: lang_names=["USen"]
        self.languages=lang_names
        for lang_idx,lang_texts in enumerate(texts_by_lang):
            lang=lang_names[lang_idx] if lang_idx<len(lang_names) else f"lang_{lang_idx}"
            for text_idx,text in enumerate(lang_texts):
                if text_idx<len(labels):
                    label=labels[text_idx][1]
                    if label not in self.entries: self.entries[label]={}
                    self.entries[label][lang]=text
    def to_text(self):
        lines=[]
        for label in self.labels_order:
            entry=self.entries.get(label,{})
            if entry:
                lines.append(f"[{label}]")
                for lang,text in entry.items(): lines.append(f"{lang}: {text}")
                lines.append("")
        return "\n".join(lines)
    def to_yaml(self):
        data={}
        for label in self.labels_order:
            entry=self.entries.get(label,{})
            if entry: data[label]=entry
        return yaml.dump(data, allow_unicode=True, sort_keys=False)
    def from_yaml(self, yaml_str):
        data=yaml.safe_load(yaml_str)
        for label,translations in data.items():
            self.entries[label]=translations
            if label not in self.labels_order: self.labels_order.append(label)
    def update_from_text(self, text_str):
        current_label=None
        for line in text_str.splitlines():
            line=line.strip()
            if not line: current_label=None; continue
            if line.startswith("[") and line.endswith("]"): current_label=line[1:-1]; continue
            if current_label and ":" in line:
                lang,value=line.split(":",1)
                lang=lang.strip(); value=value.strip()
                if current_label not in self.entries: self.entries[current_label]={}
                self.entries[current_label][lang]=value
    def save(self):
        out=BytesIO()
        cfg=self.game
        all_langs=set()
        for entry in self.entries.values():
            for lang in entry: all_langs.add(lang)
        if not all_langs: all_langs={"USen"}
        ordered_langs=[]
        for lang in cfg.language_order:
            if lang in all_langs: ordered_langs.append(lang)
        remaining=sorted(all_langs-set(ordered_langs))
        ordered_langs.extend(remaining)
        num_labels=len(self.labels_order)
        lang_texts={lang:[] for lang in ordered_langs}
        for label in self.labels_order:
            entry=self.entries.get(label,{})
            for lang in ordered_langs: lang_texts[lang].append(entry.get(lang,""))
        out.write(b'MsgStdBn'); out.write(b'\xFF\xFE'); out.write(b'\x00\x00')
        num_sections=2+len(ordered_langs)
        out.write(struct.pack('<H', num_sections)); out.write(b'\x00\x00')
        size_pos=out.tell(); out.write(struct.pack('<I',0)); out.write(b'\x00'*10)
        def write_section(magic,content):
            pos=out.tell()
            align=(cfg.align_bytes-(pos%cfg.align_bytes))%cfg.align_bytes
            out.write(cfg.section_padding*align)
            out.write(magic); out.write(struct.pack('<I',len(content)))
            out.write(cfg.section_padding*8); out.write(content)
        # LBL1
        lbl_body=BytesIO(); slots=[[] for _ in range(cfg.lbl1_num_slots)]
        for i,label in enumerate(self.labels_order):
            h=0
            for c in label.encode('utf-8'): h=(h*cfg.hash_mult+c)&0xFFFFFFFF
            slots[h%cfg.lbl1_num_slots].append((label,i))
        lbl_body.write(struct.pack('<I',cfg.lbl1_num_slots))
        label_block=BytesIO()
        for slot in slots:
            lbl_body.write(struct.pack('<I',len(slot)))
            lbl_body.write(struct.pack('<I',label_block.tell()))
            for label,idx in slot:
                enc=label.encode('utf-8')
                label_block.write(struct.pack('B',len(enc)))
                label_block.write(enc); label_block.write(struct.pack('<I',idx))
        lbl_body.write(label_block.getvalue())
        write_section(b'LBL1', lbl_body.getvalue())
        # ATR1
        atr_body=BytesIO(); atr_body.write(struct.pack('<I',len(ordered_langs)))
        name_offsets=[4+len(ordered_langs)*4]
        for i in range(1,len(ordered_langs)):
            name_offsets.append(name_offsets[i-1]+len(ordered_langs[i-1].encode('utf-8'))+1)
        for off in name_offsets: atr_body.write(struct.pack('<I',off))
        for lang in ordered_langs: atr_body.write(lang.encode('utf-8')+b'\x00')
        write_section(b'ATR1', atr_body.getvalue())
        # TXT2
        for lang in ordered_langs:
            txt_body=BytesIO(); txt_body.write(struct.pack('<I',num_labels))
            texts=lang_texts[lang]; offsets=[0]
            for i in range(1,num_labels):
                offsets.append(offsets[i-1]+len(texts[i-1].encode('utf-16-le'))+2)
            for off in offsets: txt_body.write(struct.pack('<I',off))
            for text in texts:
                enc=text.encode('utf-16-le')+b'\x00\x00'
                txt_body.write(enc)
            write_section(b'TXT2', txt_body.getvalue())
        total=out.tell(); out.seek(size_pos); out.write(struct.pack('<I',total))
        return out.getvalue()

# --------------------------------------------------------------------
class ArchiveManager:
    @staticmethod
    def list_archive_files(path):
        ext=Path(path).suffix.lower()
        if ext=='.zip':
            with zipfile.ZipFile(path) as z: return [i.filename for i in z.infolist() if not i.is_dir()]
        elif ext=='.7z':
            with py7zr.SevenZipFile(path,'r') as sz: return sz.getnames()
        elif ext in ('.tar','.gz','.bz2','.xz'):
            with tarfile.open(path) as t: return [m.name for m in t.getmembers() if m.isfile()]
        elif ext=='.sarc':
            with open(path,'rb') as f: return SarcReader(f.read()).list_files()
        elif ext=='.zs':
            with open(path,'rb') as f: raw=f.read()
            dec=decompress_zs(raw)
            if dec[:4]==b'SARC': return SarcReader(dec).list_files()
            return [Path(path).stem]
        return []

    @staticmethod
    def extract_file(archive_path, internal_name, dest_path):
        ext=Path(archive_path).suffix.lower(); parent=os.path.dirname(dest_path)
        if parent: os.makedirs(parent,exist_ok=True)
        if ext=='.zip':
            with zipfile.ZipFile(archive_path) as z:
                with open(dest_path,'wb') as f: f.write(z.read(internal_name))
        elif ext=='.7z':
            with py7zr.SevenZipFile(archive_path,'r') as sz:
                sz.extract(targets=[internal_name],path=parent)
            extracted=os.path.join(parent,internal_name)
            if extracted!=dest_path and os.path.exists(extracted): shutil.move(extracted,dest_path)
        elif ext in ('.tar','.gz','.bz2','.xz'):
            with tarfile.open(archive_path) as t:
                f=t.extractfile(t.getmember(internal_name))
                with open(dest_path,'wb') as out: out.write(f.read())
        elif ext=='.sarc':
            arc=SarcReader(open(archive_path,'rb').read())
            with open(dest_path,'wb') as f: f.write(arc.get_file(internal_name))
        elif ext=='.zs':
            with open(archive_path,'rb') as f: raw=f.read()
            dec=decompress_zs(raw)
            if dec[:4]==b'SARC':
                sarc=SarcReader(dec)
                with open(dest_path,'wb') as f: f.write(sarc.get_file(internal_name))
            else:
                with open(dest_path,'wb') as f: f.write(dec)

    @staticmethod
    def update_archive(archive_path, internal_name, new_file_path):
        ext=Path(archive_path).suffix.lower()
        if ext=='.zip':
            tmp=tempfile.mkdtemp()
            try:
                with zipfile.ZipFile(archive_path) as z: z.extractall(tmp)
                dst=os.path.join(tmp,internal_name)
                os.makedirs(os.path.dirname(dst),exist_ok=True); shutil.copy2(new_file_path,dst)
                base=archive_path[:-4]
                if os.path.exists(archive_path): os.remove(archive_path)
                shutil.make_archive(base,'zip',tmp)
            finally: shutil.rmtree(tmp,ignore_errors=True)
        elif ext=='.sarc':
            arc=SarcReader(open(archive_path,'rb').read()); writer=SarcWriter()
            new_data=open(new_file_path,'rb').read()
            for fname in arc.list_files():
                if fname==internal_name: writer.add_file(fname,new_data)
                else: writer.add_file(fname,arc.get_file(fname))
            with open(archive_path,'wb') as f: f.write(writer.save())
        elif ext=='.zs':
            with open(archive_path,'rb') as f: raw=f.read()
            dec=decompress_zs(raw)
            if dec[:4]==b'SARC':
                arc=SarcReader(dec); writer=SarcWriter()
                new_data=open(new_file_path,'rb').read()
                for fname in arc.list_files():
                    if fname==internal_name: writer.add_file(fname,new_data)
                    else: writer.add_file(fname,arc.get_file(fname))
                sarc_bytes=writer.save()
                with open(archive_path,'wb') as f: f.write(compress_zs(sarc_bytes))
            else:
                new_data=open(new_file_path,'rb').read()
                with open(archive_path,'wb') as f: f.write(compress_zs(new_data))

# --------------------------------------------------------------------
class FileTreeWidget(QTreeWidget):
    open_file_signal=pyqtSignal(str)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Nom","Taille"]); self.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.setContextMenuPolicy(Qt.CustomContextMenu); self.customContextMenuRequested.connect(self._context_menu)
        self.itemDoubleClicked.connect(self._on_double_click); self.itemExpanded.connect(self._on_expand)
        self.root_path=""
    def set_root(self,root_dir):
        self.clear(); self.root_path=root_dir; self._populate(self.invisibleRootItem(),root_dir)
    def _populate(self,parent_item,path):
        try: items=sorted(os.listdir(path),key=lambda x:(not os.path.isdir(os.path.join(path,x)),x.lower()))
        except PermissionError: return
        for name in items:
            full=os.path.join(path,name)
            if os.path.isdir(full):
                folder_item=QTreeWidgetItem(parent_item,[name,""])
                folder_item.setData(0,Qt.UserRole,("folder",full))
                folder_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                self._add_dummy_child(folder_item)
            else:
                size=os.path.getsize(full)
                item=QTreeWidgetItem(parent_item,[name,self._format_size(size)])
                item.setData(0,Qt.UserRole,("file",full)); self._set_icon(item,full)
    def _add_dummy_child(self,item):
        dummy=QTreeWidgetItem(item,["Chargement..."]); item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
    def _on_expand(self,item):
        if item.childCount()==1 and item.child(0).text(0)=="Chargement...":
            item.removeChild(item.child(0))
            path=item.data(0,Qt.UserRole)[1]; self._populate(item,path)
    def _on_double_click(self,item,col):
        kind,path=item.data(0,Qt.UserRole)
        if kind=="file":
            ext=os.path.splitext(path)[1].lower()
            if ext in ('.zip','.7z','.sarc','.zs','.tar','.gz','.bz2','.xz'):
                self._load_archive_content(item,path)
            else: self.open_file_signal.emit(path)
        elif kind=="archive_file":
            archive_path=item.parent().data(0,Qt.UserRole)[1]; internal_name=path
            tmp=tempfile.mkdtemp(); dest=os.path.join(tmp,os.path.basename(internal_name))
            try:
                ArchiveManager.extract_file(archive_path,internal_name,dest); self.open_file_signal.emit(dest)
            except Exception as e: QMessageBox.critical(self,"Erreur",str(e))
    def _load_archive_content(self,item,archive_path):
        if item.childCount()>0 and item.child(0).data(0,Qt.UserRole) is not None: return
        item.takeChildren()
        try:
            files=ArchiveManager.list_archive_files(archive_path)
            for f in files:
                child=QTreeWidgetItem(item,[f,""]); child.setData(0,Qt.UserRole,("archive_file",f))
                child.setIcon(0,self.style().standardIcon(QStyle.SP_FileIcon))
        except Exception as e: QMessageBox.critical(self,"Erreur",str(e))
    def _context_menu(self,pos):
        item=self.itemAt(pos)
        if not item: return
        kind,path=item.data(0,Qt.UserRole); menu=QMenu()
        if kind=="file":
            action_open=menu.addAction("Ouvrir"); action_open.triggered.connect(lambda: self.open_file_signal.emit(path))
            action_extract=menu.addAction("Extraire vers..."); action_extract.triggered.connect(lambda: self._extract_file_to(path))
        elif kind=="archive_file":
            action_extract=menu.addAction("Extraire ce fichier..."); action_extract.triggered.connect(lambda: self._extract_archive_file(item))
        menu.exec_(self.viewport().mapToGlobal(pos))
    def _extract_file_to(self,file_path):
        dest=QFileDialog.getSaveFileName(self,"Extraire sous",os.path.basename(file_path))[0]
        if dest: shutil.copy2(file_path,dest)
    def _extract_archive_file(self,item):
        archive_path=item.parent().data(0,Qt.UserRole)[1]; internal=item.data(0,Qt.UserRole)[1]
        dest=QFileDialog.getSaveFileName(self,"Extraire",os.path.basename(internal))[0]
        if dest:
            try: ArchiveManager.extract_file(archive_path,internal,dest)
            except Exception as e: QMessageBox.critical(self,"Erreur",str(e))
    def _set_icon(self,item,path):
        ext=os.path.splitext(path)[1].lower()
        if ext in ('.zip','.7z','.sarc','.zs','.tar','.gz','.bz2','.xz'):
            item.setIcon(0,self.style().standardIcon(QStyle.SP_DriveHDIcon))
        else: item.setIcon(0,self.style().standardIcon(QStyle.SP_FileIcon))
    @staticmethod
    def _format_size(size):
        for unit in ['o','Ko','Mo','Go']:
            if size<1024.0: return f"{size:.1f} {unit}"
            size/=1024.0
        return f"{size:.1f} To"

# --------------------------------------------------------------------
class EditorTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout=QVBoxLayout(self); self.layout.setContentsMargins(0,0,0,0)
        self.info_label=QLabel("Fermé"); self.info_label.setStyleSheet("background:#2a2a2a; color:#aaa; padding:2px;")
        self.layout.addWidget(self.info_label)
        self.editor=QPlainTextEdit(); self.editor.setReadOnly(True); self.editor.setFont(QFont("Consolas",10))
        self.layout.addWidget(self.editor)
        # barre recherche
        search_layout=QHBoxLayout()
        self.search_input=QLineEdit(); self.search_input.setPlaceholderText("Rechercher…"); self.search_input.setVisible(False)
        self.btn_search_next=QPushButton("↓"); self.btn_search_prev=QPushButton("↑"); self.btn_search_close=QPushButton("✕")
        self.search_input.returnPressed.connect(self.search_next)
        self.btn_search_next.clicked.connect(self.search_next); self.btn_search_prev.clicked.connect(self.search_prev)
        self.btn_search_close.clicked.connect(self.hide_search)
        search_layout.addWidget(self.search_input); search_layout.addWidget(self.btn_search_next)
        search_layout.addWidget(self.btn_search_prev); search_layout.addWidget(self.btn_search_close)
        self.layout.addLayout(search_layout)
        # barre outils
        tool_layout=QHBoxLayout()
        self.btn_toggle_edit=QPushButton("✏️ Éditer"); self.btn_toggle_edit.clicked.connect(self.toggle_edit)
        self.btn_hex_mode=QPushButton("🔢 Hex"); self.btn_hex_mode.clicked.connect(self.toggle_hex_mode)
        self.btn_save=QPushButton("💾 Sauver"); self.btn_save.clicked.connect(self.save_to_file_dialog)
        self.btn_reload=QPushButton("↺ Recharger"); self.btn_reload.clicked.connect(lambda: self.load_file(self.current_file) if self.current_file else None)
        tool_layout.addWidget(self.btn_toggle_edit); tool_layout.addWidget(self.btn_hex_mode)
        tool_layout.addWidget(self.btn_save); tool_layout.addWidget(self.btn_reload)
        self.layout.addLayout(tool_layout)
        self.current_file=None; self.mode='text'; self.original_data=None; self.is_compressed=False; self.hex_data=None

    def load_file(self, filepath):
        try:
            with open(filepath,'rb') as f: raw=f.read()
            if raw[:4]==b'\x28\xB5\x2F\xFD':
                try: raw=zstd.ZstdDecompressor().decompress(raw); self.is_compressed=True
                except: pass
            self.current_file=filepath; self.original_data=raw; self.hex_data=raw
            if raw[:8]==b'MsgStdBn':
                try:
                    parser=MsbtParser(raw); self.editor.setPlainText(parser.to_text())
                    self.mode='msbt'; self.info_label.setText(f"📝 MSBT – {os.path.basename(filepath)}")
                    return
                except: pass
            if is_probably_text(raw):
                try:
                    text=raw.decode('utf-8'); self.editor.setPlainText(text)
                    self.mode='text'; self.info_label.setText(f"📄 Texte – {os.path.basename(filepath)}")
                    return
                except: pass
                try:
                    text=raw.decode('utf-16'); self.editor.setPlainText(text)
                    self.mode='text'; self.info_label.setText(f"📄 Texte UTF-16 – {os.path.basename(filepath)}")
                    return
                except: pass
            # hex
            self._display_hex(raw); self.mode='hex'; self.info_label.setText(f"🔢 Binaire – {os.path.basename(filepath)}")
        except Exception as e: self.editor.setPlainText(f"Erreur : {e}")

    def _display_hex(self,data):
        lines=[]; lines.append(f"{'Offset':>10}  {'Hex (16 octets)':48}  {'ASCII':16}"); lines.append("-"*80)
        for i in range(0,len(data),16):
            chunk=data[i:i+16]; hex_part=' '.join(f'{b:02X}' for b in chunk)
            ascii_part=''.join(chr(b) if 32<=b<127 else '.' for b in chunk)
            lines.append(f"0x{i:08X}  {hex_part:<48}  {ascii_part}")
        self.editor.setPlainText('\n'.join(lines))

    def _hex_to_bytes(self,hex_text):
        data=bytearray()
        for line in hex_text.splitlines():
            line=line.strip()
            if not line or line.startswith("Offset") or line.startswith("-"): continue
            parts=line.split()
            if len(parts)<2: continue
            hex_part=parts[1:17]
            try:
                for h in hex_part:
                    if len(h)==2: data.append(int(h,16))
                    else: break
            except: pass
        return bytes(data)

    def toggle_hex_mode(self):
        if self.mode in ('hex','hex_edit'):
            if self.mode=='hex_edit': self.hex_data=self._hex_to_bytes(self.editor.toPlainText())
            self.load_file(self.current_file); self.btn_hex_mode.setText("🔢 Hex")
        else:
            self._display_hex(self.original_data); self.mode='hex_edit'
            self.editor.setReadOnly(False); self.btn_hex_mode.setText("📝 Normal")
            self.info_label.setText(f"🔢 Édition Hex – {os.path.basename(self.current_file)}")

    def toggle_edit(self):
        if self.mode in ('text','msbt'):
            self.editor.setReadOnly(not self.editor.isReadOnly())
            self.btn_toggle_edit.setText("🔒 Verrouiller" if not self.editor.isReadOnly() else "✏️ Éditer")

    def get_edited_data(self):
        if self.mode=='hex_edit': return self._hex_to_bytes(self.editor.toPlainText())
        text=self.editor.toPlainText()
        if self.mode=='msbt':
            parser=MsbtParser(self.original_data); parser.update_from_text(text)
            new_data=parser.save()
            if self.is_compressed: new_data=compress_zs(new_data)
            return new_data
        elif self.mode=='text': return text.encode('utf-8')
        else: return self.original_data

    def save_to_file_dialog(self):
        if not self.current_file: return
        path=QFileDialog.getSaveFileName(self,"Enregistrer sous",self.current_file)[0]
        if path:
            try:
                with open(path,'wb') as f: f.write(self.get_edited_data())
                QMessageBox.information(self,"Succès","Fichier sauvegardé")
            except Exception as e: QMessageBox.critical(self,"Erreur",str(e))

    def show_search(self):
        self.search_input.setVisible(True); self.btn_search_next.setVisible(True)
        self.btn_search_prev.setVisible(True); self.btn_search_close.setVisible(True)
        self.search_input.setFocus()

    def hide_search(self):
        self.search_input.setVisible(False); self.btn_search_next.setVisible(False)
        self.btn_search_prev.setVisible(False); self.btn_search_close.setVisible(False)

    def search_next(self):
        text=self.search_input.text()
        if not text: return
        cursor=self.editor.textCursor()
        if cursor.hasSelection(): cursor.setPosition(cursor.selectionEnd())
        found=self.editor.document().find(text,cursor)
        if not found:
            cursor.setPosition(0); found=self.editor.document().find(text,cursor)
        if found: self.editor.setTextCursor(found)

    def search_prev(self):
        text=self.search_input.text()
        if not text: return
        cursor=self.editor.textCursor()
        if cursor.hasSelection(): cursor.setPosition(cursor.selectionStart())
        found=self.editor.document().find(text,cursor,Qt.TextDocument.FindBackward)
        if not found:
            cursor.movePosition(QTextCursor.End); found=self.editor.document().find(text,cursor,Qt.TextDocument.FindBackward)
        if found: self.editor.setTextCursor(found)

# --------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TOTK MOD EDITOR")
        self.setGeometry(100,100,1400,800)
        self._setup_ui()
        self._apply_theme()
        self.statusBar().showMessage("Prêt")

    def _setup_ui(self):
        toolbar=self.addToolBar("Outils")
        toolbar.addWidget(QLabel("Jeu: "))
        self.game_combo=QComboBox()
        self.game_combo.addItems(GAMES.keys())
        self.game_combo.currentTextChanged.connect(self._change_game)
        toolbar.addWidget(self.game_combo)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Racine: "))
        self.root_edit=QLineEdit(); self.root_edit.setMaximumWidth(300); toolbar.addWidget(self.root_edit)
        btn_browse=QPushButton("Parcourir"); btn_browse.clicked.connect(self.browse_root); toolbar.addWidget(btn_browse)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Dictionnaire Zstd:"))
        self.dict_path_edit=QLineEdit(); self.dict_path_edit.setMaximumWidth(250); self.dict_path_edit.setReadOnly(True)
        toolbar.addWidget(self.dict_path_edit)
        btn_load_dict=QPushButton("Charger .dict"); btn_load_dict.clicked.connect(self.load_dictionary); toolbar.addWidget(btn_load_dict)
        toolbar.addSeparator()
        self.btn_export_yaml=QPushButton("📤 Export YAML"); self.btn_export_yaml.clicked.connect(lambda: self.batch_export(True))
        toolbar.addWidget(self.btn_export_yaml)
        self.btn_import_yaml=QPushButton("📥 Import YAML"); self.btn_import_yaml.clicked.connect(lambda: self.batch_export(False))
        toolbar.addWidget(self.btn_import_yaml)
        # Splitter
        splitter=QSplitter(Qt.Horizontal)
        self.file_tree=FileTreeWidget(); self.file_tree.open_file_signal.connect(self.open_file_in_editor)
        splitter.addWidget(self.file_tree)
        self.tab_widget=QTabWidget(); self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_tab)
        splitter.addWidget(self.tab_widget)
        splitter.setSizes([400,1000])
        central=QWidget(); layout=QVBoxLayout(central); layout.addWidget(splitter)
        self.setCentralWidget(central)

    def _apply_theme(self):
        dark=QPalette()
        dark.setColor(QPalette.Window, QColor(30,30,30)); dark.setColor(QPalette.WindowText, Qt.white)
        dark.setColor(QPalette.Base, QColor(37,37,37)); dark.setColor(QPalette.AlternateBase, QColor(40,40,40))
        dark.setColor(QPalette.Text, Qt.white); dark.setColor(QPalette.Button, QColor(50,50,50))
        dark.setColor(QPalette.ButtonText, Qt.white); dark.setColor(QPalette.Highlight, QColor(9,71,113))
        dark.setColor(QPalette.HighlightedText, Qt.white)
        self.setPalette(dark)
        self.setStyleSheet("""
            QTreeWidget { background:#252526; color:#d4d4d4; }
            QPlainTextEdit, QTextEdit { background:#1e1e1e; color:#d4d4d4; }
            QLineEdit { background:#3c3c3c; color:#d4d4d4; border:1px solid #555; }
            QPushButton { background:#3a3a3a; color:#d4d4d4; border:1px solid #555; padding:4px; }
            QPushButton:hover { background:#505050; }
            QTabWidget::pane { border:1px solid #444; }
            QTabBar::tab { background:#2d2d2d; color:#ccc; padding:5px 15px; }
            QTabBar::tab:selected { background:#1e1e1e; }
        """)

    def _change_game(self, name):
        global current_game_config
        if name in GAMES: current_game_config=GAMES[name]; self.statusBar().showMessage(f"Jeu : {name}")

    def browse_root(self):
        folder=QFileDialog.getExistingDirectory(self,"Dossier ROMFS")
        if folder: self.root_edit.setText(folder); self.file_tree.set_root(folder)

    def load_dictionary(self):
        path,_=QFileDialog.getOpenFileName(self,"Fichier dictionnaire Zstd","","*.dict")
        if path: set_zstd_dictionary(path); self.dict_path_edit.setText(path)

    def open_file_in_editor(self, filepath):
        for i in range(self.tab_widget.count()):
            tab=self.tab_widget.widget(i)
            if hasattr(tab,'current_file') and tab.current_file==filepath:
                self.tab_widget.setCurrentIndex(i); return
        tab=EditorTab(); tab.load_file(filepath)
        self.tab_widget.addTab(tab, os.path.basename(filepath)); self.tab_widget.setCurrentWidget(tab)

    def close_tab(self,index):
        widget=self.tab_widget.widget(index); self.tab_widget.removeTab(index); widget.deleteLater()

    def batch_export(self, export=True):
        root=self.root_edit.text()
        if not root: QMessageBox.warning(self,"Erreur","Sélectionnez un dossier racine"); return
        dest=QFileDialog.getExistingDirectory(self,"Dossier cible")
        if not dest: return
        progress=QProgressDialog("Traitement des fichiers MSBT...","Annuler",0,0,self)
        progress.setWindowModality(Qt.WindowModal); progress.show()
        count=0
        for dirpath,_,files in os.walk(root):
            if progress.wasCanceled(): break
            for f in files:
                if progress.wasCanceled(): break
                if f.endswith('.msbt'):
                    full=os.path.join(dirpath,f)
                    try:
                        with open(full,'rb') as fh: data=fh.read()
                        parser=MsbtParser(data)
                        if export:
                            yaml_str=parser.to_yaml()
                            out_name=os.path.splitext(f)[0]+'.yaml'
                            with open(os.path.join(dest,out_name),'w',encoding='utf-8') as out: out.write(yaml_str)
                        else:
                            yaml_name=os.path.splitext(f)[0]+'.yaml'
                            yaml_path=os.path.join(dest,yaml_name)
                            if os.path.exists(yaml_path):
                                with open(yaml_path,'r',encoding='utf-8') as yf: parser.from_yaml(yf.read())
                                with open(full,'wb') as fh: fh.write(parser.save())
                        count+=1
                    except Exception as e: print(f"Erreur {full}: {e}")
        progress.close()
        QMessageBox.information(self,"Terminé",f"{count} fichiers traités")

if __name__=="__main__":
    app=QApplication(sys.argv); app.setStyle("Fusion")
    win=MainWindow(); win.show()
    sys.exit(app.exec_())