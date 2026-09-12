# astrbot_plugin_savagetype

Savage Type 是面向 AstrBot 的全局人格记忆中枢。Savage 只是插件名。身份和语气永远读 AstrBot 当前人格；本插件只负责记住事实、处理改口、在需要时把少量相关记忆注入本轮对话。不改写人格文件，不做日程和主动陪伴。

当前版本 `v3.0.0`。仓库：https://github.com/Sxd55/astrbot_plugin_savagetype

要求 AstrBot `>= 4.22.0`。

## 它解决什么问题

对话记录不等于记忆。本插件把 QQ 消息写入时间线，经 AI 缩写和审核后沉淀为记忆；主链请求前只注入本轮真正相关的一小包资料。当前用户消息始终是主任务，记忆只是辅助。

记忆分两层：

- **主人记忆**：主人（`owner_qq`，留空回退 AstrBot 管理员）的指令和自述。全局可注入。
- **人物档案**：QQ 里出现过的每个人自动建档。本人出现或点名时才注入。

## 数据怎么流动

1. QQ 消息进入时间线，任何新面孔自动建空档案。ChatUI（webchat）只记录主人自己的对话（进主人记忆），不建人物档案、不记录其他人。
2. 有长期价值的候选消息（自述、指令、状态）进入后台 AI 管线：缩写原文 → 对照原文审核是否脱离原意 → 最多改 2 轮 → 通过才写入；不通过进「待审记忆」并 QQ 通知主人，主人回复「是/否 + 编号」决定。
3. 每条记忆带证据消息 id、直白文本、关键词、审核状态；时间线里被引用的原文不会被维护清理。
4. 同一人格、同一说话人、同一规范化槽发生冲突时：新的生效，旧的删除；高证据旧事实被单次非纠正说法挑战时先进「待确认覆盖」。玩笑、转述、不确定不会覆盖。
5. 注入包走 `req.extra_user_content_parts`，并 `mark_as_temp()`，不改 `system_prompt`，避免打爆前缀缓存。超预算时核心事实按行尽量塞，黑话 / few-shot / 草稿整块丢。

没有可用模型时退回启发式写入，标「未审核」；不会阻塞使用。

数据目录：`AstrBot/data/plugin_data/astrbot_plugin_savagetype/`。更新插件不会覆盖这个库。升级到 v2.8.0 首次启动会**自动备份并清空旧脏数据**（旧版本的归属错位、admin 名下错记等无法自动纠正），别名映射和配置保留。

## 身份与安全

- bot 回复固定记为 `bot_self`，不再借用人类 QQ。
- 每条事实必须能追到证据消息：`subject=self` 只接受用户消息，`subject=bot` 只接受 bot 消息。
- 关系词守卫：主人 / owner / 老公老婆 / 爸妈 / 老板 等关系或权限自称，非主人一律拒绝；只有主人自己说的才写入，且降级为备注。
- 主人只认 `owner_qq`（或管理员回退），不认聊天里的自称。

## 面板（推荐日常都用这里）

AstrBot WebUI → 插件 → Savage Type → 拓展页。三个主区 + 设置：

- **记忆库**：主人记忆、待审记忆、手动补记、学习审查、待确认覆盖、说话人归并。顶栏有刷新 / 抽取整理 / 维护 / 跑一轮学习 / 导出 JSONL。
- **人物档案**：搜索、点选查看；可改昵称和备注，条目可增删改。
- **诊断**：注入显微镜、聊天导入、原始 JSON 诊断、清空并重建（先自动备份）。
- **设置**：字段与 AstrBot 插件配置页相同，保存后立刻生效。

界面为 Shader Gradient 风格：近黑底上跑真实的 WebGL 片元着色器流动渐变（fbm 域扭曲），内容坐在冻毛玻璃面板上，只用主色一支 UI 强调色；devicePixelRatio 封顶 2、离屏暂停、`prefers-reduced-motion` 单帧、WebGL 不可用时回退静态渐变。主色与撞色共同驱动背景着色器场，设置页内置 8 组预设按钮并支持取色器自定义。

## 审核与通知

- 审核不过的条目在「待审记忆」，也可以在 UI 直接通过 / 删除 / 编辑后过审。
- QQ 通知优先发给主人最近一次**私聊**会话；没有私聊记录时用 `notify_umo`。多条合并，带冷却。
- 主人在 QQ 回复 `是 12` 通过，`否 12` 删除；只有一条待审时可直接回 `是/否`。只认主人。

## 命令（管理员部分需要管理员）

主入口 `/stype`。

| 命令 | 说明 |
| --- | --- |
| `/stype status` | 时间线、记忆数量、采集/注入是否开启 |
| `/stype search <关键词>` | 当前说话人可见 live 事实 |
| `/stype explain <关键词>` | 召回路由、命中和过滤原因 |
| `/stype add <内容>` | 手动写入（说话人是当前聊天对象） |
| `/stype recent [n]` | 最近时间线 |
| `/stype extract` | 立刻抽取/整理 |
| `/stype sleep` | 维护（含空档案清理） |
| `/stype pending` | 待审记忆列表 |
| `/stype pass <id>` / `/stype drop <id>` | 通过 / 删除待审记忆 |
| `/stype learn` | 跑一轮黑话/few-shot/人格草稿学习 |
| `/stype reviews [kind]` | 待审学习项 |
| `/stype approve <id>` / `/stype reject <id>` | 批准 / 驳回学习草稿 |
| `/stype dossier [QQ]` | 当前说话人或指定 QQ 的短档案 |
| `/stype export` | 导出 JSONL 到数据目录 |
| `/stype import 预览\|确认 <路径>` | 预览或导入 JSONL（确认前会备份当前库） |
| `/stype alias <旧id> <主id>` | 说话人归并 |
| `/stype aliases` | 已映射别名 + 同名建议 |
| `/stype diagnostics` | 诊断快照 |

LLM 工具：`savagetype_recall` 检索，`savagetype_remember` 写入（只有主人可用，返回 `ok=true` 才算记住），`savagetype_navigate` 多跳召回。

## 学习审查是什么

事实会自动写。审查管的是「怎么说」：黑话释义、few-shot 表达样本、人格草稿（不写回人格文件，批准后约 14 天有效）。和「待审记忆」不是一回事。

## 检索：先 Rerank，Embedding 后上

检索模式默认 `auto`：有 Rerank Provider 就用，失败回退本地关键词。Embedding 默认关，live 事实达到 `embedding_auto_threshold`（默认 2500，0=从不自动）时自动补充召回，不改配置开关。

## 主要配置项

- `owner_qq`：主人 QQ，只允许一个。留空回退管理员。
- `memory_source_platforms`：记忆来源适配器类型，默认 `aiocqhttp,qq_official,qq_official_webhook`；不填 webchat 就不记录 ChatUI。`/stype status` 会显示本会话平台和上次采集跳过原因。
- `pipeline_enabled` / `normalize_provider_id` / `verify_provider_id` / `pipeline_max_revisions`：AI 整理与审核。插件面板设置里抽取/整理/审核只列对话模型，Embedding / Rerank 各列对应类型；AstrBot 原生配置页还不支持嵌入/重排序选择器，那两项需要手填 ID。
- `pipeline_batch_size` / `pipeline_notify_cooldown_seconds`：后台批量与通知冷却。
- `empty_profile_ttl_days`：空档案清理天数，默认 7，0=不清理。
- `ui_theme_color` / `ui_theme_color2`：面板主色 + 撞色。设置页有 5 组预设按钮，也可以取色器自定义。
- 其余：抽取、注入预算、检索、睡眠维护、共存降级等，见插件设置页。

## 共存

检测到 `memory_companion` / LivingMemory 时跳过重叠的采集或注入。检测到 `self_learning` 时跳过黑话 / few-shot / 人格草稿，只保留事实层。

## 安装

目录名保持 `astrbot_plugin_savagetype`。可从 GitHub 安装：

https://github.com/Sxd55/astrbot_plugin_savagetype

或拷到 `AstrBot/data/plugins/astrbot_plugin_savagetype` 后重载。

## 离线测试

```text
python tests/test_core.py -v
```

只需 Python 3.11+ 标准库。

## 灵感与边界

- 记忆分层、Embedding/Rerank、分槽召回：参考 [astrbot_plugin_memory_companion](https://github.com/menglimi/astrbot_plugin_memory_companion) 的产品分工，没有克隆其权限拓扑和陪伴功能。
- 人格状态 vs 长期记忆：参考 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)。本插件不做日程和主动消息。
- 省 token：参考 [lily](https://github.com/mcxxiu/lily)。动态记忆进用户消息附加块并标临时，不改 system_prompt，不回灌整段历史。
- 学习审查：参考 [astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) 的「先审后用」，未使用其代码（AGPL-3.0）。

AstrBot 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html

明确不做：好感/情绪数值、日程与主动陪伴、把草稿写回人格文件、私聊/群聊 ACL 拓扑。
