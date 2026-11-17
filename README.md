# drawing-review · 施工图审查 Skill

让 WorkBuddy、Claude、Codex 等支持 `SKILL.md` 的 AI 助手按固定审图流程审查施工图 PDF（也可以同时放入计算书、勘察报告 PDF 做交叉核对），并生成带原图定位和证据分级的审查报告。

审图口径：只报能用图纸原文证明的问题；比较前先确认是同一对象；缺前提就写“需补充条件”；数值由脚本复算；规范结论只用条文已核对的规则；“未发现”不等于“合格”。

## 它做什么

```
PDF ──extract──▶ 剔除图签 + 脱敏 ──▶ AI 按清单审查 ──▶ 自我反驳复核 ──verify──▶ E1 引文回原位 / E2 对象对应 / E3 复算 ──report──▶ 审查报告.md
                     │                    ▲
                     └──render / zoom─────┘  （可选：模型能看图时，给它本机遮盖后的页面图）
```

- **图签、单位、人名不审查，也不交给模型**：脚本在本机识别图签并整块剔除；正文里的单位名、工程名称、电话、证书编号替换为占位符（［单位1］［工程名称1］…）。原文对照只保存在本机 `mapping.local.json`。
- **审查清单**：C1–C9 设计说明与通用检查（参数一致性、构件前后一致、防水做法、书写、引用版本、混凝土最低强度等级 GB 55008-2021 2.0.2、编号、跨页矛盾、自相矛盾）；C10–C15 图面与文件交叉复核（几何与标高、配筋一致性、计算书与图纸对应、勘察参数取值、算式复算、施工阶段工况）。
- **证据核验**：块编号全局唯一（`F1-P3-B12` = 文件 1 第 3 页第 12 块）。引文必须逐字出现在所写块或其后连续的块；只在别的页/文件找到的不算定位，降为“待查线索”。跨页/跨文件比较必须声明对象对应，否则降为“需补充条件”。计算类问题由脚本独立复算。
- **有没有看图能力都能用**：
  - 文字模式（默认）：短标注附带页面坐标，`near` 命令列出某个标注周围的文字，帮模型判断标注属于哪个构件。
  - 图像模式（可选，需用户同意）：`render` 渲染整页、`zoom` 局部放大，图签区域、脱敏文字、疑似印章先在本机涂黑，并画上页面坐标网格；看图读到但文字层里找不到的数值只能作为“需补充条件”。
- **报告**：概况、疑似问题（页码、页面坐标、对象、证据分级、复算过程、可选的遮盖后原图截图）、需补充条件、待查线索、未覆盖范围、人工复核栏。

## 安装

把 `drawing-review/` 文件夹放进助手的 skills 目录：

| 助手 | 目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/drawing-review/`（或在 WorkBuddy 技能管理中从本仓库导入） |
| Claude Code | `~/.claude/skills/drawing-review/` |
| 其他兼容 SKILL.md 的助手 | 按其文档放入 skills 目录 |

```bash
git clone <本仓库地址> drawing-review-skill
cp -r drawing-review-skill/drawing-review ~/.workbuddy/skills/
pip install -r drawing-review-skill/requirements.txt   # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

需要 Python 3.9+。依赖只有 pdfplumber（MIT），它会一起装上 pypdfium2（Apache-2.0/BSD-3）和 Pillow，图像模式用它们渲染；不使用 AGPL 的 PyMuPDF。只读取 PDF：DWG/DXF 请先在 CAD 中导出矢量 PDF。

## 使用

对助手说：

> 用 drawing-review 审查这几份文件：`D:\项目\结构总说明.pdf`、`D:\项目\主体结构计算书.pdf`，重点看混凝土、防水参数和计算书与图纸是否对应，生成审查报告。

助手会依次运行 `extract → 审查 → verify → report`，最后给出 `review_pack/审查报告.md`。也可以手动运行脚本：

```bash
S=drawing-review/scripts/drawing_review.py
python $S extract 图纸.pdf 计算书.pdf --out review_pack
python $S near --pack review_pack --block F1-P3-B12          # 看某个标注周围的文字
python $S calc "As_s(25,150)" "1.375*300"                     # 复算
python $S render 图纸.pdf --pack review_pack --pages F1-P3     # 可选：遮盖后的页面图
python $S zoom 图纸.pdf --pack review_pack --page F1-P3 --region 150,180,620,480
# ……由 AI 读取 review_pack/review_input.md 并写出 review_pack/findings.json ……
python $S verify --pack review_pack --findings review_pack/findings.json
python $S evidence 图纸.pdf 计算书.pdf --pack review_pack      # 可选：问题截图
python $S report --pack review_pack
```

## 边界

- 文字模式只读取 PDF 的**文字层**；扫描页、字体无法解码的页标为“未审查”，不做 OCR。只有图名、少量标注的图形页标为“以图形为主”，图形内容未逐项审查。
- 图像模式的识读只作辅助，结论仍须引用文字层；扫描页默认不渲染。CAD 把文字炸成线条导出时，这些字只能靠图签区域遮盖，给云端模型看图前请先在本机抽查 `review_pack/images/`。
- 结果是 AI 辅助自查候选，须设计人员逐条确认，不是正式审图意见或合规结论。C10–C15 尚未在真实样本上量化误报率。
- 模型通常运行在云端：脱敏后的文字（图像模式下还有遮盖后的页面图）会发给你在助手中配置的模型。涉密图纸请先确认单位的保密要求，或使用本地部署模型。
- 不包含任何规范全文；规范是否现行须以官方来源核实。

## 开发

```bash
pip install pdfplumber reportlab pypdf
python -m unittest discover -s tests -v
```

测试只使用合成 PDF（虚构单位与人名），不含真实工程数据。缺少 reportlab 或 pdfplumber 时相应用例会跳过。

## 许可

MIT
