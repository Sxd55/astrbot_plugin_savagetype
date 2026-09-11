# astrbot_plugin_savagetype

Savage Type 是面向 AstrBot 的全局人格记忆中枢。Savage 只是插件名。身份和语气永远读 AstrBot 当前人格；本插件只负责记住事实、处理改口、在需要时把少量相关记忆注入本轮对话。不改写人格文件，不做日程和主动陪伴。

当前版本 `v2.6.0`。仓库：https://github.com/Sxd55/astrbot_plugin_savagetype

要求 AstrBot `>= 4.22.0`。

## 它解决什么问题

对话记录不等于记忆。本插件把消息先写入时间线，再抽成稳定事实；主链请求前只注入本轮真正相关的一小包资料。当前用户消息始终是主任务，记忆只是辅助。

一条记忆会带说话人 QQ / 平台 id、昵称、人格 id。默认**只采集 AstrBot 管理员用第一人称明确说出的关于自己的事**（例如「我喜欢喝茶」「记住我叫小明」「我改口了」）。闲聊、别人的话、非管理员消息不进时间线。检索仍偏向当前说话人；白名单可再限制窗口。

## 数据怎么流动

1. 用户说话、Bot 回复进入时间线。原文不当长期记忆。
2. 未总结条数达到阈值（默认 8）后自动抽取；也可在面板点「抽取事实」立刻抽。启发式能抓住「我喜欢 / 不喜欢 / 叫我」；配了总结模型时额外走 LLM。
3. 抽出来的是 live 事实。同一人格、同一说话人、同一规范化槽（例如喜欢/口味都算 likes）发生冲突时：新的生效，旧的直接删除。高证据旧事实被单次非纠正说法挑战时，先进「待确认覆盖」。玩笑、转述、不确定不会覆盖。
4. 下一轮 LLM 请求前，按当前这句话检索，把核心事实、本轮相关、必要时的黑话释义 / 表达样本 / 人格草稿打成临时记忆包注入。低信息消息（嗯、好、哈哈哈）几乎不召回。
5. 注入包走 `req.extra_user_content_parts`，并 `mark_as_temp()`，不改 `system_prompt`，避免打爆前缀缓存。超预算时核心事实按行尽量塞，黑话 / few-shot / 草稿整块丢，不从中间截标签。

数据目录：`AstrBot/data/plugin_data/astrbot_plugin_savagetype/`。更新插件不会覆盖这个库。

## 面板（推荐日常都用这里）

AstrBot WebUI → 插件 → Savage Type → 拓展页。常用能力都做成按钮，不必打 `/stype`。

工作台顶栏：

- 刷新：重新加载计数、列表、注入快照。
- 抽取事实：立刻把未总结时间线压成事实，不等满 8 条。
- 维护：睡眠整理。合并同一槽重复、把旧的 likes/dislikes/note 折进同一条、压缩过期已总结时间线、归档低价值事实、过期人格草稿。
- 跑一轮学习：从对话里挖黑话候选、user→bot 样本、人格草稿，全部进审查队列，批准后才注入。
- 导出 JSONL：下载可移植档案（不含向量索引和 Provider 凭据）。浏览器会弹出保存。

工作台分区：

- 人物档案：每人一张短卡，键是 QQ。有 live 事实才出现。主链只注入当前说话人的压缩卡（称呼、偏好、习惯、约定），不是整份传记。
- 学习审查：黑话释义、few-shot、人格草稿的待办。批准后才进主链；驳回后同一指纹默认不再入队。和「待确认覆盖」不是一回事。
- 待确认覆盖：高证据旧事实被新说法挑战时，在这里确认换还是驳回。
- 说话人归并：同名不同 QQ 的建议。不会自动合并，要点「映射」。
- 注入显微镜：最近几轮主链实际注入了什么（路由、QQ、字数、过滤原因）。用来排障，不是学习审查。
- 聊天导入：把导出的聊天文本或 JSONL 预览后导入。路径默认插件数据目录。
- 诊断：最近一次接口返回的 JSON，可滚动。排障时看这里。
- 设置：字段与 AstrBot 插件配置页相同。保存后立刻写回配置文件。

点按钮后屏幕中间会出短暂提示。AstrBot 拓展页在 iframe 里，删除不再弹系统确认框（会被拦，看起来像失败）。

## 学习审查是什么

事实会自动写。审查管的是「怎么说」：

- 黑话释义：高频词的含义。只在用户这句话里真出现该词时注入，只解释、不教 Bot 复读。
- few-shot：真实的「用户一句 → Bot 一句」。批准后当接话参考，不逐字照搬。
- 人格草稿：根据已批准样本写的不超过 80 字补丁。不写回 AstrBot 人格文件，只当本轮语气提示。默认一天最多生成一次，批准后约 14 天有效。

## 三个后台动作

抽取事实：时间线 → 稳定事实。自动有防抖（默认 45 秒）和失败冷却（默认 180 秒）。LLM 抽取失败不会把时间线标成已总结。

维护：库的打扫。live 多了、改口留下多条重复时点它。

跑一轮学习：只生产审查草稿，不直接改人格、不直接注入。

## 白名单

设置里的「记忆白名单」。留空不限制。填写群号或 QQ，逗号分隔后，只有名单内的群聊 / 私聊会采集和注入。

## Embedding

默认关。可手开。live 事实达到 2500（`embedding_auto_threshold`，0=从不自动）时自动用向量补充召回，但不会改掉配置开关。没有 Embedding Provider 只提示、不强开。Rerank 在检索模式 `auto` 下有 Provider 才用。

## 命令（管理员部分需要管理员）

主入口 `/stype`。

| 命令 | 说明 |
| --- | --- |
| `/stype status` | 时间线、live 数量、采集/注入是否开启 |
| `/stype search <关键词>` | 当前说话人可见 live 事实 |
| `/stype explain <关键词>` | 召回路由、命中和过滤原因 |
| `/stype add <内容>` | 手动写入（说话人是当前聊天对象） |
| `/stype recent [n]` | 最近时间线 |
| `/stype extract` | 立刻抽取 |
| `/stype sleep` | 维护 |
| `/stype learn` | 跑一轮学习 |
| `/stype reviews [kind]` | 待审学习项 |
| `/stype approve <id>` | 批准学习草稿 |
| `/stype reject <id>` | 驳回学习草稿 |
| `/stype dossier [QQ]` | 当前说话人或指定 QQ 的短档案 |
| `/stype export` | 导出 JSONL 到数据目录 |
| `/stype import 预览\|确认 <路径>` | 预览或导入 JSONL（确认前会备份当前库） |
| `/stype alias <旧id> <主id>` | 说话人归并 |
| `/stype aliases` | 已映射别名 + 同名建议 |
| `/stype diagnostics` | 诊断快照 |

LLM 工具：`savagetype_recall` 检索，`savagetype_remember` 写入（只有返回 `ok=true` 才算记住），`savagetype_navigate` 多跳召回（最多 3 步、每步 6 条）。模型打开插件页不等于已经写入，要以工具返回或面板 live 列表为准。

## 共存

检测到 `memory_companion` / LivingMemory 时跳过重叠的采集或注入。检测到 `self_learning` 时跳过黑话 / few-shot / 人格草稿，只保留事实层。目录里有插件文件不等于 AstrBot 当前启用了它。

## 安装

目录名保持 `astrbot_plugin_savagetype`。可从 GitHub 安装：

https://github.com/Sxd55/astrbot_plugin_savagetype

或拷到 `AstrBot/data/plugins/astrbot_plugin_savagetype` 后重载。

封面图：`_logo.webp`（市场卡片）和 `pages/console/_logo.webp`（拓展页左上角）。

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
