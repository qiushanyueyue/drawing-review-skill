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
        blocks = [{"id": "P1-b1", "sheet": "P1", "text": "顶板厚度 800mm", "file": "x", "bbox": None}]
        idx = {b["id"]: b for b in blocks}
        self.assertEqual(dr.locate({"block": "P1-b1", "text": "800"}, blocks, idx)[0], [])
        self.assertTrue(dr.locate({"block": "P1-b9", "text": "顶板厚度800mm"}, blocks, idx)[0])


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


if __name__ == "__main__":
    unittest.main()
