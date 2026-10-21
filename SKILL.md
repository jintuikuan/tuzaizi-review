---
name: tuzaizi-review
description: Orchestrate bundled AIpoch skills for gene-centered literature searches, evidence landscapes and research gaps, pause for the user's direction selection, then invoke K-Dense literature-review to write and verify a narrative review. Use for gene + date window + disease or mechanism research and selected-topic review continuation.
---

# 基因调研与综述总控

由本技能调度两套上游的真实子技能：选题前执行 AIpoch 检索策略、证据全景和创新点工作流，交付中文候选方向并暂停；收到用户选择后，执行 K-Dense `literature-review` 撰写英文机制导向叙述性综述（默认正文 4,000–6,000 词），再执行 AIpoch 论断核验，交付 Markdown、Word、文献库和证据表。

## 调度约定

- 将实际技能目录记为 `SKILL`，独立研究目录记为 `RUN`。先读[上游调度契约](references/upstream-routing.md)；输入输出和数据格式见[文件契约](references/workflow-contract.md)，证据与评分见[证据标准](references/evidence-rubrics.md)。
- 必用子技能及原始资源已随本技能放在 `upstream/`，锁定提交与逐文件 SHA256 见 `upstream/manifest.json`。不依赖另行安装整个上游仓库。资源缺失或校验失败要报告并修复，不能悄悄退回“仅参考上游”的独立模式。
- 各阶段先运行 `research.py skills begin`，读取它返回的真实上游 `SKILL.md`，并按原文加载需要的 `references/`。代理实际遵循规程完成任务后写执行说明，再运行 `skills finish`。`plan`、下载、展示原文或 `loaded` 日志本身都不算完成调用。
- Python 命令负责可靠的数据获取、核验和断点；科学分析与写作由执行上游技能的代理完成。它们不是后台运行的另一个模型。不要伪造完成报告或将回执视为科学正确性的证明。
- `research.py --help` 及各子命令的 `--help` 是参数接口。允许通常的自动技能发现。

## 1. 解析并调用检索策略技能

1. 解析基因、疾病/机制、日期。缺省人类基因名称，但保留动物模型文献。NCBI Gene 优先、Ensembl 后备；无法消除的基因歧义才询问。
2. “近五年”按运行日滚动回溯；明确 `2021–2026` 是 2021-01-01 至运行日与 2026-12-31 中较早者，不称为精确五年。概念组内 OR、组间 AND，不把整段中文提示作查询。
3. `init` 建立带编排约定的新运行，然后 `skills begin --phase search`，实际执行 AIpoch `biomedical-search-strategy-builder`；检查概念、MeSH、别名和日期，并写 `search_strategy.md`。不确定的 MeSH 用自由词并说明。
4. 如上游策略优化了查询，用 `set-queries` 保存最终的 PubMed/Europe PMC 精确检索式。完成 search 回执后执行 `search --enrich-preprints`，核对分页、命中数和错误。`--limit` 只做连通性测试，不能进入正式选题。
5. 去重并关联预印本与正式报告；对关键证据运行 `fulltext`，仅合法开放全文。区分题录、摘要、全文及阅读深度，不补造正文细节。

## 2. 调用全景与创新点技能

1. `skills begin --phase landscape`：执行 `disease-mechanism-evidence-map` 与 `biomarker-landscape-scanner` 的适用模式。逐篇提取 `evidence.jsonl`，按机制、通路、癌种、细胞、物种和实验层级写 `landscape.md`。生物标志物用途不适用时记录适用性判断，不把机制证据包装为临床成熟度。
2. `skills begin --phase gaps`：执行 `medical-research-gap-finder`、`medical-topic-saturation-and-whitespace-checker`、`contradictory-findings-resolver` 和 `method-gap-detector`。依据真实语料识别饱和、矛盾和方法缺口，排除伪空白；条件不适用也须说明判断依据。
3. 每个候选用 `gap-search` 做针对性反证，必要时向时间窗之前追溯，标作 `gap_verification`。写 `gaps.json` / `gaps.md`：支持证据、最近邻、缺失证据、验证思路、新颖度/可行性 1–5 分及资源假设、置信度、反查范围。正式可选方向只纳入 medium/high confidence；低置信线索另列。
4. 通常归纳 3–5 个方向，证据不足则减少。无可靠候选时交付不可判定结果并等待重定范围；不凑数，不把未检到写成“全球首次”。排除含模拟数据的新颖度评分器。
5. 每阶段实际完成后记录 `skills finish`。执行记录保留所用技能版本、应用说明和产物摘要；仍须通过本技能独立的证据、分页与反查门槛。

## 3. 保存断点，必须等用户选题

运行 `verify` 和 `checkpoint`；前述三个上游阶段缺回执、资源损坏、受限/失败检索或证据不完整时，不得通过。成功后状态为 `awaiting_selection`。

向用户展示候选编号与中文报告，要求选择，随后结束本轮。**选题前不得开始 review / claims 写作阶段，不代选，不生成完整综述。** 上游“推荐一个最佳方向”只能作为建议，不能转为用户选择。

## 4. 用户选择后调用综述技能

1. 收到“继续，选第 2 个方向”等真实指令后，先 `status` 恢复同一 `RUN`，再 `select --gap G2 --user-selection '用户本次原话'`。不重跑无关检索；更换基因或日期则新建运行。
2. `skills begin --phase review` 现在才可启动；实际读取、执行 K-Dense `literature-review` 及其 `references/core_workflow.md`、`references/search_and_citation.md` 等所需资源。传入已选问题、既有语料、检索日志和证据表；只补检选中方向。
3. 按上游的范围界定、筛选、质量评价和主题综合规程写 `manuscript.md` 与 `claims.jsonl`。围绕所选主线组织背景、主要机制、矛盾与空白、研究议程和转化展望；避免逐篇罗列。默认英文正文 4,000–6,000 词，方法中写真实检索范围。已有发现、模型推断和待验证假设分开。
4. 保留稳定引文 `[@paper_id]`。完成 review 回执，再 `skills begin --phase claims`，实际执行 AIpoch `paper-to-claim-verifier`，逐条审阅主张、证据层级、原文位置和支持程度，写 `claim_verification.md`。需要修改稿件时重新完成 review 与 claims，保持回执对应最终内容。
5. `verify` 另核对 DOI/PMID、题录、撤稿/更正；无法核验记未核验。DOI 可解析不等于主张成立。修正 partial/unsupported/unassessable 的核心事实论断。
6. `validate --final` 通过后 `export`：由同一主稿生成 `review.md`、`review.docx`、证据 CSV 和 BibTeX。Word 另需 `python-docx`；可发现宿主已有文档运行环境。最终交付含真实上游阶段执行记录，不自动声称稿件已满足投稿标准。

## 边界与恢复

- 上游原文未修改；本任务的语言、选题暂停、免费检索和 Markdown/Word 交付优先，具体适配和来源覆盖限制须按[调度契约](references/upstream-routing.md)记录。
- 不默认启用 Parallel/OpenRouter 等付费服务或大规模安装；不把示例、评测数据当研究结果。缺 Google Scholar / Web of Science 就记录未检索，不声称穷尽覆盖。
- 已有旧版运行不会自动追认上游执行。选题前可用 `skills enable` 迁移，再实际执行三个前置阶段；已选题/完成的旧运行新建编排运行并复用语料，不能补造旧调用记录。
- 确认过的上游版本与许可见[来源与复用](references/sources-and-reuse.md)及 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
