# Evolving Profile 5.1.0 — Release Notes

发布日期：2026-10-10  
状态：PR #4 已提交；尚未合并、打 Tag 或创建 GitHub Release

## 解决的问题

5.1 解决 EP5.0 在长 Session、来源覆盖、Agent Research 投影和原生复核可见性
方面的边界问题，同时保持“候选不等于事实、工具调用不等于答案采用”的证据边界。

## 本次变更

- 长 Session 使用同轮安全的确定性分段，避免单个摘要承载过多用户条件。
- 标准/完整 Session 摘要预算提高；紧凑层仍保持轻量。
- Provider 状态草稿失败时生成源绑定候选，但必须通过独立覆盖审查和 CAS 才能发布。
- 纠正、约束、已答复请求和助手报告分别绑定真实来源与字段；合并投影不能生成原句。
- Native source review 使用独立非 HTTP provenance，未知模型身份不被伪造。
- Agent Research 嵌套回执解包和节点计数投影保持父子独立。
- 新增脱敏发行配图和 `scripts/install-ep51.sh` 一键本地部署入口。
- 公开文档补充 Codex、Claude Code、Hermes 的适配现状与下一版本增强边界；中文 README 使用本地化图示和数据丰富链路/回执截图。

## 升级与回退

1. 先备份 Bank、运行配置、Guidance Registry、RAG 索引签名和审计回执。
2. 从新分支执行 `./scripts/install-ep51.sh --mode local --no-launch`，填入自己的 `.env`。
3. 需要运行 API/Console 时，按 `api/README.md` 和 Console 配置启动；安装器不会自动打开 JEV、外部 RAG、云备份或复制生产记忆。
4. 回退到 5.0 时恢复上一分支和配置快照；过程记忆为独立平面，可按策略保留。

## 已知边界

- 工具回执可证明调用、返回、送达和原文回读，不保证黑盒 Agent 在最终回答中采用了候选。
- 旧长会话 `01a123a0-aa87-7cf2-a2bf-a1b8c969c3f7` 与 `01a123e6-6237-77c0-a194-69a426ba9d34` 的真实恢复仍遵守安全拒绝门：Provider 覆盖或预算不足时保留原文，不强行发布摘要。
- Native 复核可以是真实独立 Agent 复核，但宿主未暴露的精确模型身份保持 unknown。
- 本文件的 GitHub PR、合并、Tag、Release 状态要以发布后回读为准。

## 验证

| 范围 | 命令或真实读回 | 结果 | 时间 |
| --- | --- | --- | --- |
| 发行包预检 | `python3 scripts/release-preflight.py --json` | 通过；5.1.0、分支/远端/作者快照已读回 | 2026-10-10 |
| 脱敏扫描 | `./scripts/verify-package.sh` | 通过；无个人 Bank、API Key 或个人路径命中 | 2026-10-10 |
| 情境状态/覆盖 | `pytest host-adapter/test_scenario_state_v3.py host-adapter/test_memory_recovery_scenario.py host-adapter/test_scenario_episode_model.py host-adapter/test_scenario_episodes.py host-adapter/test_scenario_validation_feedback.py host-adapter/test_context_incremental.py` | 166 passed | 2026-10-10 |
| Console/TypeScript/build | `npm test -w @evolving-profile/console -- --run`; `npx tsc --noEmit -p console/tsconfig.json`; `npm run build -w @evolving-profile/console` | 78 files / 531 tests passed；TypeScript 通过；生产与 standalone 构建通过 | 2026-10-10 |
| 一键部署 | `./scripts/install-ep51.sh --mode local --skip-deps --no-launch` | 预检、脱敏、构建和本地 manifest 通过；未启动服务 | 2026-10-10 |
| 依赖安全 | `npm audit --omit=dev` | 关键漏洞 0；仍有 8 个高/中等级依赖告警，已记录，不宣称零漏洞 | 2026-10-10 |
| 浏览器/拓扑 | 真实 Prompt、节点详情、Agent Research、滚动和多语言截图 | 已有开发运行证据；发行包需回读 | 2026-10-10 |
| GitHub 状态 | 分支、PR、Tag、Release 回读 | 未执行 | 2026-10-10 |

## 作者与致谢

沿用 [`NOTICE.md`](../NOTICE.md) 的 CCY 发行署名和 Hindsight 致谢边界。Git 提交
作者、仓库拥有者、EP 发行作者和上游项目致谢不自动合并为同一身份。

## GitHub 状态

- 上游仓库：`https://github.com/94Hao94/evolving-profile`
- 脱敏发行仓库：`https://github.com/ccygod/evolving-profile-2.2`
- 分支：`release/5.1.0`（fork 已推送）
- PR：[上游 PR #4](https://github.com/94Hao94/evolving-profile/pull/4)
- Tag：个人 fork 已创建 `v5.1.0-rc.2`
- GitHub Release：[Evolving Profile 5.1.0-rc.2](https://github.com/ccygod/evolving-profile-2.2/releases/tag/v5.1.0-rc.2)，预发布状态

## 来源与署名边界

- 个人发行渠道：[`ccygod/evolving-profile-2.2`](https://github.com/ccygod/evolving-profile-2.2)，分支 `release/5.1.0`。
- 上游协作渠道：[`94Hao94/evolving-profile`](https://github.com/94Hao94/evolving-profile)，PR [#4](https://github.com/94Hao94/evolving-profile/pull/4)。
- 两者来自同一 EP 5.1 发行分支；个人 fork 用于自有发布和备份，上游 PR 用于贡献协作。
- `NOTICE.md` 的公开发行作者/维护者署名为 CCY；GitHub 账号、仓库拥有者、上游项目拥有者和本机 Git 提交身份不自动等同。
