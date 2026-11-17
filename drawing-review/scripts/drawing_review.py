#!/usr/bin/env python3
"""图纸审查 skill 的本地工具（仅 PDF）：抽取文字 → 剔除图签并脱敏 → 核验引文 → 生成报告。

全部在本机运行，不联网。模型只读取 extract 生成的脱敏文件（review_input.md / blocks.jsonl），
不直接打开原始 PDF；mapping.local.json 保存占位符与原文的对应，只留在本机。

用法：
  python drawing_review.py extract 图纸.pdf [更多.pdf ...] --out review_pack [--extra-terms 敏感词.txt]
  python drawing_review.py verify  --pack review_pack --findings findings.json
  python drawing_review.py report  --pack review_pack [--title 审查报告]

依赖：优先 pdfplumber（MIT，支持竖排文字），没有时用 pypdf（BSD，坐标为近似值）。
只读取 PDF 文字层；扫描件、纯图形页、字体无法解码的页标为“未审查”，不做 OCR。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

TOOL_VERSION = "1.0.0"

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
    id: str
    file: str                     # 脱敏后的文件代号，如 图纸1.pdf
    sheet: str                    # 页标识：P3
    text: str
    bbox: list[float] | None      # 页面点、左上角原点 [x0,y0,x1,y1]
    source: str                   # pdf-text
    height: float | None = None
    flags: list[str] = field(default_factory=list)
    in_title_block: bool = False


# ───────────────────────── 抽取 ─────────────────────────

def extract_pdf(path: Path, file_label: str) -> tuple[list[Block], list[dict]]:
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
                lines = upright.extract_text_lines(return_chars=False) if hasattr(upright, "extract_text_lines") else []
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
                    page_blocks.append(Block(f"P{i}-b{j}", file_label, f"P{i}", text, bbox, "pdf-text",
                                             height=round(height, 2), flags=flags))
                blocks += page_blocks
                pages_info.append({"sheet": f"P{i}", "width": float(page.width), "height": float(page.height),
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
                page_blocks.append(Block(f"P{i}-b{j}", file_label, f"P{i}", text,
                                         [round(x, 1), round(y - size, 1), round(x1, 1), round(y, 1)], "pdf-text",
                                         height=round(size, 2), flags=["approx-bbox"]))
        blocks += page_blocks
        pages_info.append({"sheet": f"P{i}", "width": W, "height": H, "chars": sum(len(b.text) for b in page_blocks),
                           "extractor": f"pypdf {pypdf.__version__}"})
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
        meta = {"title_block_regions": 0, "sheet_no": None, "sheet_title": None}
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
            for b in items:
                if not b.bbox:
                    continue
                cx, cy = (b.bbox[0] + b.bbox[2]) / 2, (b.bbox[1] + b.bbox[3]) / 2
                if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1, _ in regions):
                    b.in_title_block = True
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
        per.setdefault(b.sheet, Counter()).update(ch for ch in b.text if "\u4e00" <= ch <= "\u9fa5")
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
    for idx, raw in enumerate(args.files, start=1):
        path = Path(raw)
        ext = path.suffix.lower()
        label = f"图纸{idx}{ext}"
        cov = {"file": label, "format": ext.lstrip("."), "sha256": sha256_file(path), "status": "ok", "sheets": []}
        masker.map[path.name] = label   # 文件名常含工程名称，报告里只用代号
        try:
            if ext != ".pdf":
                cov.update(status="not_checked", reason=f"不支持的格式 {ext}；本工具只读取 PDF（CAD 图纸请先导出矢量 PDF）")
                files_cov.append(cov)
                continue
            blocks, info = extract_pdf(path, label)
        except SystemExit as exc:
            cov.update(status="failed", reason=str(exc))
            files_cov.append(cov)
            continue
        meta = mark_title_blocks(blocks)
        garbled = garbled_sheets(blocks)
        for p in info:
            m = meta.get(p["sheet"], {})
            p.update(m)
            if p["sheet"] in garbled:
                p["status"] = "not_checked"
                p["reason"] = "文字编码无法正确解码（读出大量重复字），未审查；请 pip install pdfplumber 后重跑，或从 CAD 重新导出 PDF"
                blocks = [b for b in blocks if b.sheet != p["sheet"]]
            elif p.get("chars", 0) < 20:
                p["status"] = "not_checked"
                p["reason"] = "无文字层或文字过少（可能是扫描件/纯图形），未做 OCR，本页未审查"
            else:
                p["status"] = "ok"
        cov["sheets"] = info
        files_cov.append(cov)
        all_blocks += blocks

    masker.learn_from_title_block(all_blocks)
    kept: list[Block] = []
    excluded = 0
    for b in all_blocks:
        if b.in_title_block:
            excluded += 1
            continue   # 图签整块不进入审查材料
        masked = masker.mask(b.text)
        if STAMP_HINT.search(masked) and len(norm_text(masked)) <= 30:
            excluded += 1
            continue   # 出图章、注册章文字
        b.text = masked
        kept.append(b)

    # 剔除图签后再看正文文字量：只剩会签栏/少量标注的页，正文多半是扫描图像
    kept_chars: dict[tuple[str, str], int] = {}
    for b in kept:
        if "rotated" in b.flags:
            continue   # 竖排文字多为会签栏、侧边栏，不计入正文文字量
        kept_chars[(b.file, b.sheet)] = kept_chars.get((b.file, b.sheet), 0) + len(norm_text(b.text))
    for cov in files_cov:
        for p in cov.get("sheets", []):
            n = kept_chars.get((cov["file"], p["sheet"]), 0)
            p["chars_after_title_block"] = n
            if p.get("status") == "ok" and n < 80:
                p["status"] = "not_checked"
                p["reason"] = f"剔除图签后正文只有 {n} 字（可能是扫描件/纯图形），未做 OCR，本页未审查"
            elif p.get("status") == "ok" and n < 400:
                p["low_text"] = True

    # 图号/图名作为页面标签：也做一次脱敏（图名里偶有工程名称）
    sheet_labels = {}
    for cov in files_cov:
        for p in cov.get("sheets", []):
            key = f"{cov['file']}|{p['sheet']}"
            sheet_labels[key] = " ".join(masker.mask(x, whole_block=False) for x in (p.get("sheet_no"), p.get("sheet_title")) if x)
            for k in ("sheet_no", "sheet_title"):
                if p.get(k):
                    p[k] = masker.mask(p[k], whole_block=False)

    with (out / "blocks.jsonl").open("w", encoding="utf-8") as f:
        for b in kept:
            d = asdict(b)
            d.pop("in_title_block")
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    lines = ["# 审查材料（已剔除图签、已脱敏）", "",
             "引用原文时请逐字复制方括号后的文字，并写明块编号，例如 `P3-b12`。占位符如［单位1］［人员2］是脱敏数据，不属于审查范围。", ""]
    for cov in files_cov:
        lines.append(f"## {cov['file']}（{cov['format'].upper()}）")
        if cov["status"] != "ok":
            lines += [f"> 未审查：{cov.get('reason')}", ""]
            continue
        for p in cov["sheets"]:
            label = sheet_labels.get(f"{cov['file']}|{p['sheet']}", "")
            lines.append(f"### 【{p['sheet']}】{label}".rstrip())
            if p["status"] != "ok":
                lines += [f"> 未审查：{p['reason']}", ""]
                continue
            for b in kept:
                if b.file == cov["file"] and b.sheet == p["sheet"]:
                    lines.append(f"[{b.id}] {b.text.replace(chr(10), ' / ')}")
            lines.append("")
    (out / "review_input.md").write_text("\n".join(lines), encoding="utf-8")

    coverage = {
        "tool": f"drawing_review.py {TOOL_VERSION}",
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "files": files_cov,
        "blocks_kept": len(kept),
        "blocks_excluded_title_block_or_stamp": excluded,
        "masked": dict(masker.counts),
        "scope_note": "图签（单位、人员、工程名称、日期、出图章等）已剔除并脱敏，不属于审查范围；图号/图名仅用于定位。",
    }
    (out / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "mapping.local.json").write_text(json.dumps({v: k for k, v in masker.map.items()}, ensure_ascii=False, indent=2), encoding="utf-8")

    pages = sum(len(c.get("sheets", [])) for c in files_cov)
    unchecked = sum(1 for c in files_cov for p in c.get("sheets", []) if p.get("status") != "ok") + sum(1 for c in files_cov if c["status"] != "ok")
    print(json.dumps({"out": str(out), "files": len(files_cov), "sheets": pages, "not_checked": unchecked,
                      "blocks_kept": len(kept), "blocks_excluded": excluded, "masked": masker.counts,
                      "read_next": str(out / "review_input.md"),
                      "warning": "mapping.local.json 含原文对应关系，只留本机，不要上传或粘贴给模型"}, ensure_ascii=False, indent=2))


# ───────────────────────── verify 命令 ─────────────────────────

def load_blocks(pack: Path) -> list[dict]:
    with (pack / "blocks.jsonl").open(encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def locate(quote: dict, blocks: list[dict], by_id: dict[str, dict]) -> tuple[list[dict], str]:
    """引文必须逐字出现在原文块中（忽略空白与全半角）。返回（命中的块，定位方式）。"""
    text = norm_text(quote.get("text", ""))
    if len(text) < 4:
        return [], "引文过短（少于 4 字）"
    claimed = by_id.get(quote.get("block", ""))
    if claimed and text in norm_text(claimed["text"]):
        return [claimed], "exact"
    sheet = claimed["sheet"] if claimed else (quote.get("block", "").split("-b")[0] or None)
    same = [b for b in blocks if sheet and (b["sheet"] == sheet or b["id"].startswith(sheet + "-"))]
    for pool, how in ((same, "same-sheet"), (blocks, "other-sheet")):
        hit = [b for b in pool if text in norm_text(b["text"])]
        if hit:
            return hit[:3], how
        # 跨相邻文字块的引文：被若干完整块基本覆盖
        parts = [b for b in pool if len(norm_text(b["text"])) >= 4 and norm_text(b["text"]) in text]
        if parts and sum(len(norm_text(b["text"])) for b in parts) >= len(text) * 0.7:
            return parts[:6], how + "-multi"
    return [], "原文中找不到这段引文"


def cmd_verify(args):
    pack = Path(args.pack)
    blocks = load_blocks(pack)
    by_id = {b["id"]: b for b in blocks}
    data = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    findings = data.get("findings", data if isinstance(data, list) else [])
    out = []
    stats = {"input": len(findings), "located": 0, "downgraded_to_lead": 0, "out_of_scope": 0, "merged": 0}
    for i, f in enumerate(findings, start=1):
        f = dict(f)
        f.setdefault("id", f"F{i}")
        quotes = f.get("quotes") or []
        joined = " ".join(q.get("text", "") for q in quotes) + f.get("problem", "") + f.get("title", "")
        if re.search(r"［(单位|人员|工程名称|信息|编号|电话|邮箱|证件号|敏感词)\d+］", joined) and not quotes_have_other_content(quotes):
            f["scope"] = "out_of_scope"
            f["scope_reason"] = "只涉及图签/单位/人员等脱敏数据，不属于审查范围"
            stats["out_of_scope"] += 1
            out.append(f)
            continue
        located, notes = [], []
        for q in quotes:
            hit, how = locate(q, blocks, by_id)
            q["located"] = bool(hit)
            q["how"] = how
            q["blocks"] = [{"id": b["id"], "file": b["file"], "sheet": b["sheet"], "bbox": b["bbox"]} for b in hit]
            located.append(bool(hit))
            if not hit:
                notes.append(f"引文“{q.get('text', '')[:30]}”{how}")
        if quotes and all(located):
            f["located"] = "all"
            stats["located"] += 1
        else:
            f["located"] = "partial" if any(located) else "none"
            if f.get("decision", "suspected") == "suspected":
                f["decision_original"] = "suspected"
                f["decision"] = "lead"     # 不能完整回到原文的只作“待查线索”
                stats["downgraded_to_lead"] += 1
            f["verify_notes"] = notes or ["没有引文"]
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
    result = {"verified_at": _dt.datetime.now().isoformat(timespec="seconds"), "stats": stats,
              "rules_run": data.get("rules_run", []), "focus": data.get("focus"), "findings": kept}
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
    L += ["## 一、审查概况", "", "| 文件 | 格式 | 页 | 已审查 | 未审查 | SHA-256（前 12 位） |", "|---|---|---|---|---|---|"]
    for c in cov["files"]:
        sh = c.get("sheets", [])
        ok = sum(1 for p in sh if p.get("status") == "ok")
        L.append(f"| {c['file']} | {c['format'].upper()} | {len(sh)} | {ok} | {len(sh) - ok if c['status'] == 'ok' else '全部'} | {c['sha256'][:12]} |")
    L += ["", f"- 审查重点：{ver.get('focus') or '按清单全面审查'}",
          f"- 执行的检查项：{('、'.join(ver.get('rules_run') or [])) or '见第六节'}",
          f"- 结果：疑似问题 {len(groups['suspected'])} 条，需补充条件 {len(groups['needs_context'])} 条，待查线索 {len(groups['lead'])} 条",
          f"- 脱敏：图签及出图章文字 {cov['blocks_excluded_title_block_or_stamp']} 块已剔除；替换为占位符 {sum(cov['masked'].values())} 处（{'、'.join(f'{k}{v}' for k, v in cov['masked'].items()) or '无'}）。{cov['scope_note']}", ""]

    def section(title, items, empty):
        L.extend([f"## {title}", ""])
        if not items:
            L.extend([empty, ""])
            return
        L.extend(["| 编号 | 级别 | 问题 | 位置 | 检查项 |", "|---|---|---|---|---|"])
        for f in items:
            q0 = (f.get("quotes") or [{}])[0]
            b0 = (q0.get("blocks") or [{}])[0]
            where = f"{b0.get('file', '')} {b0.get('sheet', '')} {labels.get((b0.get('file'), b0.get('sheet')), '')}".strip() or "未定位"
            L.append(f"| {f['id']} | {f.get('severity', '-')} | {f.get('title', '')} | {where} | {f.get('rule', '-')} |")
        L.append("")
        for f in items:
            L.append(f"### {f['id']}　{f.get('title', '')}")
            L.append("")
            L.append(f"- **结论**：{DECISION_CN.get(f.get('decision', 'suspected'))}（置信度 {f.get('confidence', '-')}；检查项 {f.get('rule', '-')}；类别 {f.get('category', '-')}）")
            L.append(f"- **问题**：{f.get('problem', '')}")
            for q in f.get("quotes", []):
                L.append(f"- **原文**：“{q.get('text', '')}” —— {fmt_loc(q)}")
            if f.get("basis"):
                L.append(f"- **判断依据**：{f['basis']}")
            norm = f.get("norm") or {}
            if norm.get("code"):
                state = "已核对条文" if norm.get("verified") else "条文待人工核对"
                L.append(f"- **规范**：{norm.get('code')} {norm.get('clause', '')}（{state}）")
            if f.get("missing"):
                L.append(f"- **缺少的条件**：{'；'.join(f['missing'])}")
            if f.get("suggestion"):
                L.append(f"- **建议核对**：{f['suggestion']}")
            if f.get("verify_notes"):
                L.append(f"- **定位说明**：{'；'.join(f['verify_notes'])}")
            if f.get("merged_ids"):
                L.append(f"- 已合并重复候选：{'、'.join(f['merged_ids'])}")
            L.append("- **人工处理**：□ 确认　□ 排除（原因：　　　　）　□ 待办")
            L.append("")

    section("二、疑似问题", groups["suspected"], "本次已执行的检查范围内未发现可定位的疑似问题。这不代表图纸审查合格，未覆盖范围见第五节。")
    section("三、需补充条件（缺少前提，未下结论）", groups["needs_context"], "无。")
    section("四、待查线索（无法回到原文定位，仅供参考）", groups["lead"], "无。")

    L += ["## 五、核查范围与限制", ""]
    for c in cov["files"]:
        if c["status"] != "ok":
            L.append(f"- {c['file']}：未审查 —— {c.get('reason')}")
        sheets = c.get("sheets", [])
        unchecked = [p["sheet"] for p in sheets if p.get("status") != "ok"]
        low = [p["sheet"] for p in sheets if p.get("status") == "ok" and p.get("low_text")]
        if unchecked:
            L.append(f"- {c['file']} 未审查 {len(unchecked)} 页（{sheet_ranges(unchecked)}）：无文字层或剔除图签后正文文字过少（多为扫描件/纯图形），未做 OCR")
        if low:
            L.append(f"- {c['file']} 文字偏少 {len(low)} 页（{sheet_ranges(low)}）：正文可能部分为图像，只审查了可读文字")
    for f in groups["not_checked"]:
        L.append(f"- {f.get('rule', '')}：{f.get('problem') or f.get('title')}")
    L += ["- 只审查图纸中可读取的文字；图形、线型、尺寸标注几何关系、填充和扫描页未做判定。",
          "- 图签、单位、人员、工程名称、出图章属于脱敏数据，不在审查范围；目录与图签一致性未检查。",
          "- 规范条文引用以设计人员核对现行有效版本原文为准；“未发现”只表示在本次检查项和可读文字范围内未命中。", ""]
    out_of_scope = [f for f in ver["findings"] if f.get("scope") == "out_of_scope"]
    if out_of_scope:
        L.append(f"- 另有 {len(out_of_scope)} 条候选只涉及脱敏数据，按范围排除，未列入。")
        L.append("")
    L += ["## 六、检查项与版本", ""]
    for r in ver.get("rules_run") or []:
        L.append(f"- {r}")
    L += ["", f"核验统计：输入候选 {ver['stats']['input']} 条，完整定位 {ver['stats']['located']} 条，降为线索 {ver['stats']['downgraded_to_lead']} 条，合并重复 {ver['stats']['merged']} 条，范围外 {ver['stats']['out_of_scope']} 条。", ""]
    L += ["## 七、人工复核记录", "", "| 复核人 | 日期 | 确认 | 排除 | 待办 | 备注 |", "|---|---|---|---|---|---|", "|  |  |  |  |  |  |", ""]
    target = Path(args.out) if args.out else pack / "审查报告.md"
    target.write_text("\n".join(L), encoding="utf-8")
    print(json.dumps({"out": str(target), "suspected": len(groups["suspected"]), "needs_context": len(groups["needs_context"]),
                      "lead": len(groups["lead"])}, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description="图纸审查 skill 本地工具（PDF 抽取/脱敏/引文核验/报告）")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="抽取 PDF 文字、剔除图签并脱敏")
    e.add_argument("files", nargs="+")
    e.add_argument("--out", default="review_pack")
    e.add_argument("--extra-terms", help="额外敏感词文件（每行一个，如工程名、地名、人名）")
    e.set_defaults(func=cmd_extract)
    v = sub.add_parser("verify", help="核验模型给出的引文能否逐字回到原文")
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
