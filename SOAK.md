# Savage Type 实机联调（约 10 分钟）

离线 28 测过的是库和规则。这里确认 AstrBot 真的挂上了采集、注入、面板。

## 0. 安装

1. 把整个 `astrbot_plugin_savagetype` 目录放到 AstrBot 插件目录，名字不要改。常见位置：
   - `AstrBot\data\plugins\astrbot_plugin_savagetype`
   - `%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_savagetype`
2. 重启 AstrBot，或在插件管理里重新加载。
3. 日志应出现 `Savage Type loaded, db=...`。没有这句话 = 没装上。

联调时建议先关掉 / 不要同时开 `memory_companion`、LivingMemory。开着的话 Savage Type 会降级采集或注入，显微镜会是空的，这是设计不是故障。`self_learning` 开着则风格/黑话会让出，事实记忆仍应工作。

Embedding 保持默认关。这一轮不测向量。

## 1. 加载

对 Bot 发：`/stype status`

应看到时间线/live 数量、采集与注入是否开启、Embedding 状态（关闭 / 缺 Provider / 实际启用）。报未知命令 = 插件没进加载列表。

## 2. 采集

对 Bot 说一句稳定事实，例如：`我喜欢喝美式，不喜欢拿铁。`

再发：`/stype recent 5`

应能看到刚才那句用户消息。没有 = `on_message` 没挂上，或消息被当成指令丢掉了。

等一两分钟（抽取阈值默认 8 条；联调可在面板点「抽取」或发 `/stype extract`），再发：`/stype search 美式`

应出现 live 事实。没有 = 抽取没跑，看日志里有没有 `Savage Type extract failed`。

## 3. 注入

再问：`我喜欢喝什么来着？`

Bot 应能答美式。然后发：`/stype microscope`

应有一条 route / core / pack_chars。没有快照 = `on_llm_request` 没跑到 `build_injection`（常见原因：共存降级、总开关关了、问题被当成低信息）。

打开拓展页 Savage Type，看「注入显微镜」是否同一条记录。

## 4. 改口

说：`我改口了，喜欢拿铁。`

抽取后再问喜欢什么。应答拿铁。`/stype search 美式` 里旧条应是 superseded，或显微镜出现改口摘要。

## 5. 面板

拓展页应能：总览 KPI、检索、待确认覆盖、待审学习、显微镜。点不开 = Pages 没注册，看启动日志。

## 判定

- 1–5 都过：实机联调通过，savagetype 可以收口。
- 某步失败：把该步的聊天原文、`/stype status`、`/stype microscope`、相关日志贴回来。不要先开 Bili Learn。
