# 上游来源与实际复用

本版本为总控：实际加载、执行并记录下列锁定版本的子技能。检索公共 API、数据规范、证据门槛、断点和 Word 导出是本项目实现；科学分析与写作按指定上游规程执行。原文资源已随包附带，不再只是可选的方法引用。

| 来源 | 固定提交 | 分发与用途 |
|---|---|---|
| [aipoch/medical-research-skills](https://github.com/aipoch/medical-research-skills) | `686e09dfdb45d9fa1a08664f06bb93769b3c9d91` | `upstream/aipoch/` 的8个技能：策略、全景、机制图、空白、饱和、矛盾、方法缺口、论断核验 |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | `a1043ef544e3a100c28e70a736b6cf32554d0677` | `upstream/kdense/literature-review/`：用户选题后综合与综述写作 |

完整原始路径、逐文件 SHA256 在 `upstream/manifest.json`；原文件未改动。仅排除了不参与执行的 AIpoch `eval_report*` 评测结果，不把其示例当研究证据。对应根 MIT 许可在 `upstream/aipoch/LICENSE`、`upstream/kdense/LICENSE.md`；项目 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 同时保留许可文本。

每阶段必须加载原 SKILL.md 和任务所需 references；由代理实际执行，然后保留应用说明与产物摘要。单纯打出上游名称、下载文件、或者 begin 的 loaded 日志，不能称为完成调用。阶段映射、输入输出、已选方向门槛及明确适配见[调度契约](upstream-routing.md)。

## 没有纳入默认链条的组件

- `research-lookup` 的付费 Parallel 后端不是检索的默认依赖。
- `scientific-writing` 不是此版 `literature-review` 的硬依赖，未捆绑；不宣称通过该技能独立的人类核验/投稿验收。
- 含合成/示例新颖度数据的 `target-novelty-scorer` 不运行、不捆绑。
- 不安装整个仓库或额外付费服务。上游额外可选能力只在用户已授权、配置存在且确有用途时使用。

MIT 只授予相应软件/说明的使用权，不授予检索到的论文全文再分发权。全文限合法来源，保留来源和许可；生成综述使用规范转述、必要短引和引用，不公开用户私有全文。
