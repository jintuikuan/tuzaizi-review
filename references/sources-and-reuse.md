# 来源、方法借鉴与可选复用

本技能独立实现可执行检索、状态管理、证据门槛与导出；没有复制、捆绑或默认安装以下仓库的程序。方法组织参考了用户指定的两套技能库；根目录 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 保留核对过的 MIT 声明。快照用于说明本次评估依据，不能把上游之后的行为变化当成本技能已经支持的功能。

## 已核对快照

| 来源 | 快照与实际路径 | 本技能采用的思想/边界 |
|---|---|---|
| [aipoch/medical-research-skills](https://github.com/aipoch/medical-research-skills) | `686e09dfdb45d9fa1a08664f06bb93769b3c9d91`；`awesome-med-research-skills/Evidence Insight/` 下的 `biomedical-search-strategy-builder`、`biomarker-landscape-scanner`、`disease-mechanism-evidence-map`、`medical-research-gap-finder`、`medical-topic-saturation-and-whitespace-checker`、`contradictory-findings-resolver`、`method-gap-detector`、`paper-to-claim-verifier` | 概念检索、方向归纳、最近邻与证据缺口、论断回指；目录中的技能名称不自动等于已执行的检索服务 |
| 同上 | `scientific-skills/Evidence Insight/research-hotspot-analysis`、`target-novelty-scorer` | 作为评分问题的启发；已发现 novelty scorer 的合成/示例数据逻辑，**不作为真实文献热度、新颖度或统计结果运行** |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | 核对的 main 快照 `a1043ef544e3a100c28e70a736b6cf32554d0677`；`skills/literature-review/SKILL.md`、`skills/research-lookup/SKILL.md` | 检索—筛选—综合—核验的组织；`research-lookup` 的付费 Parallel 服务是可选能力，不是本技能默认依赖 |

K-Dense `literature-review` 的文件最近修改提交曾核对为 `a92663a0214b3b9fa1ba6b958284ffd919c00a0c`，这是该文件的历史提交，**不等于仓库当前 main SHA**。如需检查具体资源，使用上表 main 快照的固定链接，例如 [literature-review](https://github.com/K-Dense-AI/scientific-agent-skills/blob/a1043ef544e3a100c28e70a736b6cf32554d0677/skills/literature-review/SKILL.md)。

## 重要能力差异

- K-Dense `literature-review` 附带的 `search_databases.py` 主要处理本地条目/文件，不可当作本技能已经完成 PubMed 或 bioRxiv 实时检索的证明。本技能通过自己的公开 API 适配器实际翻页获取数据。
- 其 `verify_citations.py` 中 DOI 注册信息/可解析性核验不能证明论文支持某句话。本技能分开保留题录身份审核和代理的原文支持判断。
- aipoch 的方向/热点/新颖度技能含工作流说明和示例；没有真实输入和执行记录的输出不能引用为研究证据。本技能的新颖度由本次语料、最近邻比较和实际反查支撑。
- 上述方法借鉴不使生成综述自动成为系统综述、独立双人筛选结果或经过验证的新颖性量表。

## 可选复用规则

仅在当前环境确有对应技能目录、脚本或连接器，且能增益本次任务时复用：

1. 读实际 `SKILL.md` 与目标脚本说明，确认真实路径、参数、网络来源、费用与输入输出；不从技能名称猜测能力。
2. 保留本技能的规范语料 ID、证据深度、真实用户选题门槛与日期范围。不让外部示例数据进入正式语料，不套用其额外自动发送/定时任务行为。
3. 默认继续使用本技能内置公开 API。付费服务只在用户已授权且已配置时调用；不为了凑齐上游名字安装整仓库、添加密钥或订阅服务。
4. 若真正复制或修改上游代码/大段说明，记录具体文件、固定提交和适用许可证，保留相应声明；不要把上游根许可证推定为所有第三方子资源的许可证。

## 数据与全文的许可

MIT 声明适用于所列软件与说明，不授予论文全文的再分发权。保留文献来源 URL、检索日期、标识符与可获知的全文许可；通过 PMC/Europe PMC 的合法公开接口获取文本。付费全文未获得时保留摘要级证据，不绕过访问控制。用户自己的全文可用于本次分析，但不因此默认公开发布。最终综述以准确转述、必要短引与规范引用呈现研究内容，不复制大段全文。
