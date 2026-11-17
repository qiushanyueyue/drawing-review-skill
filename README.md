# drawing-review · 施工图审查 Skill

让 WorkBuddy、Claude、Codex 等支持 `SKILL.md` 的 AI 助手按固定审图流程审查施工图 PDF，并生成带原图定位的审查报告。

审图口径：只报能用图纸原文证明的问题；缺前提就写“需补充条件”；规范结论只用条文已核对的规则；“未发现”不等于“合格”。

## 它做什么

```
PDF ──extract──▶ 剔除图签 + 脱敏 ──▶ AI 按清单审查 ──▶ 自我反驳复核 ──verify──▶ 引文逐字回原图 ──report──▶ 审查报告.md
```

- **图签、单位、人名不审查，也不交给模型**：脚本在本机识别图签并整块剔除；正文里的单位名、工程名称、电话、证书编号替换为占位符（［单位1］［工程名称1］…）。原文对照只保存在本机 `mapping.local.json`。
- **审查清单**（C1–C9）：说明内部参数一致性、构件前后一致、防水做法、表达与书写、规范/图集引用版本、已核对规范数值规则（混凝土最低强度等级 GB 55008-2021 2.0.2）、说明编号、跨页参数矛盾、自相矛盾语句。
- **引文核验**：模型给出的每段引文都要逐字回到原文块，找不到的自动降为“待查线索”，避免编造。
- **报告**：概况、疑似问题（含页码与页面坐标）、需补充条件、待查线索、未覆盖范围、人工复核栏。

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

需要 Python 3.9+。只读取 PDF：DWG/DXF 请先在 CAD 中导出矢量 PDF。

## 使用

对助手说：

> 用 drawing-review 审查这份图纸：`D:\项目\结构总说明.pdf`，重点看混凝土和防水参数，生成审查报告。

助手会依次运行 `extract → 审查 → verify → report`，最后给出 `review_pack/审查报告.md`。也可以手动运行脚本：

```bash
python drawing-review/scripts/drawing_review.py extract 图纸.pdf --out review_pack
# ……由 AI 读取 review_pack/review_input.md 并写出 review_pack/findings.json ……
python drawing-review/scripts/drawing_review.py verify --pack review_pack --findings review_pack/findings.json
python drawing-review/scripts/drawing_review.py report --pack review_pack
```

## 边界

- 只读取 PDF 的**文字层**。扫描件、纯图形页、字体无法解码的页标为“未审查”，不做 OCR；图形、线条、尺寸几何不做判定。
- 结果是 AI 辅助自查候选，须设计人员逐条确认，不是正式审图意见或合规结论。
- 模型通常运行在云端：脱敏后的设计说明文字会发给你在助手中配置的模型。涉密图纸请先确认单位的保密要求，或使用本地部署模型。
- 不包含任何规范全文；规范是否现行须以官方来源核实。

## 开发

```bash
pip install pdfplumber reportlab
python -m unittest discover -s tests -v
```

测试只使用合成 PDF（虚构单位与人名），不含真实工程数据。

## 许可

MIT
