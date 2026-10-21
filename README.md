# tuzaizi-review

输入“基因 + 时间窗 + 疾病/机制”，由总控**实际调度两套上游子技能**：先用 AIpoch 形成检索策略、证据全景与候选方向，**暂停等你选题**；选定后调用 K-Dense `literature-review` 生成英文机制叙述性综述，再调用 AIpoch 论断核验。

## 安装

```bash
git clone git@github.com:jintuikuan/tuzaizi-review.git \
  "$HOME/.codex/Skills/tuzaizi-review"
```

仓库已包含所需的9个上游技能及支持资源、固定版本、SHA256与MIT许可，无须再安装整个上游仓库。安装后下一轮对话可发现技能。

## 两轮调用

第一轮：

```text
$tuzaizi-review 调研近5年 METTL3 在肿瘤免疫逃逸中的研究。调用上游技能完成检索策略、研究全景和有文献依据的创新点；交付候选方向后暂停，等我选择。
```

第二轮，在同一研究对话中：

```text
继续，选第2个方向。复用前面的文献与证据，调用 literature-review，生成英文综述并导出 Markdown 和 Word。
```

如果换了对话，附上之前的运行目录 `RUN`，总控先读取其状态再继续。默认中文调研报告、英文综述正文 4,000–6,000 词；篇幅、基因和主题可覆盖；当前终稿字数校验面向默认英文综述。“近五年”滚动回溯，明确年份范围则从首年年初至运行日/末年年末中较早者，不包含未来。

## 实际执行链

| 阶段 | 上游子技能 | 完成后 |
|---|---|---|
| search | AIpoch biomedical-search-strategy-builder | 公共API按策略检索 |
| landscape | AIpoch disease-mechanism-evidence-map、biomarker-landscape-scanner | 中文证据全景 |
| gaps | AIpoch medical-research-gap-finder、medical-topic-saturation-and-whitespace-checker、contradictory-findings-resolver、method-gap-detector | 候选方向及反查依据 |
| awaiting_selection | **停止，等用户选择** | 不能提前写完整综述 |
| review | K-Dense literature-review | 围绕选中方向的英文综述 |
| claims | AIpoch paper-to-claim-verifier | 论断支持检查与修订 |

每阶段 `skills begin` 输出真实上游规程与任务，代理实际按其工作后写应用说明，`skills finish` 才登记完成。仅下载、打印或加载技能不能通过选题/终稿门槛。日志是代理执行声明，不能替代独立的证据判断；文献身份和论断支持仍分别核验。

## 依赖与范围

Python 3.10+，核心调度/检索只用标准库；Word 导出需 `python-docx`。核心检索使用免费 NCBI Gene、PubMed、Europe PMC、Ensembl及bioRxiv版本接口。Parallel/OpenRouter、付费画图、PDF工具链不默认启用。

AIpoch 原规程要求的 Google Scholar/WoS 若不可用，会在覆盖说明和应用记录中写明，而不会声称已检索。方法适配、不可用来源、标志物适用模式、低置信线索及归因规则见[调度契约](references/upstream-routing.md)。上游原文未改，当前交付为机制叙述性综述草稿，不自动宣称系统综述或满足投稿标准。

CLI 与证据格式见[文件契约](references/workflow-contract.md)。关键参数：

```bash
python3 scripts/research.py --help
python3 scripts/research.py skills --help
python3 -m unittest discover -s tests -v
```

新运行自动启用上游调度。旧运行不会被追认为已调用：仅选题前可用 `skills enable`，再实际执行前置阶段；旧运行已选题/完成时新建编排运行并复用语料。

## 来源与许可

上游来源为 [aipoch/medical-research-skills](https://github.com/aipoch/medical-research-skills) 和 [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)。固定提交、文件哈希和原始路径见 `upstream/manifest.json`，MIT声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与上游许可文件。许可不包括检索到的论文全文；语料、全文和生成报告请放 `work/`、`runs/` 或 `outputs/`，不要提交私有研究资料。
