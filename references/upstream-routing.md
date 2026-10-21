# 上游技能的实际调度与断点

## 固定阶段、实际任务与产物

上游是可由代理加载并遵循的程序性技能，不是两个后台服务。总控发出任务；执行者可以是当前代理或具备同样资源的子代理。必须实际阅读对应原文与必要参考，按规程分析真实输入，写任务产物和应用说明；调用 Python 只会调度、记录和校验，不会自动完成科学推理。

| phase | 必须实际使用的上游技能 | 输入与产物 |
|---|---|---|
| search | AIpoch biomedical-search-strategy-builder | 基因/日期/主题 → 概念与MeSH核对、查询分组、范围说明 `search_strategy.md`，更新精确 `query_plan` |
| landscape | AIpoch disease-mechanism-evidence-map + biomarker-landscape-scanner | 语料与阅读深度 → 机制关系、证据层级、标志物适用性；`landscape.md`、`evidence.jsonl` |
| gaps | AIpoch medical-research-gap-finder + medical-topic-saturation-and-whitespace-checker + contradictory-findings-resolver + method-gap-detector | 全景、最近邻与反查 → 伪空白排除、矛盾/方法分析、可选方向；`gaps.json`、`gaps.md` |
| **暂停** | checkpoint → awaiting_selection | 展示方向编号；结束本轮。等待真实选择，禁止提前起草全文 |
| review | K-Dense literature-review | `selected_gap_id`、真实选题记录、既有语料和证据 → 主题综合与英文机制叙述综述；`manuscript.md`、`claims.jsonl` |
| claims | AIpoch paper-to-claim-verifier | 当前稿件、论断和原文 → 逐条证据审阅、`claim_verification.md` |

所有阶段的原始路径由 `skills plan/begin` 返回；必用资源随包提供，固定提交与文件摘要在 `upstream/manifest.json`。`literature-review` 的 `references/core_workflow.md` 和 `references/search_and_citation.md` 在 review 阶段必须加载。AIpoch SKILL.md 指定的必要 references 同理，按实际任务读取，勿一次把无关技能全部加载。

## 命令协议

```bash
python3 "$SKILL/scripts/research.py" skills begin --run-dir "$RUN" --phase landscape
```

这会返回 `invocation_id`、具体任务、输入、要求产物以及真实 SKILL.md 内容，并记录 **loaded**。它不代表代理已经执行、也不会创建科学报告。代理遵循上游规程工作后，写一个自己的完成报告 JSON，例如 `RUN/landscape_execution.json`：

```json
{
  "summary": "完成机制证据全景，并区分关联、干预和临床验证层级。",
  "applications": [
    {"skill_id": "aipoch/biomarker-landscape-scanner", "applied": "按 targeted 模式审查预测/预后标志物证据；缺少临床验证的部分保留为未验证。"},
    {"skill_id": "aipoch/disease-mechanism-evidence-map", "applied": "从已保存摘要/全文映射方向、细胞和实验类型，关系见 landscape.md，逐条依据见 evidence.jsonl。"}
  ],
  "outputs": ["landscape.md"]
}
```

内容必须如实描述本次已做的工作，记录适用模式、输入证据和对原规程的适配；不要照抄示例来假装执行。随后：

```bash
python3 "$SKILL/scripts/research.py" skills finish --run-dir "$RUN"   --invocation ACTUAL_INVOCATION_ID --report "$RUN/landscape_execution.json"
```

执行记录在 `orchestration_events.jsonl`。只有真实完成后的回执可算 **completed**；日志保存原技能哈希、语义参数、选择记录、产物哈希。它是可审阅的代理执行声明，不是独立的人类核验，不保证分析正确。文本加载记录与完成记录分开，原有证据门槛继续有效。报告/稿件修改后要更新对应回执；稿件或选择变更会使既有写作/论断审核失效。`evidence.jsonl`、`papers.jsonl` 是可随补检增长的共享数据，前半程回执的 outputs 只列本阶段定稿报告，不把这两个共享文件列为不可变产物；终稿 claims 审核则绑定当时的精确稿件、论断与证据。

正式顺序：`init → begin search → 执行策略 → set-queries（需要时）→ finish search → search → begin/finish landscape → begin/finish gaps → verify → checkpoint → 停止`。

收到真实选择后的顺序：`status → select → begin review → 加载上游与参考并成文 → finish review → begin claims → 核验论断 → finish claims → verify → validate --final → export`。

选题前尝试 begin review/claims 会报错。仅完成下载、plan 或 begin 也不能通过 checkpoint。缺少 review/claims 回执不能通过终稿导出。

## 任务约束对上游的适配

- **查询构建器**只负责策略，后续公共 API 适配器负责执行。其 3+ 概念 MeSH check-in 允许 explicit user instruction to continue：用户已要求自动完成选题前阶段时可继续并展示映射表；没有这项指令或存在影响基因身份的实质歧义则询问。不得虚构 MeSH 验证。
- **来源覆盖**：AIpoch gap finder 原文要求 PubMed + Google Scholar，WoS 可用时补充。此工作流核心来源为 PubMed + Europe PMC；有可用的 Scholar/引文追踪工具时补检并保留记录。无法访问时明确记 `Google Scholar: not searched` / `Web of Science: not searched`，写原因和此次采用的补充来源。此时执行的是有范围限制的适配流程，不能声称完全满足其原始多源覆盖要求；必要时降低候选置信度。
- **标志物**：单基因采用 targeted 模式。纯机制主题先做适用性判断，缺少诊断/预后/疗效预测用途时写“不适用”，不得强造临床成熟度结论。矛盾或某类方法缺口不适用时也说明证据和理由；适用性判断本身需按技能规程完成。
- **推荐方向**：上游允许推荐最优候选，本总控仍必须由用户选择。只将中/高置信候选写入正式 `gaps.json`；低置信线索留在报告的探索性部分。
- **综述类型与交付**：采用 K-Dense 的主题综合、筛选、质量与引用核验步骤，类型为机制叙述性综述。复用真实既有检索，选题后仅补检相关问题，不宣称 PRISMA 系统综述/独立双人筛选；Markdown+Word 代替其 PDF 默认交付。Parallel 搜索、OpenRouter 画图以及 Pandoc/LaTeX 均不默认启用。`verify_citations.py` 仅检查 DOI 注册/题录，原文支持由 claims 阶段加本技能证据规则检查；公共 API 题录校验可以采用本技能已有的更完整核验器，须在应用说明中写明。
- **软件归因**：保留 MIT 许可；实际采用 K-Dense 方法时在方法/软件说明中注明仓库与固定版本。上游建议引用的论文必须另行联网验证作者、版本和标识符后再加入参考文献，核验失败则保留软件归因并说明，不照抄未验证题录。

## 旧运行迁移

旧版 `run.json` 没有 `orchestration` 字段时保持旧版数据兼容，不代表已调用上游。在 `initialized/retrieved/awaiting_selection` 可执行 `skills enable`，再对既有真实语料实际完成三个前置技能阶段。不得通过事后填日志追认旧工作。旧运行已选题或完成时建立新运行、复用合法语料并重新调用对应阶段，保留旧结果。

## 写入与资源修复

同一 RUN 的状态和回执由总控串行写入；子代理可并行阅读不同文献并返回结果，但不能并发修改同一个 run.json 或执行日志。共享语料/证据/检索日志不能列入前半程不可变 outputs，CLI 会当场拒绝，避免选题后合理补证据导致旧报告回执失效。

如果新 init 在资源校验时失败，运行会保留 orchestration 门槛。先从同一固定版本的分发包恢复缺失/损坏的 upstream 文件及 manifest，再对原 RUN 执行 skills enable 和 status，随后重试未完成阶段；不必删除旧语料或重新 init。不要删除门槛字段来绕过资源错误。
