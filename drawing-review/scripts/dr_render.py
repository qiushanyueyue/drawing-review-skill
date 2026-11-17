"""可选图像模式：在本机把 PDF 页面渲染成图片，先遮盖图签与敏感文字，再交给有看图能力的模型。

- 渲染用 pypdfium2（Apache-2.0/BSD-3，pdfplumber 的依赖），绘制用 Pillow；不使用 AGPL 的 PyMuPDF。
- 坐标一律用页面点（1/72 英寸，左上角原点），与 blocks.jsonl 相同；图片上画了带页面坐标刻度的网格，
  模型可以直接读出区域坐标，用 zoom 放大，或写进 findings 的 visual_evidence。
- 遮盖来源（redactions.json，只有坐标）：图签区域与图签文字、出图章文字、被脱敏替换过的文字块、疑似人名块、
  小尺寸嵌入图片（多为印章/签名）。本页没识别出图签时：同一文件同尺寸页面识别到的图签位置一并遮盖；
  仍没有且文件推测为图纸时，按页面四周保守遮盖（右侧、底部、左侧边条）。
- 局限：CAD 把文字炸成线条（SHX 字体）时文字层里没有这些字，只能靠图签区域遮盖；扫描页默认不渲染。
"""
from __future__ import annotations

import json
from pathlib import Path

from drawing_review import BLOCK_ID, PAGE_ID, block_page, load_blocks, parse_region, sha256_file

# 页面四周保守遮盖（占页宽/页高的比例）：右侧图签栏、底部图签栏、左侧会签栏
CONSERVATIVE = [(0.78, 0.0, 1.0, 1.0), (0.0, 0.86, 1.0, 1.0), (0.0, 0.0, 0.06, 1.0)]


def _deps():
    try:
        import pypdfium2 as pdfium  # type: ignore
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError:
        raise SystemExit("图像模式需要 pdfplumber（会同时安装 pypdfium2 和 Pillow）：pip install pdfplumber")
    return pdfium, Image, ImageDraw, ImageFont


def _load(pack: Path):
    cov = json.loads((pack / "coverage.json").read_text(encoding="utf-8"))
    red_path = pack / "redactions.json"
    if not red_path.exists():
        raise SystemExit("review_pack 里没有 redactions.json：请用 1.1.0 及以上版本重新 extract")
    return cov, json.loads(red_path.read_text(encoding="utf-8"))


def _match_files(files: list[str], cov: dict) -> dict[str, tuple[Path, dict]]:
    """按 SHA-256 把用户给的 PDF 对应到 extract 时的文件代号，防止拿错版本的图纸渲染。"""
    by_sha = {c["sha256"]: c for c in cov["files"] if c.get("status") == "ok"}
    out = {}
    for raw in files:
        path = Path(raw)
        c = by_sha.get(sha256_file(path))
        if not c:
            raise SystemExit(f"{path.name} 与 extract 时的任何文件都不一致（SHA-256 不同），请用同一版本的 PDF")
        if any(not str(p.get("extractor", "")).startswith("pdfplumber") for p in c.get("sheets", [])):
            raise SystemExit("该文件是用 pypdf 抽取的（坐标为近似值），无法准确遮盖：请 pip install pdfplumber 后重新 extract")
        out[c["file_id"]] = (path, c)
    return out


def _sheet(c: dict, pid: str) -> dict | None:
    return next((p for p in c.get("sheets", []) if p.get("page") == pid), None)


def mask_boxes(pid: str, c: dict, redactions: dict) -> tuple[list[list[float]], str]:
    """返回（需要涂黑的框，遮盖方式）。"""
    p = _sheet(c, pid)
    boxes = [r["bbox"] for r in redactions.get(pid, [])]
    if p.get("title_block_regions"):
        return boxes, "title_block"
    W, H = p["width"], p["height"]
    sib = [r for q in c.get("sheets", []) if q.get("title_block_bboxes") and abs(q["width"] - W) < 2 and abs(q["height"] - H) < 2
           for r in q["title_block_bboxes"]]
    if sib:
        return boxes + sib, "sibling_title_block"
    if c.get("doc_kind_guess", "图纸") == "图纸" or p.get("status") == "not_checked":
        return boxes + [[fx0 * W, fy0 * H, fx1 * W, fy1 * H] for fx0, fy0, fx1, fy1 in CONSERVATIVE], "conservative"
    return boxes, "text_only"


MASKING_CN = {"title_block": "已识别图签区域 + 敏感文字", "sibling_title_block": "本页未识别图签，按同文件同尺寸页的图签位置遮盖 + 敏感文字",
              "conservative": "未识别图签，按页面四周保守遮盖 + 敏感文字（可能遮住部分图面）", "text_only": "无图签（计算书/报告类），只遮盖敏感文字"}


def _render(doc, sheet: dict, scale: float, region: list[float] | None = None):
    page = doc[int(sheet["sheet"][1:]) - 1]
    pw, ph = page.get_size()
    if abs(pw - sheet["width"]) > 1 or abs(ph - sheet["height"]) > 1:
        return None   # 渲染坐标与抽取坐标不一致时不出图，避免遮盖错位
    crop = (0, 0, 0, 0)
    if region:
        x0, y0, x1, y1 = region
        crop = (x0, ph - y1, pw - x1, y0)
    return page.render(scale=scale, crop=crop).to_pil().convert("RGB")


def _paint(img, boxes, scale, origin, ImageDraw, pad=1.5):
    d = ImageDraw.Draw(img)
    ox, oy = origin
    for x0, y0, x1, y1 in boxes:
        d.rectangle([(x0 - pad - ox) * scale, (y0 - pad - oy) * scale, (x1 + pad - ox) * scale, (y1 + pad - oy) * scale], fill=(0, 0, 0))


def _nice_step(span: float) -> float:
    for s in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000):
        if s >= span / 12:
            return s
    return 5000


def _font(ImageFont, size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # Pillow < 10.1
        return ImageFont.load_default()


def _grid(img, scale, region, ImageDraw, ImageFont):
    """画页面坐标网格（页面点），并在上边、左边标数字，便于模型读出区域坐标。"""
    x0, y0, x1, y1 = region
    step = _nice_step(max(x1 - x0, y1 - y0))
    d = ImageDraw.Draw(img, "RGBA")
    font = _font(ImageFont, max(12, int(img.width / 110)))
    v = (x0 // step + 1) * step
    while v < x1:
        X = (v - x0) * scale
        d.line([(X, 0), (X, img.height)], fill=(0, 120, 255, 70), width=1)
        d.rectangle([X + 1, 1, X + 6 + len(str(int(v))) * font.size * 0.62, font.size + 4], fill=(255, 255, 255, 220))
        d.text((X + 3, 2), str(int(v)), fill=(0, 70, 200, 255), font=font)
        v += step
    v = (y0 // step + 1) * step
    while v < y1:
        Y = (v - y0) * scale
        d.line([(0, Y), (img.width, Y)], fill=(0, 120, 255, 70), width=1)
        d.rectangle([1, Y + 1, 6 + len(str(int(v))) * font.size * 0.62, Y + font.size + 4], fill=(255, 255, 255, 220))
        d.text((3, Y + 2), str(int(v)), fill=(0, 70, 200, 255), font=font)
        v += step
    return step


def _manifest(pack: Path) -> dict:
    path = pack / "images.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"note": "页面点坐标、左上角原点；网格数字即页面坐标。图片已在本机遮盖图签与敏感信息（黑框）。", "images": []}


def _save_manifest(pack: Path, man: dict):
    (pack / "images.json").write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_render(args):
    pdfium, Image, ImageDraw, ImageFont = _deps()
    pack = Path(args.pack)
    cov, red = _load(pack)
    files = _match_files(args.files, cov)
    wanted = {m.group(0) for m in PAGE_ID.finditer(args.pages or "")}
    (pack / "images").mkdir(exist_ok=True)
    man = _manifest(pack)
    man["images"] = [im for im in man["images"] if im.get("kind") != "page" or (wanted and im["page"] not in wanted)]
    done, skipped = [], []
    for fid, (path, c) in files.items():
        doc = pdfium.PdfDocument(str(path))
        for p in c.get("sheets", []):
            pid = p["page"]
            if wanted and pid not in wanted:
                continue
            if p.get("status") == "not_checked" and not args.allow_unmasked_pages:
                if wanted:
                    skipped.append({"page": pid, "reason": "扫描/乱码页无法识别图签，默认不渲染（需用户同意后加 --allow-unmasked-pages）"})
                continue
            boxes, how = mask_boxes(pid, c, red)
            scale = min(args.max_px / max(p["width"], p["height"]), 200 / 72)
            img = _render(doc, p, scale)
            if img is None:
                skipped.append({"page": pid, "reason": "渲染坐标与文字坐标不一致（页面裁剪框异常），为避免遮盖错位未渲染"})
                continue
            _paint(img, boxes, scale, (0, 0), ImageDraw)
            step = _grid(img, scale, [0, 0, p["width"], p["height"]], ImageDraw, ImageFont)
            rel = f"images/{pid}.png"
            img.save(pack / rel, optimize=True)
            man["images"].append({"id": pid, "kind": "page", "page": pid, "path": rel, "region_pt": [0, 0, round(p["width"], 1), round(p["height"], 1)],
                                  "px_per_pt": round(scale, 4), "grid_step_pt": step, "masked_boxes": len(boxes), "masking": MASKING_CN[how],
                                  "status": p.get("status")})
            done.append(pid)
        doc.close()
    _save_manifest(pack, man)
    print(json.dumps({"rendered": len(done), "pages": done, "skipped": skipped, "manifest": str(pack / "images.json"),
                      "next": "看图后需要细节时：zoom --page <页> --region x0,y0,x1,y1（读网格数字得到坐标）"}, ensure_ascii=False, indent=2))


def cmd_zoom(args):
    pdfium, Image, ImageDraw, ImageFont = _deps()
    pack = Path(args.pack)
    cov, red = _load(pack)
    files = _match_files(args.files, cov)
    pm = PAGE_ID.search(args.page or "")
    if not pm or pm.group(0).split("-")[0] not in files:
        raise SystemExit(f"找不到页 {args.page}，或没有提供该页所在的 PDF")
    pid = pm.group(0)
    path, c = files[pid.split("-")[0]]
    p = _sheet(c, pid)
    if p is None:
        raise SystemExit(f"找不到页 {pid}")
    if p.get("status") == "not_checked":
        raise SystemExit("该页无法识别图签（扫描/乱码页），不做局部放大")
    x0, y0, x1, y1 = parse_region(args.region)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(p["width"], x1), min(p["height"], y1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        raise SystemExit("区域太小或在页面外")
    scale = min(args.dpi / 72, args.max_px / max(x1 - x0, y1 - y0))
    doc = pdfium.PdfDocument(str(path))
    img = _render(doc, p, scale, [x0, y0, x1, y1])
    doc.close()
    if img is None:
        raise SystemExit("渲染坐标与文字坐标不一致，为避免遮盖错位未渲染")
    boxes, how = mask_boxes(pid, c, red)
    _paint(img, boxes, scale, (x0, y0), ImageDraw)
    step = _grid(img, scale, [x0, y0, x1, y1], ImageDraw, ImageFont)
    man = _manifest(pack)
    n = 1 + sum(1 for im in man["images"] if im.get("kind") == "zoom" and im["page"] == pid)
    rel = f"images/{pid}-z{n}.png"
    (pack / "images").mkdir(exist_ok=True)
    img.save(pack / rel, optimize=True)
    entry = {"id": f"{pid}-z{n}", "kind": "zoom", "page": pid, "path": rel, "region_pt": [round(v, 1) for v in (x0, y0, x1, y1)],
             "px_per_pt": round(scale, 4), "grid_step_pt": step, "masking": MASKING_CN[how]}
    man["images"].append(entry)
    _save_manifest(pack, man)
    blocks = [b for b in load_blocks(pack) if block_page(b) == pid and b.get("bbox")
              and x0 <= (b["bbox"][0] + b["bbox"][2]) / 2 <= x1 and y0 <= (b["bbox"][1] + b["bbox"][3]) / 2 <= y1]
    entry["text_blocks_in_region"] = [f"[{b['id']}] {b['text']}" for b in blocks[:60]]
    print(json.dumps(entry, ensure_ascii=False, indent=2))


def cmd_evidence(args):
    """为核验后的疑似问题/需补充条件生成原图截图：遮盖后的局部图，红框标出引文和图面证据位置。"""
    pdfium, Image, ImageDraw, ImageFont = _deps()
    pack = Path(args.pack)
    cov, red = _load(pack)
    files = _match_files(args.files, cov)
    ver = json.loads(Path(args.verified or pack / "verified.json").read_text(encoding="utf-8"))
    (pack / "evidence").mkdir(exist_ok=True)
    for old in (pack / "evidence").glob("*.png"):
        old.unlink()
    made = []
    docs = {fid: pdfium.PdfDocument(str(path)) for fid, (path, _) in files.items()}
    for f in ver["findings"]:
        if f.get("scope") == "out_of_scope" or f.get("decision") not in ("suspected", "needs_context"):
            continue
        per_page: dict[str, list[list[float]]] = {}
        for q in f.get("quotes", []):
            for b in q.get("blocks", []):
                if b.get("bbox") and BLOCK_ID.match(b["id"]):
                    per_page.setdefault(b["id"].rsplit("-", 1)[0], []).append(b["bbox"])
        for v in f.get("visual_evidence", []):
            if v.get("status") != "invalid" and v.get("bbox"):
                per_page.setdefault(v["page"], []).append(v["bbox"])
        for k, (pid, hl) in enumerate(per_page.items(), start=1):
            fid = pid.split("-")[0]
            if fid not in files:
                continue
            c = files[fid][1]
            p = _sheet(c, pid)
            if p is None or p.get("status") == "not_checked":
                continue
            W, H = p["width"], p["height"]
            x0, y0 = max(0, min(b[0] for b in hl) - 60), max(0, min(b[1] for b in hl) - 60)
            x1, y1 = min(W, max(b[2] for b in hl) + 60), min(H, max(b[3] for b in hl) + 60)
            if x1 - x0 < 200:
                x0, x1 = max(0, (x0 + x1) / 2 - 100), min(W, (x0 + x1) / 2 + 100)
            if y1 - y0 < 150:
                y0, y1 = max(0, (y0 + y1) / 2 - 75), min(H, (y0 + y1) / 2 + 75)
            scale = min(200 / 72, 1400 / max(x1 - x0, y1 - y0))
            img = _render(docs[fid], p, scale, [x0, y0, x1, y1])
            if img is None:
                continue
            boxes, _ = mask_boxes(pid, c, red)
            _paint(img, boxes, scale, (x0, y0), ImageDraw)
            d = ImageDraw.Draw(img)
            for bx0, by0, bx1, by1 in hl:
                d.rectangle([(bx0 - 2 - x0) * scale, (by0 - 2 - y0) * scale, (bx1 + 2 - x0) * scale, (by1 + 2 - y0) * scale], outline=(220, 0, 0), width=3)
            name = f"{f['id']}.png" if k == 1 else f"{f['id']}-{k}.png"
            img.save(pack / "evidence" / name, optimize=True)
            made.append(name)
    for doc in docs.values():
        doc.close()
    print(json.dumps({"evidence_images": made, "dir": str(pack / "evidence"), "next": "重新运行 report，报告会引用这些截图"}, ensure_ascii=False, indent=2))
