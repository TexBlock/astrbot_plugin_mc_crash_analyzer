# Not Enough Crash 多模型供应商配置设计

- 日期：2026-07-15
- 状态：已确认
- 参考实现：`Z:\Git\astrbot_plugin_image_guard`

## 目标

为 Not Enough Crash 增加与 Image Guard 一致的 `template_list` 供应商配置方式，使管理员可以在 AstrBot Dashboard 中增删、选择模板并调整调用顺序，用于选择 Minecraft 崩溃报告分析模型。

支持三类配置模板：

1. `openai_compatible`：任意 OpenAI Chat Completions 兼容接口。
2. `astrbot_provider`：复用当前会话使用的 AstrBot Provider。
3. `modelscope`：ModelScope 推理 API，使用 OpenAI 兼容协议。

当列表中存在多个条目时，插件按列表顺序调用；当前条目失败或返回空内容时继续尝试下一项。列表为空时保持现有行为，使用当前会话的 AstrBot Provider。

## 范围

本次修改包括：

- 在 `_conf_schema.json` 中增加 `llm_providers` `template_list`。
- 增加直接调用 OpenAI 兼容接口和 ModelScope 的异步 HTTP 逻辑。
- 将现有 LLM 调用封装为供应商路由和统一响应解析。
- 增加直接 API 的超时、最大输出 token 和可选 `reasoning_effort` 配置。
- 更新插件版本、README 和最小单元测试。

本次修改不包括：

- 修改崩溃报告、PCL zip、latest.log 或 mclo.gs 的识别和解析规则。
- 修改群白名单、消息拦截、合并转发或静默失败行为。
- 增加独立的插件配置 Web API；Dashboard 原生 `template_list` 已提供增删和排序能力。

## 配置设计

### `llm_providers`

配置类型为 `template_list`，默认值为空列表。AstrBot 会为每个列表项写入 `__template_key`。

### `openai_compatible`

- `name`：供应商别名，用于日志标识。
- `api_key`：Bearer API Key。
- `base_url`：兼容 API 根地址，插件追加 `/v1/chat/completions`。
- `model`：请求使用的模型名。

### `astrbot_provider`

- `name`：供应商别名。

该模板不额外填写 API Key 或模型名，调用当前事件会话对应的 AstrBot Provider。

### `modelscope`

- `name`：供应商别名。
- `api_key`：ModelScope API Token。
- `base_url`：默认 `https://api-inference.modelscope.cn`。
- `model`：ModelScope 模型名，例如 `Qwen/Qwen2.5-VL-72B-Instruct`。

ModelScope 使用与 `openai_compatible` 相同的请求和响应格式。

### 直接 API 请求控制

增加以下全局配置，仅作用于 `openai_compatible` 和 `modelscope`：

- `llm_timeout_seconds`：异步请求超时时间。
- `llm_max_tokens`：请求的最大输出 token 数。
- `reasoning_effort`：非空时传递给兼容 API 的可选推理强度参数。

## 运行时结构

现有消息处理流程继续负责：

1. 校验群白名单。
2. 识别并下载文件或获取 mclo.gs 原始日志。
3. 提取报告内容并按大小截取。
4. 构造分析 prompt。
5. 发送合并转发结果。

供应商路由只替换第 4 步之后的 LLM 调用：

1. 读取 `llm_providers`。
2. 列表为空时调用现有 `get_current_chat_provider_id` + `llm_generate` 路径。
3. 遍历非空列表：
   - `astrbot_provider` 调用当前事件会话的 AstrBot Provider。
   - 其他模板校验 API Key、Base URL、模型名，然后调用 `/v1/chat/completions`。
4. 从响应的 `choices[0].message.content` 提取文本。
5. 当前条目异常、响应无效或文本为空时记录日志并继续。
6. 首个有效文本作为分析结果。
7. 所有条目均失败时抛出统一错误，由现有外层异常处理记录并静默结束。

直接 API 使用一个插件级共享 `httpx.AsyncClient`，在 `terminate()` 中关闭。

## 错误处理与安全

- 不完整的供应商配置只影响当前条目，不阻断后续条目。
- HTTP 非成功状态、JSON 结构不完整、响应内容为空均视为调用失败。
- 供应商日志只包含别名、模板类型和异常摘要，不记录 API Key、Authorization Header 或完整请求体。
- 保持现有群内静默策略：所有异常只记录日志，不向用户发送错误详情。
- API Key 仅从运行时配置读取，不写入新增文件、测试数据或 README 示例。

## 测试与验证

在现有 `tests` 目录增加针对纯函数和路由逻辑的单元测试，覆盖：

- 三种 `__template_key` 的分支选择。
- OpenAI/ModelScope 响应文本提取。
- 当前供应商失败后继续尝试下一供应商。
- 所有供应商失败时的异常路径。
- `llm_providers` 为空时的旧行为回退。

提交前运行：

```text
python -m py_compile main.py
python -m json.tool _conf_schema.json
python -m unittest discover -s tests -v
```

## 变更文件

- `main.py`：增加供应商路由、直接 API 调用、共享 HTTP 客户端和资源清理。
- `_conf_schema.json`：增加三种供应商模板及直接 API 请求控制项。
- `README.md`：补充配置、调用顺序、ModelScope 示例和兼容性说明。
- `metadata.yaml`：更新版本号。
- `tests/`：增加供应商路由和响应解析测试。
