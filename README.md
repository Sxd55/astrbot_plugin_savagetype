# astrbot_plugin_savagetype

**Savage Type** 是面向 AstrBot 的全局人格记忆中枢。Savage 只是插件名：身份和语气永远读 AstrBot 当前人格，本插件只负责**记住事实、处理改口、在需要时把少量相关记忆注入本轮对话**。不改写人格文件，不做日程和主动陪伴。

当前版本 `v4.0.0`。仓库：https://github.com/Sxd55/astrbot_plugin_savagetype
要求 AstrBot `>= 4.22.0`；离线测试只需 Python 3.11+ 标准库。

---

## 一、它解决什么问题

| 问题 | 本插件的做法 |
| --- | --- |
| 对话记录不等于记忆，全塞回上下文又贵又乱 | 消息先落时间线，后台 AI 缩写成「直白事实」并对照原文审核，只把与本轮相关的一小包注入 |
| 改口/纠正会并存矛盾记忆 | 槽位冲突引擎：同槽新说法覆盖旧条，高证据旧条先人工确认；玩笑、转述、不确定不覆盖 |
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

- 候选门槛：只挑有长期价值的消息（自述、指令、状态、纠正），闲聊不进。
- **三段式**：批量缩写（normalize）→ 对照原文审核（verify）→ 至多 `pipeline_max_revisions`（默认 2）轮修订 → 通过才写入；不通过进「待审记忆」。
- 拆句：一条消息里的多个独立事实拆成多条（「喜欢美式，不喜欢拿铁」「喜欢和平精英和王者荣耀」都会拆）；模型偷懒合成一条时，修订轮也会走确定性拆句兜底。
- 证据绑定：每条事实必须挂在真实来源消息 id 上，`subject=self` 只接受用户消息，`subject=bot` 只接受 bot 消息。
- 静默触发：会话静默超过 `extract_idle_seconds`（默认 300 秒）后，即使没到批量阈值也整理一次。
- 失败降级：没有可用模型或模型输出无法解析时，退回**启发式抽取**（正则偏好模式），标「未审核」，不阻塞使用；抽取失败有冷却。
- 导入候选走启发式快速通道，不进 LLM。

### 3. 写入护栏（contradiction）

- **槽位**：`人格 | 说话人 | 主体 | 属性 | 主题`；偏好（likes/dislikes）带主题，同一主题只有一条 live。
- **改口覆盖**：新的第一人称说法/显式纠正覆盖旧条；旧条删除前会保护置顶条。
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
- 回收站可恢复；若槽位已被新记忆占用会阻止恢复，避免出现两条冲突事实。
- 面板事实卡显示**当前权重**，可编辑基础重要度；置顶/取消置顶。

### 5. 检索（retrieval）

- 本地关键词打分：recency + confidence + importance 衰减权重 + 说话人匹配 + 置顶加权。
- 可选 Embedding：默认关；live 事实达到 `embedding_auto_threshold`（默认 2500，0=从不自动）时自动补一路向量召回（不改配置开关）。
- 可选 Rerank：默认 `auto`（有 Rerank Provider 就用）；Embedding + Rerank 双路结果用 **RRF** 融合，**MMR** 做多样性去重。
- 主人条目与第三人点名（「查一下小明的资料」）单独并入。
- 检索缓存：昂贵路径（候选扫描、打分、Embedding、Rerank）按 query + 库版本缓存；去重、新颖度过滤在出包前做，所以**缓存不会被去重关掉**。
- 超时降级：Embedding / Rerank 单次调用有 `provider_timeout_seconds`（默认 5 秒，0=不限），超时回退关键词结果并记 `provider_timeout` 诊断。
- 预热：AstrBot 支持 `on_waiting_llm_request` 时，会在会话锁排队期间先跑检索，和等待时间重叠。

### 6. 注入（injection）

- 三档分层：
  - **hot**：Bot 设定（Savage 记忆）等稳定内容，每轮都带且不占去重名额；
  - **warm**：约定 / 近况，只有话题相关（触发词或主题命中）才注入；
  - **cold**：本轮相关事实，按预算逐行装入。
- **新颖度过滤**：用户当前消息里已经说到的事实（值 ≥2 字）不重复注入。
- **跨轮去重**：同一会话刚注入过的事实，在 `inject_dedup_window_seconds`（默认 600 秒）内不重复；「还记得 / 上次」类问题豁免；去重记录落库，重载插件不丢。
- **预算**：`inject_budget_chars`（默认 800，0=不限）；超预算时事实按行尽量塞，黑话 / few-shot / 草稿整块丢；只有真的进了包的事实才占去重名额。
- **不写不存在的记忆**：档案卡内容被本轮事实覆盖或有意去重时跳过，不重复占预算。
- 注入位置：`req.extra_user_content_parts` 并 `mark_as_temp()`，**不改 system_prompt**；包内稳定块在前、波动块在后，方便供应商前缀缓存。
- 安全：整包用 `<savagetype_memory> 不可信数据` 声明包裹，防止记忆文本被当指令执行。
- 只读声明：人格以 AstrBot 为准，记忆冲突以当前消息为准。

### 7. 过滤与来源可见性

- 人格隔离：记忆带 `persona_id`，跨人格不串。
- 说话人优先：本人、主人全局条、被点名的人可见；其他人的私事默认不注入。
- 敏感来源（关系自称）降级为备注或拒收。

### 8. 学习审查（learning，管「怎么说」）

- 黑话统计：词频预筛 + LLM 注释，批准后才参与注入；拒绝的词条会从统计里清除。
- Few-shot 表达样本：真实「用户→bot」回合对，批准后按相关性注入。
- 人格补丁草稿：参考已批准样本写 80 字内草稿，**不写回人格文件**，批准后约 14 天有效。
- 与「待审记忆」分开：事实自动写，重点审查的是表达方式。

### 9. 维护（sleep / housekeeping）

- 同槽冲突消解；偏好折叠（同主题 note/dislikes → likes，正反矛盾不折叠、跨领域不折叠）。
- 时间线压缩：超保留期且不被任何事实引用的原文清理。
- 低价值归档、衰减归档、状态 TTL 过期、人格草稿过期。
- 黑话词条清理、过期待确认清理、空档案清理。
- 每 6 小时自动跑轻量清理（空档案 + 偏好折叠），手动「维护」跑全量。

### 10. 备份、导入导出

- 全量 JSONL 导出：facts / timeline / pending / reviews / profiles / memory_reviews / aliases。
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

- **记忆库**：主人记忆 / Savage 记忆（Bot 自己的定义，可手动写入）切换；待审记忆（可直接编辑后过审）；手动补记（自动识别偏好句，一次可写多条）；学习审查；待确认覆盖（原因中文化）；说话人归并建议。
- **人物档案**：搜索、点选查看，改昵称/备注，条目增删改、置顶。
- **诊断**：注入显微镜（路由、命中、过滤原因、`chars≈tokens`）、聊天导入、回收站（归档恢复，槽位冲突阻止）、原始 JSON 诊断、清空并重建（先自动备份）。
- **设置**：62 项配置按 8 组分类（默认收起）：总开关与采集 / 抽取与整理 / 检索与注入 / 重要性与维护 / 学习与人格草稿 / 图片 / Embedding 与 Rerank / 界面配色。
- **外观**：Shader Gradient 风格——近黑底上跑真实 WebGL 片元着色器流动渐变（fbm 域扭曲），内容在磨砂玻璃面板上；5 组主题预设（极光 / 碧金 / 暮霞 / 午夜 / 森林）+ 三色取色器；**动态颜色**开关按固定顺序循环渐变（停 1 秒 / 过渡 5 秒），手动点预设自动关闭。
- 动效降级：devicePixelRatio 封顶 2、离屏暂停、`prefers-reduced-motion` 单帧、WebGL 不可用或上下文丢失时回退静态 CSS 渐变。

### 14. 命令与 LLM 工具

主入口 `/stype`：

| 命令 | 说明 |
| --- | --- |
| `/stype status` | 时间线、记忆数量、采集/注入状态、上次采集跳过原因 |
| `/stype search <关键词>` | 当前说话人可见 live 事实 |
| `/stype explain <关键词>` | 召回路由、命中和过滤原因 |
| `/stype add <内容>` | 手动写入（说话人是当前聊天对象） |
| `/stype recent [n]` | 最近时间线 |
| `/stype extract` | 立刻抽取/整理 |
| `/stype sleep` | 全量维护（含偏好折叠、空档案清理，管理员） |
| `/stype pending` | 待审记忆列表（管理员） |
| `/stype pass <id>` / `/stype drop <id>` | 通过 / 删除待审记忆（管理员） |
| `/stype supersede <id>` | 确认一条待覆盖（管理员） |
| `/stype learn` | 跑一轮黑话/few-shot/人格草稿学习（管理员） |
| `/stype reviews [kind]` | 待审学习项（管理员） |
| `/stype approve <id>` / `/stype reject <id>` | 批准 / 驳回学习草稿（管理员） |
| `/stype dossier [QQ]` | 当前说话人或指定 QQ 的短档案（查别人需管理员/主人） |
| `/stype export` | 导出 JSONL 到数据目录（管理员） |
| `/stype import 预览\|确认 <路径>` | 预览或导入 JSONL（确认前备份） |
| `/stype alias <旧id> <主id>` | 说话人归并（管理员） |
| `/stype aliases` | 已映射别名 + 同名建议（管理员） |
| `/stype microscope [n]` | 最近注入快照（管理员） |
| `/stype diagnostics` | 诊断快照 |

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
- **检索融合**：本地关键词 + 可选向量余弦，RRF 融合排名，可选 cross-encoder 重排，MMR 去冗余。
- **检索缓存与降级**：昂贵路径缓存 + Provider 超时降级 + 会话锁期间预热。
- **注入工程**：预算逐行装载、hot/warm/cold 分层、新颖度过滤、跨轮去重落库、稳定块优先排序、UNTRUSTED 包裹、临时附加块（不动 system_prompt）。
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
| `memory_source_platforms` | 记忆来源平台，默认 `aiocqhttp,qq_official,qq_official_webhook` |
| `memory_whitelist` | 群号/QQ 白名单，留空不限制 |
| `pipeline_enabled` / `normalize_provider_id` / `verify_provider_id` | AI 整理与审核（面板只列对话模型） |
| `extract_min_messages` / `extract_cooldown_seconds` / `extract_idle_seconds` | 抽取阈值、冷却、空闲触发 |
| `pipeline_batch_size` / `pipeline_max_revisions` / `pipeline_notify_cooldown_seconds` | 批量、最大修订轮数、通知冷却 |
| `retrieval_mode` / `top_k` / `core_fact_limit` / `related_fact_limit` | 检索模式与条数 |
| `embedding_enabled` / `embedding_auto_threshold` / `embedding_provider_id` / `rerank_provider_id` | 向量与重排（AstrBot 原生页不支持选择器，需手填 ID） |
| `provider_timeout_seconds` | Embedding/Rerank 超时（默认 5 秒，0=不限） |
| `inject_budget_chars` / `inject_warm_triggered` / `inject_novelty_filter` / `inject_dedup_window_seconds` | 注入预算、按需注入、新颖度过滤、跨轮去重 |
| `importance_weight` / `importance_half_life_days` / `importance_reinforce_factor` / `importance_max_half_life_multiplier` / `importance_prune_threshold` | 重要性、半衰期、强化、归档阈值 |
| `sleep_timeline_retain_days` / `sleep_low_value_days` / `sleep_low_value_confidence` | 维护保留期与归档条件 |
| `image_caption_provider_id` / `image_caption_timeout_seconds` | 图片转述模型与超时 |
| `jargon_enabled` / `fewshot_enabled` / `persona_draft_enabled` 等 | 学习开关与限额 |
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
```

93 个测试，只用 Python 3.11+ 标准库，不联网。前端可做静态检查：把 `pages/console/app.js` / `shader.js` 复制为 `.mjs` 后 `node --check`。

---

## 八、灵感与边界

- 记忆分层、Embedding/Rerank、分槽召回：参考 [astrbot_plugin_memory_companion](https://github.com/menglimi/astrbot_plugin_memory_companion) 的产品分工，未克隆其权限拓扑和陪伴功能。
- 省 token、动态记忆注入：参考 [lily](https://github.com/mcxxiu/lily)。
- 学习审查「先审后用」：参考 [astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning)（AGPL-3.0，未使用其代码）。
- 生命周期打分、跨轮去重、回收站等思路参考 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)、[Memorix](https://github.com/exynos967/astrbot_plugin_memorix)、[lancedb-pro](https://github.com/win4r/memory-lancedb-pro) 等开源插件的公开设计，均为自研实现，未复制代码，避免引入 AGPL/GPL 传染。
- 人机工程与延迟优化（检索缓存、Provider 超时、会话锁预热）参考通用 RAG 延迟实践与 AstrBot 生态讨论。
- AstrBot 插件开发文档：https://docs.astrbot.app/dev/star/plugin-new.html

**明确不做**：好感/情绪数值、日程与主动陪伴、把草稿写回人格文件、私聊/群聊 ACL 拓扑、改 system_prompt。
