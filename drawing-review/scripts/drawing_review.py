#!/usr/bin/env python3
"""图纸审查 skill 的本地工具（仅 PDF）：抽取文字 → 剔除图签并脱敏 → 核验引文 → 生成报告。

全部在本机运行，不联网。模型只读取 extract 生成的脱敏文件（review_input.md / blocks.jsonl），
不直接打开原始 PDF；mapping.local.json 保存占位符与原文的对应，只留在本机。

用法：
  python drawing_review.py extract 图纸.pdf [更多.pdf ...] --out review_pack [--extra-terms 敏感词.txt]
  python drawing_review.py near    --pack review_pack --block F1-P3-B12 [--radius 80]
  python drawing_review.py near    --pack review_pack --page F1-P3 --region x0,y0,x1,y1
  python drawing_review.py calc    "1000/150*pi*25**2/4" ["As(25,6)" ...]
  python drawing_review.py render  图纸.pdf [...] --pack review_pack [--pages F1-P3,F1-P5]   # 可选图像模式
  python drawing_review.py zoom    图纸.pdf [...] --pack review_pack --page F1-P3 --region x0,y0,x1,y1
  python drawing_review.py verify  --pack review_pack --findings findings.json
  python drawing_review.py evidence 图纸.pdf [...] --pack review_pack                          # 可选：问题截图
  python drawing_review.py report  --pack review_pack [--title 审查报告]

依赖：优先 pdfplumber（MIT，支持竖排文字；自带 pypdfium2 与 Pillow，图像模式用它们渲染），
没有时用 pypdf（BSD，坐标为近似值，不支持图像模式）。
只读取 PDF 文字层；扫描件、纯图形页、字体无法解码的页标为“未审查”，不做 OCR。
图像模式只渲染已在本机遮盖图签与敏感文字的页面图，供有看图能力的模型辅助判断标注归属。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

TOOL_VERSION = "1.1.0"
BLOCK_ID = re.compile(r"F\d+-P\d+-B\d+")
PAGE_ID = re.compile(r"F\d+-P\d+")
SCRIPT_DIR = Path(__file__).resolve().parent

# ───────────────────────── 通用 ─────────────────────────

def norm_text(text: str) -> str:
    """比较用：NFKC + 去空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Block:
    id: str                       # 全局唯一：F1-P3-B12（文件-页-块），多文件同页号不冲突
    file: str                     # 脱敏后的文件代号，如 图纸1.pdf
    sheet: str                    # 页标识：P3
    text: str
    bbox: list[float] | None      # 页面点、左上角原点 [x0,y0,x1,y1]
    source: str                   # pdf-text
    height: float | None = None
    flags: list[str] = field(default_factory=list)
    in_title_block: bool = False
    file_id: str = ""             # F1


def page_id(file_id: str, sheet: str) -> str:
    return f"{file_id}-{sheet}"


def block_page(b: dict) -> str:
    return page_id(b.get("file_id") or b["id"].split("-")[0], b["sheet"])


# ───────────────────────── 抽取 ─────────────────────────

def group_lines(words: list[dict], gap_ratio: float = 3.0) -> list[dict]:
    """把词按行合并；同一行里间距超过 gap_ratio 倍字高的拆成独立块。
    图纸上同一水平线常有互不相干的标注（梁号与另一处强度等级），合成一行会造成错误关联。"""
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[list[dict]] = []
    for w in words:
        h = w["bottom"] - w["top"]
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= 0.5 * max(h, rows[-1][0]["bottom"] - rows[-1][0]["top"]):
            rows[-1].append(w)
        else:
            rows.append([w])
    out = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        h = statistics.median([w["bottom"] - w["top"] for w in row]) or 1
        seg = [row[0]]
        for w in row[1:] + [None]:
            if w is not None and w["x0"] - seg[-1]["x1"] <= gap_ratio * h:
                seg.append(w)
                continue
            text = seg[0]["text"]
            for a, b in zip(seg, seg[1:]):
                text += (" " if b["x0"] - a["x1"] > 0.15 * h else "") + b["text"]
            out.append({"text": text, "x0": min(x["x0"] for x in seg), "x1": max(x["x1"] for x in seg),
                        "top": min(x["top"] for x in seg), "bottom": max(x["bottom"] for x in seg)})
            if w is not None:
                seg = [w]
    return out


def extract_pdf(path: Path, file_label: str, file_id: str = "F1") -> tuple[list[Block], list[dict]]:
    """返回文字块与逐页信息。优先 pdfplumber 的文字行；否则 pypdf 文字片段按行合并。"""
    pages_info: list[dict] = []
    blocks: list[Block] = []
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        pdfplumber = None

    if pdfplumber is not None:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                # 横排文字按行；竖排（旋转 90°）文字按旋转方向单独成词，否则会读成倒序碎片
                upright = page.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))
                lines = group_lines(upright.extract_words())
                rotated = []
                for ccw, (ld, cd) in ((True, ("ltr", "btt")), (False, ("rtl", "ttb"))):
                    sub = page.filter(lambda o, ccw=ccw: o.get("object_type") != "char" or (
                        not o.get("upright", True) and (o.get("matrix", (0, 1))[1] > 0) == ccw))
                    try:
                        rotated += sub.extract_words(line_dir_rotated=ld, char_dir_rotated=cd)
                    except (TypeError, ValueError):   # 旧版 pdfplumber 不支持旋转方向参数
                        rotated += sub.extract_words()
                page_blocks = []
                for j, (ln, flags) in enumerate([(ln, []) for ln in lines] + [(w, ["rotated"]) for w in rotated], start=1):
                    text = (ln.get("text") or "").strip()
                    if not text:
                        continue
                    bbox = [round(ln["x0"], 1), round(ln["top"], 1), round(ln["x1"], 1), round(ln["bottom"], 1)]
                    height = min(ln["bottom"] - ln["top"], ln["x1"] - ln["x0"]) if flags else ln["bottom"] - ln["top"]
                    page_blocks.append(Block(f"{file_id}-P{i}-B{j}", file_label, f"P{i}", text, bbox, "pdf-text",
                                             height=round(height, 2), flags=flags, file_id=file_id))
                blocks += page_blocks
                # 嵌入的位图（印章、签名多为小图片）：只记位置，图像模式下小图按疑似印章遮盖
                area = float(page.width) * float(page.height) or 1.0
                images = []
                for im in page.images:
                    w_, h_ = im["x1"] - im["x0"], im["bottom"] - im["top"]
                    images.append({"bbox": [round(im["x0"], 1), round(im["top"], 1), round(im["x1"], 1), round(im["bottom"], 1)],
                                   "area_ratio": round(w_ * h_ / area, 4)})
                pages_info.append({"sheet": f"P{i}", "width": float(page.width), "height": float(page.height),
                                   "rotation": int(getattr(page, "rotation", 0) or 0), "images": images,
                                   "chars": sum(len(b.text) for b in page_blocks), "extractor": f"pdfplumber {pdfplumber.__version__}"})
        return blocks, pages_info

    try:
        import pypdf  # type: ignore
    except ImportError:
        raise SystemExit("缺少 PDF 解析库：请先 pip install pdfplumber（或 pypdf）")

    reader = pypdf.PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        W, H = float(box.width), float(box.height)
        x_off, y_off = float(box.left), float(box.bottom)
        frags: list[tuple[float, float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            if not text or not text.strip():
                return
            # 文字矩阵 × 当前变换矩阵 → 页面坐标
            a, b, c, d, e, f = cm
            x = tm[4] * a + tm[5] * c + e
            y = tm[4] * b + tm[5] * d + f
            size = abs(font_size * (tm[3] if tm[3] else 1) * (d if d else 1)) or 8.0
            for k, part in enumerate(text.split("\n")):
                if part.strip():
                    frags.append((x - x_off, H - (y - y_off) + k * size * 1.2, size, part))

        try:
            page.extract_text(visitor_text=visitor)
        except Exception as exc:  # 个别页解析失败不影响其他页
            pages_info.append({"sheet": f"P{i}", "width": W, "height": H, "chars": 0, "error": str(exc)[:200], "extractor": f"pypdf {pypdf.__version__}"})
            continue
        # 按基线合并成行
        frags.sort(key=lambda t: (round(t[1] / max(t[2] * 0.6, 1)), t[0]))
        lines: list[list] = []
        for x, y, size, text in frags:
            if lines and abs(lines[-1][1] - y) <= max(size, lines[-1][2]) * 0.5 and x - lines[-1][4] <= size * 3:
                lines[-1][3] += text
                lines[-1][4] = max(lines[-1][4], x + len(text) * size * 0.9)
            else:
                lines.append([x, y, size, text, x + len(text) * size * 0.9])
        page_blocks = []
        for j, (x, y, size, text, x1) in enumerate(lines, start=1):
            text = text.strip()
            if text:
                page_blocks.append(Block(f"{file_id}-P{i}-B{j}", file_label, f"P{i}", text,
                                         [round(x, 1), round(y - size, 1), round(x1, 1), round(y, 1)], "pdf-text",
                                         height=round(size, 2), flags=["approx-bbox"], file_id=file_id))
        blocks += page_blocks
        pages_info.append({"sheet": f"P{i}", "width": W, "height": H, "chars": sum(len(b.text) for b in page_blocks),
                           "rotation": int(page.get("/Rotate", 0) or 0), "extractor": f"pypdf {pypdf.__version__}"})
    return blocks, pages_info


# ───────────────────────── 图签识别与脱敏 ─────────────────────────

ROLE_LABEL = re.compile(
    r"^(设计|设计人|审核|审核人|校对|校核|审定|审批|批准|制图|绘图|描图|复核|专业负责人?|项目负责人?|工程负责人|设计总负责人|"
    r"总工程师|院长|所长|室主任|主任工程师|日期|出图日期|比例|图号|图名|版次|版本|修改|修改记录|工程名称|项目名称|子项名称|子项|子项工程|"
    r"阶段|设计阶段|建设单位|设计单位|业主|委托单位|合作单位|会签|会签栏|出图章|注册章|签名|签字|专业|工程编号|项目编号|合同号|图别|"
    r"注册师|注册建筑师|注册结构工程师|资质证书|证书编号|设计号|档案号|张数|第张|共张)[:：]?$"
)
ROLE_WORDS = ROLE_LABEL.pattern.removeprefix("^(").removesuffix(")[:：]?$")
ROLE_PREFIX = re.compile(r"^\s*(" + ROLE_WORDS + r")(?:\s*[:：]\s*|\s+)(\S.{0,60})$")
# 只出现在图签里的签署/图纸身份字样；正文“工程概况”也会写建设单位、设计单位，不能单凭它们认定图签
DISTINCTIVE = re.compile(r"^(设计人?|审核人?|校对|校核|审定|审批|批准|制图|绘图|复核|专业负责人?|项目负责人?|工程负责人|设计总负责人|图号|图名|比例|版次)$")
# 图签里出现这类长句，多半是把正文误划进了图签
BODY_LIKE = re.compile(r"(混凝土|钢筋|施工|应|须|不得|使用年限|抗渗|保护层|标高|做法|荷载|防水)")


def role_of(text: str) -> tuple[str, str | None] | None:
    """整块是角色标签 → (标签, None)；“标签 值”同一行 → (标签, 值)；否则 None。"""
    n = norm_text(text)
    m = ROLE_LABEL.match(n)
    if m:
        return m.group(1), None
    m = ROLE_PREFIX.match(text or "")
    if m:
        return m.group(1), m.group(2).strip()
    return None


SHEET_NO = re.compile(r"^[A-Za-z一-龥]{0,6}[-－_—]?[A-Za-z]{0,4}[-－_—]?\d{1,4}([-－_—]\d{1,4}){0,3}[A-Za-z]?$")
COMPANY = re.compile(
    r"[^\s，。,；;：:、（）()\[\]为由与和及的是在]{2,24}"
    r"(股份有限公司|有限责任公司|有限公司|集团公司|集团|设计研究总院|设计研究院|勘察设计院|设计总院|设计院|研究总院|研究院|勘察院|事务所|工程局|分公司|管理局|指挥部)"
)
PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9}|0\d{2,3}-?\d{7,8})(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
IDNO = re.compile(r"(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])")
CERT = re.compile(r"((?:资质|证书|注册|执业|印章|备案|统一社会信用代码)[^\s：:]{0,6}[:：]?\s*)([A-Za-z0-9\-－]{6,})")
KEYVAL = re.compile(
    r"((?:建设|设计|施工|监理|勘察|审图|咨询|委托|业主|代建|合作)单位|设计人|审核人|校对人|项目负责人|专业负责人|"
    r"工程负责人|联系人|电话|地址|签字|签名)(\s*[:：]\s*)([^\s，。；;]{2,40})"
)
STAMP_HINT = re.compile(r"(出图专用章|注册[^\s]{0,8}章|执业印章|签章|盖章处)")
CJK_NAME = re.compile(r"^[一-龥]{2,4}(·[一-龥]{1,6})?$")


def cluster_labels(labels: list[Block], gap: float) -> list[list[Block]]:
    """单链接聚类：中心距离不超过 gap 的标签归为同一个图签。"""
    def center(b):
        return ((b.bbox[0] + b.bbox[2]) / 2, (b.bbox[1] + b.bbox[3]) / 2)
    groups: list[list[Block]] = []
    for lab in labels:
        cx, cy = center(lab)
        hit = [g for g in groups if any(abs(center(o)[0] - cx) <= gap and abs(center(o)[1] - cy) <= gap for o in g)]
        merged = [lab]
        for g in hit:
            merged += g
            groups.remove(g)
        groups.append(merged)
    return groups


def mark_title_blocks(blocks: list[Block]) -> dict[str, dict]:
    """在每页里找图签区域，标记 in_title_block；同时取出图号、图名作为页面标签（只用于定位）。"""
    by_sheet: dict[str, list[Block]] = {}
    for b in blocks:
        by_sheet.setdefault(b.sheet, []).append(b)
    sheet_meta: dict[str, dict] = {}
    for sheet, items in by_sheet.items():
        labels = [b for b in items if b.bbox and role_of(b.text)]
        meta = {"title_block_regions": 0, "title_block_bboxes": [], "title_block_body_like": 0, "sheet_no": None, "sheet_title": None}
        if len(labels) >= 3:
            h = statistics.median([b.height or 3 for b in labels]) or 3
            regions = []
            for g in cluster_labels(labels, gap=h * 18):
                if len(g) < 3 or sum(1 for b in g if DISTINCTIVE.match(role_of(b.text)[0])) < 2:
                    continue
                m = h * 9
                x0 = min(b.bbox[0] for b in g) - m
                y0 = min(b.bbox[1] for b in g) - m
                x1 = max(b.bbox[2] for b in g) + m * 2.5   # 填写值多在标签右侧
                y1 = max(b.bbox[3] for b in g) + m
                regions.append((x0, y0, x1, y1, g))
            # 扩展后互相重叠的区域属于同一个图签
            merged = True
            while merged:
                merged = False
                for i in range(len(regions)):
                    for j in range(i + 1, len(regions)):
                        a, b2 = regions[i], regions[j]
                        if a[0] <= b2[2] and b2[0] <= a[2] and a[1] <= b2[3] and b2[1] <= a[3]:
                            regions[i] = (min(a[0], b2[0]), min(a[1], b2[1]), max(a[2], b2[2]), max(a[3], b2[3]), a[4] + b2[4])
                            del regions[j]
                            merged = True
                            break
                    if merged:
                        break
            regions = [r for r in regions if sum(1 for b in r[4] if DISTINCTIVE.match(role_of(b.text)[0])) >= 2]
            meta["title_block_regions"] = len(regions)
            meta["title_block_bboxes"] = [[round(max(r[0], 0), 1), round(max(r[1], 0), 1), round(r[2], 1), round(r[3], 1)] for r in regions]
            for b in items:
                if not b.bbox:
                    continue
                cx, cy = (b.bbox[0] + b.bbox[2]) / 2, (b.bbox[1] + b.bbox[3]) / 2
                if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1, _ in regions):
                    b.in_title_block = True
                    if not role_of(b.text) and len(norm_text(b.text)) >= 20 and BODY_LIKE.search(b.text):
                        meta["title_block_body_like"] += 1
            # 图号 / 图名：取标签附近最近的取值块
            key_cn = {"sheet_no": "图号", "sheet_title": "图名"}
            for key, label_re in (("sheet_no", r"^图号[:：]?$"), ("sheet_title", r"^图名[:：]?$")):
                inline = next((b for b in labels if b.in_title_block and role_of(b.text)[0] == key_cn[key] and role_of(b.text)[1]), None)
                if inline:
                    meta[key] = role_of(inline.text)[1]
                    inline.flags.append(f"sheet-label:{key}")
                    continue
                lab = next((b for b in labels if re.match(label_re, norm_text(b.text))), None)
                if not lab:
                    continue
                lx, ly = (lab.bbox[0] + lab.bbox[2]) / 2, (lab.bbox[1] + lab.bbox[3]) / 2
                cands = [b for b in items if b.in_title_block and b is not lab and not role_of(b.text)]
                if key == "sheet_no":
                    cands = [b for b in cands if SHEET_NO.match(norm_text(b.text))]
                else:
                    cands = [b for b in cands if 3 <= len(norm_text(b.text)) <= 40 and re.search(r"[一-龥]", b.text)
                             and re.search(r"(图|说明|表|详图|平面|立面|剖面|大样|布置|配筋|目录)", b.text)]
                if cands:
                    best = min(cands, key=lambda b: abs((b.bbox[0] + b.bbox[2]) / 2 - lx) + abs((b.bbox[1] + b.bbox[3]) / 2 - ly))
                    meta[key] = best.text.strip()
                    best.flags.append(f"sheet-label:{key}")
        sheet_meta[sheet] = meta
    return sheet_meta


def garbled_sheets(blocks: list[Block]) -> set[str]:
    """字体编码解不出来时常读成同一个字反复出现（如“本本本本”）。按页统计汉字中最高频字占比。"""
    from collections import Counter
    per: dict[str, Counter] = {}
    for b in blocks:
        per.setdefault(b.sheet, Counter()).update(ch for ch in b.text if "一" <= ch <= "龥")
    bad = set()
    for sheet, cnt in per.items():
        total = sum(cnt.values())
        if total >= 30 and cnt.most_common(1)[0][1] / total > 0.4:
            bad.add(sheet)
    return bad


class Masker:
    def __init__(self, extra_terms: list[str]):
        self.map: dict[str, str] = {}          # 原文 → 占位符
        self.counts: dict[str, int] = {}
        self.terms: list[tuple[str, str]] = []  # (原文, 类别) 全局替换
        self.whole_block_names: set[str] = set()
        for t in extra_terms:
            t = t.strip()
            if len(t) >= 2:
                self.terms.append((t, "敏感词"))

    def placeholder(self, original: str, kind: str) -> str:
        original = original.strip()
        if original in self.map:
            return self.map[original]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        ph = f"［{kind}{self.counts[kind]}］"
        self.map[original] = ph
        return ph

    def learn_from_title_block(self, blocks: list[Block]):
        for b in blocks:
            if not b.in_title_block:
                continue
            if any(f.startswith("sheet-label") for f in b.flags):
                continue
            role = role_of(b.text)
            if role and not role[1]:
                continue
            t = role[1] if role else b.text.strip()
            n = norm_text(t)
            if re.fullmatch(r"[\d\s:：/.\-年月日比例Ss=＝]+", t) or re.fullmatch(r"1[:：]\d+", n):
                continue   # 日期、比例
            if COMPANY.search(t):
                for m in COMPANY.finditer(t):
                    self.terms.append((m.group(0), "单位"))
            elif CJK_NAME.match(n):
                if len(n) >= 3:
                    self.terms.append((n, "人员"))
                self.whole_block_names.add(n)
            elif len(n) >= 6 and re.search(r"[一-龥]", n):
                self.terms.append((t, "工程名称"))

    def mask(self, text: str, whole_block: bool = True) -> str:
        n = norm_text(text)
        if whole_block and n in self.whole_block_names:
            return self.placeholder(n, "人员")
        out = text
        for term, kind in sorted(set(self.terms), key=lambda x: -len(x[0])):
            if term and term in out:
                out = out.replace(term, self.placeholder(term, kind))
        out = KEYVAL.sub(lambda m: m.group(1) + m.group(2) + (m.group(3) if m.group(3).startswith("［") else self.placeholder(m.group(3), "信息")), out)
        out = COMPANY.sub(lambda m: self.placeholder(m.group(0), "单位"), out)
        out = CERT.sub(lambda m: m.group(1) + self.placeholder(m.group(2), "编号"), out)
        out = PHONE.sub(lambda m: self.placeholder(m.group(0), "电话"), out)
        out = EMAIL.sub(lambda m: self.placeholder(m.group(0), "邮箱"), out)
        out = IDNO.sub(lambda m: self.placeholder(m.group(0), "证件号"), out)
        return out


def guess_doc_kind(blocks: list[Block]) -> str:
    """只看前 3 页文字粗分文件类型，用于提示审查口径；是推测，不作为判定依据。"""
    text = "".join(b.text for b in blocks if int(b.sheet[1:]) <= 3)
    if re.search(r"(岩土工程勘察|勘察报告|工程地质勘察)", text):
        return "勘察报告"
    if re.search(r"(计算书|计算说明书|结构计算)", text):
        return "计算书"
    return "图纸"


# ───────────────────────── extract 命令 ─────────────────────────

def cmd_extract(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    extra = []
    if args.extra_terms:
        extra = [ln for ln in Path(args.extra_terms).read_text(encoding="utf-8").splitlines() if ln.strip()]
    masker = Masker(extra)

    all_blocks: list[Block] = []
    files_cov = []
    redactions: dict[str, list[dict]] = {}   # 页 → 图像模式需要遮盖的框（只有坐标，不含原文）

    def redact(pid: str, bbox, why: str):
        if bbox:
            redactions.setdefault(pid, []).append({"bbox": [round(v, 1) for v in bbox], "why": why})

    for idx, raw in enumerate(args.files, start=1):
        path = Path(raw)
        ext = path.suffix.lower()
        fid = f"F{idx}"
        label = f"图纸{idx}{ext}"
        cov = {"file_id": fid, "file": label, "format": ext.lstrip("."), "sha256": sha256_file(path), "status": "ok", "sheets": []}
        masker.map[path.name] = label   # 文件名常含工程名称，报告里只用代号
        try:
            if ext != ".pdf":
                cov.update(status="not_checked", reason=f"不支持的格式 {ext}；本工具只读取 PDF（CAD 图纸请先导出矢量 PDF）")
                files_cov.append(cov)
                continue
            blocks, info = extract_pdf(path, label, fid)
        except SystemExit as exc:
            cov.update(status="failed", reason=str(exc))
            files_cov.append(cov)
            continue
        meta = mark_title_blocks(blocks)
        garbled = garbled_sheets(blocks)
        cov["doc_kind_guess"] = guess_doc_kind([b for b in blocks if b.sheet not in garbled])
        for p in info:
            pid = page_id(fid, p["sheet"])
            p["page"] = pid
            m = meta.get(p["sheet"], {})
            p.update(m)
            for r in m.get("title_block_bboxes", []):
                redact(pid, r, "title_block")
            imgs = p.pop("images", [])
            p["embedded_images"] = len(imgs)
            for im in imgs:
                if im["area_ratio"] < 0.04:
                    redact(pid, im["bbox"], "possible_seal_image")
            warns = []
            area = (p.get("width") or 1) * (p.get("height") or 1)
            tb_area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in m.get("title_block_bboxes", []))
            if tb_area / area > 0.35:
                warns.append(f"图签区域占页面 {tb_area / area:.0%}，可能把正文误判为图签")
            if m.get("title_block_body_like", 0) >= 3:
                warns.append(f"图签区域内有 {m['title_block_body_like']} 段像正文的长句，可能误剔除了正文")
            if warns:
                p["warnings"] = warns
            if p["sheet"] in garbled:
                p["status"] = "not_checked"
                p["reason_code"] = "garbled"
                p["reason"] = "文字编码无法正确解码（读出大量重复字），未审查；请 pip install pdfplumber 后重跑，或从 CAD 重新导出 PDF"
                blocks = [b for b in blocks if b.sheet != p["sheet"]]
            elif p.get("chars", 0) < 20:
                p["status"] = "not_checked"
                p["reason_code"] = "no_text_layer"
                p["reason"] = "无文字层或文字过少（可能是扫描件/纯图形），未做 OCR，本页未审查"
            else:
                p["status"] = "ok"
        cov["sheets"] = info
        files_cov.append(cov)
        all_blocks += blocks

    masker.learn_from_title_block(all_blocks)
    kept: list[Block] = []
    excluded = 0
    residue: list[str] = []
    short_names = {n for n in masker.whole_block_names if len(n) == 2}
    for b in all_blocks:
        pid = page_id(b.file_id, b.sheet)
        if b.in_title_block:
            excluded += 1
            redact(pid, b.bbox, "title_block_text")
            continue   # 图签整块不进入审查材料
        masked = masker.mask(b.text)
        if STAMP_HINT.search(masked) and len(norm_text(masked)) <= 30:
            excluded += 1
            redact(pid, b.bbox, "stamp_text")
            continue   # 出图章、注册章文字
        if masked != b.text:
            b.flags.append("masked")
            redact(pid, b.bbox, "masked_text")
        # 两字人名只在图签里整块替换（避免误伤正文词语）；正文里若出现，标记并在图像中遮盖
        if short_names and any(n in masked for n in short_names):
            residue.append(b.id)
            b.flags.append("possible_name")
            redact(pid, b.bbox, "possible_name")
        b.text = masked
        kept.append(b)

    # 剔除图签后再看正文文字量。竖排文字和单字块（会签栏常把“建”“筑”逐字竖排）不计入。
    # 几乎没有正文的页多为扫描件；只有少量图名、标注的页多为图形为主的配筋图/断面图，文字照常审查但标“部分审查”
    kept_chars: dict[str, int] = {}
    for b in kept:
        n = len(norm_text(b.text))
        if "rotated" in b.flags or n <= 1:
            continue
        pid = page_id(b.file_id, b.sheet)
        kept_chars[pid] = kept_chars.get(pid, 0) + n
    for cov in files_cov:
        for p in cov.get("sheets", []):
            n = kept_chars.get(p["page"], 0)
            p["chars_after_title_block"] = n
            if p.get("status") != "ok":
                continue
            if n < 15:
                p["status"] = "not_checked"
                p["reason_code"] = "minimal_text"
                p["reason"] = f"剔除图签、会签栏后正文只有 {n} 字（多为扫描件或纯图形），未做 OCR，本页未审查"
            elif n < 80:
                p["status"] = "partial"
                p["reason_code"] = "sparse_text"
                p["reason"] = f"剔除图签后正文只有 {n} 字，以图形为主（配筋图、断面图等）：只审查了图名、标注等少量文字，图形内容未审查"
            elif n < 400:
                p["low_text"] = True

    # 图号/图名作为页面标签：也做一次脱敏（图名里偶有工程名称）
    sheet_labels = {}
    for cov in files_cov:
        for p in cov.get("sheets", []):
            sheet_labels[p["page"]] = " ".join(masker.mask(x, whole_block=False) for x in (p.get("sheet_no"), p.get("sheet_title")) if x)
            for k in ("sheet_no", "sheet_title"):
                if p.get(k):
                    p[k] = masker.mask(p[k], whole_block=False)

    with (out / "blocks.jsonl").open("w", encoding="utf-8") as f:
        for b in kept:
            d = asdict(b)
            d.pop("in_title_block")
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    lines = ["# 审查材料（已剔除图签、已脱敏）", "",
             "- 每行开头是块编号，如 `[F1-P3-B12]` = 第 1 个文件第 3 页第 12 块。引用原文时逐字复制编号后的文字，并写明块编号。",
             "- 短标注后面的 `@x,y` 是它在页面上的位置（页面点，左上角为原点），用来判断标注与构件的空间关系；"
             "查看某块周围的文字：`python scripts/drawing_review.py near --pack <目录> --block <块编号>`。",
             "- 占位符如［单位1］［人员2］是脱敏数据，不属于审查范围。", ""]
    by_page: dict[str, list[Block]] = {}
    for b in kept:
        by_page.setdefault(page_id(b.file_id, b.sheet), []).append(b)
    for cov in files_cov:
        kind = f"，推测为{cov['doc_kind_guess']}" if cov.get("doc_kind_guess") else ""
        lines.append(f"## {cov['file_id']} {cov['file']}（{cov['format'].upper()}{kind}）")
        if cov["status"] != "ok":
            lines += [f"> 未审查：{cov.get('reason')}", ""]
            continue
        for p in cov["sheets"]:
            label = sheet_labels.get(p["page"], "")
            lines.append(f"### 【{p['page']}】{label}".rstrip())
            if p["status"] == "not_checked":
                lines += [f"> 未审查：{p['reason']}", ""]
                continue
            if p["status"] == "partial":
                lines.append(f"> 部分审查：{p['reason']}")
            for w in p.get("warnings", []):
                lines.append(f"> 提示：{w}")
            for b in by_page.get(p["page"], []):
                pos = f" @{round(b.bbox[0])},{round(b.bbox[1])}" if b.bbox and len(norm_text(b.text)) <= 16 else ""
                lines.append(f"[{b.id}{pos}] {b.text.replace(chr(10), ' / ')}")
            lines.append("")
    (out / "review_input.md").write_text("\n".join(lines), encoding="utf-8")

    coverage = {
        "tool": f"drawing_review.py {TOOL_VERSION}",
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "files": files_cov,
        "blocks_kept": len(kept),
        "blocks_excluded_title_block_or_stamp": excluded,
        "masked": dict(masker.counts),
        "possible_name_residue": residue,
        "scope_note": "图签（单位、人员、工程名称、日期、出图章等）已剔除并脱敏，不属于审查范围；图号/图名仅用于定位。",
    }
    (out / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "redactions.json").write_text(json.dumps(redactions, ensure_ascii=False), encoding="utf-8")
    (out / "mapping.local.json").write_text(json.dumps({v: k for k, v in masker.map.items()}, ensure_ascii=False, indent=2), encoding="utf-8")

    sheets = [p for c in files_cov for p in c.get("sheets", [])]
    summary = {"out": str(out), "files": len(files_cov), "sheets": len(sheets),
               "ok": sum(1 for p in sheets if p.get("status") == "ok"),
               "partial_sparse_text": sum(1 for p in sheets if p.get("status") == "partial"),
               "not_checked": sum(1 for p in sheets if p.get("status") == "not_checked") + sum(1 for c in files_cov if c["status"] != "ok"),
               "blocks_kept": len(kept), "blocks_excluded": excluded, "masked": masker.counts,
               "title_block_warnings": sum(1 for p in sheets if p.get("warnings")),
               "possible_name_residue": len(residue),
               "read_next": str(out / "review_input.md"),
               "warning": "mapping.local.json 含原文对应关系，只留本机，不要读取、上传或粘贴给模型"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


# ───────────────────────── near 命令（无图像时的空间关联） ─────────────────────────

def load_blocks(pack: Path) -> list[dict]:
    with (pack / "blocks.jsonl").open(encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _center(b: dict) -> tuple[float, float]:
    x0, y0, x1, y1 = b["bbox"]
    return (x0 + x1) / 2, (y0 + y1) / 2


def _direction(dx: float, dy: float) -> str:
    if abs(dx) < 1 and abs(dy) < 1:
        return "重合"
    ang = math.degrees(math.atan2(-dy, dx)) % 360   # 页面 y 向下
    names = ["右", "右上", "上", "左上", "左", "左下", "下", "右下"]
    return names[int(((ang + 22.5) % 360) // 45)]


def near_blocks(blocks: list[dict], *, block: str | None = None, page: str | None = None,
                region: list[float] | None = None, radius: float | None = None, limit: int = 40) -> list[dict]:
    if block:
        m = BLOCK_ID.search(block)
        center = next((b for b in blocks if m and b["id"] == m.group(0)), None)
        if not center or not center.get("bbox"):
            raise SystemExit(f"找不到块 {block}")
        pid = block_page(center)
        r = radius or max(40.0, (center.get("height") or 3) * 12)
        cx, cy = _center(center)
        res = []
        for b in blocks:
            if b is center or block_page(b) != pid or not b.get("bbox"):
                continue
            bx, by = _center(b)
            # 用框之间的最近距离，长标注不会因为中心远而漏掉
            gx = max(b["bbox"][0] - center["bbox"][2], center["bbox"][0] - b["bbox"][2], 0)
            gy = max(b["bbox"][1] - center["bbox"][3], center["bbox"][1] - b["bbox"][3], 0)
            d = math.hypot(gx, gy)
            if d <= r:
                # 框在水平方向有重叠 → 正上/正下；竖直方向有重叠 → 正左/正右；否则按中心连线方向
                if gx == 0 and gy > 0:
                    where = "下" if by > cy else "上"
                elif gy == 0 and gx > 0:
                    where = "右" if bx > cx else "左"
                else:
                    where = _direction(bx - cx, by - cy)
                res.append({"id": b["id"], "dir": where, "dist": round(d, 1), "text": b["text"]})
        return sorted(res, key=lambda x: x["dist"])[:limit]
    if page and region:
        x0, y0, x1, y1 = region
        res = [{"id": b["id"], "at": [round(v) for v in b["bbox"][:2]], "text": b["text"]} for b in blocks
               if block_page(b) == page and b.get("bbox") and x0 <= _center(b)[0] <= x1 and y0 <= _center(b)[1] <= y1]
        return sorted(res, key=lambda x: (x["at"][1], x["at"][0]))[:limit * 5]
    raise SystemExit("near 需要 --block，或 --page 加 --region")


def parse_region(s: str) -> list[float]:
    vals = [float(v) for v in re.split(r"[,，\s]+", s.strip()) if v]
    if len(vals) != 4 or vals[2] <= vals[0] or vals[3] <= vals[1]:
        raise SystemExit("--region 格式为 x0,y0,x1,y1（页面点，左上角原点，x1>x0、y1>y0）")
    return vals


def cmd_near(args):
    blocks = load_blocks(Path(args.pack))
    region = parse_region(args.region) if args.region else None
    res = near_blocks(blocks, block=args.block, page=args.page, region=region, radius=args.radius)
    for r in res:
        where = f"{r['dir']} {r['dist']}pt" if "dir" in r else f"@{r['at'][0]},{r['at'][1]}"
        print(f"[{r['id']}] {where}　{r['text']}")
    if not res:
        print("（范围内没有其他文字块）")


def cmd_calc(args):
    sys.path.insert(0, str(SCRIPT_DIR))
    import dr_calc
    for expr in args.exprs:
        try:
            print(json.dumps({"expr": expr, "value": dr_calc.evaluate(expr)}, ensure_ascii=False))
        except ValueError as exc:
            print(json.dumps({"expr": expr, "error": str(exc)}, ensure_ascii=False))


# ───────────────────────── verify 命令 ─────────────────────────

LOCATED_HOW = {"exact", "exact-multi", "relocated-same-page", "relocated-same-page-multi"}


def _next_of(cur: dict, cands: list[dict]) -> dict | None:
    """阅读顺序的下一块：同一行右侧紧邻的块；没有则下一行、与本块水平有重叠、最靠左的块。"""
    if not cur.get("bbox"):
        return None
    h = cur.get("height") or 3
    ax0, ay0, ax1, ay1 = cur["bbox"]
    same, nxt = [], []
    for b in cands:
        if b is cur or not b.get("bbox"):
            continue
        bx0, by0, bx1, by1 = b["bbox"]
        hb = max(h, b.get("height") or 3)
        if abs(ay0 - by0) <= hb * 0.6 and 0 <= bx0 - ax1 <= hb * 4:
            same.append(b)
        elif 0 < by0 - ay0 <= hb * 3 and min(ax1, bx1) - max(ax0, bx0) > 0:
            nxt.append(b)
    if same:
        return min(same, key=lambda b: b["bbox"][0])
    if nxt:
        return min(nxt, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    return None


class _PageFlow:
    """一页内按空间关系接续的阅读链（双栏说明也能正确接续），用于跨块引文核验。"""

    def __init__(self, seq: list[dict]):
        self.seq = [b for b in seq if b.get("bbox")]
        self.seq.sort(key=lambda b: b["bbox"][1])
        self.tops = [b["bbox"][1] for b in self.seq]
        self._next: dict[str, dict | None] = {}
        self._prev: dict[str, list[dict]] | None = None

    def next(self, b: dict) -> dict | None:
        if b["id"] not in self._next:
            import bisect
            h = (b.get("height") or 3) * 4
            lo = bisect.bisect_left(self.tops, b["bbox"][1] - h)
            hi = bisect.bisect_right(self.tops, b["bbox"][1] + h)
            self._next[b["id"]] = _next_of(b, self.seq[lo:hi])
        return self._next[b["id"]]

    def prev(self, b: dict) -> list[dict]:
        if self._prev is None:
            self._prev = {}
            for a in self.seq:
                n = self.next(a)
                if n is not None:
                    self._prev.setdefault(n["id"], []).append(a)
        return self._prev.get(b["id"], [])

    def chains_with(self, text: str, must: dict | None = None, span: int = 6) -> list[list[dict]]:
        """返回按阅读链顺序拼接后恰好包含引文、且每块都有贡献的块序列。"""
        if must is not None:
            starts, frontier = [must], [must]
            for _ in range(span - 1):
                frontier = [a for b in frontier for a in self.prev(b)]
                starts += frontier
        else:
            head = text[:2]
            starts = [b for b in self.seq if (head and head in norm_text(b["text"])) or norm_text(b["text"]).endswith(text[:1])]
        found, seen = [], set()
        for st in starts:
            chain = [st]
            while len(chain) < span:
                joined = "".join(norm_text(b["text"]) for b in chain)
                if text in joined:
                    break
                nb = self.next(chain[-1])
                if nb is None or nb in chain:
                    break
                chain.append(nb)
            joined = "".join(norm_text(b["text"]) for b in chain)
            if len(chain) < 2 or text not in joined:
                continue
            if text in "".join(norm_text(b["text"]) for b in chain[1:]) or text in "".join(norm_text(b["text"]) for b in chain[:-1]):
                continue   # 不是最小链
            if must is not None and must not in chain:
                continue
            key = tuple(b["id"] for b in chain)
            if key not in seen:
                seen.add(key)
                found.append(chain)
        return found


def locate(quote: dict, blocks: list[dict], by_id: dict[str, dict], pages: dict[str, list[dict]] | None = None,
           flows: dict | None = None):
    """引文必须逐字出现在所写块（或其前后相邻、空间连续的块）中，忽略空白与全半角。
    返回（命中的块，定位方式，别处的候选块）。只在别的页/文件找到时不算定位成功。"""
    text = norm_text(quote.get("text", ""))
    if len(text) < 4:
        return [], "too_short", []
    if pages is None:
        pages = {}
        for b in blocks:
            pages.setdefault(block_page(b), []).append(b)
    flows = {} if flows is None else flows

    def flow(pid):
        if pid not in flows:
            flows[pid] = _PageFlow(pages.get(pid, []))
        return flows[pid]

    m = BLOCK_ID.search(str(quote.get("block", "")))
    claimed = by_id.get(m.group(0)) if m else None
    if claimed:
        if text in norm_text(claimed["text"]):
            return [claimed], "exact", []
        if claimed.get("bbox"):
            win = flow(block_page(claimed)).chains_with(text, must=claimed)
            if win:
                return win[0], "exact-multi", []
    pm = PAGE_ID.search(str(quote.get("block", "")))
    pid = block_page(claimed) if claimed else (pm.group(0) if pm else None)
    if pid and pid in pages:
        seq = pages[pid]
        hits = [b for b in seq if text in norm_text(b["text"])]
        if len(hits) == 1:
            return hits, "relocated-same-page", []
        if len(hits) > 1:
            return [], "ambiguous-same-page", hits[:5]
        wins = flow(pid).chains_with(text)
        if len(wins) == 1:
            return wins[0], "relocated-same-page-multi", []
        if len(wins) > 1:
            return [], "ambiguous-same-page", [w[0] for w in wins[:5]]
    cands = [b for b in blocks if text in norm_text(b["text"]) and (not pid or block_page(b) != pid)]
    if not cands:
        for p in pages:
            if p != pid:
                cands += [w[0] for w in flow(p).chains_with(text)]
    if cands:
        return [], "relocated_candidate", cands[:5]
    return [], "not_found", []


HOW_CN = {"too_short": "引文过短（少于 4 字）", "ambiguous-same-page": "同页有多处相同文字，无法确定是哪一处",
          "relocated_candidate": "所写块编号处没有这段文字，只在别的页/文件找到", "not_found": "原文中找不到这段引文"}


def check_visual(item: dict, blocks_by_page: dict[str, list[dict]], page_sizes: dict[str, tuple[float, float]]) -> dict:
    """视觉证据：页与坐标是否有效；读到的文字能否被框内的文字层佐证。"""
    res = dict(item)
    pid = (PAGE_ID.search(str(item.get("page", ""))) or [None])[0] if item.get("page") else None
    bbox = item.get("bbox")
    if not pid or pid not in page_sizes or not (isinstance(bbox, list) and len(bbox) == 4):
        res["status"] = "invalid"
        res["note"] = "页编号或坐标无效"
        return res
    W, H = page_sizes[pid]
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= W + 1 and 0 <= y0 < y1 <= H + 1):
        res["status"] = "invalid"
        res["note"] = f"坐标超出页面范围（页面 {round(W)}×{round(H)} pt）"
        return res
    pad = 3
    inside = [b for b in blocks_by_page.get(pid, []) if b.get("bbox") and x0 - pad <= _center(b)[0] <= x1 + pad and y0 - pad <= _center(b)[1] <= y1 + pad]
    res["page"] = pid
    res["text_blocks_inside"] = [b["id"] for b in inside][:20]
    reads = norm_text(item.get("reads", ""))
    if not reads:
        res["status"] = "region_only"
    elif reads in "".join(norm_text(b["text"]) for b in inside) or any(reads in norm_text(b["text"]) for b in inside):
        res["status"] = "corroborated"
    else:
        res["status"] = "vision_only"
        res["note"] = "视觉识读的文字在框内文字层中找不到，须回原图核实"
    return res


def cmd_verify(args):
    sys.path.insert(0, str(SCRIPT_DIR))
    import dr_calc
    pack = Path(args.pack)
    blocks = load_blocks(pack)
    by_id = {b["id"]: b for b in blocks}
    pages: dict[str, list[dict]] = {}
    for b in blocks:
        pages.setdefault(block_page(b), []).append(b)
    flows: dict = {}
    cov = json.loads((pack / "coverage.json").read_text(encoding="utf-8"))
    sha_of = {c["file"]: c["sha256"] for c in cov["files"]}
    page_sizes = {p["page"]: (p.get("width") or 0, p.get("height") or 0) for c in cov["files"] for p in c.get("sheets", []) if p.get("page")}
    data = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    findings = data.get("findings", []) if isinstance(data, dict) else data
    out = []
    stats = {"input": len(findings), "located": 0, "downgraded_to_lead": 0, "downgraded_to_needs_context": 0,
             "out_of_scope": 0, "merged": 0, "relocated_candidates": 0}

    def downgrade(f, to, reason):
        if f.get("decision", "suspected") != "suspected":
            return
        f["decision_original"] = "suspected"
        f["decision"] = to
        if to == "needs_context":
            f.setdefault("missing", []).append(reason)
            stats["downgraded_to_needs_context"] += 1
        else:
            f.setdefault("verify_notes", []).append(reason)
            stats["downgraded_to_lead"] += 1

    for i, f in enumerate(findings, start=1):
        f = dict(f)
        f.setdefault("id", f"F{i}")
        f.setdefault("decision", "suspected")
        f.setdefault("review_status", "pending")
        f["missing"] = list(f.get("missing") or [])
        quotes = f.get("quotes") or []
        joined = " ".join(q.get("text", "") for q in quotes) + f.get("problem", "") + f.get("title", "")
        if re.search(r"［(单位|人员|工程名称|信息|编号|电话|邮箱|证件号|敏感词)\d+］", joined) and not quotes_have_other_content(quotes):
            f["scope"] = "out_of_scope"
            f["scope_reason"] = "只涉及图签/单位/人员等脱敏数据，不属于审查范围"
            stats["out_of_scope"] += 1
            out.append(f)
            continue

        # E1：原文存在于所写位置
        located, notes = [], []
        for q in quotes:
            hit, how, cands = locate(q, blocks, by_id, pages, flows)
            ok = how in LOCATED_HOW
            q["located"] = ok
            q["how"] = how
            q["blocks"] = [{"id": b["id"], "file": b["file"], "sheet": b["sheet"], "bbox": b["bbox"],
                            "file_sha256": sha_of.get(b["file"], "")} for b in hit]
            if cands:
                q["candidates"] = [b["id"] for b in cands]
            located.append(ok)
            if how == "relocated_candidate":
                stats["relocated_candidates"] += 1
                notes.append(f"引文“{q.get('text', '')[:30]}”在所写块 {q.get('block', '')} 处找不到，只在 {'、'.join(q['candidates'][:3])} 找到；"
                             "请确认后改正块编号重新 verify，不能当作已定位")
            elif not ok:
                extra = f"（候选：{'、'.join(q['candidates'][:3])}）" if cands else ""
                notes.append(f"引文“{q.get('text', '')[:30]}”{HOW_CN.get(how, how)}{extra}")
            elif how.startswith("relocated"):
                notes.append(f"引文“{q.get('text', '')[:20]}”所写块编号有误，已在同一页唯一定位到 {q['blocks'][0]['id']}")

        visuals = [check_visual(v, pages, page_sizes) for v in (f.get("visual_evidence") or [])]
        if visuals:
            f["visual_evidence"] = visuals
        valid_visual = [v for v in visuals if v["status"] != "invalid"]
        for v in visuals:
            if v["status"] == "invalid":
                notes.append(f"视觉证据 {v.get('page', '')} {v.get('note')}")

        if quotes and all(located):
            f["located"] = "all"
            e1 = "pass"
            stats["located"] += 1
        elif not quotes and valid_visual:
            f["located"] = "vision"
            e1 = "vision_only" if all(v["status"] != "corroborated" for v in valid_visual) else "vision_corroborated"
            downgrade(f, "needs_context", "只有视觉识读、没有逐字引文，须回原图核实")
        else:
            f["located"] = "partial" if any(located) else "none"
            e1 = "fail"
            downgrade(f, "lead", "引文不能完整回到所写位置")   # 不能完整回到原文的只作“待查线索”
        if notes:
            f["verify_notes"] = notes + [n for n in f.get("verify_notes", []) if n not in notes]
        if any(v["status"] == "vision_only" for v in valid_visual):
            f.setdefault("uncertainties", []).append("部分视觉识读未被文字层佐证")

        # E2：对象对应
        pages_hit = {block_page(b) for q in quotes for b in q.get("blocks", [])} | {v["page"] for v in valid_visual}
        om = f.get("object_match")
        if len(pages_hit) > 1 and om != "explicit":
            downgrade(f, "needs_context", f"跨页/跨文件比对的对象对应关系未确认（object_match={om or '未填写'}）")
        elif om in ("inferred", "unknown"):
            om_cn = "推断" if om == "inferred" else "未确认"
            downgrade(f, "needs_context", f"对象对应关系为“{om_cn}”，不能确认是同一构件/断面/工况")
        spatial = None
        if valid_visual and quotes:
            def inside_any(b):
                cx, cy = (b["bbox"][0] + b["bbox"][2]) / 2, (b["bbox"][1] + b["bbox"][3]) / 2
                return any(block_page(b) == v["page"] and v["bbox"][0] - 3 <= cx <= v["bbox"][2] + 3 and v["bbox"][1] - 3 <= cy <= v["bbox"][3] + 3
                           for v in valid_visual)
            qb = [by_id[b["id"]] for q in quotes for b in q.get("blocks", []) if b["id"] in by_id and by_id[b["id"]].get("bbox")]
            spatial = bool(qb) and all(inside_any(b) for b in qb)
            if f.get("check_method") == "vision" and om == "explicit" and not spatial:
                downgrade(f, "needs_context", "视觉证据框没有覆盖引文所在位置，标注与构件的对应关系无法从图中复核")
        e2 = "single_page" if len(pages_hit) <= 1 and not om else (om or "unknown")

        # E3：独立复算
        e3 = "n/a"
        calcs = f.get("calculation") or []
        if calcs:
            mism = 0
            matched = 0
            for c in calcs:
                try:
                    c["computed"] = round(dr_calc.evaluate(str(c.get("expr", ""))), 6)
                except ValueError as exc:
                    c["status"] = "error"
                    c["error"] = str(exc)
                    continue
                if c.get("stated") is None:
                    c["status"] = "computed"
                    continue
                stated = float(c["stated"])
                tol = float(c.get("tolerance", 0.01))
                dev = abs(c["computed"] - stated) / max(abs(stated), 1e-9)
                c["deviation"] = round(dev, 4)
                c["status"] = "mismatch" if dev > tol else "match"
                mism += c["status"] == "mismatch"
                matched += c["status"] == "match"
            if mism:
                e3 = "calc_mismatch_confirmed"
            elif matched:
                e3 = "calc_matches_document"
                if f.get("check_method") == "calc":
                    downgrade(f, "lead", "独立复算结果与文件中的数值一致，所指计算错误不成立")
            else:
                e3 = "calc_unverified"
        elif str(f.get("rule", "")).startswith("C6") or (f.get("norm") or {}).get("verified"):
            e3 = "verified_rule"
        f["evidence"] = {"E1": e1, "E2": e2, "E3": e3, "spatial_support": spatial}
        if not f["missing"]:
            f.pop("missing")
        out.append(f)

    # 去重：引文块集合有交集且标题相近只留置信度最高的一条
    def grams(s):
        s = norm_text(s)
        return {s[k:k + 2] for k in range(max(0, len(s) - 1))}
    kept = []
    for f in sorted(out, key=lambda x: -float(x.get("confidence", 0) or 0)):
        ids = {b["id"] for q in f.get("quotes", []) for b in q.get("blocks", [])}
        dup = None
        for k in kept:
            kids = {b["id"] for q in k.get("quotes", []) for b in q.get("blocks", [])}
            g1, g2 = grams(f.get("title", "")), grams(k.get("title", ""))
            similar = len(g1 & g2) / max(1, len(g1 | g2)) >= 0.3
            same_rule_subset = ids and ids <= kids and f.get("rule") and f.get("rule") == k.get("rule")
            if ids and ids & kids and (similar or same_rule_subset):
                dup = k
                break
        if dup:
            dup.setdefault("merged_ids", []).append(f["id"])
            stats["merged"] += 1
        else:
            kept.append(f)
    result = {"verified_at": _dt.datetime.now().isoformat(timespec="seconds"), "tool": f"drawing_review.py {TOOL_VERSION}",
              "stats": stats, "rules_run": data.get("rules_run", []) if isinstance(data, dict) else [],
              "focus": data.get("focus") if isinstance(data, dict) else None,
              "image_mode": data.get("image_mode") if isinstance(data, dict) else None, "findings": kept}
    target = Path(args.out) if args.out else pack / "verified.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(target), **stats}, ensure_ascii=False, indent=2))


def quotes_have_other_content(quotes: list[dict]) -> bool:
    """引文去掉占位符后仍有实质内容（≥6 个汉字）时，问题不只关于脱敏数据。"""
    for q in quotes:
        rest = re.sub(r"［[^］]*］", "", q.get("text", ""))
        if len(re.findall(r"[一-龥]", rest)) >= 6:
            return True
    return False


# ───────────────────────── report 命令 ─────────────────────────

DECISION_CN = {"suspected": "疑似问题", "needs_context": "需补充条件", "not_checked": "未检查", "lead": "待查线索", "no_finding": "未发现"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
METHOD_CN = {"text": "文字比对", "vision": "图像识读辅助", "calc": "独立复算", "rule": "已核对规则", "spatial": "文字坐标关联"}
E1_CN = {"pass": "引文均回到所写位置", "fail": "引文未能完整定位", "vision_only": "仅视觉识读（未被文字层佐证）",
         "vision_corroborated": "视觉识读，有文字层佐证"}
E2_CN = {"single_page": "同页文字", "explicit": "明确对应", "inferred": "推断对应", "unknown": "对应关系未确认"}
E3_CN = {"n/a": "—", "calc_mismatch_confirmed": "独立复算确认数值不一致", "calc_matches_document": "独立复算与文件一致",
         "calc_unverified": "复算未完成", "verified_rule": "按已核对规则判定"}
STATUS_CN = {"pending": "待人工复核", "confirmed": "已确认", "dismissed": "已排除", "fixed_pending_recheck": "修改后待复核"}


def sheet_ranges(sheets: list[str]) -> str:
    """P3、P4、P5、P9 → P3–P5、P9。"""
    nums = sorted(int(s[1:]) for s in sheets if re.fullmatch(r"P\d+", s))
    other = [s for s in sheets if not re.fullmatch(r"P\d+", s)]
    parts, start = [], None
    for k, n in enumerate(nums):
        if start is None:
            start = n
        if k == len(nums) - 1 or nums[k + 1] != n + 1:
            parts.append(f"P{start}" if start == n else f"P{start}–P{n}")
            start = None
    return "、".join(parts + other)


def fmt_loc(q: dict) -> str:
    locs = []
    for b in q.get("blocks", []):
        bbox = b.get("bbox")
        box = f" 坐标[{', '.join(str(round(v)) for v in bbox)}]" if bbox else ""
        locs.append(f"{b['file']} {b['sheet']} 块 {b['id']}{box}")
    return "；".join(locs) or "未能定位"


def cmd_report(args):
    pack = Path(args.pack)
    cov = json.loads((pack / "coverage.json").read_text(encoding="utf-8"))
    ver_path = Path(args.verified) if args.verified else pack / "verified.json"
    ver = json.loads(ver_path.read_text(encoding="utf-8"))
    images = json.loads((pack / "images.json").read_text(encoding="utf-8")) if (pack / "images.json").exists() else None
    labels = {}
    for c in cov["files"]:
        for p in c.get("sheets", []):
            labels[(c["file"], p["sheet"])] = " ".join(x for x in (p.get("sheet_no"), p.get("sheet_title")) if x)
    fs = [f for f in ver["findings"] if f.get("scope") != "out_of_scope"]
    groups = {k: [f for f in fs if f.get("decision", "suspected") == k] for k in ("suspected", "needs_context", "lead", "not_checked")}
    for g in groups.values():
        g.sort(key=lambda f: (SEVERITY_ORDER.get(f.get("severity", "low"), 9), -float(f.get("confidence", 0) or 0)))

    L = [f"# {args.title}", "",
         f"生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}　工具：{cov['tool']}　报告性质：**AI 辅助自查候选，须设计人员逐条确认，不是正式审图意见或合规结论**", ""]
    L += ["## 一、审查概况", "", "| 文件 | 类型（推测） | 页 | 已审查 | 仅部分文字 | 未审查 | SHA-256（前 12 位） |", "|---|---|---|---|---|---|---|"]
    for c in cov["files"]:
        sh = c.get("sheets", [])
        ok = sum(1 for p in sh if p.get("status") == "ok")
        part = sum(1 for p in sh if p.get("status") == "partial")
        bad = (len(sh) - ok - part) if c["status"] == "ok" else "全部"
        L.append(f"| {c.get('file_id', '')} {c['file']} | {c.get('doc_kind_guess', '-')} | {len(sh)} | {ok} | {part} | {bad} | {c['sha256'][:12]} |")
    if images and images.get("images"):
        zooms = sum(1 for im in images["images"] if im.get("kind") == "zoom")
        img_line = (f"已渲染本机遮盖后的页面图 {len(images['images']) - zooms} 张、局部放大 {zooms} 张，用于辅助判断标注归属；"
                    "仅凭图像识读、未被文字层佐证的内容不作为确定问题")
    else:
        img_line = "未使用（仅文字层审查）"
    L += ["", f"- 审查重点：{ver.get('focus') or '按清单全面审查'}",
          f"- 执行的检查项：{('、'.join(ver.get('rules_run') or [])) or '见第六节'}",
          f"- 图像模式：{img_line}",
          f"- 结果：疑似问题 {len(groups['suspected'])} 条，需补充条件 {len(groups['needs_context'])} 条，待查线索 {len(groups['lead'])} 条",
          f"- 脱敏：图签及出图章文字 {cov['blocks_excluded_title_block_or_stamp']} 块已剔除；替换为占位符 {sum(cov['masked'].values())} 处（{'、'.join(f'{k}{v}' for k, v in cov['masked'].items()) or '无'}）。{cov['scope_note']}", ""]

    def section(title, items, empty):
        L.extend([f"## {title}", ""])
        if not items:
            L.extend([empty, ""])
            return
        L.extend(["| 编号 | 级别 | 问题 | 位置 | 检查项 | 核查方式 |", "|---|---|---|---|---|---|"])
        for f in items:
            q0 = (f.get("quotes") or [{}])[0]
            b0 = (q0.get("blocks") or [{}])[0]
            where = f"{b0.get('file', '')} {b0.get('sheet', '')} {labels.get((b0.get('file'), b0.get('sheet')), '')}".strip()
            if not where and f.get("visual_evidence"):
                where = f"{f['visual_evidence'][0].get('page', '')}（图像）"
            L.append(f"| {f['id']} | {f.get('severity', '-')} | {f.get('title', '')} | {where or '未定位'} | {f.get('rule', '-')} | {METHOD_CN.get(f.get('check_method', 'text'), f.get('check_method', '-'))} |")
        L.append("")
        for f in items:
            L.append(f"### {f['id']}　{f.get('title', '')}")
            L.append("")
            L.append(f"- **结论**：{DECISION_CN.get(f.get('decision', 'suspected'))}（置信度 {f.get('confidence', '-')}；检查项 {f.get('rule', '-')}；类别 {f.get('category', '-')}）")
            if f.get("decision_original"):
                L.append(f"- **核验调整**：原为{DECISION_CN.get(f['decision_original'])}，核验后调整")
            if f.get("object"):
                L.append(f"- **对象**：{f['object']}")
            L.append(f"- **问题**：{f.get('problem', '')}")
            for q in f.get("quotes", []):
                L.append(f"- **原文**：“{q.get('text', '')}” —— {fmt_loc(q)}")
            for v in f.get("visual_evidence", []):
                st = {"corroborated": "文字层佐证", "vision_only": "仅视觉识读，须回原图核实", "region_only": "图面区域", "invalid": "无效"}.get(v.get("status"), "")
                box = ", ".join(str(round(x)) for x in v.get("bbox", []))
                L.append(f"- **图面证据**：{v.get('page', '')} 区域[{box}] {('读作“' + v['reads'] + '”') if v.get('reads') else ''}（{st}）{v.get('note_model', '')}")
            for c in f.get("calculation", []):
                if c.get("status") == "error":
                    L.append(f"- **复算**：`{c.get('expr')}` 无法计算（{c.get('error')}）")
                    continue
                stated = f"，文件中为 {c['stated']}，偏差 {c.get('deviation', 0):.1%}" if c.get("stated") is not None else ""
                L.append(f"- **复算**：`{c.get('expr')}` = {c.get('computed')} {c.get('unit', '')}{stated}{('；' + c['note']) if c.get('note') else ''}")
            ev = f.get("evidence")
            if ev:
                L.append(f"- **证据分级**：E1 原文定位：{E1_CN.get(ev['E1'], ev['E1'])}；E2 对象对应：{E2_CN.get(ev['E2'], ev['E2'])}；E3 结论支撑：{E3_CN.get(ev['E3'], ev['E3'])}")
            if f.get("basis"):
                L.append(f"- **判断依据**：{f['basis']}")
            norm = f.get("norm") or {}
            if norm.get("code"):
                state = "已核对条文" if norm.get("verified") else "条文待人工核对"
                L.append(f"- **规范**：{norm.get('code')} {norm.get('clause', '')}（{state}）")
            if f.get("missing"):
                L.append(f"- **缺少的条件**：{'；'.join(f['missing'])}")
            if f.get("uncertainties"):
                L.append(f"- **不确定因素**：{'；'.join(f['uncertainties'])}")
            if f.get("suggestion"):
                L.append(f"- **建议核对**：{f['suggestion']}")
            if f.get("verify_notes"):
                L.append(f"- **定位说明**：{'；'.join(f['verify_notes'])}")
            if f.get("merged_ids"):
                L.append(f"- 已合并重复候选：{'、'.join(f['merged_ids'])}")
            shots = sorted((pack / "evidence").glob(f"{f['id']}.png")) + sorted((pack / "evidence").glob(f"{f['id']}-*.png"))
            if shots:
                L.append("- **原图位置**（红框为证据位置，黑框为已遮盖的图签与敏感信息）：")
                L.append("")
                L.extend(f"  ![{f['id']}](evidence/{s.name})" for s in shots)
                L.append("")
            L.append(f"- **人工处理**（当前：{STATUS_CN.get(f.get('review_status', 'pending'), f.get('review_status'))}）：□ 确认　□ 排除（原因：　　　　）　□ 修改后待复核")
            L.append("")

    section("二、疑似问题", groups["suspected"], "本次已执行的检查范围内未发现可定位的疑似问题。这不代表图纸审查合格，未覆盖范围见第五节。")
    section("三、需补充条件（缺少前提或对象对应未确认，未下结论）", groups["needs_context"], "无。")
    section("四、待查线索（无法回到原文定位，仅供参考）", groups["lead"], "无。")

    L += ["## 五、核查范围与限制", ""]
    for c in cov["files"]:
        if c["status"] != "ok":
            L.append(f"- {c['file']}：未审查 —— {c.get('reason')}")
        sheets = c.get("sheets", [])
        unchecked = [p["sheet"] for p in sheets if p.get("status") == "not_checked"]
        partial = [p["sheet"] for p in sheets if p.get("status") == "partial"]
        low = [p["sheet"] for p in sheets if p.get("status") == "ok" and p.get("low_text")]
        warned = [p["sheet"] for p in sheets if p.get("warnings")]
        if unchecked:
            L.append(f"- {c['file']} 未审查 {len(unchecked)} 页（{sheet_ranges(unchecked)}）：无文字层、几乎没有正文或文字无法解码（多为扫描件/纯图形），未做 OCR")
        if partial:
            L.append(f"- {c['file']} 以图形为主 {len(partial)} 页（{sheet_ranges(partial)}）：只审查了图名、标注等少量文字，图形内容{'仅作图像辅助查看，' if images else ''}未逐项审查")
        if low:
            L.append(f"- {c['file']} 文字偏少 {len(low)} 页（{sheet_ranges(low)}）：正文可能部分为图像，只审查了可读文字")
        if warned:
            L.append(f"- {c['file']} 图签识别提示 {len(warned)} 页（{sheet_ranges(warned)}）：图签范围可能偏大，部分正文可能被剔除，请人工查看")
    if cov.get("possible_name_residue"):
        L.append(f"- 有 {len(cov['possible_name_residue'])} 个正文块可能含图签人名（两字人名不在正文自动替换），图像模式中已遮盖该块；如需在文字中替换，请用 --extra-terms")
    for f in groups["not_checked"]:
        L.append(f"- {f.get('rule', '')}：{f.get('problem') or f.get('title')}")
    L += ["- 尺寸标注的几何关系、线型、填充、钢筋实际排布等图形信息未做自动判定；图像模式的识读只作为辅助，须回原图核实。",
          "- 图签、单位、人员、工程名称、出图章属于脱敏数据，不在审查范围；目录与图签一致性未检查。",
          "- 规范条文引用以设计人员核对现行有效版本原文为准；“未发现”只表示在本次检查项和可读文字范围内未命中。", ""]
    out_of_scope = [f for f in ver["findings"] if f.get("scope") == "out_of_scope"]
    if out_of_scope:
        L.append(f"- 另有 {len(out_of_scope)} 条候选只涉及脱敏数据，按范围排除，未列入。")
        L.append("")
    L += ["## 六、检查项与版本", ""]
    for r in ver.get("rules_run") or []:
        L.append(f"- {r}")
    st = ver["stats"]
    L += ["", f"核验统计：输入候选 {st['input']} 条，完整定位 {st['located']} 条，降为线索 {st['downgraded_to_lead']} 条，"
              f"降为需补充条件 {st.get('downgraded_to_needs_context', 0)} 条，合并重复 {st['merged']} 条，范围外 {st['out_of_scope']} 条。", ""]
    L += ["## 七、人工复核记录", "", "| 复核人 | 日期 | 确认 | 排除 | 待办 | 备注 |", "|---|---|---|---|---|---|", "|  |  |  |  |  |  |", ""]
    target = Path(args.out) if args.out else pack / "审查报告.md"
    target.write_text("\n".join(L), encoding="utf-8")
    print(json.dumps({"out": str(target), "suspected": len(groups["suspected"]), "needs_context": len(groups["needs_context"]),
                      "lead": len(groups["lead"])}, ensure_ascii=False))


def _render_cmd(name):
    def run(args):
        sys.path.insert(0, str(SCRIPT_DIR))
        import dr_render
        getattr(dr_render, name)(args)
    return run


def main(argv=None):
    p = argparse.ArgumentParser(description="图纸审查 skill 本地工具（PDF 抽取/脱敏/引文核验/报告）")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="抽取 PDF 文字、剔除图签并脱敏")
    e.add_argument("files", nargs="+")
    e.add_argument("--out", default="review_pack")
    e.add_argument("--extra-terms", help="额外敏感词文件（每行一个，如工程名、地名、人名）")
    e.set_defaults(func=cmd_extract)

    n = sub.add_parser("near", help="列出某块周围或某区域内的文字（无图像时判断标注归属）")
    n.add_argument("--pack", default="review_pack")
    n.add_argument("--block")
    n.add_argument("--page")
    n.add_argument("--region", help="x0,y0,x1,y1（页面点）")
    n.add_argument("--radius", type=float)
    n.set_defaults(func=cmd_near)

    c = sub.add_parser("calc", help="安全计算表达式（复算用，支持 As/As_s/rho 等）")
    c.add_argument("exprs", nargs="+")
    c.set_defaults(func=cmd_calc)

    for name, helptext in (("render", "可选图像模式：渲染遮盖后的整页图"), ("zoom", "可选图像模式：渲染局部放大图"),
                           ("evidence", "可选：为核验后的问题生成遮盖后的原图截图")):
        rp = sub.add_parser(name, help=helptext)
        rp.add_argument("files", nargs="+", help="与 extract 相同的原始 PDF（按 SHA-256 自动对应，顺序不限）")
        rp.add_argument("--pack", default="review_pack")
        if name == "render":
            rp.add_argument("--pages", help="只渲染这些页，如 F1-P3,F1-P5；默认全部已审查/部分审查的页")
            rp.add_argument("--max-px", type=int, default=2000, help="整页图长边像素上限")
            rp.add_argument("--allow-unmasked-pages", action="store_true",
                            help="也渲染无法识别图签的扫描/乱码页（只按页面四周保守遮盖，须用户明确同意）")
        if name == "zoom":
            rp.add_argument("--page", required=True)
            rp.add_argument("--region", required=True, help="x0,y0,x1,y1（页面点）")
            rp.add_argument("--dpi", type=int, default=300)
            rp.add_argument("--max-px", type=int, default=2400)
        if name == "evidence":
            rp.add_argument("--verified")
        rp.set_defaults(func=_render_cmd(f"cmd_{name}"))

    v = sub.add_parser("verify", help="核验引文、视觉证据、复算和对象对应")
    v.add_argument("--pack", default="review_pack")
    v.add_argument("--findings", required=True)
    v.add_argument("--out")
    v.set_defaults(func=cmd_verify)
    r = sub.add_parser("report", help="生成 Markdown 审查报告")
    r.add_argument("--pack", default="review_pack")
    r.add_argument("--verified")
    r.add_argument("--out")
    r.add_argument("--title", default="施工图审查报告（AI 辅助自查）")
    r.set_defaults(func=cmd_report)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
