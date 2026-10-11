# Evolving Profile 5.1.0

> English release notes first. 中文更新说明见本文后半部分。

发布日期：2026-10-10（贡献分支准备）  
状态：release candidate; PR, merge, tag, and GitHub Release are tracked separately

## Why 5.1

EP 5.0 separated user knowledge, agent process memory, external RAG, and the
visible execution topology. EP 5.1 hardens the boundary cases that appear when
those planes meet: long sessions, incomplete provider drafts, nested host
receipts, native source review, and source-to-state coverage.

## Main changes

### 1. Long-session scenario coverage

- Adds deterministic episode boundaries after a bounded number of user-message
  starts while keeping messages from the same turn together.
- Treats the boundary as a transport/coverage safeguard, not as a semantic
  claim that the user's task changed.
- Raises standard/full Session budgets without removing the compact budget or
  allowing silent truncation of exact user constraints.
- Keeps a source-linked deterministic fallback when a provider state response is
  malformed; the candidate remains pending independent coverage review.

### 2. Exact user-intent coverage

- Requires every substantive user request, constraint, correction, and answered
  request to bind to real message IDs, episode fields, and summary tiers.
- Normalizes explicit user corrections that a provider accidentally places in
  `constraints`, and recovers omitted source-visible corrections from the exact
  original user clause.
- Never derives a new correction from compressed merge-projection text.
- Resolves an omitted `answer_message_ref` only when selected assistant-report
  fields point to one unique later assistant message; multiple answers require
  an explicit model selection.

### 3. Native review provenance and host observability

- Adds a native source-coverage review receipt that is visibly distinct from
  background provider HTTP review.
- Never fabricates provider request/response hashes or an exact model identity
  for a native review whose host identity is not exposed.
- Keeps user and agent route receipts separate, including Agent Recall, Agent
  Research, returned content, source readback, and answer-use uncertainty.
- Unwraps nested MCP envelopes without copying parent aggregates into unrelated
  child nodes.

### 4. Release-grade packaging

- Adds a sanitized EP 5.1 hero illustration showing the three memory planes,
  evidence checkpoints, readback, and delivery flow.
- Adds `scripts/install-ep51.sh` with dry-run, dependency, preflight, package
  verification, and no-secret local deployment boundaries.
- Updates the release manifest, console package version, README, Notice, and
  release ledger consistently.
- Keeps JEV, external RAG, cloud backup, and provider credentials opt-in; the
  installer never enables or imports them automatically.

### 5. Host compatibility and documentation

- Documents current high compatibility with Codex, Claude Code, and Hermes
  through shared MCP/Hook, transcript-normalization, controller, and receipt
  contracts.
- Makes Codex the most deeply exercised 5.1 host and explicitly marks Claude
  Code/Hermes onboarding, native receipt, and capability-probe improvements as
  next-version work rather than claiming identical behavior today.
- Adds localized Chinese diagrams and data-rich Chinese console captures so the
  visual explanation follows the selected README language.

## Compatibility and migration

- EP 4.0/5.0 user-memory records, route aliases, L0/L1/L2 navigation, and
  existing RAG index metadata remain readable.
- Existing Embedding/Rerank indexes must still be checked against their model
  signature after a model/path/dimension change.
- Upgrade a contribution checkout by backing up Bank data, runtime settings,
  Guidance Registry, and audit receipts. Roll back by restoring the previous
  branch and configuration snapshot. Process-memory records remain separate.

## Known boundaries

- A receipt proves a call, return, delivery, or source readback state; it does
  not prove that an opaque model used a candidate in final prose.
- Long-session recovery is safe-by-default: if independent coverage cannot bind
  every user intent, the original source remains protected and no summary is
  published. This release does not convert an unresolved provider review into a
  fact.
- Native review provenance is real but its exact host/model identity can remain
  unknown; the UI says so explicitly.
- This contribution branch is not a merged/tagged GitHub Release until those
  stages are separately verified in the release ledger.

## Verification record

The release package gate records the exact command output and current counts in
[`docs/RELEASE-NOTES-5.1.0.md`](docs/RELEASE-NOTES-5.1.0.md). It includes
scenario/state/coverage tests, full Python and Console tests where available,
TypeScript/build checks, package secret scanning, and real-browser topology
evidence. Historical test counts are not reused as current results.

---

# 中文更新说明

## 为什么是 5.1

5.0 已经把用户知识、智能体过程记忆、外部 RAG 和完整执行拓扑分开。5.1
进一步处理这些平面交汇时最容易出错的边界：长会话、模型草稿不完整、嵌套
宿主回执、独立原文复核，以及用户原句到情境状态的逐条覆盖。

## 主要变化

1. **长会话情境覆盖**：按安全的用户消息边界分段，同一轮消息不拆开；分段只
   是传输和覆盖保护，不擅自宣称用户开启了新任务。标准/完整摘要预算提高，
   但不允许静默截断用户的精确约束。
2. **用户意图逐条对账**：请求、条件、纠正和已答复请求必须绑定真实消息、真实
   情境字段和实际摘要层；模型误把纠正放入约束时会规范化，原始来源中漏掉的
   纠正会按原句补回；压缩合并文本不会被当作原文。
3. **独立复核来源标识**：原生 Agent 原文复核与后台 Provider HTTP 复核分开标示，
   不伪造请求哈希、响应哈希或未知的模型身份。Agent Recall、Agent Research、
   返回内容、原文回读和答案采用状态保持独立。
4. **可发布脱敏包**：新增表达三条记忆平面、证据检查点、回读和送达过程的
   5.1 配图；新增 `scripts/install-ep51.sh`，支持预检、脱敏检查、依赖安装、
   构建、试运行和不启动服务的选项。JEV、外部 RAG、云备份和 API Key 仍默认
   关闭或由操作者显式配置。

## 兼容与边界

4.0/5.0 的用户记忆、旧路由兼容映射和 RAG 索引元数据仍可读取。若模型、路径
或维度改变，索引仍需按签名检查和重建。长会话覆盖无法独立核验时，系统保留
原文并保护写入，不把不完整结果升级为事实。工具回执也不能单独证明最终回答
采用了某条候选。

## 验证

本次实际验证命令、通过数、脱敏检查、构建和浏览器证据记录在
[`docs/RELEASE-NOTES-5.1.0.md`](docs/RELEASE-NOTES-5.1.0.md)，不复用历史版本的
测试数字。
