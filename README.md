# ScholarlyReview v0.1（AI 领域优先）

依据《ScholarlyReview 论文自查与审稿辅助系统实施规范 v1.0》实现的第一版：**共享执行内核 + 结构化证据库 + AI 优先的领域配置 + 两个宿主 Skill**。在 Claude Code 或 Codex 中以 interactive 方式使用。

核心原则：**模型只提交候选记录，内核负责校验、记账、定位、复核规则和报告准入**。每条进入报告的意见都能追溯到原文块、确定性计算或复核记录。

## 快速开始

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/sreview install --host claude --scope project
```

之后在 Claude Code 中输入 `/paper-preflight papers/draft.pdf，standard，中文输出`，或 `/paper-referee papers/submission.pdf，standard，使用 reviews/form.json`。Codex 使用 `--host codex`，入口为 `$paper-preflight` / `$paper-referee`。

一条命令跑完整个流程，每个任务都在内核启动的短命 Worker 中执行：Claude 用 `claude -p`，Codex 用 `codex exec`。宿主只需读取紧凑的 JSON 结果：

```bash
sreview auto --manuscript paper.pdf --mode author --effort quick --host claude [--worker-model sonnet]
```

```bash
sreview auto --manuscript submission.pdf --mode reviewer --effort standard --host codex \
  --rubric form.json --scores --recommendation    # 审稿：按会议表单起草评分和建议，内核校验
```

`examples/rubrics/generic-ml-conference.json` 只是通用模板，**不是**任何会议的官方表单。评分与建议只出现在审稿人私有视图和编辑视图中，不进入给作者的视图，并且始终是草稿，不会自动提交。

手动跑一遍内核（宿主 Agent 也是这样调用的）：

```bash
sreview run --manuscript examples/papers/synthetic_ml_paper.md --mode author --effort quick --cutoff 2026-05-01
sreview task claim --run <run_id>          # 领取任务：返回说明、上下文、Schema、result.json 位置和 task_token
sreview task submit --run <run_id> --task-token <token> --result <result.json>
sreview status --run <run_id>
sreview export --run <run_id>              # author_private：报告、发现、证据、覆盖、预算、修改计划
```

所有命令只向 stdout 输出一个 JSON 信封 `{ok, data, error, warnings, request_id}`，退出码遵循规范 §13.4。

## 领域配置（AI 优先，其余同步支持）

| 配置 | 适用 | 专用检查 |
| --- | --- | --- |
| `ml-v1`（默认） | 机器学习 / AI | 评测协议、基线公平性、消融与收益归因、统计证据、泄漏与预训练污染、主张范围与泛化、可复现与算力、局限与许可；按 `understand` 识别的子领域自动追加：LLM/NLP 评测有效性（prompt 对等、LLM-as-judge、模型版本）、智能体评测、视觉/多模态、强化学习、学习理论、图学习 |
| `systems-v1` | 网络与分布式系统 | 模型与可执行决策一致、资源守恒与可行性、负载与测试平台真实性、基线配置、开销与尾延迟 |
| `security-v1` | 安全 | 威胁模型、攻击者能力一致性、攻击覆盖与防御目标、自适应攻击、伦理与漏洞披露 |
| `generic-v1` | 其他学科 | 通用六项标准，报告会注明缺少专用标准 |

所有配置都继承 `generic-v1` 的通用检查（主张证据对应、技术正确性、实验有效性、创新性定位、内部一致性、可复现、引用、表达），只追加检查，不放宽证据要求。期刊或会议的评分表、AI 使用政策不内置，需通过 `--rubric` 提供，不会凭模型记忆补造。

## 一次运行的流程

```
import（冻结输入快照 + DocumentMap） → plan（展开有效配置、预算账户、任务图）
→ understand（结构摘要、主张、审查假设；识别论文类型和 AI 子领域，据此调整任务）
→ numeric_extract（模型只抄录数字，内核确定性复算） ∥ 各项 check ∥ literature（受控检索）
→ verify（中性问题、隐藏候选结论、先找反证） → 人工确认（verify-human）
→ admit_finding + readiness → 按受众导出
```

## 目录

```
src/scholarly_review/
  models/      实体与 Schema（附录 A 的精确键）
  core/        配置、存储/CAS、预算账本、内核状态机、准入函数、文献操作
  modules/     解析与引文定位、数值核验、领域配置注册表、文献 Provider
  reports/     快照、覆盖统计、确定性报告模板与导出
  interfaces/  CLI 与共享服务函数
  adapters/    宿主能力探测与 Skill 安装、任务 Worker（claude -p / codex exec）、一条命令运行（auto）
  profiles/    generic / ml / systems / security 配置
  prompts/     每类任务的工作说明
  skill_sources/ 两个入口 Skill（安装时替换宿主入口符号）
tests/          数值黄金用例、准入规则、预算并发、端到端、Worker（Claude/Codex）与审稿表单用例
schemas/        导出的 JSON Schema 2020-12
docs/IMPLEMENTATION_STATUS.md  规范对照：已实现、偏离与未实现
```

工作区 `.scholarly-review/` 按项目建立，包含 state.sqlite、artifacts（SHA-256 内容寻址）、runs、exports、cache 和 tmp。

> 注意：本项目位于 OneDrive 同步目录。SQLite 使用回滚日志模式而非 WAL，但同步盘仍可能干扰锁；正式使用时建议把 `--workspace` 指向本地非同步目录。
