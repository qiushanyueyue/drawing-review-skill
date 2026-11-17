# findings.json 与审查报告格式

## 结论分级

| decision | 含义 | 进入报告的位置 |
|---|---|---|
| `suspected` | 疑似问题：有逐字引文、能回原图、对象对应明确，且经过自我反驳仍成立。**不是违规定论** | 二、疑似问题 |
| `needs_context` | 需补充条件：缺构件类别、年限、抗震等级、适用范围、版本核实等前提，或对象对应只是推断、只有视觉识读，未下结论 | 三、需补充条件 |
| `not_checked` | 该检查项本次未执行或该参数未与规范比对 | 五、核查范围与限制 |
| `lead` | 待查线索：由 `verify` 自动设置（引文无法回到所写位置，或复算否定了所指错误） | 四、待查线索 |

severity：`high`（低于已核对规范最低要求、结构安全相关参数矛盾、计算书结论配筋大于图纸实配）/ `medium`（参数、构件、做法前后矛盾，引用废止标准，算式错误）/ `low`（表达、书写、编号问题）/ `info`。级别按**工程后果**定，不按置信度定。

## 证据分级（verify 自动给出，写在 `evidence` 字段）

| 层次 | 检验内容 | 可能结果 |
|---|---|---|
| E1 原文存在 | 引文是否逐字出现在所写块，或从所写块起按阅读顺序连续的几块 | `pass` / `fail` / `vision_only` / `vision_corroborated` |
| E2 对象对应 | 跨页/跨文件比较是否声明 `object_match: explicit`；图像证据框是否覆盖引文位置 | `single_page` / `explicit` / `inferred` / `unknown` |
| E3 结论支撑 | 复算是否证实数值不一致；是否来自已核对规则 | `calc_mismatch_confirmed` / `calc_matches_document` / `verified_rule` / `n/a` |

只有 E1 通过、E2 明确（或同页）、E3 不否定时才保留为 `suspected`。

## findings.json

```json
{
  "focus": "某出入口 2-2 断面：图纸与计算书对应",
  "image_mode": false,
  "rules_run": ["C1 说明内部参数一致性", "C12 计算书与图纸对应", "C14 计算书算式复算", "C5 引用版本（已联网核实 3 项，未核实 1 项）"],
  "findings": [
    {
      "id": "F1",
      "rule": "C1",
      "category": "说明内部参数矛盾",
      "title": "顶板抗渗等级前后不一致",
      "problem": "材料第1条顶板抗渗等级P10，第2条顶板为P8。",
      "suggestion": "核对顶板抗渗等级取值",
      "severity": "medium",
      "decision": "suspected",
      "confidence": 0.85,
      "check_method": "text",
      "object": "主体结构顶板",
      "basis": "两处均明确写“顶板”，同一说明“材料”节，非不同工况",
      "quotes": [
        {"block": "F1-P3-B12", "text": "顶板、底板、侧墙混凝土强度等级C35，抗渗等级P10"},
        {"block": "F1-P3-B13", "text": "顶板混凝土强度等级C40，抗渗等级P8"}
      ]
    },
    {
      "id": "F2",
      "rule": "C14",
      "category": "计算书算式",
      "title": "剪力计算结果与代入值不符",
      "problem": "计算书按所列公式和代入值复算剪力约为 412.5 kN，文件结果写 385.0 kN。",
      "suggestion": "核对该处剪力计算与后续引用",
      "severity": "medium",
      "decision": "suspected",
      "confidence": 0.9,
      "check_method": "calc",
      "object": "2-2 断面 底板支座",
      "object_match": "explicit",
      "basis": "算式、代入值与结果在同一段落连续给出",
      "calculation": [{"expr": "1.375*300", "stated": 385.0, "unit": "kN", "note": "按计算书所列公式 V=γ·q·l/2 的代入值"}],
      "quotes": [{"block": "F2-P46-B8", "text": "V=1.375×300=385.0kN"}]
    },
    {
      "id": "F3",
      "rule": "C11",
      "category": "配筋一致性",
      "title": "顶板下层钢筋与剖面不一致",
      "problem": "平面配筋图顶板下层筋标Φ25@150，2-2剖面同位置标Φ25@200。",
      "suggestion": "核对顶板下层钢筋间距",
      "severity": "medium",
      "decision": "suspected",
      "confidence": 0.75,
      "check_method": "vision",
      "object": "2-2 断面 顶板下层横向钢筋",
      "object_match": "explicit",
      "basis": "两处引出线均指向顶板下层，剖切号 2-2 与平面图剖切位置一致",
      "uncertainties": ["平面图该区域另有附加筋标注，已排除"],
      "quotes": [
        {"block": "F1-P8-B41", "text": "Φ25@150"},
        {"block": "F1-P12-B17", "text": "Φ25@200"}
      ],
      "visual_evidence": [
        {"page": "F1-P8", "bbox": [610, 420, 760, 480], "reads": "Φ25@150", "image": "F1-P8-z1"},
        {"page": "F1-P12", "bbox": [300, 210, 420, 260], "reads": "Φ25@200", "image": "F1-P12-z1"}
      ]
    }
  ]
}
```

字段要求：
- `quotes[].text` **逐字复制**审查材料中块编号后的文字（可截取其中一段，至少 4 个字），`block` 填块编号（`F文件-P页-B块`）。跨块引文的 `block` 填第一块。跨页矛盾至少两条引文。
- `check_method`：`text`（文字比对）/ `spatial`（用块坐标、`near` 判断归属）/ `vision`（看图辅助）/ `calc`（独立复算）/ `rule`（已核对规则）。
- `object`：问题所指的构件/断面/部位（脱敏后的写法）；`object_match`：`explicit` / `inferred` / `unknown`。跨页、跨文件比较不写 `explicit` 会被降为“需补充条件”。
- `calculation[]`：`expr` 复算式（`calc` 支持的写法，先统一单位），`stated` 文件中写的结果（没有就省略，只展示复算值），`unit`，`tolerance`（相对偏差，默认 0.01），`note`。
- `visual_evidence[]`：`page`（如 `F1-P8`）、`bbox`（页面点 [x0,y0,x1,y1]，读图片上的网格数字）、`reads`（看到的文字，看不清就不写）、`image`（图片编号）。框要同时覆盖相关引文所在位置。
- `title` ≤ 20 字；`problem` ≤ 80 字；`suggestion` ≤ 40 字，只说应核对什么，不替设计人员下结论。
- `confidence` 0–1；低于 0.7 的一般不要写入，除非是 `needs_context`。
- `norm.verified` 只有在 verified-rules.md 内或已联网核对官方原文时才为 `true`。
- `review_status`：`pending`（默认）/ `confirmed` / `dismissed` / `fixed_pending_recheck`，复查时沿用用户的决定。
- 不要在任何字段里写还原后的单位、人员、工程名称。

## verify 后新增的字段

`located`（all / partial / none / vision）、`quotes[].how`（exact / exact-multi / relocated-same-page / relocated_candidate / ambiguous-same-page / not_found）、`quotes[].blocks`（文件、页、坐标、文件 SHA-256）、`quotes[].candidates`、`visual_evidence[].status`（corroborated / vision_only / region_only / invalid）、`calculation[].computed / deviation / status`、`evidence`、`decision_original`、`verify_notes`、`merged_ids`、`scope: out_of_scope`。不要手工修改 `verified.json`。

## 报告结构（report 命令生成）

1. 审查概况：文件代号、推测类型、页数、已审查/以图形为主/未审查页数、SHA-256、审查重点、执行的检查项、图像模式、结果计数、脱敏统计。
2. 疑似问题：汇总表（含核查方式）+ 每条详情（结论、对象、问题、原文引文与定位、图面证据、复算、证据分级、判断依据、规范、缺少的条件、不确定因素、建议核对、原图截图、人工处理状态）。
3. 需补充条件。
4. 待查线索。
5. 核查范围与限制：未审查页与以图形为主的页（合并为页码区间）、文字偏少页、图签识别提示、疑似人名残留、未执行的检查项、固定限制声明。
6. 检查项与版本、核验统计。
7. 人工复核记录表（空白，由设计人员填写）。

报告首行固定声明：AI 辅助自查候选，须设计人员逐条确认，不是正式审图意见或合规结论。不要删除这句话，也不要在报告任何位置写“审查合格”“符合规范”。

坐标说明：页面点（1/72 英寸），左上角为原点 [x0, y0, x1, y1]；用 pypdf 抽取时为近似范围。
