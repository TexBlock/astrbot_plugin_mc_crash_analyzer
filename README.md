# Not Enough Crash

Not Enough Crash 是一个 AstrBot 插件，用于在配置的群聊中自动、静默地分析 Minecraft/PCL 崩溃报告文件。

插件会监听白名单群聊里的直接文件消息，识别受支持的崩溃报告文件后，使用 AstrBot 当前或默认 LLM 提供商生成分析结果。分析成功时，结果会以合并转发消息发送回来源群；分析失败时只写入日志，不在群内提示。

## 平台支持

本插件主要面向 QQ/NapCat/OneBot v11 场景设计。

合并转发报告依赖 OneBot v11 的相关支持，因此其他平台即使能接收文件，也不保证能正常发送合并转发结果。

## 配置项

在插件配置中设置以下字段：

- `enabled_group_ids`：启用自动分析的群号列表。只有列表中的群会被监听。
- `max_full_crash_chars`：直接送入模型分析的崩溃报告最大字符数。超过后会按插件逻辑截断或整理。
- `latest_log_tail_lines`：当 zip 中没有崩溃报告文件时，从 `latest.log` 末尾提取的行数，默认 `800`。
- `max_input_file_bytes`：允许读取的单个文件最大字节数，默认 `20971520`，即 20 MiB。
- `max_zip_member_bytes`：允许读取的 zip 内单个日志条目最大解压字节数，默认 `5242880`，即 5 MiB。
- `max_zip_entries`：允许扫描的 zip 条目数量上限，默认 `200`。

## 支持的文件

插件只处理直接发送到群里的 File 消息，不处理文本链接、文本路径或回复消息。

支持以下文件：

- PCL zip：`错误报告-日期_时间.zip`，只识别这个文件名模式。
- 直接崩溃报告：`crash-YYYY-MM-DD_HH.mm.ss-client.txt`、`crash-YYYY-MM-DD_HH.mm.ss-server.txt`、`crash-YYYY-MM-DD_HH.mm.ss-fml.txt`。

## zip 解析规则

收到受支持的 PCL zip 后，插件会解压并优先查找崩溃报告文件。

如果 zip 中没有崩溃报告文件，插件会尝试读取 `latest.log`，并只分析末尾配置行数的内容。默认读取末尾 `800` 行。

## 行为说明

- 没有手动命令，所有分析都由群文件消息触发。
- 只处理直接发送的 File 消息。
- 忽略文本链接、文本路径和回复消息。
- 只监听 `enabled_group_ids` 中配置的群。
- 分析使用 AstrBot 当前或默认 LLM 提供商。
- 成功后向来源群发送合并转发报告。
- 失败时只记录日志，不向群内发送提示。

## 限制

- 只支持 `.zip`，不支持 `.rar` 或 `.7z`。
- 没有 NapCat raw/API 文件获取兜底。
- 文件获取依赖 AstrBot 的 `File.get_file()`。
- 分析能力依赖已配置且可用的 AstrBot LLM 提供商。
- 超过大小或条目数量限制的文件会被静默忽略，并只在日志中记录原因。

## 最小验证命令

```bash
python -m py_compile main.py
python -m json.tool _conf_schema.json
python -m unittest discover -s tests -v
```
