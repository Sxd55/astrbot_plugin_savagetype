# astrbot_plugin_savagetype

Savage Type 是面向 AstrBot 的**全局人格记忆中枢**。Savage 只是插件名。身份和语气永远读 AstrBot 当前人格；本插件只负责记住事实、处理改口、在需要时把少量相关记忆注入本轮对话。

当前版本 `v2.4.0`。仓库：https://github.com/Sxd55/astrbot_plugin_savagetype

要求 AstrBot `>= 4.22.0`。

## 它做什么

- 用户消息和 Bot 回复进入时间线，原文不当长期记忆。
- 时间线达到阈值后抽取稳定事实（启发式始终可用；配置了总结模型时额外走 LLM）。
- 同一说话人、同一规范化槽（`persona` + 说话人 + subject + attribute）冲突时：**新的 live，旧的 superseded 归档**，可回滚。不硬删除。`口味/喜欢` 会撞在同一槽上。
- 事实绑定当前 AstrBot 人格，换人格不会把上一套补丁层带过去。
- 全局一份库，检索默认偏向当前说话人（含别名）。只有当前问题明显点到别人的名字时才拉那个人的条目。
- 注入走 `req.extra_user_content_parts`，并 `mark_as_temp()`，不改 `system_prompt`。包装说明已压短。低信息消息几乎不召回；普通闲聊也可能带一条改口摘要。
- 检索：本地关键词 + 可选 Embedding + 可选 Rerank（`basic` / `auto` / `rerank`）。
- 检测到 `memory_companion` / LivingMemory 时跳过重叠的采集或注入；检测到 `self_learning` 时跳过黑话/few-shot/人格草稿，只保留事实层。
- v2 学习面：黑话释义、真实 user→bot few-shot、人格增量草稿，一律进审查队列，批准后才注入。
- v2.2：聊天记录文本导入、JSONL 换机导入（导入前自动备份、指纹去重）、睡眠维护会压缩已总结时间线、归档低价值事实、过期人格草稿。
- v2.3：同名说话人归并建议（不自动合并）、审查项带质量分、LLM 真实 Token 入账、`savagetype_navigate` 最多 3 步多跳召回。

## v2 学习面

- 黑话：停用词、命令、URL 先丢掉，再按次数问模型；普通词释义直接丢。注入整词匹配，`yy` 不会撞 `yyds`。每轮最多 3 条。驳回过的词默认不再提审。
- few-shot：同一说话人、约 90 秒内的邻接对；命令/工具回执丢掉；近重指纹合并。批准后每轮最多 2 条，句式打散。
- 人格草稿：冷却写入数据库；对照当前人格摘要，身份句或和人设重复度过高则丢。已批准草稿默认 14 天有效。**不写回** AstrBot 人格文件。
- 注入超预算时整块丢弃（核心事实按行尽量塞，黑话 / few-shot / 草稿整块丢），不再从中间截断标签。
- 人格草稿会读当前 AstrBot 人格正文做对照；few-shot 要求同一窗口、同一说话人回合。
- 驳回的黑话会从统计表清掉，避免反复提审。

## 矛盾覆盖护栏

冲突槽是「同一人格 + 说话人（含别名）+ 规范化 subject + 规范化 attribute」。例如 `用户/口味` 与 `self/likes` 视为同一槽。

- 本人自述或明确纠正 → 新事实立刻生效，旧条目标 `superseded`。
- 玩笑/反话、转述、不确定 → 不覆盖；转述最多写成 uncertain。
- 高置信且已被用过的旧事实，被单次非纠正说法挑战 → 进待确认，不立刻换。
- 主链只读 `live`。用户提起旧说法时，注入里可带一条改口摘要。

## 安装

把本目录放到 AstrBot 插件目录，目录名保持：

```text
astrbot_plugin_savagetype
```

常见位置：`AstrBot/data/plugins/astrbot_plugin_savagetype`

数据在 `data/plugin_data/astrbot_plugin_savagetype/`，更新插件不会覆盖库。

## 命令

主入口 `/stype`（管理员抽取指令另需管理员权限）：

| 命令 | 说明 |
| --- | --- |
| `/stype status` | 库规模、采集/注入是否因共存降级 |
| `/stype search <关键词> [k]` | 当前说话人可见 live 事实 |
| `/stype explain <关键词>` | 召回路由、命中和过滤原因 |
| `/stype add <内容>` | 手动写入 |
| `/stype recent [n]` | 最近时间线 |
| `/stype supersede <pending_id>` | 确认待覆盖 |
| `/stype rollback <fact_id>` | 回滚当前 live，恢复被覆盖的旧事实 |
| `/stype diagnostics` | 诊断快照 |
| `/stype extract` | 立刻抽取（管理员，绕过防抖） |
| `/stype alias <旧id> <主id>` | 说话人归并（管理员） |
| `/stype aliases` | 已映射别名 + 同名建议（管理员） |
| `/stype reviews [kind]` | 待审学习项（jargon / fewshot / persona） |
| `/stype approve <id>` | 批准学习草稿 |
| `/stype reject <id>` | 驳回学习草稿 |
| `/stype learn` | 立刻跑一轮学习（管理员） |
| `/stype export` | 导出 JSONL 到插件数据目录 |
| `/stype import 预览 <路径>` | 预览 JSONL 档案 |
| `/stype import 确认 <路径>` | 备份当前库后导入 JSONL |
| `/stype sleep` | 睡眠维护（管理员） |
| `/stype microscope [n]` | 最近注入快照 |

LLM 工具：`savagetype_recall`、`savagetype_remember`、`savagetype_navigate`。只有 remember 返回 `ok=true` 才允许嘴上说「记住了」。navigate 默认最多 3 步、每步 6 条。

## 配置要点

- 总结模型、Embedding、Rerank 都可以留空：抽取回退当前聊天模型；Embedding 默认关；live 事实达到 2500（可改 `embedding_auto_threshold`）时自动启用向量检索，但不会改掉配置里的开关。没有 Embedding Provider 只提示、不强开。Rerank 在 `auto` 下有 Provider 才用。
- 注入预算默认 800 字。
- 自动抽取有防抖（默认 45 秒）和失败冷却（默认 180 秒）。LLM 抽取失败时不把时间线标成已总结。
- `savagetype_remember` 不再默认当成「明确纠正」，高证据旧事实仍会进待确认。
- `debug_log_injection` 仅排障时打开。注入快照默认写入诊断库，面板「注入显微镜」和 `/stype microscope` 可查；该开关只控制是否打日志。总览里有用量账本（抽取/嵌入/重排次数）。
- `learning_enabled` 默认开。可单独关黑话 / few-shot / 人格草稿。面板「学习审查」里批准或驳回。
- 聊天导入只接受粘贴/文件文本，不调用 QQ 接口。面板「备份 / 导入」可预览后再确认。
- 睡眠维护默认保留 30 天已总结时间线；低置信且很少被访问的事实会归档而不是删除。

## 灵感与边界

- 记忆分层、Embedding/Rerank、分槽召回：参考 [astrbot_plugin_memory_companion](https://github.com/menglimi/astrbot_plugin_memory_companion)
- 陪伴体系分工（人格状态 vs 长期记忆）：参考 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)
- 稳定层缓存、动态包进用户消息、不回灌历史：参考 [lily](https://github.com/mcxxiu/lily)
- 学习闭环与审查意识：参考 [astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) 的产品思路，**未使用其代码**（该项目为 AGPL-3.0）

AstrBot 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html
