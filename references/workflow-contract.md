# 执行与文件契约

以下命令中的 `SKILL` 为安装目录，`RUN` 为一次研究的独立目录。命令不调用另一个 LLM；当前代理负责证据阅读与内容生成。

## 命令与时间规则

```bash
SKILL=/actual/path/to/gene-research-review
RUN=/actual/path/to/project/work/mettl3-immune-review

python3 "$SKILL/scripts/research.py" init --run-dir "$RUN" \
  --gene METTL3 --topic 'tumor immune escape' --years 2021:2026 \
  --concept 'immune escape|immune evasion|immunosuppression|checkpoint|PD-L1' \
  --concept 'cancer|tumor|tumour|neoplasm'
python3 "$SKILL/scripts/research.py" search --run-dir "$RUN" --enrich-preprints
python3 "$SKILL/scripts/research.py" status --run-dir "$RUN"
```

`--concept` 可重复：各参数之间 AND，`|` 分隔的同义词之间 OR。词组由代理按研究语境构建，不直接拼接整段提示语。NCBI Gene 解析生成基因名/别名组，默认解析人类但不将人类限定加入文献查询。实际查询以运行记录为准，保留可复现原式。

时间选一种：`--years 2021:2026`、`--last-years 5`（未指定时的默认值）、或者 `--start YYYY-MM-DD --end YYYY-MM-DD`。明确范围优先于自然语言“近 5 年”；冲突范围须解决，不能两套混用。结束日期截至检索日。`--as-of YYYY-MM-DD` 可固定历史检索截止日，未来截止日仍被截断。记录原始日期、日期精度与筛选口径；正式出版和预印本版本日期不要混为一谈。

默认基因解析物种为 `--taxon 9606`；改用其他物种必须符合用户语境。这只是 Gene 名称解析，不在文献查询中强制物种过滤。默认不在最终综述引用窗外论文；只有用户明确允许经典背景/窗外依据时，初始化加 `--allow-background-refs`，在正文方法中声明其作用。反查旧论文不需要该开关，但不得混入核心数量。

```bash
# 仅连通性测试；在独立测试 RUN 中运行，不截断正式运行。
python3 "$SKILL/scripts/research.py" search --run-dir "$RUN" --limit 5

# 阅读特定关键论文；paper ID 从实际 papers.jsonl 取得。
python3 "$SKILL/scripts/research.py" fulltext --run-dir "$RUN" --paper-id P0123456789ab
# --paper-id 可重复；需要时可用 --all。

# 反查创新点，查询须针对候选重新构建；范围可早于核心时间窗。
python3 "$SKILL/scripts/research.py" gap-search --run-dir "$RUN" --gap-id G1 \
  --pubmed-query '(METTL3[tiab]) AND (immune evasion[tiab] OR immune escape[tiab])' \
  --europepmc-query 'TITLE_ABS:METTL3 AND (TITLE_ABS:"immune evasion" OR TITLE_ABS:"immune escape")' \
  --start 1900-01-01 --end 2026-10-19
```

上例反查结束日期只是演示；运行时替换为实际 as-of 日期。首轮反查用 PubMed Title/Abstract 与 Europe PMC TITLE_ABS 明确字段；Europe PMC 无字段查询可能搜索全文，显著增加无关命中。需要扩大到全文时明确记录此目的。先检查近邻论文再缩小或扩展检索式，不把过窄的零命中结果当成新颖性证据。分页完整不代表每条记录已被阅读；记录实际筛选范围，未阅读结果不能支撑“未发现”的结论。

代理写完证据、全景和候选文件后：

```bash
python3 "$SKILL/scripts/research.py" verify --run-dir "$RUN"
python3 "$SKILL/scripts/research.py" checkpoint --run-dir "$RUN"
# 此处结束本轮，展示报告并等待用户；不得直接执行下一行。

# 只有真实用户选定后，填入真实选择原话。
python3 "$SKILL/scripts/research.py" select --run-dir "$RUN" \
  --gap G2 --user-selection '我选择第 2 个创新点作为综述主线'

# 代理完成 manuscript.md 和 claims.jsonl 后：
python3 "$SKILL/scripts/research.py" verify --run-dir "$RUN"
python3 "$SKILL/scripts/research.py" validate --run-dir "$RUN" --final
python3 "$SKILL/scripts/research.py" export --run-dir "$RUN"
```

检索失败时看状态及错误后恢复，不删除已有数据重新来过。`run.json` 为配置和状态权威；状态顺序为 `initialized → retrieved → awaiting_selection → selected → draft_ready → complete`，其中 `selected_gap_id` 只能由真实用户选择设置。命令检查不通过时修复对应来源/文件；不要直接编辑状态绕过门槛。

`verify` 默认检查证据表、候选最近邻和稿件实际使用的文献；`--paper-id ID` 可重复指定，`--all` 才核验整个检索语料。默认获取最新身份与已知出版状态；`--cached` 仅用于离线测试。若有更正，代理阅读通知后可在对应 audit 记录添加 `notice_reviewed: true` 与 `notice_review_note`；再次核验只在文献身份及通知集合均未改变时保留该记录。撤稿或存疑通知不能作为最终事实论断的支持。

`checkpoint` 与最终核验要求核心检索无截断、各配置数据库最新分页日志完整且计数一致。完整重跑可覆盖旧的失败或限量记录；创新点反查不能替代核心检索。题录仅有 `metadata` 深度时可留在语料库，但不能充当实质证据。部分或不明日期须在文献增加 `date_scope_review: {eligible: true, rationale: "基于真实来源的日期判断"}` 才能引用；不能据此虚构时间归属。

## 运行文件

| 文件 | 写入者与用途 |
|---|---|
| `run.json` | CLI：研究参数、日期、阶段与用户选择 |
| `papers.jsonl` | CLI：去重后的规范文献，稳定 ID 与来源 |
| `search_log.jsonl` / `.cache/` | CLI：实际数据库请求、分页、返回量、错误及缓存响应；不作为额外独立数据库计数 |
| `fulltext_log.jsonl` / `fulltext/` | CLI：全文获取日志与保存的可核对文本 |
| `gap_searches.jsonl` | CLI：创新点反查，含 `query_id, source, query, reported_count, retrieved_count, complete, errors` |
| `evidence.jsonl` | 代理：可追溯发现、原文位置、证据深度与语境 |
| `landscape.md` | 代理：中文研究方向、证据全景与实际覆盖范围 |
| `gaps.json` / `gaps.md` | 代理：结构化创新点与中文选择报告 |
| `manuscript.md` / `claims.jsonl` | 选题后由代理：英文主稿及论断—证据映射 |
| `reference_audit.json` | CLI：身份、题录匹配与已知出版状态核验 |
| `review.md` / `review.docx` | CLI：同一主稿生成的编号引文 Markdown 与 Word |
| `evidence.csv` / `references.bib` | CLI：证据表和本综述已引用文献的 BibTeX |

文献 `corpus_role` 为 `core` 或 `gap_verification`。同一文献已在核心检索找到时仍保留核心身份；反查补充文献不混计近年研究数量。窗外文献可影响候选排序和阶段报告；最终综述引用窗外文献还须符合用户许可与 `--allow-background-refs` 设置。

## 数据结构与最小例子

以下 `P0123456789ab` 是格式占位 ID，**不是检索结果**。所有例子必须替换为实际运行的 paper、evidence 与 query ID；quote 必须替换为真实原文。JSONL 每行一个完整 JSON 对象；不可加入 Markdown 代码围栏。未报告的语境用空字符串或准确说明，不能推测。

文献的规范字段为：`paper_id, title, authors, year, doi, pmid, pmcid, publication_date, abstract, source_depth, is_preprint, related_dois, sources, provenance, corpus_role`，日期还保留 `date_precision` 和 `dates`，有合法全文时另含 `full_text_path`。以脚本实际生成的数据为准，不手工构造文献以填充语料。`sources` 和 `provenance` 保留多数据库记录；DOI、PMID、版本关联不应被后续覆盖丢失。

`evidence.jsonl`（一条记录的字段示例）：

```json
{"evidence_id":"E1","paper_id":"P0123456789ab","direction":"Tumor-intrinsic immune regulation","claim":"Replace with the finding supported by the actual source.","source_depth":"abstract","locator":"Abstract, sentence 3","quote":"Replace with a short verbatim excerpt from the actual source.","mechanism":"","pathway":"","cancer_type":"","cell_type":"","organism":"","experiment_type":"","support":"supported"}
```

`source_depth` 只接受 `abstract` 或 `full_text`；仅题录不能生成实质发现。`support` 为 `supported|partial|unsupported|unassessable`。`full_text` 要有实际可读来源及定位，不能只因论文有 PMCID 就标为读过全文。

`gaps.json` 是数组，而不是带外层 `gaps` 键的对象：

```json
[
  {
    "gap_id": "G1",
    "title": "Replace with an evidence-grounded candidate",
    "question": "Replace with a specific unanswered question",
    "evidence_ids": ["E1"],
    "nearest_paper_ids": ["P0123456789ab"],
    "missing_evidence": "What the nearest study did not establish",
    "validation_idea": "A concrete test that distinguishes competing explanations",
    "novelty": {"score": 3, "rationale": "Compare with the retrieved nearest work"},
    "feasibility": {
      "score": 3,
      "rationale": "Explain the main experimental dependency",
      "resource_assumptions": "State the assumed model, assay, cohort and facility access"
    },
    "confidence": "medium",
    "disconfirmation": {
      "query_ids": ["replace-with-real-gap-query-id"],
      "conclusion": "Describe the actual counter-search result and its limits"
    }
  }
]
```

`claims.jsonl`：

```json
{"claim_id":"C1","text":"The corresponding factual statement in the manuscript.","paper_ids":["P0123456789ab"],"evidence_ids":["E1"],"required_depth":"abstract"}
```

`required_depth` 可省略或为 `abstract|full_text`。正文引用相同稳定 ID：`The accurately scoped finding is described here [@P0123456789ab].` 多文献用各自引文标记。代理不得把示例发现或未核验题录写成最终综述。

## 交付与恢复验收

- `landscape.md` 包含实际窗口、查询截至日期、来源覆盖、去重计数、证据深度和独立研究支持的方向。
- `gaps.md` 的候选可回指 JSON、证据和反查；无足够证据时宁可减少候选，保留不确定性。
- `awaiting_selection` 时只交阶段报告；恢复后用真实用户选择继续，不能绕过选择门槛。
- 正文默认 4,000–6,000 英文词，引用必须存在于规范语料且可回指相应证据。显式测试可在 `init` 使用 `--word-min N --word-max M` 缩短词数，不改变真实运行默认要求。
- Markdown 与 Word 来自同一主稿，参考文献身份和编号一致。检查 Word 能打开、正文与参考文献齐全；不把只生成 Markdown 当作双格式完成。
