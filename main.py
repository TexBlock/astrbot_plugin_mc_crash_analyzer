import inspect
import io
import os
import re
import socket
import zipfile

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register


PCL_ERROR_REPORT_ZIP_PATTERN = re.compile(r"^错误报告-\d{4}-\d{1,2}-\d{1,2}_\d{1,2}\.\d{1,2}\.\d{1,2}\.zip$")
CRASH_REPORT_FILE_PATTERN = re.compile(r"(^|/)crash-\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2}-(client|server|fml)\.txt$")
MCLOGS_LINK_PATTERN = re.compile(r"https://mclo\.gs/([A-Za-z0-9]{7})(?![A-Za-z0-9])")
CRASH_KEYWORDS = (
    "Caused by",
    "-- System Details --",
    "Exception",
    "Error",
    "Minecraft Version",
    "Mod File",
)
DEFAULT_MAX_FULL_CRASH_CHARS = 60000
DEFAULT_LATEST_LOG_TAIL_LINES = 800
DEFAULT_MAX_INPUT_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ZIP_MEMBER_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_ZIP_ENTRIES = 200
DEFAULT_MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_FULL_CRASH_CHARS_LIMIT = 200000
LATEST_LOG_TAIL_LINES_LIMIT = 5000
MAX_INPUT_FILE_BYTES_LIMIT = 50 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES_LIMIT = 10 * 1024 * 1024
MAX_ZIP_ENTRIES_LIMIT = 1000
PARSED_MESSAGE_EMOJI_ID = 289
PARSED_MESSAGE_EMOJI_TYPE = "1"


class CrashReportTooLarge(ValueError):
    pass


class MclogsFetchError(RuntimeError):
    pass


def _precheck_zip_directory(zip_bytes, max_entries, max_central_directory_bytes=DEFAULT_MAX_ZIP_CENTRAL_DIRECTORY_BYTES):
    eocd_signature = b"PK\x05\x06"
    eocd_offset = zip_bytes.rfind(eocd_signature, max(0, len(zip_bytes) - 65557))
    if eocd_offset < 0 or eocd_offset + 22 > len(zip_bytes):
        raise zipfile.BadZipFile("未找到有效 zip 目录结尾")

    eocd = zip_bytes[eocd_offset:eocd_offset + 22]
    entry_count = int.from_bytes(eocd[10:12], "little")
    central_directory_size = int.from_bytes(eocd[12:16], "little")

    if entry_count == 0xFFFF or central_directory_size == 0xFFFFFFFF:
        raise CrashReportTooLarge("不支持 ZIP64 压缩包")
    if entry_count > max_entries:
        raise CrashReportTooLarge(f"zip 条目数量超过限制：{entry_count} > {max_entries}")
    if central_directory_size > max_central_directory_bytes:
        raise CrashReportTooLarge(
            f"zip 中央目录过大：{central_directory_size} > {max_central_directory_bytes}"
        )


def is_pcl_error_report_zip(filename):
    return bool(PCL_ERROR_REPORT_ZIP_PATTERN.fullmatch(str(filename)))


def is_crash_report_file(filename):
    normalized = str(filename).replace("\\", "/")
    return bool(CRASH_REPORT_FILE_PATTERN.search(normalized))


def extract_mclogs_id_from_text(text):
    match = MCLOGS_LINK_PATTERN.search(str(text))
    if not match:
        return None
    return match.group(1)


def is_group_allowed(group_id, whitelist):
    if not whitelist:
        return False
    return str(group_id) in {str(item) for item in whitelist}


def _decode_zip_text(data):
    for encoding in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_limited_file(path, max_bytes):
    size = os.path.getsize(path)
    if size > max_bytes:
        raise CrashReportTooLarge(f"文件大小超过限制：{size} > {max_bytes}")

    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise CrashReportTooLarge(f"文件读取大小超过限制：{len(data)} > {max_bytes}")
    return data


def read_text_file_robust(path, max_bytes=DEFAULT_MAX_INPUT_FILE_BYTES):
    return _decode_zip_text(read_limited_file(path, max_bytes))


async def fetch_mclogs_raw_text(log_id, max_bytes=DEFAULT_MAX_INPUT_FILE_BYTES):
    import httpx

    limits = httpx.Limits(
        max_connections=200,
        max_keepalive_connections=40,
        keepalive_expiry=30.0,
    )
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
    transport = httpx.AsyncHTTPTransport(
        http2=True,
        retries=3,
        limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        trust_env=False,
    )
    url = f"https://api.mclo.gs/1/raw/{log_id}"

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = []
                total_size = 0
                async for chunk in response.aiter_bytes():
                    total_size += len(chunk)
                    if total_size > max_bytes:
                        raise CrashReportTooLarge(f"mclo.gs 日志大小超过限制：{total_size} > {max_bytes}")
                    chunks.append(chunk)
    except httpx.HTTPError as error:
        raise MclogsFetchError(f"获取 mclo.gs 日志失败：{url}") from error

    return _decode_zip_text(b"".join(chunks))


def _read_zip_member_text(archive, info, max_member_bytes):
    if info.file_size > max_member_bytes:
        raise CrashReportTooLarge(f"zip 条目过大：{info.filename} ({info.file_size} > {max_member_bytes})")
    return _decode_zip_text(archive.read(info))


def extract_report_from_zip_bytes(
    zip_bytes,
    tail_lines=DEFAULT_LATEST_LOG_TAIL_LINES,
    max_member_bytes=DEFAULT_MAX_ZIP_MEMBER_BYTES,
    max_entries=DEFAULT_MAX_ZIP_ENTRIES,
):
    _precheck_zip_directory(zip_bytes, max_entries)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise CrashReportTooLarge(f"zip 条目数量超过限制：{len(entries)} > {max_entries}")

        crash_info = next((info for info in entries if is_crash_report_file(info.filename)), None)
        if crash_info:
            return {
                "filename": crash_info.filename,
                "source": "crash-report",
                "content": _read_zip_member_text(archive, crash_info, max_member_bytes),
            }

        latest_info = next((info for info in entries if info.filename.replace("\\", "/").endswith("latest.log")), None)
        if latest_info:
            lines = _read_zip_member_text(archive, latest_info, max_member_bytes).splitlines()
            return {
                "filename": latest_info.filename,
                "source": "latest.log tail",
                "content": "\n".join(lines[-int(tail_lines):]),
            }

    return None


def prepare_crash_text(text, max_chars=12000):
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    selected = []
    for index, line in enumerate(lines):
        if any(keyword in line for keyword in CRASH_KEYWORDS):
            start = max(0, index - 2)
            end = min(len(lines), index + 4)
            selected.extend(lines[start:end])

    excerpt = "\n".join(dict.fromkeys(selected)) if selected else text[:max_chars]
    prefix = "[内容已截取，仅保留关键崩溃片段]\n"
    available = max_chars - len(prefix)
    return prefix + excerpt[:available]


CASUAL_MODE_PROMPT = """请用最通俗的口语化中文分析下面的 Minecraft 崩溃报告。

文件名：{filename}
来源：{source}
发送者：{sender}

要求：
- 就像一个懂 MC 的朋友在群里聊天一样，用人话解释游戏为什么崩了。
- 不要使用任何专业术语（如 mixin、类加载、注入、堆栈、异常链等）。
- 直接说两件事：①游戏为什么崩了 ②怎么修。
- 控制在 3-5 句话以内，不分段、不用符号列表，就一段纯文字。
- 可以适当用口语和语气词，让回复自然一些。
- 不要编造报告中不存在的 Mod 或版本；不确定时明确说不确定。

崩溃报告内容：
{report_content}
"""


DEFAULT_ANALYSIS_PROMPT = """请对下面的 Minecraft 崩溃报告做详细的中文分析。

文件名：{filename}
来源：{source}
发送者：{sender}

请按以下结构输出：
1. 文件信息：说明正在分析的文件和来源。
2. 普通玩家结论：用通俗语言说明最可能的崩溃原因。
3. 关键异常：摘出最重要的异常类、报错信息或堆栈线索。
4. 疑似 Mod / 组件 / 环境问题：列出可能相关的 Mod、组件、加载器或版本线索。
5. 证据片段：引用报告里的关键文本并解释为什么它支持你的判断。
6. 解决步骤：按优先级给出可执行的修复建议。
7. 仍无法解决时需要补充的信息。

要求：先给结论，再给技术证据；不要编造报告中不存在的 Mod 或版本；不确定时明确说明不确定。

崩溃报告内容：
{report_content}
"""


def build_analysis_prompt(filename, source, sender, report_content, custom_prompt="", casual_mode=False):
    base = CASUAL_MODE_PROMPT.format(
        filename=filename,
        source=source,
        sender=sender,
        report_content=report_content,
    ) if casual_mode else DEFAULT_ANALYSIS_PROMPT.format(
        filename=filename,
        source=source,
        sender=sender,
        report_content=report_content,
    )
    extra = custom_prompt.strip() if custom_prompt else ""
    if not extra:
        return base

    try:
        extra = extra.format(
            filename=filename,
            source=source,
            sender=sender,
            report_content=report_content,
        )
    except KeyError as error:
        logger.warning("自定义提示词包含未知占位符 %s，将原样附加", error)

    return f"{base}\n\n{extra}"


def _config_get(config, key, default):
    if config is None:
        return default
    if hasattr(config, "get"):
        return config.get(key, default)
    return getattr(config, key, default)


def _bounded_config_int(config, key, default, minimum, maximum):
    try:
        value = int(_config_get(config, key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


async def _read_astrbot_provider_stream_text(stream):
    parts = []
    final_text = ""

    async for llm_response in stream:
        text = str(getattr(llm_response, "completion_text", "") or "")
        if not text:
            continue
        if getattr(llm_response, "is_chunk", False):
            parts.append(text)
        else:
            final_text = text

    content = (final_text or "".join(parts)).strip()
    if not content:
        raise ValueError("AstrBot Provider 流式响应内容为空")
    return content


def _get_group_id(event):
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        group_id = getter()
        if group_id:
            return group_id
    message_obj = getattr(event, "message_obj", None)
    return getattr(message_obj, "group_id", None)


def _get_message_segments(event):
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        messages = getter()
        if messages is not None:
            return messages
    message_obj = getattr(event, "message_obj", None)
    return getattr(message_obj, "message", []) or []


def _get_message_id(event):
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    if isinstance(raw_message, dict):
        return raw_message.get("message_id")
    return getattr(raw_message, "message_id", None)


async def _react_to_parsed_message(event):
    bot = getattr(event, "bot", None)
    setter = getattr(bot, "set_msg_emoji_like", None)
    if not callable(setter):
        return

    message_id = _get_message_id(event)
    if message_id is None:
        return

    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        logger.warning("无法获取可解析消息的有效 message_id：%s", message_id)
        return

    try:
        await setter(
            message_id=message_id,
            emoji_id=PARSED_MESSAGE_EMOJI_ID,
            emoji_type=PARSED_MESSAGE_EMOJI_TYPE,
            set=True,
        )
    except Exception:
        logger.exception("给可解析消息贴表情失败：message_id=%s", message_id)


def _get_file_name(file_segment):
    for attr in ("name", "file_", "file", "filename"):
        value = getattr(file_segment, attr, None)
        if value:
            return os.path.basename(str(value))
    return ""


def _get_plain_text(text_segment):
    return str(getattr(text_segment, "text", ""))


def _is_accepted_file_name(filename):
    return is_pcl_error_report_zip(filename) or is_crash_report_file(filename)


def _sender_text(event):
    sender_name = "未知发送者"
    sender_id = "未知ID"
    name_getter = getattr(event, "get_sender_name", None)
    id_getter = getattr(event, "get_sender_id", None)
    if callable(name_getter):
        sender_name = name_getter() or sender_name
    if callable(id_getter):
        sender_id = id_getter() or sender_id
    return f"{sender_name} ({sender_id})"


def _build_forward_nodes(filename, source, sender, analysis, show_detail=True):
    nodes = []
    if show_detail:
        info = f"来源/文件信息\n文件名：{filename}\n来源：{source}\n发送者：{sender}"
        nodes.append(Comp.Node(uin="0", name="崩溃报告来源", content=[Comp.Plain(info)]))
    nodes.append(Comp.Node(uin="0", name="LLM 分析", content=[Comp.Plain(analysis)]))
    return Comp.Nodes(nodes)


def _build_plain_reply(filename, source, sender, analysis, show_detail=True):
    if show_detail:
        info = f"来源/文件信息\n文件名：{filename}\n来源：{source}\n发送者：{sender}"
        return Comp.Plain(f"{info}\n\n{analysis}")
    return Comp.Plain(analysis)


@register("astrbot_plugin_mc_crash_analyzer", "TexBlock", "静默分析群聊中的 Minecraft 崩溃报告文件", "0.1.2")
class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}

    async def initialize(self):
        pass

    async def _call_astrbot_provider(self, event, prompt):
        configured_id = str(_config_get(self.config, "provider_id", "") or "").strip()
        if configured_id:
            provider_id = configured_id
        else:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        provider_getter = getattr(self.context, "get_provider_by_id", None)
        if callable(provider_getter):
            provider = provider_getter(provider_id)
            if inspect.isawaitable(provider):
                provider = await provider
            stream_method = getattr(provider, "text_chat_stream", None)
            if callable(stream_method):
                stream = stream_method(prompt=prompt)
                if inspect.isawaitable(stream):
                    stream = await stream
                return await _read_astrbot_provider_stream_text(stream)

        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        analysis = getattr(llm_resp, "completion_text", None)
        if analysis is None:
            raise ValueError("AstrBot Provider 返回内容为空")
        analysis = str(analysis).strip()
        if not analysis:
            raise ValueError("AstrBot Provider 返回内容为空")
        return analysis

    async def _generate_analysis(self, event, prompt):
        analysis = await self._call_astrbot_provider(event, prompt)
        if not str(analysis).strip():
            raise ValueError("LLM 返回内容为空")
        logger.info("LLM 分析完成")
        return str(analysis).strip()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            group_id = _get_group_id(event)
            whitelist = _config_get(self.config, "enabled_group_ids", [])
            if not is_group_allowed(group_id, whitelist):
                return

            messages = _get_message_segments(event)
            if len(messages) != 1:
                return

            max_chars = _bounded_config_int(
                self.config,
                "max_full_crash_chars",
                DEFAULT_MAX_FULL_CRASH_CHARS,
                1000,
                MAX_FULL_CRASH_CHARS_LIMIT,
            )
            tail_lines = _bounded_config_int(
                self.config,
                "latest_log_tail_lines",
                DEFAULT_LATEST_LOG_TAIL_LINES,
                1,
                LATEST_LOG_TAIL_LINES_LIMIT,
            )
            max_input_bytes = _bounded_config_int(
                self.config,
                "max_input_file_bytes",
                DEFAULT_MAX_INPUT_FILE_BYTES,
                1024,
                MAX_INPUT_FILE_BYTES_LIMIT,
            )
            max_zip_member_bytes = _bounded_config_int(
                self.config,
                "max_zip_member_bytes",
                DEFAULT_MAX_ZIP_MEMBER_BYTES,
                1024,
                MAX_ZIP_MEMBER_BYTES_LIMIT,
            )
            max_zip_entries = _bounded_config_int(
                self.config,
                "max_zip_entries",
                DEFAULT_MAX_ZIP_ENTRIES,
                1,
                MAX_ZIP_ENTRIES_LIMIT,
            )

            segment = messages[0]
            if isinstance(segment, Comp.File):
                filename = _get_file_name(segment)
                if not _is_accepted_file_name(filename):
                    return
                logger.info("识别到可分析崩溃报告文件：filename=%s, group_id=%s", filename, group_id)
                event.stop_event()
                downloaded_path = await segment.get_file()

                if is_pcl_error_report_zip(filename):
                    report = extract_report_from_zip_bytes(
                        read_limited_file(downloaded_path, max_input_bytes),
                        tail_lines=tail_lines,
                        max_member_bytes=max_zip_member_bytes,
                        max_entries=max_zip_entries,
                    )
                    if not report:
                        logger.warning("压缩包中未找到可分析的崩溃报告：%s", filename)
                        return
                    report_filename = report["filename"]
                    source = report["source"]
                    prepared_content = prepare_crash_text(report["content"], max_chars=max_chars)
                else:
                    report_filename = filename
                    source = "crash-report"
                    prepared_content = prepare_crash_text(
                        read_text_file_robust(downloaded_path, max_bytes=max_input_bytes),
                        max_chars=max_chars,
                    )
            elif isinstance(segment, Comp.Plain):
                log_id = extract_mclogs_id_from_text(_get_plain_text(segment))
                if not log_id:
                    return
                logger.info(
                    "识别到可分析 mclo.gs 链接：url=https://mclo.gs/%s, group_id=%s",
                    log_id,
                    group_id,
                )
                event.stop_event()
                report_filename = f"mclo.gs/{log_id}"
                source = "mclo.gs raw"
                prepared_content = prepare_crash_text(
                    await fetch_mclogs_raw_text(log_id, max_bytes=max_input_bytes),
                    max_chars=max_chars,
                )
            else:
                return

            await _react_to_parsed_message(event)
            sender = _sender_text(event)
            custom_prompt = str(_config_get(self.config, "custom_prompt", "") or "")
            casual_groups = _config_get(self.config, "casual_mode_group_ids", [])
            casual_mode = is_group_allowed(group_id, casual_groups)
            show_detail = bool(_config_get(self.config, "show_detail", True))
            prompt = build_analysis_prompt(report_filename, source, sender, prepared_content, custom_prompt, casual_mode=casual_mode)
            analysis = await self._generate_analysis(event, prompt)
            reply_mode = str(_config_get(self.config, "reply_mode", "forward") or "forward").strip().lower()
            if reply_mode == "plain":
                yield event.chain_result([_build_plain_reply(report_filename, source, sender, analysis, show_detail)])
            else:
                yield event.chain_result([_build_forward_nodes(report_filename, source, sender, analysis, show_detail)])
        except Exception:
            logger.exception("处理崩溃报告文件时发生异常")
            return
