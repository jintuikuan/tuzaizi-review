# tuzaizi-review

面向 Codex 的基因调研与综述总控技能。输入“基因 + 时间窗 + 疾病或机制”，依次执行真实文献检索、证据全景整理、创新点反查，等用户选择方向后撰写并核验综述。

## 安装与调用

将本仓库的完整目录安装到 `~/.codex/Skills/tuzaizi-review/`，确保该目录下直接包含 `SKILL.md`：

```bash
git clone git@github.com:jintuikuan/tuzaizi-review.git \
  "$HOME/.codex/Skills/tuzaizi-review"
```

在能发现该技能的新 Codex 对话中输入：

```text
$tuzaizi-review 帮我调研近5年 METTL3 在肿瘤免疫逃逸中的研究，梳理方向并提出有证据支持的创新点。
```

默认先交付中文研究全景和创新点说明，进入 `awaiting_selection` 后**必须停止并等待用户选题**。收到“继续，选第 2 个方向”等明确选择后，才撰写 4,000–6,000 词的英文机制导向叙述性综述，导出 Markdown、可编辑 Word、参考文献和证据表。

“近五年”按检索日向前滚动五年；明确的 `2021–2026` 则从 2021-01-01 起算，截止检索日或 2026-12-31 中较早者。日期按 UTC+08 计算。默认解析人类基因名称，同时保留相关动物模型研究。

## 依赖与执行方式

- Python 3.10+；核心脚本仅使用标准库和公开 API，无需付费 API。实时检索需要网络。
- Word 导出额外需要 `python-docx`，可使用宿主已有的文档运行环境；也可在自行管理的虚拟环境安装 `python-docx`。
- 当前智能体负责阅读、筛选、证据提取、创新判断和写作；CLI 负责检索、去重、核验、状态管理和导出，单独运行检索命令不会自动生成综述。
- 数据来源为 NCBI Gene、PubMed、Europe PMC，必要时使用 Ensembl；通过 Europe PMC 发现预印本，再用 bioRxiv DOI 接口补充版本信息。

实际 CLI 示例：

```bash
SKILL="$HOME/.codex/Skills/tuzaizi-review"
RUN="$PWD/work/mettl3-immune-review"

python3 "$SKILL/scripts/research.py" init --run-dir "$RUN" \
  --gene METTL3 --topic 'tumor immune escape' --last-years 5 \
  --concept 'immune escape|immune evasion|immunosuppression|checkpoint|PD-L1' \
  --concept 'cancer|tumor|tumour|neoplasm'
python3 "$SKILL/scripts/research.py" search --run-dir "$RUN" --enrich-preprints
python3 "$SKILL/scripts/research.py" status --run-dir "$RUN"
```

`--concept` 组内用 `|` 表示 OR，多组之间为 AND。正式调研应完成全部分页；`--limit` 只用于独立运行中的连通性测试。分析文件、反查、选题恢复及导出命令见[执行与文件契约](references/workflow-contract.md)，参数以 `research.py --help` 和各子命令的 `--help` 为准。

## 质量与测试

证据记录区分题录、摘要和全文，分开核验文献身份与论断支持度。候选创新点必须有最近邻比较和针对性反查；不把零命中等同于全球首次，也不使用模拟新颖度评分。完整分页不代表系统综述或穷尽检索。

在仓库根目录执行离线测试：

```bash
python3 -m unittest discover -s tests -v
```

Word 导出测试在安装 `python-docx` 的环境运行。真实调研的缓存、全文、研究状态和输出请放到 `work/`、`runs/` 或 `outputs/`，不提交到仓库。

## 来源与许可说明

本技能独立实现，方法组织参考 [aipoch/medical-research-skills](https://github.com/aipoch/medical-research-skills) 与 [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)，未捆绑上游程序代码。固定版本、能力边界及适用的上游 MIT 声明见[来源与复用说明](references/sources-and-reuse.md)和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；上游声明不等于为本仓库原创代码或论文全文重新授予许可。
