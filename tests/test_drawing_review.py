"""drawing_review.py 的回归测试：合成 PDF（虚构单位、人名、工程名，不含任何真实工程数据）。

运行：python -m unittest discover -s tests -v
端到端用例需要 reportlab 生成带文字层的 PDF，缺少时跳过；其余用例只依赖标准库。
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "drawing-review" / "scripts"))
import drawing_review as dr  # noqa: E402

try:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
except ImportError:
    rl_canvas = None

FAKE_COMPANY = "虚构市示例设计研究院有限公司"
FAKE_OWNER = "虚构市轨道交通建设有限公司"
FAKE_PROJECT = "虚构市轨道交通九号线示例站主体结构"
FAKE_PEOPLE = ["张三丰", "李四", "王小五", "赵六"]


def has_pdfplumber():
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


def make_pdf(path: Path):
    """A3 横向图纸样式：左侧设计说明，右下角图签，左侧竖排会签栏；第 2 页几乎无文字（模拟扫描页）。"""
    c = rl_canvas.Canvas(str(path), pagesize=(1190, 842))
    c.setFont("STSong-Light", 10)
    lines = ["一、工程概况",
             f"1.本工程为{FAKE_PROJECT}，设计单位：{FAKE_COMPANY}。",
             "2.设计使用年限100年，结构安全等级一级。",
             "二、材料",
             "1.主体结构顶板、底板、侧墙混凝土强度等级C35，抗渗等级P10。",
             "2.顶板混凝土强度等级C40，抗渗等级P8。",
             "3.钢筋采用HRB400，保护层厚度：迎土面50mm，背土面35mm。",
             "4.执行《混凝土结构设计规范》GB 50010-2010（2015年版）。",
             "联系电话：13800138000"] + [f"{i}.其他说明第{i}条：施工应符合相关规范要求。" for i in range(5, 20)]
    for i, line in enumerate(lines):
        c.drawString(40, 800 - i * 16, line)
    c.setFont("STSong-Light", 7)
    rows = [("建设单位", FAKE_OWNER), ("设计单位", FAKE_COMPANY), ("工程名称", FAKE_PROJECT),
            ("设计", FAKE_PEOPLE[0]), ("校对", FAKE_PEOPLE[1]), ("审核", FAKE_PEOPLE[2]), ("审定", FAKE_PEOPLE[3]),
            ("图名", "结构设计总说明（一）"), ("图号", "JG-001")]
    for i, (a, b) in enumerate(rows):
        c.drawString(960, 170 - i * 14, a)
        c.drawString(1010, 170 - i * 14, b)
    c.drawString(1100, 40, "出图专用章")
    c.saveState()
    c.translate(20, 300)
    c.rotate(90)   # 逆时针 90° 的竖排会签栏
    c.drawString(0, 0, "建筑 结构 通风空调 给排水 动力配电")
    c.restoreState()
    c.showPage()
    c.setFont("STSong-Light", 10)
    c.drawString(40, 800, "图")
    c.showPage()
    # 第 3 页：以图形为主的配筋图，只有少量标注文字 + 同样的图签
    c.setFont("STSong-Light", 8)
    for x, y, t in ((200, 600, "KL1(2) 300×700"), (420, 600, "C35"), (200, 420, "顶板厚800"), (420, 420, "Φ25@150")):
        c.drawString(x, y, t)
    c.rect(180, 380, 400, 260)
    c.setFont("STSong-Light", 7)
    for i, (a, b) in enumerate(rows[:-2] + [("图名", "顶板配筋图"), ("图号", "JG-003")]):
        c.drawString(960, 170 - i * 14, a)
        c.drawString(1010, 170 - i * 14, b)
    c.save()


@unittest.skipIf(rl_canvas is None, "需要 reportlab 生成测试 PDF")
class PdfPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pdf = cls.tmp / f"{FAKE_PROJECT}.pdf"
        make_pdf(cls.pdf)
        (cls.tmp / "x.dwg").write_bytes(b"not a pdf")
        cls.pack = cls.tmp / "pack"
        dr.main(["extract", str(cls.pdf), str(cls.tmp / "x.dwg"), "--out", str(cls.pack)])
        cls.review = (cls.pack / "review_input.md").read_text(encoding="utf-8")
        cls.cov = json.loads((cls.pack / "coverage.json").read_text(encoding="utf-8"))
        cls.blocks = dr.load_blocks(cls.pack)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_sensitive_text_in_review_material(self):
        material = self.review + (self.pack / "blocks.jsonl").read_text(encoding="utf-8") + (self.pack / "coverage.json").read_text(encoding="utf-8")
        for secret in [FAKE_COMPANY, FAKE_OWNER, FAKE_PROJECT, *FAKE_PEOPLE, "13800138000", "出图专用章"]:
            self.assertNotIn(secret, material, secret)

    def test_title_block_excluded_and_sheet_label_kept(self):
        sheet = self.cov["files"][0]["sheets"][0]
        self.assertEqual(sheet["title_block_regions"], 1)
        self.assertEqual(sheet["sheet_no"], "JG-001")
        self.assertEqual(sheet["sheet_title"], "结构设计总说明（一）")

    def test_body_text_kept_for_review(self):
        self.assertIn("顶板混凝土强度等级C40，抗渗等级P8。", self.review)
        self.assertIn("GB 50010-2010", self.review)
        self.assertIn("本工程为［工程名称", self.review)
        self.assertIn("设计单位：［", self.review)

    def test_scanned_page_and_non_pdf_not_checked(self):
        files = self.cov["files"]
        self.assertEqual(files[0]["sheets"][1]["status"], "not_checked")
        self.assertEqual(files[1]["status"], "not_checked")
        self.assertIn("只读取 PDF", files[1]["reason"])

    @unittest.skipUnless(has_pdfplumber(), "竖排文字需要 pdfplumber")
    def test_rotated_text_read_in_order(self):
        rotated = [b["text"] for b in self.blocks if "rotated" in b["flags"]]
        self.assertIn("给排水", rotated)

    def test_mapping_is_local_only(self):
        mapping = json.loads((self.pack / "mapping.local.json").read_text(encoding="utf-8"))
        self.assertIn(FAKE_COMPANY, mapping.values())
        self.assertEqual(self.cov["files"][0]["file"], "图纸1.pdf")

    def test_verify_and_report(self):
        b_c35 = next(b for b in self.blocks if "C35" in b["text"])
        b_c40 = next(b for b in self.blocks if "C40" in b["text"])
        findings = {"focus": "结构设计说明", "rules_run": ["C1 说明内部参数一致性"], "findings": [
            {"id": "F1", "rule": "C1", "category": "说明内部矛盾", "title": "顶板混凝土强度与抗渗等级前后不一致",
             "problem": "材料第1条顶板为C35/P10，第2条顶板为C40/P8。", "suggestion": "核对顶板强度与抗渗等级",
             "severity": "medium", "decision": "suspected", "confidence": 0.9,
             "quotes": [{"block": b_c35["id"], "text": "顶板、底板、侧墙混凝土强度等级C35，抗渗等级P10"},
                        {"block": b_c40["id"], "text": "顶板混凝土强度等级C40，抗渗等级P8"}]},
            {"id": "F2", "rule": "C1", "title": "顶板强度等级矛盾", "problem": "重复候选", "severity": "low", "decision": "suspected",
             "confidence": 0.6, "quotes": [{"block": b_c40["id"], "text": "顶板混凝土强度等级C40"}]},
            {"id": "F3", "rule": "C2", "title": "编造的引文", "problem": "x", "decision": "suspected", "confidence": 0.8,
             "quotes": [{"block": b_c40["id"], "text": "顶板混凝土强度等级C50，抗渗等级P12"}]},
            {"id": "F4", "rule": "C5", "title": "图签人员签字不全", "problem": "［人员1］未签字", "decision": "suspected", "confidence": 0.8,
             "quotes": [{"block": "P1-b1", "text": "［人员1］"}]},
        ]}
        fpath = self.pack / "findings.json"
        fpath.write_text(json.dumps(findings, ensure_ascii=False), encoding="utf-8")
        dr.main(["verify", "--pack", str(self.pack), "--findings", str(fpath)])
        ver = json.loads((self.pack / "verified.json").read_text(encoding="utf-8"))
        byid = {f["id"]: f for f in ver["findings"]}
        self.assertEqual(byid["F1"]["located"], "all")
        self.assertIn("F2", byid["F1"].get("merged_ids", []))
        self.assertEqual(byid["F3"]["decision"], "lead")
        self.assertEqual(byid["F4"]["scope"], "out_of_scope")
        dr.main(["report", "--pack", str(self.pack)])
        report = (self.pack / "审查报告.md").read_text(encoding="utf-8")
        self.assertIn("顶板混凝土强度与抗渗等级前后不一致", report)
        self.assertIn("待查线索", report)
        self.assertIn("未审查 1 页（P2）", report)
        self.assertIn("以图形为主 1 页（P3）", report)
        self.assertIn("证据分级", report)
        self.assertNotIn("图签人员签字不全", report)
        for secret in [FAKE_COMPANY, FAKE_PROJECT, *FAKE_PEOPLE]:
            self.assertNotIn(secret, report)


class Helpers(unittest.TestCase):
    def test_sheet_ranges(self):
        self.assertEqual(dr.sheet_ranges(["P3", "P4", "P5", "P9"]), "P3–P5、P9")

    def test_masker_patterns(self):
        m = dr.Masker([])
        out = m.mask("由虚构市示例设计院负责，电话 010-12345678，证书编号：A123456789，邮箱 a@b.cn")
        for secret in ["虚构市示例设计院", "010-12345678", "A123456789", "a@b.cn"]:
            self.assertNotIn(secret, out)
        self.assertIn("由［单位1］负责", out)

    def test_masker_keeps_generic_terms(self):
        m = dr.Masker([])
        text = "施工单位应按设计要求施工，张拉控制应力详见说明。"
        self.assertEqual(m.mask(text), text)

    def test_extra_terms(self):
        m = dr.Masker(["示例站"])
        self.assertEqual(m.mask("示例站顶板厚800mm"), "［敏感词1］顶板厚800mm")

    def test_locate_rejects_short_or_missing(self):
        blocks = [{"id": "F1-P1-B1", "file_id": "F1", "sheet": "P1", "text": "顶板厚度 800mm", "file": "x", "bbox": None}]
        idx = {b["id"]: b for b in blocks}
        self.assertEqual(dr.locate({"block": "F1-P1-B1", "text": "800"}, blocks, idx)[0], [])
        hit, how, _ = dr.locate({"block": "F1-P1-B9", "text": "顶板厚度800mm"}, blocks, idx)
        self.assertEqual(how, "relocated-same-page")


class OverviewNotTitleBlock(unittest.TestCase):
    def test_project_overview_units_are_masked_not_dropped(self):
        rows = ["建设单位：" + FAKE_OWNER, "设计单位：" + FAKE_COMPANY, "监理单位：另一虚构监理有限公司", "施工单位：待定",
                "本工程主体结构顶板混凝土强度等级C35。"]
        blocks = [dr.Block(f"P1-b{i}", "x", "P1", t, [40, 100 + i * 16, 400, 110 + i * 16], "pdf-text", height=10) for i, t in enumerate(rows)]
        meta = dr.mark_title_blocks(blocks)
        self.assertEqual(meta["P1"]["title_block_regions"], 0)
        self.assertFalse(any(b.in_title_block for b in blocks))
        m = dr.Masker([])
        masked = [m.mask(b.text) for b in blocks]
        self.assertNotIn(FAKE_OWNER, "".join(masked))
        self.assertEqual(masked[-1], rows[-1])


class Garbled(unittest.TestCase):
    def test_repeated_char_page_flagged(self):
        blocks = [dr.Block(f"P1-b{i}", "x", "P1", "本本本本本本本本本本C35本本本本P8本", [0, 0, 1, 1], "pdf-text") for i in range(3)]
        blocks.append(dr.Block("P2-b1", "x", "P2", "本工程主体结构顶板混凝土强度等级C35，抗渗等级P10，侧墙同顶板。", [0, 0, 1, 1], "pdf-text"))
        self.assertEqual(dr.garbled_sheets(blocks), {"P1"})


def has_render():
    try:
        import pdfplumber, pypdfium2, PIL  # noqa: F401
        return True
    except ImportError:
        return False


def blk(fid, page, j, text, bbox, h=10):
    return {"id": f"{fid}-P{page}-B{j}", "file_id": fid, "file": f"图纸{fid[1:]}.pdf", "sheet": f"P{page}",
            "text": text, "bbox": bbox, "height": h, "flags": []}


def write_pack(tmp: Path, blocks: list[dict], pages=((1, 1),), size=(1190, 842)) -> Path:
    """合成 review_pack：blocks.jsonl + coverage.json（不经过 PDF）。pages 为 (文件序号, 页码)。"""
    pack = tmp / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    with (pack / "blocks.jsonl").open("w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(b, ensure_ascii=False) + "\n")
    files = {}
    for fi, pg in pages:
        files.setdefault(fi, []).append({"sheet": f"P{pg}", "page": f"F{fi}-P{pg}", "width": size[0], "height": size[1], "status": "ok"})
    cov = {"tool": "test", "files": [{"file_id": f"F{fi}", "file": f"图纸{fi}.pdf", "format": "pdf", "sha256": "0" * 64, "status": "ok",
                                      "sheets": sh} for fi, sh in files.items()],
           "blocks_excluded_title_block_or_stamp": 0, "masked": {}, "scope_note": ""}
    (pack / "coverage.json").write_text(json.dumps(cov, ensure_ascii=False), encoding="utf-8")
    return pack


def run_verify(pack: Path, findings: list[dict]) -> dict:
    fp = pack / "findings.json"
    fp.write_text(json.dumps({"findings": findings}, ensure_ascii=False), encoding="utf-8")
    dr.main(["verify", "--pack", str(pack), "--findings", str(fp)])
    return {f["id"]: f for f in json.loads((pack / "verified.json").read_text(encoding="utf-8"))["findings"]}


class StrictLocate(unittest.TestCase):
    """引文定位：只在所写块或其相邻、按顺序、空间连续的块中算定位成功。"""

    def setUp(self):
        self.blocks = [
            blk("F1", 1, 1, "1.主体结构顶板混凝土强度等级C35，", [40, 100, 400, 110]),
            blk("F1", 1, 2, "抗渗等级P10，侧墙同顶板。", [40, 116, 300, 126]),
            blk("F1", 1, 3, "侧墙保护层厚度50mm。", [40, 600, 300, 610]),      # 远处，不连续
            blk("F1", 1, 4, "施工应符合相关规范要求。", [40, 700, 300, 710]),
            blk("F1", 1, 5, "施工应符合相关规范要求。", [40, 716, 300, 726]),  # 同页重复文字
            blk("F2", 1, 1, "底板混凝土强度等级C40。", [40, 100, 300, 110]),
        ]
        self.idx = {b["id"]: b for b in self.blocks}

    def loc(self, block, text):
        return dr.locate({"block": block, "text": text}, self.blocks, self.idx)

    def test_exact_and_ordered_multi_block(self):
        self.assertEqual(self.loc("F1-P1-B1", "顶板混凝土强度等级C35")[1], "exact")
        hit, how, _ = self.loc("F1-P1-B1", "强度等级C35，抗渗等级P10")
        self.assertEqual(how, "exact-multi")
        self.assertEqual([b["id"] for b in hit], ["F1-P1-B1", "F1-P1-B2"])

    def test_reversed_or_distant_fragments_rejected(self):
        self.assertEqual(self.loc("F1-P1-B2", "抗渗等级P10，侧墙同顶板。1.主体结构")[0], [])
        self.assertEqual(self.loc("F1-P1-B2", "侧墙同顶板。侧墙保护层厚度50mm")[0], [])

    def test_two_column_flow(self):
        blocks = [blk("F1", 1, 1, "1.顶板混凝土强度等级C35，", [40, 100, 300, 110]),
                  blk("F1", 1, 2, "5.施工缝设置钢板止水带，", [600, 100, 860, 110]),
                  blk("F1", 1, 3, "抗渗等级P10。", [40, 116, 200, 126]),
                  blk("F1", 1, 4, "钢板厚度3mm。", [600, 116, 760, 126])]
        idx = {b["id"]: b for b in blocks}
        hit, how, _ = dr.locate({"block": "F1-P1-B1", "text": "强度等级C35，抗渗等级P10"}, blocks, idx)
        self.assertEqual((how, [b["id"] for b in hit]), ("exact-multi", ["F1-P1-B1", "F1-P1-B3"]))
        hit, how, _ = dr.locate({"block": "F1-P1-B4", "text": "钢板止水带，钢板厚度3mm"}, blocks, idx)
        self.assertEqual((how, [b["id"] for b in hit]), ("exact-multi", ["F1-P1-B2", "F1-P1-B4"]))

    def test_distant_labels_on_same_line_split(self):
        def w(t, x0, x1, top=100):
            return {"text": t, "x0": x0, "x1": x1, "top": top, "bottom": top + 8}
        lines = dr.group_lines([w("KL1(2)", 200, 226), w("300×700", 230, 262), w("C35", 420, 434), w("顶板厚800", 200, 240, 300)])
        self.assertEqual([ln["text"] for ln in lines], ["KL1(2) 300×700", "C35", "顶板厚800"])

    def test_ambiguous_same_page(self):
        hit, how, cands = self.loc("F1-P1-B9", "施工应符合相关规范要求")
        self.assertEqual((hit, how), ([], "ambiguous-same-page"))
        self.assertEqual(len(cands), 2)

    def test_other_file_is_only_candidate(self):
        hit, how, cands = self.loc("F1-P1-B1", "底板混凝土强度等级C40")
        self.assertEqual((hit, how), ([], "relocated_candidate"))
        self.assertEqual([b["id"] for b in cands], ["F2-P1-B1"])


class VerifyEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        blocks = [
            blk("F1", 1, 1, "顶板混凝土强度等级C35，抗渗等级P10。", [40, 100, 400, 110]),
            blk("F1", 1, 2, "KL1(2) 300×700", [500, 300, 560, 308], h=8),
            blk("F1", 1, 3, "Φ25@150", [500, 312, 540, 320], h=8),
            blk("F1", 2, 1, "顶板混凝土强度等级C40，抗渗等级P8。", [40, 100, 400, 110]),
            blk("F2", 1, 1, "顶板实配钢筋面积As=3272mm²/m", [40, 100, 400, 110]),
        ]
        self.pack = write_pack(self.tmp, blocks, pages=((1, 1), (1, 2), (2, 1)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def finding(self, fid, **kw):
        base = {"id": fid, "rule": "C8", "title": f"测试{fid}", "problem": "x", "decision": "suspected", "confidence": 0.9}
        base.update(kw)
        return base

    def test_cross_page_needs_explicit_object_match(self):
        q = [{"block": "F1-P1-B1", "text": "顶板混凝土强度等级C35"}, {"block": "F1-P2-B1", "text": "顶板混凝土强度等级C40"}]
        res = run_verify(self.pack, [self.finding("A", quotes=q)])
        self.assertEqual(res["A"]["decision"], "needs_context")
        self.assertIn("对象对应关系未确认", "".join(res["A"]["missing"]))
        res = run_verify(self.pack, [self.finding("B", quotes=q, object_match="explicit")])
        self.assertEqual(res["B"]["decision"], "suspected")
        self.assertEqual(res["B"]["evidence"]["E2"], "explicit")

    def test_quote_in_wrong_file_becomes_lead(self):
        res = run_verify(self.pack, [self.finding("A", quotes=[{"block": "F1-P1-B1", "text": "顶板实配钢筋面积As=3272"}])])
        self.assertEqual(res["A"]["decision"], "lead")
        self.assertEqual(res["A"]["quotes"][0]["how"], "relocated_candidate")
        self.assertEqual(res["A"]["evidence"]["E1"], "fail")

    def test_calc_mismatch_confirms_and_match_refutes(self):
        q = [{"block": "F2-P1-B1", "text": "顶板实配钢筋面积As=3272mm²/m"}]
        res = run_verify(self.pack, [
            self.finding("A", rule="C14", check_method="calc", quotes=q, calculation=[{"expr": "As_s(25,150)", "stated": 3000, "unit": "mm2/m"}]),
            self.finding("B", rule="C14", title="另一复算", check_method="calc", confidence=0.8, quotes=[{"block": "F1-P1-B2", "text": "KL1(2) 300×700"}],
                         calculation=[{"expr": "As_s(25,150)", "stated": 3272, "unit": "mm2/m"}]),
        ])
        self.assertEqual(res["A"]["decision"], "suspected")
        self.assertEqual(res["A"]["evidence"]["E3"], "calc_mismatch_confirmed")
        self.assertEqual(res["B"]["decision"], "lead")
        self.assertEqual(res["B"]["calculation"][0]["status"], "match")

    def test_visual_evidence(self):
        res = run_verify(self.pack, [
            self.finding("A", check_method="vision", object_match="explicit", quotes=[{"block": "F1-P1-B3", "text": "Φ25@150"}],
                         visual_evidence=[{"page": "F1-P1", "bbox": [490, 290, 580, 330], "reads": "Φ25@150"}]),
            self.finding("B", title="仅视觉", check_method="vision", visual_evidence=[{"page": "F1-P1", "bbox": [600, 600, 700, 700], "reads": "Φ28@100"}]),
            self.finding("C", title="框不覆盖引文", check_method="vision", object_match="explicit", confidence=0.85,
                         quotes=[{"block": "F1-P1-B1", "text": "顶板混凝土强度等级C35"}],
                         visual_evidence=[{"page": "F1-P1", "bbox": [490, 290, 580, 330], "reads": "KL1"}]),
            self.finding("D", title="坐标越界", check_method="vision", visual_evidence=[{"page": "F1-P1", "bbox": [0, 0, 5000, 10]}]),
        ])
        self.assertEqual(res["A"]["decision"], "suspected")
        self.assertEqual(res["A"]["visual_evidence"][0]["status"], "corroborated")
        self.assertTrue(res["A"]["evidence"]["spatial_support"])
        self.assertEqual(res["B"]["decision"], "needs_context")
        self.assertEqual(res["B"]["evidence"]["E1"], "vision_only")
        self.assertEqual(res["C"]["decision"], "needs_context")
        self.assertEqual(res["D"]["decision"], "lead")

    def test_near(self):
        blocks = dr.load_blocks(self.pack)
        res = dr.near_blocks(blocks, block="F1-P1-B2")
        self.assertEqual(res[0]["id"], "F1-P1-B3")
        self.assertEqual(res[0]["dir"], "下")
        self.assertNotIn("F1-P2-B1", [r["id"] for r in res])
        inside = dr.near_blocks(blocks, page="F1-P1", region=[480, 280, 600, 340])
        self.assertEqual([r["id"] for r in inside], ["F1-P1-B2", "F1-P1-B3"])


class Calc(unittest.TestCase):
    def test_values(self):
        sys.path.insert(0, str(ROOT / "drawing-review" / "scripts"))
        import dr_calc
        self.assertAlmostEqual(dr_calc.evaluate("As(25,6)"), 2945.24, places=1)
        self.assertAlmostEqual(dr_calc.evaluate("As_s(25, 150)"), 3272.49, places=1)
        self.assertAlmostEqual(dr_calc.evaluate("1.3×120+1.5×80"), 276.0)
        self.assertAlmostEqual(dr_calc.evaluate("rho(3272, 1000, 750)"), 0.0043627, places=6)
        for bad in ("__import__('os').system('echo')", "open('x')", "9**9**9", "1/0", "x+1", "(1).real"):
            with self.assertRaises(ValueError, msg=bad):
                dr_calc.evaluate(bad)


class TitleBlockWarnings(unittest.TestCase):
    def test_body_like_text_inside_title_block_flagged(self):
        rows = [("设计", "甲"), ("校对", "乙"), ("审核", "丙"), ("图号", "JG-01")]
        blocks = []
        for i, (a, b) in enumerate(rows):
            blocks.append(dr.Block(f"F1-P1-B{2 * i + 1}", "x", "P1", a, [900, 700 + i * 12, 920, 708 + i * 12], "pdf-text", height=8, file_id="F1"))
            blocks.append(dr.Block(f"F1-P1-B{2 * i + 2}", "x", "P1", b, [940, 700 + i * 12, 980, 708 + i * 12], "pdf-text", height=8, file_id="F1"))
        for k in range(3):
            blocks.append(dr.Block(f"F1-P1-B{20 + k}", "x", "P1", f"{k + 1}.顶板混凝土强度等级C35，施工缝处应设置止水钢板。",
                                   [860, 690 + k * 10, 1000, 698 + k * 10], "pdf-text", height=8, file_id="F1"))
        meta = dr.mark_title_blocks(blocks)["P1"]
        self.assertEqual(meta["title_block_regions"], 1)
        self.assertGreaterEqual(meta["title_block_body_like"], 3)


@unittest.skipIf(rl_canvas is None or not has_render(), "图像模式需要 reportlab + pdfplumber（pypdfium2、Pillow）")
class ImageMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pdf = cls.tmp / "a.pdf"
        make_pdf(cls.pdf)
        cls.pack = cls.tmp / "pack"
        dr.main(["extract", str(cls.pdf), "--out", str(cls.pack)])
        dr.main(["render", str(cls.pdf), "--pack", str(cls.pack)])
        cls.man = json.loads((cls.pack / "images.json").read_text(encoding="utf-8"))
        cls.red = json.loads((cls.pack / "redactions.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def open(self, rel):
        from PIL import Image
        return Image.open(self.pack / rel).convert("RGB")

    def test_pages_rendered_scanned_skipped(self):
        pages = sorted(im["page"] for im in self.man["images"] if im["kind"] == "page")
        self.assertEqual(pages, ["F1-P1", "F1-P3"])

    def test_title_block_and_masked_text_painted_black(self):
        im = next(i for i in self.man["images"] if i["page"] == "F1-P1")
        img = self.open(im["path"])
        s = im["px_per_pt"]
        boxes = [r for r in self.red["F1-P1"] if r["why"] in ("title_block_text", "masked_text", "stamp_text")]
        self.assertTrue(boxes)
        for r in boxes:
            x0, y0, x1, y1 = r["bbox"]
            for fx, fy in ((0.25, 0.5), (0.5, 0.5), (0.75, 0.5)):
                px = img.getpixel((int((x0 + (x1 - x0) * fx) * s), int((y0 + (y1 - y0) * fy) * s)))
                self.assertEqual(px, (0, 0, 0), r)
        body = dr.load_blocks(self.pack)
        b = next(b for b in body if "顶板混凝土强度等级C40" in b["text"])
        crop = img.crop([int(v * s) for v in b["bbox"]])
        self.assertGreater(len(crop.getcolors(maxcolors=1 << 20) or []), 2)   # 正文保留可读，没有被涂黑

    def test_zoom_and_wrong_file(self):
        dr.main(["zoom", str(self.pdf), "--pack", str(self.pack), "--page", "F1-P3", "--region", "150,180,620,480", "--dpi", "300"])
        man = json.loads((self.pack / "images.json").read_text(encoding="utf-8"))
        z = next(i for i in man["images"] if i["kind"] == "zoom")
        img = self.open(z["path"])
        self.assertLessEqual(max(img.size), 2400)
        self.assertAlmostEqual(img.size[0] / img.size[1], 470 / 300, delta=0.05)
        other = self.tmp / "other.pdf"
        rl = rl_canvas.Canvas(str(other))
        rl.drawString(10, 10, "x")
        rl.save()
        with self.assertRaises(SystemExit):
            dr.main(["render", str(other), "--pack", str(self.pack)])

    def test_evidence_crop(self):
        blocks = dr.load_blocks(self.pack)
        b = next(b for b in blocks if "Φ25@150" in b["text"])
        fp = self.pack / "findings.json"
        fp.write_text(json.dumps({"findings": [{"id": "F9", "rule": "C11", "title": "配筋标注待核", "problem": "x", "decision": "needs_context",
                                                "confidence": 0.7, "quotes": [{"block": b["id"], "text": "Φ25@150"}]}]}, ensure_ascii=False), encoding="utf-8")
        dr.main(["verify", "--pack", str(self.pack), "--findings", str(fp)])
        dr.main(["evidence", str(self.pdf), "--pack", str(self.pack)])
        self.assertTrue((self.pack / "evidence" / "F9.png").exists())
        dr.main(["report", "--pack", str(self.pack)])
        self.assertIn("evidence/F9.png", (self.pack / "审查报告.md").read_text(encoding="utf-8"))


@unittest.skipIf(rl_canvas is None or not has_render(), "需要 reportlab + pdfplumber")
class RotatedPageAlignment(unittest.TestCase):
    def test_mask_covers_text_on_rotated_pages(self):
        from pypdf import PdfReader, PdfWriter
        from PIL import ImageDraw
        import pypdfium2 as pdfium
        sys.path.insert(0, str(ROOT / "drawing-review" / "scripts"))
        import dr_render
        tmp = Path(tempfile.mkdtemp())
        try:
            base = tmp / "base.pdf"
            c = rl_canvas.Canvas(str(base), pagesize=(600, 400))
            c.setFont("STSong-Light", 20)
            c.drawString(400, 300, "MARK标记")
            c.save()
            for rot in (0, 90, 270):
                r = PdfReader(str(base))
                w = PdfWriter()
                pg = r.pages[0]
                pg.rotate(rot)
                w.add_page(pg)
                path = tmp / f"r{rot}.pdf"
                w.write(str(path))
                blocks, info = dr.extract_pdf(path, "x.pdf", "F1")
                box = [blocks[0].bbox]
                doc = pdfium.PdfDocument(str(path))
                img = dr_render._render(doc, info[0], 2.0)
                self.assertIsNotNone(img, rot)
                dark = img.convert("L").point(lambda v: 255 if v < 128 else 0)
                self.assertIsNotNone(dark.getbbox(), rot)
                dr_render._paint(img, box, 2.0, (0, 0), ImageDraw)
                gray = img.convert("L")
                # 涂黑后，框外不应还有文字的深色像素
                outside = gray.point(lambda v: 255 if v < 128 else 0)
                x0, y0, x1, y1 = [int(v * 2) for v in box[0]]
                outside.paste(0, (max(0, x0 - 4), max(0, y0 - 4), x1 + 4, y1 + 4))
                self.assertIsNone(outside.getbbox(), rot)
                doc.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
