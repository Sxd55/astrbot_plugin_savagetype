# astrbot_plugin_savagetype

**Savage Type** 是面向 AstrBot 的全局人格记忆中枢。Savage 只是插件名：身份和语气永远读 AstrBot 当前人格，本插件只负责**记住事实、处理改口、在需要时把少量相关记忆注入本轮对话**。不改写人格文件，不做日程和主动陪伴。

当前版本 `v5.4.0`。仓库：https://github.com/Sxd55/astrbot_plugin_savagetype
要求 AstrBot `>= 4.22.0`；运行依赖只有 `jieba`（可选，BM25 分词用，装不上自动回退）；离线测试只需 Python 3.11+ 标准库。

---

## 一、它解决什么问题

| 问题 | 本插件的做法 |
| --- | --- |
| 对话记录不等于记忆，全塞回上下文又贵又乱 | 消息先落时间线，后台 AI 缩写成「直白事实」并对照原文审核，只把与本轮相关的一小包注入 |
| 「喜欢美式」这种碎片记住了，整件事却记不住 | **事件层**：一段连续聊天整理成「一件事」（经历 / 决定 / 聊过的话题），追加式互不覆盖，按时间注入 |
| 提到小明只想起「小明说的」，想不起「和小明有关的事」 | **实体链接**：人名、关键词、领域登记成实体；提问里出现谁，相关的事实和事件一起加权召回 |
| 「他以前喜欢什么」答不了 | **时序查询**：问「以前 / 去年 / 上个月」时，检索那段时间为真的旧记忆，注入时自带「现在：…」对照 |
| 改口/纠正会并存矛盾记忆 | 槽位冲突引擎：同槽新说法覆盖旧条（旧条作废保留可回滚），高证据旧条先人工确认；玩笑、转述、不确定不覆盖 |
| 记忆越积越多、旧的永远占上下文 | 重要性 + 半衰期衰减 + 召回强化；低价值进回收站；置顶永不归档；事件同规则归档 |
| 「喜欢猫 / 喜欢狗 / 喜欢咖啡」互相覆盖 | 偏好按**主题分槽**，不同主题并存；同一个词不同含义（美式咖啡 / 美式穿搭）按**领域**分槽 |
| 同一个人有多个 id（QQ + ChatUI） | 身份归一：ChatUI 主人自动并入主人 QQ，别名映射 + 历史迁移 |
| 记忆越积越多、旧的永远占上下文 | 重要性 + 半衰期衰减 + 召回强化；低价值进回收站；置顶永不归档 |
| 注入被重复内容淹没 | 跨轮去重 + 「用户刚说过的不重复注入」+ 分层注入（约定/近况按需） |
| 记忆文本可能被人当提示注入 | 注入包用「不可信数据」声明包裹；关系自称有人工守卫 |
| 换模型/换机器后记忆丢失 | 全量 JSONL 导出导入 + SQLite 在线备份（WAL 安全）+ 版本升级自动备份 |

---

## 二、功能总览

### 1. 采集（capture）

- 时间线记录：QQ / 适配器消息落 SQLite `timeline`，同秒重复自动去重。
- 新面孔自动建空档案（人物档案层）；空档案默认 7 天清理（可关）。
- ChatUI（webchat）：只记录主人自己的对话（进主人记忆），不建人物档案、不采集其他人；配置 `owner_qq` 后 ChatUI 记录归到主人 QQ 名下。
- 平台白名单 `memory_source_platforms`：默认只记录 QQ 系（`aiocqhttp / qq_official / qq_official_webhook`）。
- 会话白名单 `memory_whitelist`：留空不限制；填群号 / QQ 后仅名单内采集和注入。
- 图片消息：至少记一条 `[图片]` 占位；配 `image_caption_provider_id` 后记「[图片] 转述文字」，带超时保护（`image_caption_timeout_seconds`，0=不转述）。
- bot 回复固定记 `bot_self`，不借用人类 QQ。
- 采集跳过原因会写入 `capture_skip` 诊断，面板和 `/stype status` 可见。

### 2. AI 整理与审核管线（pipeline）

- 候选门槛：只挑有长期价值的消息（自述、指令、状态、纠正），闲聊不进。自述允许主语和谓语之间夹副词（「我平时喜欢…」「我其实不喜欢…」「我最近迷上了…」），也认「我家的猫叫…」「我养了…」这类自家信息。
- **三段式**：批量缩写（normalize）→ 对照原文审核（verify）→ 至多 `pipeline_max_revisions`（默认 2）轮修订 → 通过才写入；不通过进「待审记忆」。
- 拆句：一条消息里的多个独立事实拆成多条（「喜欢美式，不喜欢拿铁」「喜欢和平精英和王者荣耀」都会拆）；模型偷懒合成一条时，修订轮也会走确定性拆句兜底。
- 证据绑定：每条事实必须挂在真实来源消息 id 上，`subject=self` 只接受用户消息，`subject=bot` 只接受 bot 消息。
- 静默触发：会话静默超过 `extract_idle_seconds`（默认 300 秒）后，即使没到批量阈值也整理一次。
- 失败降级：没有可用模型或模型输出无法解析时，退回**启发式抽取**（正则偏好模式），标「未审核」，不阻塞使用；抽取失败有冷却。
- 导入候选走启发式快速通道，不进 LLM。

### 2.5 事件层（整件事记忆，v4.2.0）

- **切段**：同一会话按静默间隔（`event_gap_minutes`，默认 45 分钟）和最长时长（`event_max_hours`，默认 6 小时）切成段；封段后整段送模型。
- **门槛**：用户消息达到 `event_min_messages`（默认 4）或出现叙事线索（去了/决定/参加/聚会…）才成事件，防止把灌水记成事件。
- **续聊合并**：封段后 `event_merge_minutes`（默认 120 分钟）内继续聊，会并入原事件重新摘要（`event_max_per_run` 控制单轮次数）。
- **一次产出**：`kind`（life 经历 / talk 聊过）、标题、80–160 字摘要、2–4 条要点、关键词、重要度、置信度；证据绑定这一段真实消息 id，原文受保护不被时间线压缩。
- **审核**：对照原文审核 + 至多一次修订；没通过不静默丢，存成「待审事件」（低置信），面板确认或改写后转正。
- **降级**：没有可用模型/模型输出不可解析时，用确定性摘要兜底（标记待审），保证事件不丢。
- **追加式**：事件永不互相覆盖；同一窗口短期内继续聊只会更新同一条。

### 2.6 实体链接与时序查询（v4.3.0）

- **实体登记**：写入事实/事件时自动登记人名、关键词、领域（不需要额外模型调用）；改昵称、身份归并后自动跟着换。
- **实体加权**：提问里出现某个实体（「小明」「成都」）时，登记了同一实体的事实和事件加权召回，解决「提谁只想起谁本人说的」。
- **时序查询**：路由识别「以前 / 原来 / 当时 / 曾经」和具体时间（`2025年3月` / `去年12月` / `上个月` / `最近3天`）；检索那段时间为真的旧记忆（含被覆盖的旧说法）。
- **现状对照**：历史块注入时带有效期，被覆盖的旧条同时给出「现在：…」，防止模型把旧状态当现状。
- **隐私照旧**：历史记忆同样遵守 `memory_session_isolation` 的会话隔离规则。

### 3. 写入护栏（contradiction）

- **槽位**：`人格 | 说话人 | 主体 | 属性 | 主题`；偏好（likes/dislikes）带主题，同一主题只有一条 live。
- **改口覆盖**：新的第一人称说法/显式纠正覆盖旧条；旧条标记「作废保留」不删除，默认保留 90 天可回滚，超期由维护清理；置顶条任何冲突都需人工确认。
- **高证据待确认**：旧条置信度高且被访问过，新的单次非纠正说法先进入「待确认覆盖」队列，主人确认才覆盖。
- **玩笑 / 反话 / 转述守卫**：命中玩笑词进入待确认；转述（听说/别人说）标「不确定」；人工补记与审核通过不受此限。
- **关系词守卫**：主人 / owner / 老公老婆 / 爸妈 / 老板 等关系或权限自称，非主人一律拒绝；主人自己的降级为备注。
- **同词不同义**：同一个词、领域明确且不同（「美式」咖啡 vs 「美式」穿搭）→ 分槽并存，不合并。
- **安全阀**：旧条判不出领域、新条判出来了 → 进「待确认覆盖」，通过=存两条，驳回=丢新条。
- **置顶记忆永不自动删除/归档**，冲突也必须人工确认。

### 4. 记忆生命周期（lifecycle）

- 基础重要性：主人手动写入 1.0 / AI 审核通过 0.8 / 未审核 0.5，显式纠正和第一人称再加权。
- 衰减：按半衰期指数衰减；被召回会缩短密度、拉长半衰期（访问强化）。
- 归档：权重低于阈值且超期的记忆在维护时进回收站；状态类记忆按 TTL 过期归档。
- 事件：同一套衰减/强化/归档规则（`event_archive_days`，默认 90 天）；置顶事件不归档，归档后可在面板恢复。
- 作废：改口覆盖的旧说法转 `superseded` 保留（参与「改口摘要」注入、支持回滚），超过 `sleep_superseded_retain_days`（默认 90 天）由维护清理。
- 回收站可恢复；若槽位已被新记忆占用会阻止恢复，避免出现两条冲突事实。
- 面板事实卡显示**当前权重**，可编辑基础重要度；置顶/取消置顶。

### 5. 检索（retrieval）

- 本地关键词打分：BM25（稀有词权重 + 词频饱和 + 长度归一化）叠加 recency + confidence + importance 衰减权重 + 说话人匹配 + 置顶加权；装了 jieba 自动分词并把已批准黑话加入自定义词典，jieba 缺失时回退字符切分，检索照常工作。
- 可选 Embedding：默认关；live 事实达到 `embedding_auto_threshold`（默认 2500，0=从不自动）时自动补一路向量召回（不改配置开关）。
- 可选 Rerank：默认 `auto`（有 Rerank Provider 就用）；Embedding + Rerank 双路结果用 **RRF** 融合，**MMR** 做多样性去重。
- 主人条目与第三人点名（「查一下小明的资料」）单独并入。
- 事件检索：BM25 对标题+摘要+要点+关键词打分，叠加时间新鲜度、重要度衰减与访问强化；`recall`（还记得/上次）事件优先，`time_window`（上周/那天）按事件时间过滤，`current_status`（最近怎么样）只带最近 7 天。
- 检索缓存：昂贵路径（候选扫描、打分、Embedding、Rerank）按 query + 库版本缓存；去重、新颖度过滤在出包前做，所以**缓存不会被去重关掉**。
- 超时降级：Embedding / Rerank 单次调用有 `provider_timeout_seconds`（默认 5 秒，0=不限），超时回退关键词结果并记 `provider_timeout` 诊断。
- 预热：AstrBot 支持 `on_waiting_llm_request` 时，会在会话锁排队期间先跑检索，和等待时间重叠。

### 6. 注入（injection）

- 三档分层：
  - **hot**：Bot 设定（Savage 记忆）等稳定内容，每轮都带且不占去重名额；
  - **warm**：约定 / 近况，只有话题相关（触发词或主题命中）才注入；
  - **cold**：本轮相关事实，按预算逐行装入。
- **事件块**：`【事件】` 放在核心事实之前，按时间渲染 `[日期 · 标题] 摘要（谁讲的）`；预算 `event_budget_chars`（默认 300，0=不限），每轮最多 `event_max_inject`（默认 2）条。
- **窗口全流（C 层，v4.6.0）**：把其他窗口（群聊 / 私聊）最近的**完整消息流**单独作为一段动态内容注入，含其他成员与 Bot 发言、带说话人与时间，解决「群里什么情况」这类问题——B 层只带本人发言的碎片，C 层带整段。默认**双向全通**（私聊 ↔ 群聊），回看 24 小时、最多 150 条 / 6000 字 / 3 个窗口；命中 `window_flow_keywords`（默认「群里 / 群友 / 私聊…」）时才注入，或把 `window_flow_always` 打开每次注入；`window_flow_exclude_private_users` 可屏蔽指定用户的私聊窗口，防止他人私聊内容流入群聊。预览：`/stype flow`。
| `/stype groups` | 列出已知群与编号（指派发言选目标用，管理员/主人） |
| `/stype default <群号\|序号>` | 设置默认群（指派发言的默认目标；`clear` 清除，管理员/主人） |
- **免@主动接话（v4.7.0）**：群聊里没 @ 机器人时，插件按 `reply_gate_mode` 判定是否主动接话——probability 按概率、keyword 命中词表、memory 只在「这条消息命中了记忆（核心/相关事实/事件）」时才接；判定命中后把事件标记为已唤醒，走 AstrBot 默认 LLM 通路（人格 / 记忆注入 / 分段 / TTS 全部照旧）。另有同群冷却（`reply_gate_cooldown_seconds`）、每群每日上限（`reply_gate_daily_limit`）、群白名单（`reply_gate_groups`）、最小字数与跳过命令（`reply_gate_min_chars` / `reply_gate_skip_commands`）。建议与 AstrBot 内置「主动回复」（配置→扩展功能→群聊上下文感知）**二选一**，避免重复接话；判定过程记录在 `/stype diagnostics` 的 `reply_gate` 项。
- **疑问句不再误伤（v5.1.0，v5.3.0 修双向漏洞）**：`query_mentioned` 过滤此前会把「你闺蜜是谁」这类问句里出现的短 value 当作「用户已经说过」而整条挡掉，导致 Bot 明明有记录却答不上来（要查一下才知道）。现在**疑问句 + content 比 value 更长**时不再过滤，正常注入完整内容；v5.3.0 补了两个反向漏洞——短事实（「闺蜜是小美」）问句也能豁免，而「我闺蜜是小美呢」这类带语气词「呢」的**陈述句**不再被当成疑问句放行；历史/事件的新颖度过滤与事实同口径。
- **关键词必回（v5.2.0）**：打开 `reply_gate_keywords_force` 后，任何模式（probability / keyword / memory / judge）下消息只要包含「接话关键词」（`reply_gate_keywords`）里任一词就**必回**，等同于点名必接——@别人、话轮被别人占着也会回；免打扰 / 冷却 / 硬间隔 / 日限仍然生效。适合「问一下」「为什么」「小萨」这类你想让它一定接的词。
- **审查修补 v5.3.0**：无人应答检测之前因把触发问句本行计入查询而**永远不触发**，现已修活（问者自己的追问不算应答）；防抖换人交错发言不再丢消息，且接话门开着时合并消息交由 `reply_gate` 重新判定（不再强制回复）；窗口全流与空@提醒同样包进 `<savagetype_memory>` 不可信声明；strict/owner 隔离下群聊窗口不再带入私聊全流；`/stype flow|rollback|diagnostics|add|groups` 仅管理员/主人可用；画像过与档案卡同一套可见性；`event_max_inject=0` 可彻底关闭事件注入；`learn_window` 与 `webchat_is_owner` 新配置项。
- **免@接话 v2（v5.0.0）**：把「一次掷骰子」换成**分层漏斗**——①规则预筛（长度/命令/白名单/冷却/硬间隔/日限）→ ②**称呼命中必接**（话里叫到 Bot 名字，不@也接；大小写不敏感）/ **关键词必回** → ③**话轮判断**（消息 @/引用了别人就不插嘴，只有开放话轮才可能接）→ ④模式判定（`probability` / `keyword` / `memory` / **`judge` 读空气**：四维打分 相关度 0.3 / 意愿 0.25 / 氛围 0.25 / 时机 0.2，过阈值才接）→ ⑤**无人应答检测**（问句发出后等 N 秒没人回应，Bot 再接「没人答我来」）。注意②的必回也要过①的规则层（只有免打扰/冷却等全过才放行）。另有 **免打扰时段**（`reply_gate_quiet_hours`）。每层判定原因都记录在 `/stype diagnostics` 的 `reply_gate` / `reply_gate_delayed` 项。
- **指派发言（v4.8.0）**：主人在私聊说「去群里说：晚上八点开黑」「跟群友说 明天休息」「去 2 群说：我下课了」「去 987654321 群说：到家了」，Bot 就会把这句发到目标群（默认群或指定群），并回执「已发到群 X」。目标群从插件见过的群窗口里解析（`/stype groups` 查看编号，`/stype default` 设默认群）；发言会计入目标群时间线（`speak_record_to_target`）保证记忆一致；另有每分钟限流、最大字数与群名单（`speak_groups`）约束。发送走 `context.send_message`，需要平台支持主动消息（QQ/NapCat 支持）。
- **谁对谁说（v4.9.0）**：消息捕获时解析 @ 与引用组件，窗口全流的每条记录升级为「谁 → 谁: 内容」（`timeline.addressee` 列，老数据为空不影响）。
- **空@上下文提醒（v4.9.0）**：群里有人只 @ 机器人、不带正文时，注入「上次明确和你对话的人是 X（N 秒前、隔了 M 条）」的提醒，并给出「同一人可能在续话题；拿不准就自然问一句」的引导。开关与有效期见 `blank_mention_hint_*`。
- **消息防抖（v4.9.0，启发式，v5.3.0 补强）**：用户短时间连发几条短消息时，插件先把消息挂起，在「最后一条之后等 `debounce_window_seconds`」或达到 `debounce_max_fragments` 后合并成一条重新提交（走完整的人格/记忆流程）。纯启发式判断（句末标点/连接词/长度），无模型依赖；@/唤醒消息默认直接回复；「好的」「收到」这类完整短回复不等了直接走；换人交错发言时先刷出旧 hold 再建新 hold（不丢消息）；接话门开着时合并消息交由 `reply_gate` 按合并文本重新判定（私聊不受门控）。判定与合并记录在 `/stype diagnostics` 的 `debounce` 项。（v4.9.1 修复：命令回复如 `/stype flow` 的回执不再被当成 Bot 发言写入记忆。）
- **新颖度过滤**：用户当前消息里已经说到的事实（值 ≥2 字）不重复注入；疑问句 + content 更长时豁免（历史/事件同口径）。
- **跨轮去重**：同一会话刚注入过的事实，在 `inject_dedup_window_seconds`（默认 600 秒）内不重复；「还记得 / 上次」与「昨天/上周」类召回问法不受去重限制；去重记录落库且保留时长跟随窗口（最长=窗口+1天），重载插件不丢。
- **预算**：`inject_budget_chars`（默认 800，0=不限）；超预算时事实按行尽量塞，黑话 / few-shot / 草稿整块丢；只有真的进了包的事实才占去重名额。
- **不写不存在的记忆**：档案卡内容被本轮事实覆盖或有意去重时跳过，不重复占预算。
- 注入位置：`req.extra_user_content_parts` 并 `mark_as_temp()`，**不改 system_prompt**；包内稳定块在前、波动块在后，方便供应商前缀缓存。
- 安全：整包用 `<savagetype_memory> 不可信数据` 声明包裹，防止记忆文本被当指令执行。
- 只读声明：人格以 AstrBot 为准，记忆冲突以当前消息为准。

### 7. 过滤与来源可见性

- 人格隔离：记忆带 `persona_id`，跨人格不串。
- **会话隐私隔离**（`memory_session_isolation`，默认 `strict`）：`owner` 档下主人记忆只在主人自己的会话注入；`strict` 档再加一条——私聊来源的记忆（含别人私聊说的）不再注入到群聊，即使被点名。`off` 回到旧行为。
- 说话人优先：本人、主人全局条、被点名的人可见；其他人的私事默认不注入。
- 敏感来源（关系自称）降级为备注或拒收。

### 8. 学习审查（learning，管「怎么说」）

- 黑话统计：词频预筛 + LLM 注释，批准后才参与注入；拒绝的词条会从统计里清除。统计范围默认覆盖平台/白名单内所有人的可见消息（`jargon_scope=all`），可切回只统计主人（`owner`）。
- Few-shot 表达样本：真实「用户→bot」回合对，批准后按相关性注入。
- 排队节流：默认 10 分钟最多扫一次时间线、每轮最多排队 5 条（按质量分择优），避免每条消息都产生待审样本；管理命令 `/stype learn` 会绕过节流立刻扫描。
- 面板审查：按状态（待审/已批准/已驳回）和类型（表达样本/黑话/人格草稿）筛选，支持勾选批量批准、驳回，已审条目可改回待审。
- 人格补丁草稿：参考已批准样本写 80 字内草稿，**不写回人格文件**，批准后约 14 天有效。
- 与「待审记忆」分开：事实自动写，重点审查的是表达方式。

### 9. 维护（sleep / housekeeping）

- 同槽冲突消解；偏好折叠（同主题 note/dislikes → likes，正反矛盾不折叠、跨领域不折叠）。
- 时间线压缩：超保留期且不被任何事实引用的原文清理。
- 低价值归档、衰减归档、状态 TTL 过期、作废记录过期清理、人格草稿过期。
- 黑话词条清理、过期待确认清理、空档案清理。
- 每 6 小时自动跑轻量清理（空档案 + 偏好折叠），手动「维护」跑全量。

### 10. 备份、导入导出

- 全量 JSONL 导出：facts / timeline / pending / reviews / profiles / memory_reviews / aliases / events。
- 聊天文本导入：支持 QQ 风格和字段风格（发送者/时间/内容），可预览。
- JSONL 导入：预览、确认导入（导入前自动备份）、冲突自动消解、可重复导入去重（指纹）。
- SQLite **在线备份 API**（WAL 安全，拷贝不丢未落盘数据）；插件版本升级时自动备份一次。

### 11. 通知与主人回复

- 待审记忆产生后 QQ 通知主人：优先最近一次私聊会话，否则用 `notify_umo`；多条合并、带冷却。
- 主人在 QQ 回复 `是 12` 通过、`否 12` 删除；只有一条待审时可直接回 `是/否`。只认主人。

### 12. 身份归一与别名

- ChatUI 主人自动合并到主人 QQ 身份，历史事实 / 时间线 / 待审记忆一起迁移，合并时同槽自动消解。
- 同名不同 id 会给出归并建议（不自动合并）；`/stype alias` 或面板一键归并。
- 别名会参与检索归组；导出导入保留别名映射。
- 主人身份与人物档案分开：主人只进主人记忆，不建/不显示人物档案；Bot 记忆同理走独立板块。

### 13. 面板（AstrBot WebUI → 插件 → Savage Type → 拓展页）

- **记忆库**：主人记忆 / Savage 记忆（Bot 自己的定义，可手动写入）切换；待审记忆（可直接编辑后过审）；手动补记（自动识别偏好句，一次可写多条）；学习审查（状态/类型筛选、全选、一键批准/驳回全部、改回待审）；待确认覆盖（旧→新对比、来源与通过含义说明）；说话人归并建议。事实卡显示当前权重、有效期（被覆盖/归档的旧条）和「相关」实体。
- **事件**：经历/聊过的事件列表（当前 / 待审 / 已归档 / 置顶筛选），看原文（这一段消息）、编辑标题/摘要/要点/重要度、确认写入、置顶、删除与恢复。
- **人物档案**：搜索、点选查看，改昵称/备注，条目增删改、置顶。
- **诊断**：注入显微镜（路由、命中、过滤原因、`chars≈tokens`）、聊天导入、回收站（归档+被覆盖恢复，槽位冲突阻止）、原始 JSON 诊断、清空并重建（先自动备份）。
- **设置**：150 项配置按左右分栏展示（左侧导航、右侧只显示选中的一组，未保存的输入切组不丢；保存按钮固定在右下；窄屏导航变为顶部横向条）——总开关与主人 / 免@接话 / 指派发言 / 消息防抖 / 记忆注入 / 隐私、画像与跨会话 / 事件与历史 / 抽取与整理 / 学习与表达 / 重要性与维护 / 模型与预算 / 图片 / 外观。
- **外观**：Shader Gradient 风格——近黑底上跑真实 WebGL 片元着色器流动渐变（fbm 域扭曲），内容在磨砂玻璃面板上；5 组主题预设（极光 / 碧金 / 暮霞 / 午夜 / 森林）+ 三色取色器；**动态颜色**开关按固定顺序循环渐变（停 1 秒 / 过渡 5 秒），手动点预设自动关闭。
- 动效降级：devicePixelRatio 封顶 2、离屏暂停、`prefers-reduced-motion` 单帧、WebGL 不可用或上下文丢失时回退静态 CSS 渐变。

### 14. 命令与 LLM 工具

主入口 `/stype`：

| 命令 | 说明 |
| --- | --- |
| `/stype status` | 时间线、记忆数量、采集/注入状态、上次采集跳过原因 |
| `/stype search <关键词>` | 当前说话人可见 live 事实 |
| `/stype explain <关键词>` | 召回路由、命中和过滤原因 |
| `/stype add <内容>` | 手动写入（管理员/主人） |
| `/stype recent [n]` | 最近时间线 |
| `/stype events [n]` | 当前会话最近的事件（整件事记忆） |
| `/stype history [时间或问题]` | 看那段时间的状态与事件，例：`/stype history 去年12月` |
| `/stype extract` | 立刻抽取/整理 |
| `/stype sleep` | 全量维护（含偏好折叠、空档案清理，管理员） |
| `/stype pending` | 待审记忆列表（管理员） |
| `/stype pass <id>` / `/stype drop <id>` | 通过 / 删除待审记忆（管理员） |
| `/stype supersede <id>` | 确认一条待覆盖（管理员） |
| `/stype rollback <id>` | 回滚一条覆盖：新条归档、旧条恢复（管理员） |
| `/stype learn` | 跑一轮黑话/few-shot/人格草稿学习（管理员） |
| `/stype reviews [kind]` | 待审学习项（管理员） |
| `/stype approve <id>` / `/stype reject <id>` | 批准 / 驳回学习草稿（管理员） |
| `/stype dossier [QQ]` | 当前说话人或指定 QQ 的短档案（查别人需管理员/主人） |
| `/stype profile` | 查看自己的跨会话画像（私聊/群里同一份） |
| `/stype cross` | 预览跨会话衔接块：条数、方向拦截（管理员） |
| `/stype flow` | 预览窗口全流（其他窗口最近消息流，含群成员与 Bot；管理员/主人） |
| `/stype export` | 导出 JSONL 到数据目录（管理员） |
| `/stype import 预览\|确认 <路径>` | 预览或导入 JSONL（确认前备份） |
| `/stype alias <旧id> <主id>` | 说话人归并（管理员） |
| `/stype aliases` | 已映射别名 + 同名建议（管理员） |
| `/stype microscope [n]` | 最近注入快照（管理员） |
| `/stype diagnostics` | 诊断快照（管理员/主人） |

LLM 工具（模型可主动调用）：

- `savagetype_recall(query)`：检索当前说话人可见的长期事实；
- `savagetype_remember(content)`：写入事实，**只有主人可用**，返回 `ok=true` 才算记住；
- `savagetype_navigate(query, fact_id)`：多跳召回（最多 3 步，每步 6 条）。

### 15. 共存与降级

- 检测到 `memory_companion` / LivingMemory：重叠的采集或注入自动跳过。
- 检测到 `self_learning`：跳过黑话 / few-shot / 人格草稿，只保留事实层。
- 面板与 `/stype status` 显示降级原因。

---

## 三、技术手段与实现

### 架构

```text
main.py                  AstrBot Star 插件入口：事件钩子、命令、LLM 工具、Web API
savagetype/
  service.py             编排：采集、身份、检索、注入、维护、Provider 调度
  store.py               SQLite 存储（WAL、迁移、原子访问计数、在线备份）
  extract.py             AI 缩写 + 启发式抽取、确定性拆句、证据绑定
  events.py              事件层：按静默间隔切段、整段摘要、审核、追加式写库
  pipeline.py            后台三段式管线（缩写 → 审核 → 修订）
  contradiction.py       写入护栏引擎（槽位、覆盖、待确认、守卫）
  slots.py               规范槽位、领域检测、主题归一
  retrieve.py            检索：本地打分 + Embedding + Rerank + RRF + MMR + 缓存
  inject.py              分层注入包（hot/warm/cold）+ 预算裁剪
  learn.py               黑话 / few-shot / 人格草稿学习
  archive.py             维护、聊天导入、JSONL、备份
  profiles.py            人物档案卡
  coexistence.py         重叠插件检测
  util.py                领域词典、主题归一、token 估算等
pages/console/           面板：index.html / app.js / style.css / shader.js(WebGL)
```

### 关键技术

- **SQLite WAL + 迁移**：单连接 + 可重入锁；启动自动 `ALTER TABLE` 迁移与一次性数据回填（槽位、领域），升级不清库。
- **版本号做缓存键**：内容变更递增 `revision`，检索缓存按 revision 失效；访问计数用 SQL 原子自增，避免旧值回写（不影响缓存）。
- **证据绑定抽取**：LLM 缩写 + 二次审核（对照原文）形成双模型闭环；无模型时正则启发式兜底。
- **CJK 正则偏好解析**：主语（我/俺/咱/主人 可选）、拒绝转述的边界守卫、列举拆分（`、`/`，`/`和`，带词边界断言不拆坏「和平精英」）。
- **领域消歧**：内置领域词典（饮品/食物/穿搭/娱乐/运动），从分句上下文判域，解决「美式」这类同词不同义。
- **槽位 + 冲突决策树**：同槽 → 同值刷新 / 冲突覆盖 / 高证据待确认 / 领域分槽 / 玩笑待审，分支明确可审计。
- **重要性建模**：基础分 × 半衰期指数衰减，访问强化拉长半衰期；维护时按阈值归档。
- **检索融合**：BM25 关键词 + 可选向量余弦，RRF 融合排名，可选 cross-encoder 重排，MMR 去冗余。
- **检索缓存与降级**：昂贵路径缓存 + Provider 超时降级 + 会话锁期间预热。
- **注入工程**：预算逐行装载、hot/warm/cold 分层、新颖度过滤、跨轮去重落库、稳定块优先排序、UNTRUSTED 包裹、临时附加块（不动 system_prompt）。
- **实体链接**：事实/事件写入时登记人名/关键词/领域（改昵称、归并自动跟随），检索时从提问匹配实体并给相关条目加权；不额外调用模型。
- **时序查询**：确定性时间短语解析（绝对年月 / 去年-上个月 / 最近N天 / 模糊「以前」）→ 有效期窗口查询（`created_at` 起、被覆盖或归档时止）→「当时 vs 现在」对照注入，隐私规则照旧生效。
- **事件切段（无 LLM 的确定性算法）**：按会话+静默间隔+最长时长切段，续聊窗口合并、超长按条数/字数切块，门槛过滤灌水，保证同一件事只成一条并可持续更新。
- **会话隐私隔离**：owner 记忆与私聊来源记忆按当前会话类型过滤，拦截原因进注入显微镜。
- **模型调用策略（v4.4.0）**：任务分档（`quality_provider_id` 精准档 / `fast_provider_id` 快速档）→ 显式单任务配置优先 → 旧回退链 → 当前会话模型；解析来源逐次记账，面板显示「今日用量 + 各任务消耗 + 实际走了哪一档」。Token 预算：硬限额停止一切模型调用（消息留给额度恢复后重试，不丢数据）、软限额只停表达学习/向量回填/重排、单次预估超限切备用模型；模型拒答时自动换备用模型重试一次，不把拒答当整理结果。
- **学习审查**：统计预筛 + LLM 注释 + 人工批准后才生效，「先审后用」。
- **WebGL 背景**：手写片元着色器（fbm 噪声域扭曲）、三色插值循环动画、ResizeObserver、上下文丢失回退。

---

## 四、身份与安全

- 主人只认 `owner_qq`（或管理员回退），不认聊天里的自称。
- 每条事实必须能追到证据消息；主体与消息角色强绑定。
- 关系词守卫、玩笑/转述守卫、Bot 定义仅主人可写。
- 平台/会话白名单；人格隔离。
- 记忆注入按「不可信数据」处理，只当参考资料。
- 密钥不进代码不进库；日志不打印记忆原文。

---

## 五、主要配置项（完整见面板「设置」）

| 配置 | 说明 |
| --- | --- |
| `owner_qq` | 主人 QQ，只允许一个；留空回退 AstrBot 管理员 |
| `webchat_is_owner` | ChatUI 视同主人（默认开；暴露在不可信网络请关闭） |
| `enabled` / `capture_enabled` / `inject_enabled` / `extract_enabled` / `learning_enabled` | 总开关与采集、注入、抽取、学习各层开关 |
| `high_evidence_confidence` / `cache_ttl_seconds` / `summary_provider_id` / `debug_log_injection` | 高证据阈值（默认 0.8）、检索缓存秒数、抽取模型、注入调试日志 |
| `memory_source_platforms` | 记忆来源平台，默认 `aiocqhttp,qq_official,qq_official_webhook` |
| `memory_whitelist` | 群号/QQ 白名单，留空不限制 |
| `pipeline_enabled` / `normalize_provider_id` / `verify_provider_id` | AI 整理与审核（面板只列对话模型） |
| `quality_provider_id` / `fast_provider_id` / `fallback_provider_id` | 模型档位与备用模型（显式单任务配置优先于档位） |
| `daily_token_limit` / `soft_token_limit` / `single_call_token_cap` | 每日硬限额、软限额、单次预估上限（0=不限） |
| `extract_min_messages` / `extract_cooldown_seconds` / `extract_idle_seconds` | 抽取阈值、冷却、空闲触发 |
| `pipeline_batch_size` / `pipeline_max_revisions` / `pipeline_notify_cooldown_seconds` | 批量、最大修订轮数、通知冷却 |
| `retrieval_mode` / `retrieval_bm25` / `top_k` / `core_fact_limit` / `related_fact_limit` | 检索模式与条数 |
| `embedding_enabled` / `embedding_auto_threshold` / `embedding_provider_id` / `rerank_provider_id` | 向量与重排（AstrBot 原生页不支持选择器，需手填 ID） |
| `provider_timeout_seconds` | Embedding/Rerank 超时（默认 5 秒，0=不限） |
| `inject_budget_chars` / `inject_warm_triggered` / `inject_novelty_filter` / `inject_dedup_window_seconds` | 注入预算、按需注入、新颖度过滤、跨轮去重 |
| `event_enabled` / `event_gap_minutes` / `event_max_hours` / `event_min_messages` | 事件层开关、切段静默、单段最长时长、成段最少用户消息 |
| `event_merge_minutes` / `event_max_per_run` / `event_provider_id` | 续聊合并窗口、单轮最多整理段数、事件摘要模型（默认回退整理模型） |
| `event_budget_chars` / `event_max_inject` / `event_archive_days` | 事件注入预算、每轮最多注入条数、事件归档最短天数 |
| `memory_session_isolation` | 会话隐私隔离：`off` / `owner` / `strict`（默认 strict） |
| `profile_inject_enabled` / `profile_max_chars` | 每轮注入跨会话画像卡（称呼/身份/偏好/语气），默认 开、上限 300 字 |
| `cross_window_enabled` / `cross_window_minutes` / `cross_window_max_items` / `cross_window_max_chars` | 跨会话衔接：默认 开、窗口 30 分钟、最多 6 条、上限 320 字 |
| `cross_window_private_to_group` / `cross_window_group_to_group` | 衔接方向：私聊→群、群→群 默认 关（私→私、群→私 恒开） |
| `window_flow_enabled` / `window_flow_always` / `window_flow_keywords` | 窗口全流（C 层）：其他窗口的完整消息流；开关、是否每次都注入、触发关键词（回看 24 小时 / 150 条 / 6000 字 / 3 窗口为默认值） |
| `window_flow_hours` / `window_flow_max_items` / `window_flow_max_chars` | 窗口全流回看小时数（24）、最多条数（150）、最多字符（6000） |
| `window_flow_max_windows` / `window_flow_msg_chars` / `window_flow_include_bot` | 窗口全流最多窗口数（3）、单条截断（200 字）、是否包含 Bot 发言 |
| `window_flow_group_to_private` / `window_flow_private_to_group` / `window_flow_exclude_private_users` | 窗口全流方向（默认双向开）与私聊排除名单（填用户 ID，逗号分隔）；strict/owner 隔离下群聊窗口强制不带私聊来源 |
| `reply_gate_enabled` / `reply_gate_mode` | 免@主动接话开关与判定方式（probability 概率 / keyword 关键词 / memory 记忆命中） |
| `reply_gate_probability` / `reply_gate_keywords` | 概率模式的接话概率（默认 0.05）、关键词词表（keyword 模式判定依据；打开必回后为任何模式的必回词表） |
| `reply_gate_keywords_force` | 关键词必回：任何模式下命中词表就必回（等同点名，越过话轮过滤；免打扰/冷却/日限仍生效） |
| `reply_gate_groups` / `reply_gate_cooldown_seconds` / `reply_gate_daily_limit` | 接话群白名单（空=所有群）、同群冷却（90 秒）、每群每日上限（30） |
| `reply_gate_min_chars` / `reply_gate_skip_commands` | 最小接话字数（2）、是否跳过以 / ！ 开头的命令消息 |
| `reply_gate_judge_provider_id` / `reply_gate_judge_threshold` / `reply_gate_judge_context_messages` | 读空气判定（judge 模式）：打分模型（留空=当前会话模型）、阈值（0.6）、参考消息数（6） |
| `reply_gate_turn_filter_enabled` | 话轮判断：消息 @/引用了别人就不插嘴（只有开放话轮才可能接） |
| `reply_gate_unanswered_enabled` / `reply_gate_unanswered_seconds` | 无人应答检测：问句发出后等 20 秒没人回应才接话 |
| `reply_gate_quiet_hours` | 免打扰时段（如 `1:00-7:00`，分号分隔，支持跨零点） |
| `reply_gate_name_hit_enabled` / `reply_gate_bot_names` | 称呼命中必接：不@、但叫到了 Bot 的名字（名单 + Bot 设定事实） |
| `reply_gate_min_interval_seconds` | 同群接话硬间隔（默认 60 秒，防连续刷屏） |
| `speak_enabled` / `speak_default_group` | 指派发言开关与默认目标群（群号或 umo；留空用最近活跃的群） |
| `speak_groups` / `speak_require_owner` | 允许发言的群名单（留空=不额外限制）、是否仅主人可指派 |
| `speak_rate_limit_per_min` / `speak_max_chars` | 每分钟上限（5）、单条最大字数（300） |
| `speak_reply_receipt` / `speak_record_to_target` | 发送后私聊回执、是否记入目标群时间线 |
| `blank_mention_hint_enabled` / `blank_mention_hint_ttl_minutes` / `blank_mention_hint_gap_messages` | 空@上下文提醒：开关、有效期（30 分钟）、间隔条数（12） |
| `debounce_enabled` / `debounce_scope` | 消息防抖（启发式）：开关、范围（both/group/private） |
| `debounce_window_seconds` / `debounce_max_seconds` / `debounce_max_fragments` | 防抖等待窗口（2.5 秒）、最长等待（8 秒）、最多合并条数（4） |
| `debounce_short_chars` / `debounce_skip_wake` / `debounce_max_chars` | 只防抖短消息（12 字）、@/唤醒消息直接回复、合并上限（600 字） |
| `entity_linking_enabled` / `entity_boost_weight` | 实体链接开关与命中加成分（默认 0.2） |
| `history_enabled` / `history_max_facts` | 时序查询开关与历史块条数上限（默认 6） |
| `importance_weight` / `importance_half_life_days` / `importance_reinforce_factor` / `importance_max_half_life_multiplier` / `importance_prune_threshold` | 重要性、半衰期、强化、归档阈值 |
| `sleep_timeline_retain_days` / `sleep_low_value_days` / `sleep_low_value_confidence` / `sleep_superseded_retain_days` | 维护保留期与归档条件 |
| `image_caption_provider_id` / `image_caption_timeout_seconds` | 图片转述模型与超时 |
| `jargon_enabled` / `jargon_scope` / `fewshot_enabled` / `fewshot_cooldown_seconds` / `fewshot_max_per_run` / `fewshot_min_quality` / `persona_draft_enabled` 等 | 学习开关与限额 |
| `learn_window` | 学习每轮回看的最近时间线条数（默认 40） |
| `empty_profile_ttl_days` / `notify_umo` / `coexistence_degrade` | 空档案清理、通知会话、共存降级 |
| `ui_theme_color` / `ui_theme_color2` / `ui_theme_color3` / `ui_dynamic_colors` | 面板三色主题与动态颜色 |

---

## 六、安装与数据

目录名保持 `astrbot_plugin_savagetype`。可从 GitHub 安装或拷贝到 `AstrBot/data/plugins/astrbot_plugin_savagetype` 后重载。

数据目录：`AstrBot/data/plugin_data/astrbot_plugin_savagetype/`（SQLite 库 + backups/exports）。更新插件不会覆盖这个库。升级会先自动备份；v2.8.0 首次启动会备份并清空旧脏数据（旧版归属错位无法自动纠正），别名和配置保留。

插件 Logo：`logo.png`（256×256，由 `_logo.webp` 转换，遵循插件开发指南的 1:1 规范）。拓展页左上角使用 `pages/console/_logo.webp`。

---

## 七、离线测试

```text
python tests/test_core.py -v
python tests/test_soak.py -v
```

170 + 14 个测试，只用 Python 3.11+ 标准库，不联网。`test_soak.py` 偏慢，专门压异常模型输出、并发写入、几千条规模、老库迁移和全链路。`tests/verify_readme.py` 核查文档里的命令、配置、默认值、面板路由与版本号是否和代码一致。前端可做静态检查：把 `pages/console/app.js` / `shader.js` 复制为 `.mjs` 后 `node --check`。

集成测试（24 个，真实 AstrBot 框架 + 假 Context/假事件/脚本化假模型，覆盖插件加载、采集钩子、注入、全部命令、LLM 工具、全部面板接口，以及任务分档、Token 预算闸、拒答重试）：

```text
python -m venv .itvenv
.itvenv/Scripts/python -m pip install astrbot
.itvenv/Scripts/python tests/test_integration.py -v
```

面板 UI 测试（jsdom 里跑真实的 `app.js`：学习审查的筛选/全选/批量按钮行为、按钮必须在滚动区之外、卡片不被拉高等排版规则）：

```text
npm install jsdom --prefix %TEMP%\uitest
set JSDOM_DIR=%TEMP%\uitest
node tests/test_panel.mjs
```

没有 `astrbot` / 没装 jsdom 时对应套件无法运行，不影响其它测试。

---

## 八、灵感与边界

- 记忆分层、Embedding/Rerank、分槽召回：参考 [astrbot_plugin_memory_companion](https://github.com/menglimi/astrbot_plugin_memory_companion) 的产品分工，未克隆其权限拓扑和陪伴功能。
- 省 token、动态记忆注入：参考 [lily](https://github.com/mcxxiu/lily)。
- 学习审查「先审后用」：参考 [astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning)（AGPL-3.0，未使用其代码）。
- 生命周期打分、跨轮去重、回收站等思路参考 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)、[Memorix](https://github.com/exynos967/astrbot_plugin_memorix)、[lancedb-pro](https://github.com/win4r/memory-lancedb-pro) 等开源插件的公开设计，均为自研实现，未复制代码，避免引入 AGPL/GPL 传染。
- 人机工程与延迟优化（检索缓存、Provider 超时、会话锁预热）参考通用 RAG 延迟实践与 AstrBot 生态讨论。
- AstrBot 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html

**明确不做**：好感/情绪数值、日程与主动陪伴、把草稿写回人格文件、私聊/群聊 ACL 拓扑、改 system_prompt。
