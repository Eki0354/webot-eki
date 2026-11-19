# webot 代码参考手册

> 微信消息总结机器人 — 完整代码架构与 API 参考文档
> 生成日期: 2026-06-11
> 最后更新: 2026-06-11

---

## 📝 维护指南（修改代码前必读）

**本文档必须与代码同步更新。** 每次修改代码后，在 `git commit` 之前，按照以下规则更新本文档对应章节。

### 变更类型 → 更新位置速查表

| 代码变更 | 更新本文档的位置 | 具体操作 |
|---|---|---|
| 新增 `.env` 环境变量 | §1.1 环境变量表 | 添加一行：名称、类型、默认值、定义位置、消费位置 |
| 删除/重命名环境变量 | §1.1 + §5.1 | 更新对应行，同步修改 `.env` 模板 |
| 新增模块级常量 | §1.2 模块级常量表 | 添加一行：常量名、类型、值、所在文件、用途 |
| 新增函数/方法 | §2 对应文件的函数表 | 添加一行：函数名、描述、参数、返回值 |
| 删除函数 | §2 对应文件的函数表 | 删除对应行 |
| 修改函数签名（参数名/类型/默认值） | §2 对应函数条目 | 更新参数列 |
| 修改函数返回值 | §2 对应函数条目 | 更新返回值列 |
| 函数A 新增调用了函数B | §2 函数A条目 + §3 调用关系图 | 更新"调用了哪些函数"字段 + 更新 ASCII 调用链 |
| 新增源文件 | §1 + §2 新增小节 + §3 | 添加文件的常量/函数/依赖关系 |
| 删除源文件 | §1 + §2 删除对应小节 + §3 | 移除所有相关条目 |
| 新增功能模块 | §4 新增功能小节 | 按模板写：涉及文件、调用链、关键参数、表结构 |
| 修改现有功能逻辑 | §4 对应功能小节 | 更新调用链 ASCII 图 + 关键参数表 |
| 新增/修改 API 端点 | §4 功能D + §2 server.py 表 | 更新端点清单和函数表 |
| 新增/修改数据库表 | §4 对应功能小节 + §2 schema.py | 更新 DDL + 表说明 |
| 新增/修改前端组件 | §4 功能D + §2 新增前端文件小节 | 更新组件树和函数表 |
| 修改 `build.spec` | §5.2 | 更新打包配置说明 |
| 修改 `requirements.txt` | §5.3 | 更新依赖说明 |

### 更新格式规范

1. **函数签名使用代码块**: `def func_name(param: type = default) -> ReturnType:`
2. **调用链使用 ASCII 图**: 保持 `│ ├ └ →` 风格一致
3. **表格对齐**: 所有 Markdown 表格的列数和表头必须一致
4. **文件路径可点击**: 使用反引号包裹路径如 `` `src/bot.py` ``
5. **交叉引用**: 修改函数时，同时检查"调用了哪些函数"和"被哪些函数调用"两个方向
6. **以代码为准**: 不要凭记忆写文档，必要时重新阅读源码确认

### 快速定位命令

```bash
# 查找某个函数在文档中的所有出现位置
grep "函数名" CODEBASE_REFERENCE.md

# 查找某个参数
grep "参数名" CODEBASE_REFERENCE.md

# 查找某个文件
grep "文件名" CODEBASE_REFERENCE.md
```

---

## 目录

1. [全局变量/参数清单](#1-全局变量参数清单)
2. [函数清单](#2-函数清单)
3. [模块间关系图](#3-模块间关系图)
4. [功能模块展开](#4-功能模块展开)
   - [功能A: 消息获取 (WCDB直读)](#功能a-消息获取-wcdb直读)
   - [功能B: AI总结/聊天](#功能b-ai总结聊天)
   - [功能C: 主动发言](#功能c-主动发言)
   - [功能D: Web UI 和 API](#功能d-web-ui-和-api)
   - [功能E: 微信窗口操控](#功能e-微信窗口操控)
   - [功能F: 触发器系统](#功能f-触发器系统)
   - [功能G: 待办事项](#功能g-待办事项)
   - [功能H: 飞书集成](#功能h-飞书集成)
   - [功能I: 语音识别](#功能i-语音识别)
   - [功能J: 聊天记忆](#功能j-聊天记忆)
   - [功能K: macOS适配](#功能k-macos适配)
5. [配置文件和环境变量](#5-配置文件和环境变量)

---

## 1. 全局变量/参数清单

### 1.1 环境变量 (`.env` 配置项)

所有环境变量均通过 `src/config.py` → `load_config()` → `BotConfig` 数据类加载。

| 环境变量名 | 类型 | 默认值 | 定义位置 | 消费位置 |
|---|---|---|---|---|
| `AI_BACKEND` | `str` | `"claude"` | `src/config.py:load_config()` | `src/summarize/__init__.py:create_summarizer()` |
| `ANTHROPIC_API_KEY` | `str` | `""` | `src/config.py` | `src/summarize/claude_backend.py:ClaudeSummarizer.__init__()` |
| `ANTHROPIC_BASE_URL` | `str` | `"https://api.anthropic.com"` | `src/config.py` | `src/summarize/claude_backend.py` |
| `SUMMARIZE_MODEL` | `str` | `"claude-haiku-4-5-20251001"` | `src/config.py` | `src/bot.py:_log_banner()`, `src/summarize/claude_backend.py` |
| `DEEPSEEK_API_KEY` | `str` | `""` | `src/config.py` | `src/summarize/deepseek_backend.py:DeepSeekSummarizer.__init__()` |
| `DEEPSEEK_MODEL` | `str` | `"deepseek-v4-flash"` | `src/config.py` | `src/summarize/deepseek_backend.py`, `src/bot.py:_log_banner()` |
| `DEEPSEEK_BASE_URL` | `str` | `"https://api.deepseek.com"` | `src/config.py` | `src/summarize/deepseek_backend.py` |
| `OPENAI_API_KEY` | `str` | `""` | `src/config.py` | `src/summarize/openai_backend.py:OpenAISummarizer.__init__()` |
| `OPENAI_BASE_URL` | `str` | `"https://api.openai.com/v1"` | `src/config.py` | `src/summarize/openai_backend.py` |
| `OPENAI_MODEL` | `str` | `"gpt-4o-mini"` | `src/config.py` | `src/summarize/openai_backend.py`, `src/bot.py:_log_banner()` |
| `WECHAT_BACKEND` | `str` | `"wcdb"` | `src/config.py` | `src/bot.py:_create_wechat_backend()` |
| `WECHAT_GROUPS` | `str` | `"*"` | `src/config.py` (经 `_decode_wechat_groups` URL解码) | `src/bot.py:_create_wechat_backend()`, `src/wechat/wcdb_backend.py:_resolve_groups()` |
| `WECHAT_DATA_DIR` | `str` | `""` | `src/config.py` | `src/wechat/wcdb_client.py`, `src/voice/file_locator.py`, `src/web/server.py` |
| `BOT_DISPLAY_NAME` | `str` | `"群聊小助手"` | `src/config.py` (经 `_sanitize_display_name` 清洗) | `src/router.py:handle()` (多处), `src/bot.py:_log_banner()` |
| `ADMIN_WXID` | `str` | `""` | `src/config.py` | `src/router.py:handle()` (管理员命令判断) |
| `TRIGGER_KEYWORDS` | `list[str]` | 见下方默认值 | `src/config.py` | `src/trigger/detector.py:TriggerDetector.__init__()` |
| `SUMMARIZE_ENABLED` | `bool` | `True` | `src/config.py` | `src/router.py:_handle_summary()` |
| `FALLBACK_WINDOW_HOURS` | `int` | `8` | `src/config.py` | `src/router.py:_handle_summary()` |
| `FUN_ENABLED` | `bool` | `True` | `src/config.py` | `src/router.py:handle()` (抽签) |
| `PROACTIVE_ENABLED` | `bool` | `False` | `src/config.py` | `src/proactive/gate.py:ProactiveGate.should_speak()` |
| `PROACTIVE_RATE_WINDOW_SEC` | `int` | `120` | `src/config.py` | `src/proactive/rate_tracker.py:RateTracker.__init__()` |
| `PROACTIVE_RATE_QUIET` | `float` | `1.5` | `src/config.py` | `src/proactive/modes.py:build_modes()` |
| `PROACTIVE_RATE_CASUAL` | `float` | `4.0` | `src/config.py` | `src/proactive/modes.py` |
| `PROACTIVE_RATE_LIVELY` | `float` | `6.5` | `src/config.py` | `src/proactive/modes.py` |
| `PROACTIVE_RATE_BURST` | `float` | `8.5` | `src/config.py` | `src/proactive/modes.py` |
| `WELCOME_ENABLED` | `bool` | `False` | `src/config.py` | `src/router.py:handle()` |
| `STICKY_MENTION_ENABLED` | `bool` | `True` | `src/config.py` | `src/router.py:MessageRouter.__init__()` |
| `STICKY_MENTION_TTL_SEC` | `int` | `60` | `src/config.py` | `src/proactive/sticky.py:StickyMentionTracker.__init__()` |
| `TODO_ENABLED` | `bool` | `True` | `src/config.py` | `src/router.py:MessageRouter.__init__()` |
| `TODO_GROUPS` | `list[str]` | `["*"]` | `src/config.py` | `src/router.py:_is_todo_group()` |
| `TODO_MAX_PER_GROUP` | `int` | `50` | `src/config.py` | `src/todo/store.py:TodoStore.add()` |
| `TODO_COMPLETED_RETENTION_DAYS` | `int` | `30` | `src/config.py` | `src/todo/store.py:TodoStore.cleanup()` |
| `TODO_DELETED_RETENTION_DAYS` | `int` | `30` | `src/config.py` | `src/todo/store.py:TodoStore.cleanup()` |
| `TODO_ADD_KEYWORDS` | `list[str]` | `["记一下","添加待办","新建待办","帮我记","待办"]` | `src/config.py` | `src/todo/handler.py:TodoHandler.handle()` |
| `TODO_COMPLETE_KEYWORDS` | `list[str]` | `["搞定","做完了","完成","完成了","done"]` | `src/config.py` | `src/todo/handler.py` |
| `TODO_DELETE_KEYWORDS` | `list[str]` | `["删掉","删除","取消","不要了"]` | `src/config.py` | `src/todo/handler.py` |
| `POLL_INTERVAL_SEC` | `float` | `1.0` | `src/config.py` | `src/bot.py:_create_wechat_backend()` → `WcdbBackend.__init__()` |
| `DEDUP_WINDOW_SEC` | `int` | `60` | `src/config.py` | (通过 config 传递，当前代码未直接使用) |
| `MAX_MESSAGES_FOR_SUMMARY` | `int` | `5000` | `src/config.py` | `src/router.py:_handle_summary()` |
| `CHUNK_SIZE` | `int` | `400` | `src/config.py` | `src/summarize/__init__.py:create_summarizer()` → 各后端 |
| `LOG_LEVEL` | `str` | `"INFO"` | `src/config.py` | `src/bot.py:run()` → `setup_logging()` |
| `LOG_FILE` | `str` | `"data/bot.log"` | `src/config.py` | `src/bot.py:run()` → `setup_logging()` |
| `DB_PATH` | `str` | `"data/messages.db"` | `src/config.py` | `src/bot.py:run()` → `initialize_db()` |
| `VOICE_ASR_ENABLED` | `bool` | `False` | `src/config.py` | `src/voice/pipeline.py:VoicePipeline.__init__()` |
| `VOICE_ASR_BACKEND` | `str` | `"local_whisper"` | `src/config.py` | `src/voice/asr.py:create_asr()` |
| `VOICE_ASR_LANGUAGE` | `str` | `"zh"` | `src/config.py` | `src/voice/pipeline.py:VoicePipeline.__init__()` |
| `VOICE_OPENAI_API_KEY` | `str` | `""` | `src/config.py` | `src/voice/asr.py:OpenAiWhisperASR.__init__()` |
| `VOICE_OPENAI_BASE_URL` | `str` | `""` | `src/config.py` | `src/voice/asr.py:OpenAiWhisperASR.__init__()` |
| `VOICE_LOCAL_MODEL` | `str` | `"small"` | `src/config.py` | `src/voice/asr.py:LocalWhisperASR.__init__()` |
| `VOICE_ASR_TO_SIMPLIFIED` | `bool` | `True` | `src/config.py` | `src/voice/asr.py` (各ASR后端) |
| `MEDIA_VISION_ENABLED` | `bool` | `False` | `src/config.py` | `src/media/pipeline.py:MediaPipeline.__init__()` |
| `MEDIA_VISION_MODEL` | `str` | `""` | `src/config.py` | `src/media/pipeline.py:MediaPipeline._create_vision()` |
| `IMAGE_XOR_KEY` | `str` | `""` | `src/config.py` | `src/media/keys.py:resolve_image_keys()` |
| `IMAGE_AES_KEY` | `str` | `""` | `src/config.py` | `src/media/keys.py:resolve_image_keys()` |
| `IMAGE_KEY_PROVIDER` | `str` | `"env"` | `src/config.py` | `src/media/keys.py:resolve_image_keys()` |
| `FEISHU_EXPORT_ENABLED` | `bool` | `False` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_APP_ID` | `str` | `""` | `src/config.py` | `src/integrations/feishu/client.py:FeishuClient.__init__()` |
| `FEISHU_APP_SECRET` | `str` | `""` | `src/config.py` | `src/integrations/feishu/client.py` |
| `FEISHU_EXPORT_MODE` | `str` | `"knowledge"` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_EXPORT_WINDOW_HOURS` | `int` | `8` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_AUTO_SYNC_ENABLED` | `bool` | `False` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_AUTO_SYNC_MIN_MESSAGES` | `int` | `20` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_AUTO_SYNC_COOLDOWN_SEC` | `int` | `1800` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_KNOWLEDGE_BASE_NAME` | `str` | `"webot 群聊沉淀"` | `src/config.py` | `src/integrations/feishu/exporter.py` |
| `FEISHU_EXPORT_TRIGGER_KEYWORDS` | `list[str]` | `["同步到飞书","导出到飞书","写到飞书","沉淀到飞书"]` | `src/config.py` | `src/integrations/feishu/exporter.py:is_export_command()` |
| `FEISHU_*` (其他) | — | — | `src/config.py` | `src/integrations/feishu/` (spreadsheet/bitable/doc 模式) |
| `WEBOT_APP_HOME` | `str` | `""` (自动检测) | `src/config.py:_resolve_project_root()` | `src/config.py`, `src/web/server.py`, `desktop.py` |
| `WEBOT_ENV_FILE` | `str` | `""` | `src/config.py:resolve_env_file()` | `src/config.py` (显式指定 .env 路径) |
| `ONBOARDING_DONE` | `bool` | `False` | `.env` 写入 | `src/config.py:is_onboarding_done()`, `src/web/server.py`, `desktop.py` |
| `WCDB_KEY` | `str` | `""` | `.env` / 引导流程写入 | `src/wechat/wcdb_client.py` |
| `MAC_WECHAT_SEND_SHORTCUT` | `str` | `"enter"` | `desktop_mac.py` | `src/wechat/mac_ui_backend.py` |
| `MAC_CHAT_TITLE_CACHE_FILE` | `str` | `""` | 环境变量 | `src/wechat/mac_hybrid_backend.py` |
| `MAC_CHAT_TITLE_MAP` | `str` (JSON) | `""` | 环境变量 | `src/wechat/mac_hybrid_backend.py` |

### 1.2 模块级常量

| 常量名 | 类型 | 值 | 所在文件 | 用途 |
|---|---|---|---|---|
| `PROJECT_ROOT` | `Path` | 自动解析 | `src/config.py` | 项目根目录（支持 frozen EXE 模式） |
| `UI_DIR` | `Path` | `ui/dist/` | `src/web/server.py` | 前端静态文件目录 |
| `WEBSOCKET_GUID` | `bytes` | `b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"` | `src/web/server.py` | WebSocket 协议 GUID |
| `_env_write_lock` | `threading.Lock` | `threading.Lock()` | `src/config.py` | 串行化所有 .env 写盘（`write_env_file` 内部持有；server 侧不再单独加锁） |
| `CONSOLIDATE_MSG_THRESHOLD` | `int` | `50` | `src/memory/consolidator.py` | 触发记忆合并的消息数阈值 |
| `CONSOLIDATE_TIME_THRESHOLD_SEC` | `int` | `3600` (1小时) | `src/memory/consolidator.py` | 触发记忆合并的时间阈值 |
| `MAX_NEW_MSGS_PER_CONSOLIDATION` | `int` | `400` | `src/memory/consolidator.py` | 每次合并最多处理的消息数 |
| `CHAT_CONTEXT_WINDOW_SEC` | `int` | `600` | `src/router.py` | AI 聊天上下文时间窗口(秒) |
| `MAX_CONTENT_LENGTH` | `int` | `997` | `src/router.py` | 发送给 AI 的单条消息最大字符数 |
| `MAX_CONTENT_LINES` | `int` | `20` | `src/router.py` | AI 聊天上下文最大行数 |
| `AT_MENTION_MAX_AGE_SEC` | `int` | `300` | `src/router.py` | @mention 消息最大有效年龄(秒) |
| `DEFAULT_POLL_SEC` | `float` | `1.0` | `src/wechat/wcdb_backend.py` | 默认轮询间隔 |
| `MAX_DEDUP_SIZE` | `int` | `5000` | `src/wechat/wcdb_backend.py` | 去重集合最大容量 |
| `MAX_CONSECUTIVE_ERRORS` | `int` | `5` | `src/wechat/wcdb_backend.py` | 连续错误触发重初始化阈值 |
| `WECHAT_PROCESS_NAMES` | `set` | `{"wechat.exe", "weixin.exe"}` | `src/wechat/window_controller.py` | 微信进程名集合 |
| `MIN_WINDOW_WIDTH` / `MIN_WINDOW_HEIGHT` | `int` | `200` | `src/wechat/window_controller.py` | 窗口最小尺寸验证 |
| `MSG_TYPE_MAP` | `dict` | 见代码 | `src/wechat/helpers.py` | 消息类型映射表 |
| `DEFAULT_NICKNAME_PATH` | `Path` | `data/nicknames.json` | `src/nickname.py` | 昵称映射文件路径 |
| `DEFAULT_WELCOME_CONFIG_PATH` | `Path` | `data/welcome_templates.json` | `src/welcome.py` | 欢迎模板文件路径 |
| `HEX_KEY_LEN` | `int` | `64` | `src/wechat/mac_weflow_client.py` | WCDB 密钥长度 |
| `_CACHE_MAX_ENTRIES` | `int` | `10000` | `src/voice/pipeline.py` | 语音缓存最大条目 |
| `_CACHE_TTL_SEC` | `int` | `604800` (7天) | `src/voice/pipeline.py` | 语音缓存过期时间 |

### 1.3 触发关键词默认值

```python
# 定义位置: src/config.py BotConfig 数据类

trigger_keywords: list[str] = [
    "总结一下", "之前发了什么", "错过了什么", "summarize",
    "what did i miss", "聊天总结", "帮我总结", "前面说了什么",
    "说了啥", "发生了什么",
]

feishu_export_trigger_keywords: list[str] = [
    "同步到飞书", "导出到飞书", "写到飞书", "沉淀到飞书",
]

todo_add_keywords: list[str] = [
    "记一下", "添加待办", "新建待办", "帮我记", "待办",
]

todo_complete_keywords: list[str] = [
    "搞定", "做完了", "完成", "完成了", "done",
]

todo_delete_keywords: list[str] = [
    "删掉", "删除", "取消", "不要了",
]
```

---

## 2. 函数清单

### 2.1 `src/config.py`

| 函数 | 行号 | 描述 | 参数 | 返回值 |
|---|---|---|---|---|
| `_decode_wechat_groups(raw)` | ~20 | URL解码微信群名称 | `raw: str` | `str` (逗号分隔的解码后群名) |
| `_sanitize_display_name(name)` | ~40 | 清理机器人显示名中的危险字符 | `name: str` | `str` |
| `_resolve_project_root()` | ~70 | 解析项目根目录(支持 EXE frozen 模式) | 无 | `Path` |
| `detect_wechat_data_dir()` | ~80 | 自动探测微信数据目录(包含 `wxid_*` 且带 `session.db` 的父目录); UI 与媒体管线共用同一份答案 | 无 | `str` (探测不到返回 `""`) |
| `resolve_wechat_data_dir(config)` | ~110 | 返回读取附件用的微信数据目录: `WECHAT_DATA_DIR` 优先, 留空则回退到自动探测 | `config: BotConfig` | `str` |
| `resolve_env_file()` | ~85 | 返回 .env 的权威路径(单一来源, 读写统一) | 无 | `Path` |
| `find_env_file()` | ~100 | 返回实际存在的 .env 路径(canonical 优先, 向后兼容 cwd) | 无 | `Path \| None` |
| `apply_env_file(env_path=None)` | ~120 | 把 .env 重新载入 `os.environ`(.env→进程内存的唯一入口, `override=True`; 文件缺失/读取失败返回 None) | `env_path: Path \| None` | `Path \| None` |
| `write_env_file(env_path, content)` | ~165 | 写 .env 的唯一落盘入口: 优先"唯一临时名 + `os.replace`"原子写, rename 被拒(UNC / VMware 共享文件夹 / SMB)时回退为原地覆盖并记 warning; 临时名基于完整文件名, 不会像 `Path.with_suffix` 那样把 `config.env` 变成 `config.tmp` | `env_path: Path, content: str` | `str` (`"atomic"` / `"direct"`; 两种策略都失败时抛 `OSError`) |
| `load_config()` | ~200 | 从环境变量加载并验证配置(只读 `os.environ`; `AI_BACKEND` 为空或非法时抛 RuntimeError 指明真因) | 无 | `BotConfig` |
| `_validate_config(kwargs)` | ~240 | 验证数值配置范围 | `kwargs: dict` | `None` (无效时抛 RuntimeError) |
| `is_onboarding_done()` | ~370 | 检查引导流程是否完成 | 无 | `bool` |

### 2.2 `src/bot.py`

| 函数 | 行号 | 描述 | 参数 | 返回值 |
|---|---|---|---|---|
| `HealthMonitor.__init__()` | ~30 | 初始化健康监控器 | `summarizer, router, conn, backend, config, on_tick=None` | `None` |
| `HealthMonitor.start()` | ~45 | 启动健康监控守护线程 | 无 | `None` |
| `HealthMonitor.stop()` | ~50 | 停止健康监控 | 无 | `None` |
| `HealthMonitor._run()` | ~55 | 健康监控主循环(每30s推送, 每5min全检) | 无 | `None` |
| `HealthMonitor._tick()` | ~75 | 执行一次完整健康检查 | 无 | `None` |
| `HealthMonitor._check_db()` | ~100 | 检查数据库连接 | 无 | `str` ("OK" 或错误信息) |
| `HealthMonitor._check_wechat_hwnd()` | ~110 | 检查微信窗口句柄 | 无 | `str` |
| `HealthMonitor._last_api_ago()` | ~130 | 最后一次 API 调用距今时间 | 无 | `str` |
| `HealthMonitor._write_status_json()` | ~140 | 写入 bot_status.json 状态文件 | 无 | `None` |
| `Bot.__init__(config)` | ~165 | 初始化 Bot 实例 | `config: BotConfig` | `None` |
| `Bot.run()` | ~170 | 初始化所有组件并启动 bot (阻塞) | 无 | `None` |
| `Bot._log_banner()` | ~260 | 打印启动横幅日志 | 无 | `None` |
| `Bot._create_wechat_backend(store)` | ~275 | 根据配置创建微信后端实例 | `store: MessageStore` | `AbstractWeChatBackend` 子类实例 |

### 2.3 `src/router.py`

| 函数 | 行号 | 描述 | 参数 | 返回值 |
|---|---|---|---|---|
| `MessageRouter.__init__()` | ~45 | 初始化消息路由器 | `store, detector, summarizer, admin_handler, nickname_service, config, feishu_export_service=None` | `None` |
| `MessageRouter._strip_markdown(text)` | ~65 | 去除 Markdown 格式(静态方法) | `text: str` | `str` |
| `MessageRouter.handle(msg)` | ~70 | 处理传入群聊消息的主入口 | `msg: dict` | `str \| None` (回复文本) |
| `MessageRouter._load_group_names()` | ~115 | 加载 chat_id → 群名映射(懒加载) | 无 | `dict[str, str]` |
| `MessageRouter._is_todo_group(chat_id)` | ~125 | 判断群聊是否启用待办功能 | `chat_id: str` | `bool` |
| `MessageRouter._get_group_memory(chat_id)` | ~140 | 获取群聊记忆文本 | `chat_id: str` | `str` |
| `MessageRouter._handle_summary(msg)` | ~145 | 生成聊天总结 | `msg: dict` | `str \| None` |
| `MessageRouter._handle_chat(msg, clean_content)` | ~230 | 处理 @bot AI 对话 | `msg: dict, clean_content: str` | `str \| None` |
| `MessageRouter._handle_welcome(msg)` | ~280 | 发送新成员欢迎消息 | `msg: dict` | `str \| None` |
| `MessageRouter._handle_proactive_chat(msg, mode)` | ~305 | 处理主动发言 | `msg: dict, mode: ProactiveMode` | `str \| None` |

### 2.4 `src/summarize/base.py` (AbstractSummarizer)

| 函数 | 行号 | 描述 | 参数 | 返回值 |
|---|---|---|---|---|
| `chat(message, ...)` | ~20 | AI 对话响应 (@bot) | `message: str, context_messages=None, requester_name="", bot_name="", group_name="", group_memory=""` | `str` |
| `proactive_chat(mode, context_messages, ...)` | ~50 | 主动发言 | `mode: ProactiveMode, context_messages: list[dict], bot_name="", group_name="", group_memory=""` | `str` |
| `summarize(messages, requester_name)` | ~80 | 生成结构化摘要 | `messages: list[dict], requester_name: str` | `SummaryResult` |
| `_multi_level_map_reduce(chunks, requester_name)` | ~100 | 多级 Map-Reduce 处理大量消息 | `chunks: list[list[dict]], requester_name: str` | `SummaryResult` |
| `format_summary_for_reply(result, requester_name)` | ~130 | 格式化摘要为微信回复 | `result: SummaryResult, requester_name: str` | `str` |
| `_summarize_direct(messages, requester_name)` | — | 直接总结(单次调用) | `messages: list[dict], requester_name: str` | `SummaryResult` |
| `_summarize_map_reduce(chunks, requester_name)` | ~110 | 标准 Map-Reduce | `chunks: list[list[dict]], requester_name: str` | `SummaryResult` |
| `_summarize_chunk(chunk, chunk_num, total, requester_name)` | — | 总结单个分块 | `chunk: list[dict], chunk_num: int, total: int, requester_name: str` | `str` |
| `_merge_chunk_summaries(chunk_summaries, requester_name)` | — | 合并分块摘要 | `chunk_summaries: list[str], requester_name: str` | `SummaryResult` |
| `consolidate_memory(existing_memory, new_messages)` | — | 更新群聊记忆 | `existing_memory: str, new_messages: list[dict]` | `str` |
| `_call_chat_api(system_prompt, messages)` | — | 执行对话 API 调用(抽象) | `system_prompt: str, messages: list[dict]` | `str` |
| `_split_into_chunks(messages)` | — | 将消息列表分块 | `messages: list[dict]` | `list[list[dict]]` |
| `_estimate_tokens(messages)` | — | 估算 token 数(静态) | `messages: list[dict]` | `int` |
| `_retry_with_backoff(call_fn, label)` | — | 带指数退避的重试执行 | `call_fn: Callable, label: str` | `T` |

### 2.5 `src/summarize/claude_backend.py` (ClaudeSummarizer)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(api_key, model, base_url, chunk_size, max_retries)` | 初始化 Claude 客户端 | API配置 | `None` |
| `_call_chat_api(system_prompt, messages)` | Claude 对话 API | prompt + messages | `str` |
| `_summarize_direct(messages, requester_name)` | 直接总结(Claude Pydantic parse) | 消息列表 | `SummaryResult` |
| `_summarize_chunk(chunk, chunk_num, total, requester_name)` | 分块提取(Haiku) | 分块消息 | `str` |
| `_merge_chunk_summaries(chunk_summaries, requester_name)` | 合并分块(Claude Pydantic parse) | 摘要列表 | `SummaryResult` |
| `consolidate_memory(existing_memory, new_messages)` | 记忆合并(Haiku) | 现有记忆 + 新消息 | `str` |

### 2.6 `src/summarize/deepseek_backend.py` (DeepSeekSummarizer)

> 已重构：所有 OpenAI 兼容逻辑迁至 `openai_backend.py`，本文件仅保留 DeepSeek 专属默认值。

| 常量/函数 | 描述 |
|---|---|
| `MODEL_PRO` / `MODEL_FLASH` / `MODEL_DEFAULT` | `deepseek-v4-pro` / `deepseek-v4-flash` / 默认 `MODEL_PRO` |
| `DEEPSEEK_BASE_URL` | `"https://api.deepseek.com"` |
| `token_budget = 900_000` | 1M 上下文安全预算 |
| `disable_thinking = True` | 发送 `thinking: disabled` |
| `__init__(api_key, model, base_url, chunk_size, max_retries)` | 调用 `super().__init__()` 初始化 OpenAI 客户端 |

其余方法 (`_call_chat_api` / `_summarize_direct` / `_summarize_chunk` / `_merge_chunk_summaries` / `consolidate_memory`) 继承自 `OpenAISummarizer`。

### 2.6b `src/summarize/openai_backend.py` (OpenAISummarizer)

> 通用 OpenAI 兼容后端，可接入 OpenAI / GLM(智谱) / Moonshot / Qwen 等。GLM: `OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4/`, `OPENAI_MODEL=glm-4-flash`。

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(api_key, model, base_url, chunk_size, max_retries)` | 初始化 OpenAI 客户端 (默认 `gpt-4o-mini` / `https://api.openai.com/v1`) | API配置 | `None` |
| `_extra_body()` | 返回 provider 专属 extra_body；`disable_thinking` 时返回 `{"thinking":{"type":"disabled"}}` 否则 `None` | — | `dict \| None` |
| `_call_chat_api(system_prompt, messages)` | OpenAI 兼容对话 API | prompt + messages | `str` |
| `_summarize_direct(messages, requester_name)` | 直接总结(tool calling) | 消息列表 | `SummaryResult` |
| `_summarize_chunk(chunk, chunk_num, total, requester_name)` | 分块提取 | 分块消息 | `str` |
| `_merge_chunk_summaries(chunk_summaries, requester_name)` | 合并分块(tool calling) | 摘要列表 | `SummaryResult` |
| `consolidate_memory(existing_memory, new_messages)` | 记忆合并 | 现有记忆 + 新消息 | `str` |
| `_parse_summary_from_tool_call(response)` | 解析 tool call 响应(模块级) | `response` | `SummaryResult` |
| `STORE_SUMMARY_TOOL` | 结构化输出 tool schema(模块级) | — | `dict` |

### 2.7 `src/summarize/prompts.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `build_summary_prompt(messages, requester_name)` | 构建直接总结 XML prompt | 消息列表 + 请求人 | `str` |
| `build_chunk_summary_prompt(messages, chunk_num, total_chunks, requester_name)` | 构建分块提取 prompt | 消息 + 序号 | `str` |
| `build_merge_prompt(chunk_summaries, requester_name)` | 构建分块合并 prompt | 摘要列表 | `str` |
| `_format_messages_xml(messages)` | 将消息格式化为 XML(内部) | 消息列表 | `str` |
| `_format_time(timestamp)` | Unix时间戳→HH:MM(内部) | `timestamp: int` | `str` |
| `_escape_xml(text)` | XML 特殊字符转义(内部) | `text: str` | `str` |

### 2.8 `src/wechat/wcdb_backend.py` (WcdbBackend)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(bot_display_name, groups, poll_sec, store, config)` | 初始化 WCDB 后端 | 配置参数 | `None` |
| `start(callback)` | 启动消息监听(阻塞) | `callback: MessageCallback` | `None` |
| `send_text(chat_id, content)` | 发送文本消息 | `chat_id: str, content: str` | `bool` |
| `stop()` | 停止监听 | 无 | `None` |
| `_reinitialize()` | 关闭并重开 WCDB(故障恢复) | 无 | `None` |
| `_resolve_groups()` | 解析群组名称到 talker ID | 无 | `None` |
| `_save_group_names(chatrooms)` | 持久化群名映射(静态) | `chatrooms: dict` | `None` |
| `_save_group_members(chat_members)` | 持久化群成员列表(静态) | `chat_members: dict` | `None` |
| `_poll_cycle(callback)` | 单次轮询周期 | `callback: MessageCallback` | `None` |
| `_poll_group(group_name, talker, callback)` | 轮询单个群聊消息 | `group_name, talker, callback` | `None` |
| `_handle_message(group_name, talker, standardized, callback)` | 处理单条消息(线程池) | `group_name, talker, standardized, callback` | `None` |
| `_standardize(msg, group_name, talker)` | 标准化WCDB原始消息 | `msg: dict, group_name: str, talker: str` | `dict \| None` |
| `_send_and_confirm(group_name, talker, content)` | 通过键盘发送并确认 | `group_name, talker, content` | `bool` |
| `_get_voice()` | 懒初始化语音识别管道 | 无 | `VoicePipeline \| None \| False` |
| `_try_voice(msg)` | 尝试语音识别 | `msg: dict` | `str \| None` |

### 2.9 `src/wechat/wcdb_client.py` (WcdbNativeClient)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(dll_dir)` | 初始化 WCDB 客户端 | `dll_dir: str` | `None` |
| `init()` | 初始化 wcdb_api.dll（自动识别替代 DLL，仅在原版 WeFlow DLL 上打 DRM 补丁） | 无 | `bool` |
| `open()` | 打开加密数据库(逐个密钥源尝试; 失败时按原因抛 `KEY_MISSING`(未配置) 或 `KEY_INVALID`(密钥不匹配本机数据库)) | 无 | `bool` |
| `close()` | 关闭数据库连接 | 无 | `None` |
| `get_sessions()` | 获取所有会话列表 | 无 | `list[dict]` |
| `get_messages(talker, limit)` | 获取指定 talker 的消息 | `talker: str, limit: int` | `list[dict]` |
| `get_group_members(talker)` | 获取群成员列表 | `talker: str` | `list[dict] \| None` |
| `resolve_nickname(wxid)` | 解析 wxid → 昵称 | `wxid: str` | `str` |
| `get_display_names(wxids)` | 批量解析昵称 | `wxids: list[str]` | `dict[str, str]` |
| `_save_key_to_env(key)` | 把可用的 WCDB 密钥持久化到 `resolve_env_file()`(静态; 经 `config.write_env_file()` 落盘, 兼容 UNC / 共享文件夹), 并同步 `os.environ["WCDB_KEY"]` | `key: str` | `None` |

模块级辅助函数（非方法）：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `_preload_libcrypto(dll_dir)` | 加载 `libcrypto-3.dll`，使替代 DLL 的 OpenSSL 依赖可解析 | `dll_dir: str` | `bool` |
| `_key_failure_message(tried_real_key)` | 生成"所有密钥源都失败"的错误文案：`tried_real_key=True` → `KEY_INVALID:`（密钥不匹配，多为多机共用 .env），`False` → `KEY_MISSING:`（未配置） | `tried_real_key: bool` | `str` |
| `_is_replacement_dll(dll)` | 通过 `webot_wcdb_api_replacement` 导出识别自建替代 DLL | `dll: CDLL` | `bool` |
| `_apply_drm_patch(dll_handle, dll_path)` | 单字节 DRM 补丁（仅原版 DLL 需要；字节不匹配时拒绝写入） | `dll_handle: int, dll_path: str` | `None` |

### 2.10 `src/wechat/window_controller.py` (WeChatWindowController)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__()` | 初始化窗口控制器 | 无 | `None` |
| `find_hwnd(force=False)` | 查找微信窗口 HWND(无可见候选时尝试唤醒托盘隐藏窗口) | `force: bool` | `int \| None` |
| `_revive_hidden_candidate(candidates)` | 显示被收进托盘的隐藏微信窗口并重新打分 | `candidates: list[WindowCandidate]` | `WindowCandidate \| None` |
| `_validate_hwnd(hwnd)` | 验证 HWND 有效性 | `hwnd: int` | `bool` |
| `invalidate_cache()` | 清除 HWND 缓存 | 无 | `None` |
| `activate(hwnd)` | 激活微信窗口(4层策略) | `hwnd: int` | `bool` |
| `_try_set_foreground(hwnd)` | SetForegroundWindow 包装，失败返回 False 而不抛异常 | `hwnd: int` | `bool` |
| `navigate_to_chat(hwnd, group_name)` | 键盘导航到指定群聊 | `hwnd: int, group_name: str` | `bool` |
| `send_message(hwnd, text)` | 键盘发送消息(Ctrl+V→Enter) | `hwnd: int, text: str` | `bool` |
| `send_to_chat(group_name, text, max_retries=2)` | 完整发送管道(主入口) | `group_name: str, text: str, max_retries: int` | `bool` |
| `_score_window(hwnd)` | 给窗口打分(模块级) | `hwnd: int` | `WindowCandidate` |
| `_get_process_name(pid)` | 获取进程名(模块级) | `pid: int` | `str` |
| `get_foreground_info()` | 获取前台窗口诊断信息 | 无 | `str` |

### 2.11 `src/wechat/extract_key.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `extract_wcdb_key(require_restart=True, on_progress=None)` | 提取 WCDB 解密密钥(主入口) | `require_restart: bool, on_progress: Callable \| None` | `str \| None` (64字符hex) |
| `_find_wechat_pid()` | 查找微信进程 PID(内部) | 无 | `int \| None` |
| `_find_wx_key_dll()` | 定位 wx_key.dll(内部) | 无 | `str \| None` |
| `_verify_dll_loadable(dll_path)` | 预检查 DLL 是否能加载(内部) | `dll_path: str` | `str \| None` (None=OK, str=错误信息) |
| `_hook_and_poll(pid, dll_path, timeout=180)` | 安装 Hook 并轮询密钥(内部) | `pid: int, dll_path: str, timeout: int` | `str \| None` |
| `extract_aes_key()` | 兼容包装 | 无 | `str \| None` |
| `decrypt_wcdb_key(aes_hex)` | 验证hex密钥有效性 | `aes_hex: str` | `str \| None` |

### 2.12 `src/wechat/helpers.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `normalize_msg_type(raw_type)` | 标准化消息类型码 | `raw_type: Any` | `int` |
| `format_nontext_content(raw_type)` | 非文本消息占位符 | `raw_type: Any` | `str` |
| `generate_message_id(*fields)` | 生成稳定消息 ID(MD5) | `*fields: Any` | `str` |
| `DedupSet.__init__(max_size)` | 初始化去重集合 | `max_size: int` | `None` |
| `DedupSet.add(item)` | 添加并自动裁剪 | `item: str` | `None` |

### 2.13 `src/wechat/keyboard.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `press_key(vk)` | 发送单键按下/释放 | `vk: int` | `None` |
| `send_combo(mod_vk, key_vk)` | 发送组合键 | `mod_vk: int, key_vk: int` | `None` |

### 2.14 `src/proactive/gate.py` (ProactiveGate)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(config)` | 初始化主动发言门控 | `config: BotConfig` | `None` |
| `should_speak(msg)` | 判断是否触发 AI 评估 (4层门控) | `msg: dict` | `tuple[bool, ProactiveMode \| None, str]` |
| `record_eval(chat_id)` | 记录评估时间 | `chat_id: str` | `None` |
| `record_silence(chat_id)` | 累加静默计数(指数退避) | `chat_id: str` | `None` |
| `record_speech(chat_id)` | 重置静默计数 | `chat_id: str` | `None` |
| `get_consecutive_silence(chat_id)` | 获取连续静默次数 | `chat_id: str` | `int` |

### 2.15 `src/proactive/modes.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `build_modes(config)` | 从配置构建模式列表 | `config: BotConfig` | `list[ProactiveMode]` |
| `get_modes(config)` | 获取模式列表(模块缓存) | `config: BotConfig` | `list[ProactiveMode]` |
| `reset_modes()` | 重置模式缓存 | 无 | `None` |
| `lookup_mode(rate, config)` | 根据消息速率查找模式 | `rate: float, config: BotConfig` | `ProactiveMode` |

### 2.16 `src/proactive/rate_tracker.py` (RateTracker)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(window_sec)` | 初始化速率追踪器 | `window_sec: int` | `None` |
| `record(chat_id)` | 记录一条消息 | `chat_id: str` | `None` |
| `cleanup()` | 清理过期条目 | 无 | `None` |
| `rate(chat_id)` | 返回消息速率(条/分钟) | `chat_id: str` | `float` |
| `count(chat_id)` | 返回窗口内消息数 | `chat_id: str` | `int` |
| `clear(chat_id)` | 清除群聊追踪数据 | `chat_id: str` | `None` |

### 2.17 `src/proactive/sticky.py` (StickyMentionTracker)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(ttl_sec)` | 初始化粘性@mention追踪 | `ttl_sec: int` | `None` |
| `register(chat_id, sender_id)` | 注册粘性@mention | `chat_id: str, sender_id: str` | `None` |
| `consume(chat_id, sender_id)` | 消费(原子检查+删除) | `chat_id: str, sender_id: str` | `bool` |
| `clear(chat_id, sender_id)` | 显式移除 | `chat_id: str, sender_id: str` | `None` |

### 2.18 `src/memory/consolidator.py` (MemoryConsolidator)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(store, summarizer)` | 初始化记忆合并器 | `store: MessageStore, summarizer: AbstractSummarizer` | `None` |
| `check_and_consolidate(chat_id)` | 检查并执行合并 | `chat_id: str` | `bool` |

### 2.19 `src/db/store.py` (MessageStore)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `insert_message(msg)` | 插入消息+更新last-message游标 | `msg: dict` | `bool` |
| `log_trigger(chat_id, requester_id, trigger_msg_id)` | 记录触发事件 | `chat_id, requester_id, trigger_msg_id: str` | `None` |
| `cleanup_old_triggers()` | 清理7天前的触发记录 | 无 | `int` |
| `get_sender_display_name(sender_id)` | 查找已知显示名 | `sender_id: str` | `str \| None` |
| `get_user_last_timestamp(chat_id, sender_id)` | 获取用户最后发言时间 | `chat_id, sender_id: str` | `int \| None` |
| `get_user_previous_timestamp(chat_id, sender_id, before_ts)` | 获取用户在某时间前的最后发言 | `chat_id, sender_id: str, before_ts: int` | `int \| None` |
| `get_messages_since(chat_id, since_ts, until_ts, limit)` | 获取时间窗口内消息 | `chat_id, since_ts, until_ts, limit` | `list[dict]` |
| `was_recently_triggered(chat_id, window_sec)` | 检查最近是否触发过 | `chat_id: str, window_sec: int` | `bool` |
| `get_group_memory(chat_id)` | 获取群聊记忆 | `chat_id: str` | `dict \| None` |
| `upsert_group_memory(chat_id, memory_text, message_count, last_message_id)` | 更新/插入群聊记忆 | `chat_id, memory_text, message_count, last_message_id` | `None` |
| `get_new_message_count(chat_id, since_message_id)` | 统计新消息数 | `chat_id: str, since_message_id: str \| None` | `int` |
| `get_messages_since_id(chat_id, since_message_id, limit)` | 获取某ID之后的消息 | `chat_id, since_message_id, limit` | `list[dict]` |

### 2.20 `src/todo/store.py` (TodoStore)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(db_path)` | 初始化待办存储 | `db_path: str` | `None` |
| `add(chat_id, content, creator_id, creator_name, max_per_group)` | 添加待办 | 群/内容/创建者 | `TodoResult` |
| `complete(chat_id, target, operator_id, operator_name)` | 完成待办 | 群/目标/操作者 | `TodoResult` |
| `delete(chat_id, target, operator_id, operator_name)` | 软删除待办 | 群/目标/操作者 | `TodoResult` |
| `restore(chat_id, target)` | 恢复已删除待办 | 群/目标 | `TodoResult` |
| `list_active(chat_id)` | 列出活跃待办 | `chat_id: str` | `TodoResult` |
| `list_completed(chat_id)` | 列出已完成 | `chat_id: str` | `TodoResult` |
| `list_deleted(chat_id)` | 列出已删除 | `chat_id: str` | `TodoResult` |
| `clear_completed(chat_id)` | 清空已完成 | `chat_id: str` | `TodoResult` |
| `clear_deleted(chat_id)` | 清空已删除 | `chat_id: str` | `TodoResult` |
| `cleanup(chat_id, completed_retention_days, deleted_retention_days)` | 自动清理过期待办 | `chat_id, completed_retention_days, deleted_retention_days` | `None` |
| `get_all(status, chat_id, search)` | 获取待办列表(管理UI) | `status, chat_id, search` | `list[TodoItem]` |
| `get_counts(chat_id)` | 获取各状态计数 | `chat_id: str` | `dict[str, int]` |
| `get_active_groups()` | 获取有活跃待办的群 | 无 | `list[str]` |

### 2.21 `src/todo/handler.py` (TodoHandler)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(store, config)` | 初始化待办命令处理器 | `store: TodoStore, config` | `None` |
| `handle(clean_content, chat_id, sender_id, sender_name, is_admin)` | 解析并执行待办命令 | `clean_content, chat_id, sender_id, sender_name, is_admin` | `TodoResult \| None` |
| `_contains_any(text, keywords)` | 子串匹配(静态) | `text: str, keywords: list[str]` | `bool` |
| `_match_prefix(text, keywords)` | 前缀匹配(静态, 长关键词优先) | `text: str, keywords: list[str]` | `tuple[Optional[str], str]` |
| `format_todo_reply(result, sender_name)` | 格式化待办回复(模块级) | `result: TodoResult, sender_name: str` | `str` |

### 2.22 `src/nickname.py` (NicknameService)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(path)` | 初始化昵称服务 | `path: Path \| str` | `None` |
| `load(force=False)` | 加载昵称映射(带缓存) | `force: bool` | `dict[str, str]` |
| `resolve_wxids(text)` | 替换文本中所有 wxid | `text: str` | `str` |
| `resolve_name(wxid)` | 解析单个 wxid | `wxid: str` | `str` |
| `update(wxid, nickname)` | 添加/更新昵称映射 | `wxid: str, nickname: str` | `None` |
| `remove(wxid)` | 删除昵称映射 | `wxid: str` | `None` |
| `merge_manual(overrides)` | 批量合并手动覆盖 | `overrides: dict[str, str]` | `None` |

### 2.23 `src/welcome.py` (WelcomeManager)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `WelcomeManager.__init__(path)` | 初始化欢迎管理器 | `path: Path \| str` | `None` |
| `WelcomeManager.load()` | 加载配置 | 无 | `dict` |
| `WelcomeManager.save(data)` | 原子写入配置 | `data: dict` | `None` |
| `WelcomeManager.resolve_message(chat_id, new_member_id)` | 解析欢迎消息 | `chat_id: str, new_member_id: str` | `str \| None` |
| `get_welcome_manager()` | 获取模块单例(模块级) | 无 | `WelcomeManager` |

### 2.24 `src/admin.py` (AdminCommandHandler)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(nickname_service)` | 初始化管理员命令处理器 | `nickname_service: NicknameService` | `None` |
| `handle(content, requester_name)` | 解析并执行管理命令 | `content: str, requester_name: str` | `str \| None` |
| `_cmd_rename(content, requester_name)` | 处理"改名"命令 | `content: str, requester_name: str` | `str \| None` |
| `_cmd_delete_nickname(content, requester_name)` | 处理"删除昵称"命令 | `content: str, requester_name: str` | `str \| None` |

### 2.25 `src/fun.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `load_lots_config()` | 读取抽签配置 | 无 | `dict` |
| `save_lots_config(data)` | 保存抽签配置 | `data: dict` | `None` |
| `reset_lots_cache()` | 清除抽签缓存 | 无 | `None` |
| `draw_lots(requester_name)` | 抽签 | `requester_name: str` | `str` |

### 2.26 `src/voice/pipeline.py` (VoicePipeline)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `VoicePipeline.__init__(config)` | 初始化语音管道 | `config` | `None` |
| `VoicePipeline.process(msg)` | 转写语音消息 | `msg: dict` | `str \| None` |
| `VoicePipeline.flush()` | 持久化缓存 | 无 | `None` |

### 2.27 `src/voice/asr.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `LocalWhisperASR.transcribe(audio_path, language)` | 本地 Whisper 转写 | `audio_path: Path, language: str` | `TranscribeResult` |
| `OpenAiWhisperASR.transcribe(audio_path, language)` | OpenAI Whisper API 转写 | `audio_path: Path, language: str` | `TranscribeResult` |
| `create_asr(config)` | ASR 工厂函数 | `config` | `AbstractASR` |

### 2.28 `src/voice/decoder.py` (SilkDecoder)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `SilkDecoder.decode(audio_path)` | 解码 SILK/AMR → WAV | `audio_path: Path` | `Path` |

### 2.29 `src/voice/file_locator.py` (VoiceFileLocator)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `VoiceFileLocator.__init__(wechat_data_dir)` | 初始化文件定位器 | `wechat_data_dir: str` | `None` |
| `VoiceFileLocator.find_voice_file(msg)` | 查找语音文件 | `msg: dict` | `Path \| None` |

### 2.30 `src/integrations/feishu/client.py` (FeishuClient)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(app_id, app_secret, base_url, ...)` | 初始化飞书客户端 | 认证信息 | `None` |
| `tenant_access_token()` | 获取/刷新 tenant token | 无 | `str` |
| `append_spreadsheet_rows(...)` | 电子表格追加行 | `spreadsheet_token, range_name, rows` | `dict` |
| `create_bitable_record(...)` | 创建多维表格记录 | `app_token, table_id, fields` | `dict` |
| `create_bitable_app(name, folder_token)` | 创建多维表格应用 | `name, folder_token` | `dict` |
| `create_bitable_table(...)` | 创建数据表 | `app_token, table_name, fields` | `dict` |
| `create_docx_document(title, folder_token)` | 创建文档 | `title, folder_token` | `dict` |
| `create_docx_blocks(document_id, block_id, children)` | 文档追加内容 | `document_id, block_id, children` | `dict` |
| `create_docx_with_markdown(title, markdown, folder_token)` | 创建文档(含markdown内容) | `title, markdown, folder_token` | `dict` |

### 2.31 `src/integrations/feishu/exporter.py` (FeishuExportService)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `__init__(config, store, summarizer, client, resource_store, classifier)` | 初始化导出服务 | 各依赖 | `None` |
| `is_export_command(content)` | 判断是否飞书导出命令 | `content: str` | `bool` |
| `export_recent_chat(trigger_msg)` | 导出最近聊天(手动) | `trigger_msg: dict` | `FeishuExportResult` |
| `maybe_auto_export(msg)` | 自动同步(静默) | `msg: dict` | `FeishuExportResult \| None` |

### 2.32 `src/utils/logging_config.py`

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `setup_logging(level, log_file)` | 配置日志系统 | `level: str, log_file: str \| None` | `None` |

### 2.33 `src/web/server.py` (核心 API 模块)

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `start_web_server(host, port)` | 启动 Web UI 守护线程(幂等) | `host: str, port: int` | `threading.Thread \| None` |
| `update_status(**kwargs)` | 推送状态到所有 WebSocket 客户端 | 关键字参数 | `None` |
| `register_bot(thread, backend)` | 注册 bot 线程/后端 | `thread, backend` | `None` |
| `_stop_bot()` | 停止运行中的 bot | 无 | `bool` |
| `_start_bot_in_thread()` | 在新守护线程中启动 bot(先发布 running=True 再 start, 避免秒退被覆盖; 线程 finally 里必定复位 running=False) | 无 | `dict` ({"ok": True/False}) |
| `_register_backend(backend)` | 从 Bot.run() 注册后端 | `backend` | `None` |
| `_bot_exited()` | 通知 bot 线程已退出 | 无 | `None` |
| `signal_shutdown()` | 信号关闭所有组件 | 无 | `None` |
| `is_shutting_down()` | 检查关闭信号 | 无 | `bool` |
| `_handle_ws_upgrade(headers, conn)` | WebSocket 握手 | `headers, conn` | `bool` |
| `_send_ws_frame(sock, text)` | 发送 WebSocket 文本帧 | `sock, text: str` | `None` |
| `_read_ws_frame(sock)` | 读取 WebSocket 帧 | `sock` | `bytes \| None` |
| `_recv_exactly(sock, n)` | 精确接收 n 字节 | `sock, n: int` | `bytes \| None` |
| `_find_or_create_env()` | 查找或创建 .env 文件(统一写到 resolve_env_file() 权威路径) | 无 | `Path` |
| `_read_env_map(env_path)` | 把 .env 解析成 key→value 字典(忽略注释) | `env_path` | `dict[str, str]` |
| `_reseed_onboarding_from_env(env_path)` | 密钥重提取后, 从现有 .env 回填非密钥引导字段(调用方须持有 `_onboarding_lock`) | `env_path` | `None` |
| `_detect_wxid_and_db_path()` | 自动检测微信 wxid 和数据库路径 | 无 | `tuple[str \| None, str \| None]` |
| `_set_env_key(env_path, key, value)` | 设置单个 .env 键值(经 `config.write_env_file()` 落盘, 兼容 UNC / 共享文件夹) | `env_path: Path, key: str, value: str` | `None` |
| `_detect_default_data_dir()` | 检测默认微信数据目录 | 无 | `str` |
| `_platform_dependency_report(...)` | 平台依赖诊断 | 可选参数 | `dict` |
| `_platform_wechat_report(...)` | 微信进程状态诊断 | 可选参数 | `dict` |
| `_macos_wechat_diagnostics(...)` | macOS 微信权限诊断 | 可选参数 | `dict` |
| `_read_recent_logs()` | 读取最近 500 行日志 | 无 | `dict` |
| `_run_step1_extraction()` | 后台执行密钥提取 | 无 | `None` |
| `_write_onboarding_to_env(env_path)` | 写入引导数据到 .env, 并调用 `apply_env_file()` 同步到 `os.environ` | `env_path` | `None` |

`_UIHandler` 方法（HTTP 请求处理）：

| 方法 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `do_OPTIONS()` | 处理 CORS 预检请求 | 无 | `None` |
| `do_POST()` | POST 入口; 仅放行白名单路径(`/api/onboarding/*`、`/api/sandbox/test`、`/api/lots`、`/api/todos/action` 等), 其余返回 405 JSON | 无 | `None` |
| `do_GET()` | GET 入口 → `_handle_request()` | 无 | `None` |
| `_handle_request()` | **最后一道异常兜底**: 调用 `_handle_request_inner()`, 任何未捕获异常都记录 traceback 并走 `_send_error_json()`, 避免 socketserver 直接断连导致前端只看到"无法连接服务器" | 无 | `None` |
| `_send_error_json(exc)` | 尽力返回 `{"ok": false, "error": "<类型>: <消息>"}` 的 500 JSON; WebSocket 已升级时跳过(避免污染帧流) | `exc: Exception` | `None` |
| `_handle_request_inner()` | 真正的路由分发(静态文件 / WebSocket 升级 / 全部 API 分支) | 无 | `None` |
| `send_json(data)` | 返回 200 + `application/json`(含 CORS 头) | `data: dict` | `None` |

### 2.34 前端组件 (`ui/src/components/`)

| 文件 | 组件名 | 描述 |
|---|---|---|
| `Onboarding.jsx` | `Onboarding` | 4步引导流程主容器(侧边栏+步骤内容) |
| `Dashboard.jsx` | `Dashboard` | 主仪表板(指标卡片+系统状态+控制栏) |
| `Dashboard.jsx` | `MetricCard` | 指标卡片组件(图标/标签/数值/副标题/迷你SVG图表) |
| `Dashboard.jsx` | `LiveIndicator` | 实时状态指示灯(脉冲动画) |
| `Dashboard.jsx` | `KeyExtractionBanner` | 内联密钥提取横幅(单文件内组件); 按 `KEY_MISSING`/`KEY_INVALID` 区分标题, 轮询 step1-status |
| `LogViewer.jsx` | `LogViewer` | 实时日志查看器(过滤/搜索/高亮) |

`Dashboard.jsx` 内联辅助函数（非组件）：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `generateSmoothPath(data, width, height)` | 把数值序列转成 SVG 平滑折线路径 | `data: number[], width=120, height=30` | `str` |
| `isKeyError(text)` | 判断错误文案是否属于密钥类错误(前缀 `KEY_MISSING` / `KEY_INVALID`) | `text: str` | `bool` |
| `readApiResponse(res)` | 无论 HTTP 状态码都先读 JSON body, 优先返回 `body.error`, 其次 `HTTP <status>` —— 保证服务端真实原因不被通用文案盖住 | `res: Response` | `dict` |

模块级常量：

| 常量 | 值 | 说明 |
|---|---|---|
| `API` | `'http://127.0.0.1:7327'` | 所有请求都打本地后端 |
| `KEY_ERROR_PREFIXES` | `['KEY_MISSING', 'KEY_INVALID']` | 密钥类错误前缀 |
| `EXTRACTION_PHASE_MAP` | `{hooking, waiting_exit, waiting_login, hooking_restart}` | 提取阶段 → 提示文案 |
| `BACKEND_UNREACHABLE` | 见下 | fetch 被 reject 时的提示: "本地后端未响应（127.0.0.1:7327）。请确认 webot 仍在运行；若已退出，请重新启动程序后再试。" |

### 2.35 `src/media/` (媒体消息模块)

`src/media/blob.py` — BLOB 消息体解码：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `is_binary(msg)` | 判断消息体是否为 BLOB（`content_kind == "blob"`） | `msg: dict` | `bool` |
| `decode_blob(raw)` | base64 → zstd → UTF-8 文本；非 zstd 时回退按文本解 | `raw: str` | `str \| None` |

`src/media/appmsg.py` — appmsg/sysmsg XML 归约（纯文本处理，无网络/配置依赖）：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `strip_sender_prefix(text)` | 去掉正文前的 `wxid_xxx:` / `xxx@chatroom:` 前缀 | `text: str` | `str` |
| `looks_like_xml(text)` | 判断内容是否为标记而非聊天文本 | `text: str` | `bool` |
| `reduce_appmsg(xml)` | appmsg → `"[链接] 标题"`；sysmsg → `None` | `xml: str` | `str \| None` |

`src/media/emoji.py` — 表情图片定位与下载：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `parse_emoji_xml(xml)` | 从表情 XML 取 `(md5, cdnurl)`（含 `&amp;` 反转义） | `xml: str` | `tuple[str\|None, str\|None]` |
| `EmojiStore.__init__(wechat_data_dir, cache_dir)` | 初始化表情存取器 | `wechat_data_dir: str, cache_dir: Path` | `None` |
| `EmojiStore.fetch(xml)` | 本地 `Persist/{md5[:2]}/{md5}`（仅当确为图片）优先，否则 CDN 下载 | `xml: str` | `Path \| None` |

`src/media/vision.py` — 图片描述（OpenAI 兼容接口）：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `detect_mime(data)` | 按魔数判断图片 MIME | `data: bytes` | `str \| None` |
| `to_data_url(path)` | 读文件生成 `data:` URL；非图片/过大返回 None | `path: Path` | `str \| None` |
| `VisionClient.__init__(api_key, model, base_url, disable_thinking)` | 初始化视觉客户端 | 见参数 | `None` |
| `VisionClient.available` (property) | key 与 model 是否齐备 | 无 | `bool` |
| `VisionClient.describe(image_path, prompt)` | 返回一句话中文描述 | `image_path: Path, prompt: str` | `str \| None` |

`src/media/dat_decrypt.py` — `.dat` 四段容器解密：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `sniff_format(data)` | 按魔数返回扩展名；`wxgf` 容器返回 `"wxgf"` | `data: bytes` | `str \| None` |
| `dat_version(data)` | 2 = AES+XOR 容器，1 = 全文件 XOR，0 = 未知 | `data: bytes` | `int` |
| `tail_candidates(tail)` | 返回能让 *tail* 以图片结尾符收尾的所有 XOR 密钥 | `tail: bytes` | `list[int]` |
| `derive_xor_key(tails, min_votes=3)` | 对多个文件尾段投票推导 XOR 密钥；票数不足或次高票接近时返回 None | `tails, min_votes: int` | `int \| None` |
| `parse_xor_key(value)` | 归一化 XOR 密钥到 `0..255`（接受 `0x37` / `55` / int） | `value` | `int \| None` |
| `aes_key_from_text(text)` | 取字符串**前 16 个 ASCII 字符**作 AES key（非 hex 解码） | `text: str` | `bytes \| None` |
| `expected_dat_size(plain_length, aes_size=1024)` | 容器尺寸公式；`aes_size=1024` 时等于 `length + 31` | `plain_length: int, aes_size: int` | `int` |
| `decrypt_dat(data, xor_key, aes_key_text)` | 解密为原始图片字节；缺 key / 结构不符返回 None | 见参数 | `bytes \| None` |
| `decrypt_dat_file(path, xor_key, aes_key_text)` | 解密文件并嗅探扩展名 | 见参数 | `tuple[bytes, str] \| None` |

`src/media/wxgf.py` — `wxgf`/HEVC 容器解包：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `is_wxgf(data)` | 是否为 `wxgf` 容器 | `data: bytes` | `bool` |
| `is_plain_image(data)` | 是否已是可直接用的图片（JPEG/PNG/GIF/BMP/WEBP） | `data: bytes` | `bool` |
| `av_available()` | PyAV 是否可导入（决定能否解 HEVC） | 无 | `bool` |
| `unwrap_wxgf(data)` | 容器 → JPEG；已是图片原样返回；无 codec 返回 None | `data: bytes` | `bytes \| None` |

`src/media/keys.py` — 图片密钥来源解析：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `resolve_image_keys(config, data_dir=None)` | 按 `IMAGE_KEY_PROVIDER` 取密钥；`.env` 显式值优先于文件 | `config, data_dir: Path` | `ImageKeys` |
| `resolve_image_key_table(config, data_dir=None)` | 同上但按账号分组；只有 `auto` 会产出真正的 per-account 表 | `config, data_dir: Path` | `ImageKeyTable` |
| `read_key_file_table(data_dir=None)` | 读 `image_keys.json` 的 `accounts` 形状；扁平旧格式返回空表 | `data_dir: Path` | `Dict[str, ImageKeys]` |
| `save_image_keys(keys, data_dir=None)` | 把 `{wxid: ImageKey}` 写成 `accounts` 形状（git-ignored） | `keys: dict` | `Path` |
| `ImageKeys.usable` (property) | 至少一个密钥可用 | 无 | `bool` |
| `ImageKeyTable.for_wxid(wxid)` | 取该账号的密钥；未知账号回退 `shared`，多账号时**不**借用 | `wxid: str \| None` | `ImageKeys` |
| `ImageKeyTable.usable` (property) | 任一账号的密钥可用 | 无 | `bool` |

`src/media/image.py` — 图片消息定位/解密/缓存：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `strip_variant(stem)` | 剥离 `_h`/`_hd`/`_t` 等尺寸后缀还原基名（长后缀优先） | `stem: str` | `str` |
| `variant_rank(stem)` | 变体优先级：`_h`(600) > 无后缀(500) > `_t`(100) | `stem: str` | `int` |
| `ImageStore.__init__(wechat_data_dir, cache_dir, xor_key, aes_key_text, allow_derive_xor=True)` | 初始化图片存取器 | 见参数 | `None` |
| `ImageStore.can_decrypt` (property) | 是否值得尝试解密（有密钥或允许推导） | 无 | `bool` |
| `ImageStore.fetch(msg)` | 定位 → 解密 → wxgf 解包 → 缓存，返回图片路径 | `msg: dict` | `Path \| None` |
| `ImageStore._resolve_xor_key()` | 返回配置的 XOR 密钥，未配置时从文件投票推导（只跑一次，结果含失败都缓存） | 无 | `int \| None` |
| `ImageStore._plain_siblings(dat)` | 同基名的其他 `.dat` 变体（`_h` 优先），用于 wxgf 无 codec 时兜底 | `dat: Path` | `list[Path]` |
| `ImageStore._sample_tails()` | 逐个读出 V2 容器的掩码尾段（只读末尾 16 字节，上限 400 个） | 无 | `Iterator[bytes]` |

`src/media/pipeline.py` — 统一入口：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `MediaPipeline.__init__(config)` | 初始化媒体管道 | `config` | `None` |
| `MediaPipeline.process(msg, chat_id="")` | 媒体消息 → 文本（占位符由调用方兜底）；图片需 `chat_id` 定位目录 | `msg: dict, chat_id: str` | `str \| None` |
| `MediaPipeline.stats()` | 计数器快照（`emoji`/`image`/`described`/`failed`） | 无 | `dict` |

模块级常量：

| 常量 | 值 | 说明 |
|---|---|---|
| `LOCAL_TYPE_IMAGE` | `3` | 图片（`.dat` 解密 + wxgf 解包，需密钥与视觉） |
| `LOCAL_TYPE_EMOJI` | `47` | 表情/贴纸 |
| `LOCAL_TYPE_SYSTEM` | `10000` | 系统提示（加入/退出/撤回），直接丢弃 |

---

## 3. 模块间关系图

### 3.1 高层架构

```
desktop.py  (桌面入口)
    │
    ├── src/web/server.py  (Web UI + API + WebSocket, 守护线程)
    │       └── ui/dist/  (React 前端)
    │
    └── src/bot.py  (Bot 主控)
            │
            ├── src/config.py  (配置加载 .env → BotConfig)
            ├── src/db/  (数据库层)
            │     ├── schema.py  → initialize_db()
            │     └── store.py   → MessageStore
            ├── src/trigger/detector.py  (触发检测)
            ├── src/router.py  (消息路由)
            │       ├── src/proactive/  (主动发言)
            │       │     ├── gate.py       → ProactiveGate (4层门控)
            │       │     ├── modes.py      → ProactiveMode (5种模式)
            │       │     ├── rate_tracker.py → RateTracker (滑动窗口)
            │       │     └── sticky.py     → StickyMentionTracker (粘性@)
            │       ├── src/memory/consolidator.py  (记忆合并)
            │       ├── src/todo/  (待办)
            │       │     ├── store.py  → TodoStore (SQLite)
            │       │     └── handler.py → TodoHandler (命令解析)
            │       ├── src/nickname.py  (昵称服务)
            │       ├── src/admin.py  (管理员命令)
            │       ├── src/fun.py  (趣味功能:抽签)
            │       ├── src/welcome.py  (欢迎新成员)
            │       └── src/integrations/feishu/  (飞书导出)
            ├── src/summarize/  (AI 后端)
            │     ├── base.py           → AbstractSummarizer
            │     ├── claude_backend.py  → ClaudeSummarizer
            │     ├── deepseek_backend.py → DeepSeekSummarizer (继承 OpenAISummarizer)
            │     ├── openai_backend.py  → OpenAISummarizer (通用 OpenAI 兼容)
            │     ├── models.py         → SummaryResult (Pydantic)
            │     └── prompts.py        → Prompt 模板
            └── src/wechat/  (微信后端)
                  ├── base.py              → AbstractWeChatBackend
                  ├── wcdb_backend.py      → WcdbBackend (Windows 直读)
                  │     ├── wcdb_client.py      → WcdbNativeClient (ctypes DLL)
                  │     └── window_controller.py → WeChatWindowController (键盘操控)
                  ├── extract_key.py       → 密钥提取 (wx_key.dll Hook)
                  ├── keyboard.py          → 键盘模拟 (keybd_event)
                  ├── helpers.py           → 去重/类型映射
                  ├── mac_ui_backend.py     → MacUIBackend (macOS 界面自动化)
                  ├── mac_hybrid_backend.py → MacHybridBackend (macOS WCDB + 自动化)
                  └── mac_weflow_client.py  → MacWeFlowClient (macOS WCDB 直读)
```

### 3.2 调用关系图

#### src/bot.py → 调用链

```
Bot.run()
  ├── setup_logging()                    [src/utils/logging_config.py]
  ├── initialize_db()                    [src/db/schema.py]
  ├── MessageStore()                     [src/db/store.py]
  ├── TriggerDetector()                  [src/trigger/detector.py]
  ├── create_summarizer()                [src/summarize/__init__.py]
  │     ├── ClaudeSummarizer()            [src/summarize/claude_backend.py]
  │     ├── DeepSeekSummarizer()          [src/summarize/deepseek_backend.py]
  │     └── OpenAISummarizer()            [src/summarize/openai_backend.py]
  ├── NicknameService()                  [src/nickname.py]
  ├── AdminCommandHandler()              [src/admin.py]
  ├── FeishuExportService()              [src/integrations/feishu/exporter.py]
  ├── MessageRouter()                    [src/router.py]
  │     ├── ProactiveGate()              [src/proactive/gate.py]
  │     ├── StickyMentionTracker()       [src/proactive/sticky.py]
  │     ├── MemoryConsolidator()         [src/memory/consolidator.py]
  │     ├── TodoStore()                  [src/todo/store.py]
  │     └── TodoHandler()                [src/todo/handler.py]
  ├── HealthMonitor()                    [src/bot.py]
  └── WcdbBackend / MacUIBackend / MacHybridBackend  [src/wechat/]
```

#### src/web/server.py → 调用链

```
_API 请求 → _UIHandler._handle_request()      # 全局异常兜底 → _send_error_json()
  └── _handle_request_inner()                 # 实际路由分发
  ├── /api/start          → _start_bot_in_thread() → Bot.run()
  ├── /api/stop           → _stop_bot()
  ├── /api/load-config    → _find_or_create_env() → 读取 .env
  ├── /api/config         → 写入 .env
  ├── /api/nicknames      → NicknameService
  ├── /api/nicknames/groups → group_names.json / messages.db
  ├── /api/welcome/templates → WelcomeManager
  ├── /api/sandbox/test   → create_summarizer() → .chat()
  ├── /api/todos          → TodoStore
  ├── /api/todos/action   → TodoStore
  ├── /api/todos/counts   → TodoStore
  ├── /api/voice/model-status → 检查 HuggingFace 缓存
  ├── /api/voice/download-model → snapshot_download()
  ├── /api/lots           → load_lots_config() / save_lots_config()
  ├── /api/onboarding/*   → 引导流程状态管理
  ├── /api/logs           → _read_recent_logs()
  ├── /api/status         → _status.snapshot()
  ├── /api/browse         → _list_dir_entries()
  └── /ws                 → WebSocket 升级 → _status 广播
```

#### src/wechat/* → 微信后端的层次结构

```
AbstractWeChatBackend (base.py)
    │
    ├── WcdbBackend (wcdb_backend.py) — Windows 原生
    │     ├── WcdbNativeClient (wcdb_client.py) — ctypes DLL 封装
    │     ├── WeChatWindowController (window_controller.py) — 键盘操控发送
    │     │     └── keyboard.py — keybd_event 底层模拟
    │     └── helpers.py — DedupSet / 消息类型映射
    │
    ├── MacUIBackend (mac_ui_backend.py) — macOS 界面自动化
    │     └── MacUIAutomation — AppleScript + CoreGraphics + Vision OCR
    │
    └── MacHybridBackend (mac_hybrid_backend.py) — macOS 混合
          ├── MacWeFlowClient (mac_weflow_client.py) — WCDB 直读
          │     └── _WCDBSQLiteReader — ctypes 加载 libWCDB.dylib
          └── MacUIAutomation — 复用 UI 自动化(发送)
```

#### src/summarize/* → AI后端的层次结构

```
AbstractSummarizer (base.py)
    │  ├── chat()           — @bot AI 对话
    │  ├── proactive_chat() — 主动发言
    │  ├── summarize()      — 结构化总结
    │  ├── consolidate_memory() — 记忆合并(抽象)
    │  └── _retry_with_backoff() — 指数退避重试
    │
    ├── ClaudeSummarizer (claude_backend.py)
    │     ├── _call_chat_api()           — client.messages.create()
    │     ├── _summarize_direct()        — client.messages.parse() + Pydantic
    │     ├── _summarize_chunk()         — client.messages.create() (Haiku)
    │     ├── _merge_chunk_summaries()   — client.messages.parse() + Pydantic
    │     └── consolidate_memory()       — client.messages.create() (Haiku)
    │
    ├── DeepSeekSummarizer (deepseek_backend.py) — 继承 OpenAISummarizer
    │         token_budget=900_000, disable_thinking=True
    │
    └── OpenAISummarizer (openai_backend.py) — 通用 OpenAI 兼容实现
          ├── _extra_body()             — disable_thinking 时返回 thinking: disabled
          ├── _call_chat_api()           — client.chat.completions.create()
          ├── _summarize_direct()        — tool calling → _parse_summary_from_tool_call()
          ├── _summarize_chunk()         — client.chat.completions.create()
          ├── _merge_chunk_summaries()   — tool calling
          └── consolidate_memory()       — client.chat.completions.create()
```

### 3.3 数据流: 消息从微信到AI总结到前端展示的完整链路

```
1. 微信客户端写入加密 WCDB 数据库
         │
2. WcdbBackend._poll_cycle() 每秒轮询
         │
3. WcdbNativeClient.get_messages() — 通过 wcdb_api.dll 读取加密数据
         │
4. WcdbBackend._standardize() — 标准化为统一消息格式
         │  (解析 sender, content, timestamp, msg_type, @mention 等)
         │
5. WcdbBackend._handle_message() — 提交到 ThreadPoolExecutor
         │
6. MessageRouter.handle(msg)
         │
         ├─→ [@mention 路径]
         │      │
         │      ├─ trigger check → TriggerDetector.is_trigger()
         │      ├─ summary → _handle_summary()
         │      │     ├─ MessageStore.get_user_previous_timestamp()
         │      │     ├─ MessageStore.get_messages_since()
         │      │     ├─ NicknameService.resolve_wxids()
         │      │     └─ AbstractSummarizer.summarize()
         │      │           ├─ _split_into_chunks()
         │      │           ├─ _summarize_direct() 或 _summarize_map_reduce()
         │      │           └─ format_summary_for_reply()
         │      │
         │      ├─ chat → _handle_chat()
         │      │     ├─ MessageStore.get_messages_since() (上下文)
         │      │     └─ AbstractSummarizer.chat()
         │      │
         │      ├─ admin command → AdminCommandHandler.handle()
         │      ├─ todo command → TodoHandler.handle()
         │      ├─ feishu export → FeishuExportService.export_recent_chat()
         │      └─ fun (抽签) → draw_lots()
         │
         └─→ [Proactive 路径]
                │
                ├─ ProactiveGate.should_speak() — 4层门控
                │     ├─ RateTracker.record() + rate()
                │     ├─ lookup_mode() — 确定模式
                │     ├─ 评估间隔检查 + 指数退避
                │     └─ 概率骰子
                │
                └─ _handle_proactive_chat()
                      └─ AbstractSummarizer.proactive_chat()

7. 生成回复文本
         │
8. WcdbBackend._send_and_confirm()
         │
9. WeChatWindowController.send_to_chat()
         │  (find_hwnd → activate → navigate_to_chat → send_message)
         │  (Ctrl+F → 粘贴群名 → Enter → Ctrl+V 粘贴消息 → Enter)
         │
10. MessageStore.insert_message() — 持久化到 SQLite
         │
11. _status.update() → WebSocket 广播到前端
         │
12. Dashboard.jsx 实时更新指标卡片
```

---

## 4. 功能模块展开

### 功能A: 消息获取 (WCDB直读)

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `wcdb_backend.py` | `src/wechat/wcdb_backend.py` | 后端主控, 消息轮询与标准化 |
| `wcdb_client.py` | `src/wechat/wcdb_client.py` | 底层 DLL 封装, 加密数据库读写 |
| `extract_key.py` | `src/wechat/extract_key.py` | 密钥提取 (wx_key.dll Hook) |
| `base.py` | `src/wechat/base.py` | 抽象基类 (接口定义) |
| `helpers.py` | `src/wechat/helpers.py` | 去重集合 (DedupSet) |

#### 调用链和关键函数

```
WcdbBackend.start(callback)
  │
  ├── WcdbNativeClient.init()
  │     └── ctypes 加载 wcdb_api.dll
  │
  ├── WcdbNativeClient.open()
  │     ├── 应用 1-byte DRM patch + 打开 session.db
  │     └── _key_candidates() 逐个密钥源尝试(env → builtin)
  │           每个源再试多种投递格式(fmt) × 路径(path)
  │           成功 → os.environ["WCDB_KEY"] = key + _save_key_to_env(key)
  │           全失败 → 按 tried_real_key 抛 KEY_INVALID / KEY_MISSING
  │
  ├── _resolve_groups()
  │     ├── get_sessions() → 获取所有 @chatroom 会话
  │     ├── get_group_members() → 解析群成员
  │     ├── get_display_names() → 批量解析昵称
  │     └── _save_group_names() → 持久化到 data/group_names.json
  │
  └── while self._running:           # 主轮询循环
        ├── _poll_cycle(callback)
        │     └── for each group:
        │           _poll_group(group_name, talker, callback)
        │             ├── get_messages(talker, limit=50)
        │             ├── _standardize() → 统一消息格式
        │             └── _handle_message() → 线程池提交
        │
        └── time.sleep(poll_sec)
```

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `poll_sec` | `1.0` | 轮询间隔(秒), 对应 `POLL_INTERVAL_SEC` |
| `limit` | `50` | 每次查询最多返回消息数 |
| `max_workers` | `4` | 回调线程池大小 |
| `MAX_CONSECUTIVE_ERRORS` | `5` | 连续错误触发重初始化 |
| `MAX_DEDUP_SIZE` | `5000` | 去重集合上限 |

#### 密钥源与失败分类（排障必读）

`_key_candidates()` 的尝试顺序与日志标记：

| 顺序 | 源 | 日志 | 说明 |
|---|---|---|---|
| 1 | `env` | `Trying WCDB key source #1: env (len=64)` | 来自 `os.environ["WCDB_KEY"]`，即 `.env` 里持久化的密钥 |
| 2 | `builtin` | `Trying WCDB key source #2: builtin (len=0)` | 内置/兜底源，通常为空 |

每个源会再遍历多种投递格式与路径，日志形如 `wcdb_open_account FAIL (ret=-3) fmt=hex path=file source=env`：

| 返回值 | 含义 |
|---|---|
| `-3` | `SQLITE_NOTADB` — 密钥**不对**（该密钥不属于本机这份数据库） |
| `-4` | 数据库能打开但解出 0 条会话（代码内部标记，非 SQLite 原生码） |

`tried_real_key` 决定最终错误文案（见 `_key_failure_message()`）：

| 情况 | 抛出 | 前端标题 | 真实原因 |
|---|---|---|---|
| 至少试过一个非空密钥 | `KEY_INVALID: 微信数据库密钥不匹配…` | "密钥不匹配 — 当前密钥无法解密本机微信数据库，需在本机重新获取" | 密钥存在但打不开。**最常见：宿主机与虚拟机共用同一个 `.env`**（如 `\\vmware-host\Shared Folders\...`），或微信重新登录后密钥更换 |
| 一个非空密钥都没有 | `KEY_MISSING: 密钥未配置。` | "加密密钥缺失 — 需要重新获取才能读取微信消息" | 真的没配 |

> 排障提示：看到 `Trying WCDB key source #1: env (len=64)` 后紧跟 `ret=-3`，说明密钥**有**但是错的。
> 此时不是"重新获取"能自动解决的——必须让本机拥有自己的 `.env`（用 `WEBOT_ENV_FILE` 指向本机路径，
> 或把项目复制到本机磁盘），然后**在虚拟机里**重新提取一次密钥。
> 若 `reset` 阶段就失败，见 §功能D 的"前端错误呈现约定"与 `config.write_env_file()`：共享文件夹拒绝
> `os.replace` 会让整个请求断连，旧版本把这种情况误报成"无法连接服务器"。

#### 消息标准化输出格式

```python
{
    "message_id": str,      # MD5 哈希的稳定 ID
    "chat_id": str,         # @chatroom talker ID
    "group_name": str,      # 群显示名
    "sender_id": str,       # wxid_xxx
    "sender_name": str,     # 解析后的显示名
    "content": str,         # 消息文本 ("[图片]", "[语音]" 等占位符)
    "msg_type": int,        # 1=text, 3=image, 34=voice, 47=emoji, 49=link
    "timestamp": int,       # Unix 秒
    "is_at_mentioned": bool, # 是否 @机器人
    "is_group": bool,       # 是否群聊(始终 True)
    "is_system_join": bool, # 是否"xxx 加入了群聊"事件
    "new_member_id": str,   # 新成员 wxid
}
```

---

### 功能B: AI总结/聊天

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `base.py` | `src/summarize/base.py` | 抽象基类 (chat, proactive_chat, summarize, format) |
| `claude_backend.py` | `src/summarize/claude_backend.py` | Claude API (Pydantic parse) |
| `deepseek_backend.py` | `src/summarize/deepseek_backend.py` | DeepSeek API (继承 OpenAISummarizer) |
| `openai_backend.py` | `src/summarize/openai_backend.py` | 通用 OpenAI 兼容 API (OpenAI / GLM / Moonshot 等) |
| `prompts.py` | `src/summarize/prompts.py` | Prompt 模板 (XML格式, Map-Reduce, 记忆合并) |
| `models.py` | `src/summarize/models.py` | Pydantic 数据模型 |
| `__init__.py` | `src/summarize/__init__.py` | 工厂函数 create_summarizer() |
| `router.py` | `src/router.py` | 消息路由 (调用摘要/对话) |

#### 调用链和关键函数

```
MessageRouter.handle(msg)
  │
  ├── [摘要请求] → _handle_summary(msg)
  │     ├── MessageStore.get_user_previous_timestamp() — 确定摘要起点
  │     ├── MessageStore.get_messages_since() — 获取消息窗口
  │     ├── 排除请求者自己发送的消息
  │     ├── NicknameService.resolve_wxids() — 替换文本中的 wxid
  │     └── AbstractSummarizer.summarize(messages, requester_name)
  │           │
  │           ├── _estimate_tokens() — 估算 token 数
  │           ├── 策略选择:
  │           │     ├── ≤budget → _summarize_direct() (单次调用)
  │           │     ├── ≤5*chunk_size → _summarize_map_reduce() (分块+合并)
  │           │     └── 超大量 → _multi_level_map_reduce() (多级合并)
  │           │
  │           └── format_summary_for_reply() — 格式化为微信回复
  │
  └── [AI 对话] → _handle_chat(msg, clean_content)
        ├── MessageStore.get_messages_since() — 获取最近 600s 上下文
        ├── NicknameService.resolve_name() — 解析发送者昵称
        └── AbstractSummarizer.chat(
              message=clean_content,
              context_messages=context,
              requester_name=display_name,
              bot_name=config.bot_display_name,
              group_name=group_name,
              group_memory=memory,
            )
```

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `token_budget` (Claude) | `150000` | Claude 上下文窗口安全预算 |
| `token_budget` (DeepSeek) | `900000` | DeepSeek 上下文窗口安全预算 |
| `token_budget` (OpenAI) | `100000` | 通用 OpenAI 兼容上下文窗口安全预算 |
| `chunk_size` | `400` | 每个分块的消息数 |
| `merge_batch_size` | `5` | 合并批次大小 |
| `max_retries` | `3` | API 调用最大重试次数 |
| `max_messages_for_summary` | `5000` | 摘要最大消息数 |
| `fallback_window_hours` | `8` | 摘要回退时间窗口(小时) |
| `CHAT_CONTEXT_WINDOW_SEC` | `600` | AI 对话上下文时间窗口(秒) |
| `MAX_CONTENT_LENGTH` | `997` | 单条消息最大字符数 |
| `AT_MENTION_MAX_AGE_SEC` | `300` | @mention 最大有效年龄(秒) |

#### 媒体占位符的语义约定（重要）

`MediaPipeline` 有两种输出，prompt 必须按**标记后面有没有文字**来区分：

- **裸占位符** `[图片]` / `[视频]` / `[文件]`（后面没有文字）：`MediaPipeline` 没能解析，
  只表示"收到过一条此类消息"，不携带任何内容，AI 看不到像素，不得编造。
- **带描述的占位符** `[图片] <描述>` / `[表情] <描述>`：`MediaPipeline` 已成功用视觉模型
  描述过，这段文字就是图的内容，属于真实可用信息，可以正常引用、回答、写进总结。

> 历史坑：早期 prompt 只给 `[表情]` 开了例外，却把 `[图片]` 一律声明为"看不到实际内容"。
> 结果图片明明已解密并描述成 `[图片] 一张瑞幸券的截图`，AI 仍按规则回答"我看不到图"——
> 实测复现（上下文里带完整描述，AI 仍拒答），改成按"标记后有无文字"判断后恢复正常。

三处 prompt 均按此约定声明：

| Prompt | 位置 | 声明内容 |
|---|---|---|
| `CHAT_SYSTEM_PROMPT` | `base.py` §关于图片和媒体消息 | 标记后有文字=已描述可用；裸占位符=看不到，不得编造 |
| `PROACTIVE_SYSTEM_PROMPT` | `base.py` §关于图片和媒体消息 | 同上（主动发言不编造媒体内容） |
| `SYSTEM_PROMPT` / `CHUNK_SYSTEM_PROMPT` | `prompts.py` | 同上（总结时可用已描述的图片内容） |


#### Claude 后端特有

- 使用 `client.messages.parse()` 进行原生 Pydantic 结构化输出
- 重试异常: `RateLimitError`, `APIConnectionError`, `InternalServerError`, `OverloadedError`
- 分块提取使用 `claude-haiku-4-5-20251001` (Haiku, 速度快成本低)

#### DeepSeek 后端特有

- 使用 OpenAI-compatible tool calling (`STORE_SUMMARY_TOOL`) 进行结构化输出
- **禁用 thinking 模式**: `extra_body={"thinking": {"type": "disabled"}}`
- `tool_choice="auto"` (V4 Flash 不支持强制 tool_choice)
- 重试异常: `RateLimitError`, `APIConnectionError`, `APIStatusError`
- 自由文本回退: 当 tool call 无响应时包装为 minimal SummaryResult

---

### 功能C: 主动发言

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `gate.py` | `src/proactive/gate.py` | 4层门控: 开关→速率→间隔→概率 |
| `modes.py` | `src/proactive/modes.py` | 5种模式定义 (SLEEP/QUIET/CASUAL/LIVELY/BURST) |
| `rate_tracker.py` | `src/proactive/rate_tracker.py` | 滑动窗口速率追踪 |
| `sticky.py` | `src/proactive/sticky.py` | 粘性 @mention 追踪 |

#### 调用链

```
MessageRouter.handle(msg) → [无 @mention]
  │
  ├── ProactiveGate.should_speak(msg)
  │     │
  │     ├── Gate 1: proactive_enabled? → 主开关
  │     ├── Gate 2: rate = RateTracker.rate(chat_id)
  │     │           lookup_mode(rate) → 5种模式之一
  │     │           mode.name == "SLEEP"? → 跳过
  │     ├── Gate 3: 评估间隔检查
  │     │           elapsed >= mode.eval_interval_sec * backoff?
  │     │           连续静默 → 指数退避 (2^consecutive, 上限16x)
  │     └── Gate 4: random.random() <= mode.reply_probability?
  │
  └── _handle_proactive_chat(msg, mode)
        │
        ├── RateTracker.rate() → MessageStore.get_messages_since()
        ├── AbstractSummarizer.proactive_chat(mode, context, ...)
        │     ├── PROACTIVE_SYSTEM_PROMPT.format(...)
        │     └── _call_chat_api(system_prompt, messages)
        │
        └── AI 返回空 → record_silence() → 指数退避
            AI 返回内容 → record_speech() → 重置静默计数
```

#### 5种主动发言模式

| 模式 | 标签 | 评估间隔 | 回复概率 | 最大字符 | 上下文消息数 | 速率阈值(默认) |
|---|---|---|---|---|---|---|
| SLEEP | 沉睡 | 9999s | 0.0 | 0 | 0 | 0.0 (始终兜底) |
| QUIET | 冷清 | 300s | 0.10 | 30 | 30 | 1.5 msgs/min |
| CASUAL | 闲聊 | 120s | 0.25 | 50 | 50 | 4.0 msgs/min |
| LIVELY | 热闹 | 60s | 0.50 | 35 | 60 | 6.5 msgs/min |
| BURST | 炸了 | 30s | 0.70 | 20 | 80 | 8.5 msgs/min |

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `proactive_enabled` | `False` | 主动发言总开关 |
| `proactive_rate_window_sec` | `120` | 速率计算窗口(秒) |
| `proactive_rate_quiet` | `1.5` | QUIET 模式最低速率 |
| `proactive_rate_casual` | `4.0` | CASUAL 模式最低速率 |
| `proactive_rate_lively` | `6.5` | LIVELY 模式最低速率 |
| `proactive_rate_burst` | `8.5` | BURST 模式最低速率 |
| `exponential_backoff_max` | `16x` | 静默指数退避上限 |

---

### 功能D: Web UI 和 API

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `server.py` | `src/web/server.py` | HTTP + WebSocket 服务器 |
| `desktop.py` | `desktop.py` | Windows 桌面入口 (WebView2) |
| `desktop_mac.py` | `desktop_mac.py` | macOS 桌面入口 (WebView Cocoa) |
| React 前端 | `ui/src/` | SPA 前端 (Onboarding + Dashboard + LogViewer) |

#### API 端点清单

| 方法 | 路径 | 功能 | 认证 |
|---|---|---|---|
| `GET` | `/` | SPA 入口 (index.html) | 无 |
| `GET` | `/api/status` | 获取 bot 运行状态 | 无 |
| `POST` | `/api/start` | 启动 bot | 无 |
| `POST` | `/api/stop` | 停止 bot | 无 |
| `GET` | `/api/load-config` | 加载当前配置 (密钥脱敏) | 无 |
| `POST` | `/api/config` | 保存配置到 .env | 无 |
| `GET` | `/api/config/export` | 导出配置为 JSON 文件 | 无 |
| `POST` | `/api/config/import` | 导入 JSON 配置文件 | 无 |
| `GET` | `/api/nicknames?chat_id=xxx` | 获取群成员昵称列表 | 无 |
| `POST` | `/api/nicknames` | 保存/删除昵称映射 | 无 |
| `GET` | `/api/nicknames/groups` | 获取群组列表 | 无 |
| `GET` | `/api/welcome/templates` | 获取欢迎模板配置 | 无 |
| `POST` | `/api/welcome/templates` | 保存欢迎模板配置 | 无 |
| `GET` | `/api/todos?status=active&chat_id=xxx` | 获取待办列表 | 无 |
| `GET` | `/api/todos/counts?chat_id=xxx` | 获取各状态待办计数 | 无 |
| `POST` | `/api/todos/action` | 执行待办操作 (complete/delete/restore/clear) | 无 |
| `GET` | `/api/logs` | 获取最近 500 行日志 | 无 |
| `POST` | `/api/sandbox/test` | AI 沙盒测试 (不发送) | 无 |
| `GET` | `/api/lots` | 获取抽签配置 | 无 |
| `POST` | `/api/lots` | 保存抽签配置 | 无 |
| `GET` | `/api/browse?path=` | 浏览文件系统目录 | 无 |
| `POST` | `/api/wechat-data-dir/detect` | 检测自定义微信数据目录 | 无 |
| `GET` | `/api/onboarding/status` | 引导流程状态 | 无 |
| `GET` | `/api/onboarding/diagnose` | 系统环境诊断 | 无 |
| `POST` | `/api/onboarding/step1` | 启动密钥提取(异步) | 无 |
| `GET` | `/api/onboarding/step1-status` | 轮询密钥提取状态 | 无 |
| `POST` | `/api/onboarding/step2` | 保存微信配置 | 无 |
| `POST` | `/api/onboarding/step3` | 保存 AI 配置 (含 deepseek/openai/anthropic 三套字段) | 无 |
| `POST` | `/api/onboarding/step4` | 保存功能设置+完成引导 | 无 |
| `POST` | `/api/onboarding/reset` | 仅重置密钥提取状态(step1/key/wxid/db_path), 保留 AI 与身份配置并从 .env 回填 | 无 |
| `GET` | `/api/voice/model-status?model=small` | 语音模型下载状态 | 无 |
| `POST` | `/api/voice/download-model` | 触发语音模型下载 | 无 |
| `GET` | `/api/macos/diagnose` | macOS 微信权限诊断 | 无 |
| `GET` | `/ws` | WebSocket 升级 (状态推送) | 无 |

#### .env 生效链路（配置写入后必须同步到进程内存）

`load_config()` **只读 `os.environ`**，而 `.env` 原本只在进程 import 时 `load_dotenv()` 一次。
因此任何写 `.env` 的路径都必须在写盘后调用 `src.config.apply_env_file()`，否则磁盘已更新、进程仍看到旧值（表现为"配置好了却提示 key 未设置"）。

```
写 .env 的 4 个入口                      写盘后同步
─────────────────────────────────────   ─────────────────────────
_write_onboarding_to_env()   ────────→  apply_env_file(env_path)
/api/config        (保存)    ────────→  apply_env_file(env_path)
/api/config/import (导入)    ────────→  apply_env_file(env_path)
wcdb_client._save_key_to_env()          （只影响 WCDB_KEY，由 _key_candidates 读 environ）
                                        ↑ 该处已单独 os.environ["WCDB_KEY"] = key
```

**所有落盘统一经过 `config.write_env_file(env_path, content)`**（含 `_set_env_key()` 与
`/api/onboarding/reset`）。它先尝试"唯一临时名 + `os.replace`"原子写，rename 被拒时
（UNC / VMware 共享文件夹 / SMB / 部分虚拟磁盘）回退为原地覆盖并记 warning，返回
`"atomic"` 或 `"direct"`；两种策略都失败才抛 `OSError`。**不要在调用方再自己写
`os.replace`** —— 这正是"重新获取密钥"在共享文件夹里报"无法连接服务器"的成因。

#### 前端错误呈现约定（避免把本地后端故障误报成网络问题）

所有 API 都发往 `127.0.0.1:7327`（`const API = 'http://127.0.0.1:7327'`），因此
`fetch` 被 reject **不等于**机器没联网。约定：

| 环节 | 约定 |
|---|---|
| 服务端 | 任何未捕获异常都由 `_handle_request()` 兜底，返回 500 JSON `{ok:false, error}`；绝不能直接断连 |
| 前端读取 | `readApiResponse(res)` 无论状态码都先读 JSON body，优先用 `body.error`，其次 `HTTP <status>` |
| 前端展示 | 连不上后端时显示 `BACKEND_UNREACHABLE`（"本地后端未响应（127.0.0.1:7327）…"），而不是"无法连接服务器" |
| 密钥报错分类 | 文案含 `KEY_MISSING` → "加密密钥缺失"；含 `KEY_INVALID` → "密钥不匹配 — 当前密钥无法解密本机微信数据库，需在本机重新获取" |

`apply_env_file(env_path=None)` 语义：

| 情况 | 行为 |
|---|---|
| 未传路径 | 用 `find_env_file()`（canonical 优先，兼容 cwd） |
| 文件不存在 | 返回 `None`，不改动 `os.environ` |
| 读取失败 (OSError) | 记录 warning 并返回 `None`——写盘已成功，不能让请求失败 |
| 正常 | `load_dotenv(path, override=True)`，返回实际路径 |

#### 启动状态机（避免"面板显示运行但实际没跑"）

`_start_bot_in_thread()` 的 `running` 发布顺序是刻意的：

```
update_status(running=True)   ← 先发布
thread.start()                ← 再启动 worker
        │
        └─ worker 启动失败 → update_status(running=False, error=...)
                             finally: _bot_control.mark_stopped()
                                      update_status(running=False)
```

若先 `start()` 后发布 `running=True`，worker 秒退（如缺 API key）写入的 `False` 会被主线程覆盖，
Dashboard 就一直显示「停止机器人」，而后台没有任何轮询线程。

#### WebSocket 消息类型

WebSocket 仅用于服务器→客户端单向推送状态快照:

```json
{
    "running": false,
    "uptime_sec": 0,
    "messages_processed": 0,
    "wechat_backend": "wcdb",
    "ai_backend": "deepseek",
    "db_ok": true,
    "last_api_call_sec_ago": -1,
    "last_api_call_time": 0.0,
    "timestamp": "2026-06-11T12:00:00",
    "error": ""
}
```

---

### 功能E: 微信窗口操控

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `window_controller.py` | `src/wechat/window_controller.py` | 窗口发现/验证/激活/导航/发送 |
| `keyboard.py` | `src/wechat/keyboard.py` | 键盘底层模拟 (keybd_event) |

#### 调用链

```
WeChatWindowController.send_to_chat(group_name, text)  ← 主入口
  │
  ├── find_hwnd(force=False)
  │     ├── EnumWindows → _score_window() 打分所有窗口
  │     ├── 验证: 进程名 ∈ {wechat.exe, weixin.exe}
  │     ├── 评分: title="微信" (+50), Qt class (+30), visible (+20), size (+10)
  │     ├── 无可用的可见候选 → _revive_hidden_candidate()
  │     │     ├── 挑尺寸合格的隐藏候选(最多3个) — 微信收进托盘时清 WS_VISIBLE
  │     │     ├── ShowWindow(SW_SHOW/SW_RESTORE) + BringWindowToTop
  │     │     └── _score_window() 重新打分，仍不可见则放弃
  │     └── 缓存 30 秒
  │
  ├── activate(hwnd) — 4层激活策略
  │     ├── Layer 1: _prime_foreground_authority() — Alt 按键获取前台权限
  │     │           + AllowSetForegroundWindow(-1)
  │     ├── Layer 2: _try_set_foreground(hwnd) — SetForegroundWindow
  │     ├── Layer 3: _force_foreground() — AttachThreadInput 绕过
  │     └── Layer 4: _alt_tab_to_window() — Alt+Esc 循环切换
  │
  │     ※ 每层失败都必须返回 False 而非抛异常，否则会短路后续降级层。
  │       pywin32 的 SetForegroundWindow 失败时抛 pywintypes.error
  │       (ERROR_ACCESS_DENIED=5)，故统一走 _try_set_foreground 包装。
  │
  ├── navigate_to_chat(hwnd, group_name)
  │     ├── Phase 0: _goto_contacts_tab() — Ctrl+2 到通讯录标签
  │     ├── Phase 1: Ctrl+F → 聚焦搜索框
  │     ├── Phase 2: Ctrl+A → set_clipboard → Ctrl+V → 粘贴群名
  │     ├── Phase 3: Enter → 选择第一个搜索结果
  │     └── _verify_chat_title() — UIA 或窗口标题验证
  │
  └── send_message(hwnd, text)
        ├── _set_clipboard(text) — 写入剪贴板
        ├── Ctrl+V — 粘贴
        └── Enter — 发送
```

#### 关键常量

| 常量 | 值 | 说明 |
|---|---|---|
| `WECHAT_PROCESS_NAMES` | `{"wechat.exe", "weixin.exe"}` | 有效微信进程名 |
| `MIN_WINDOW_WIDTH/HEIGHT` | `200` | 最小有效窗口尺寸 |
| `SEARCH_FOCUS_DELAY` | `0.15s` | Ctrl+F 后等待 |
| `PASTE_DELAY` | `0.05s` | Ctrl+A 后等待 |
| `SEARCH_POPULATE_DELAY` | `0.3s` | 粘贴后等待搜索结果 |
| `SELECT_RESULT_DELAY` | `0.15s` | Enter 后等待 |
| `WHITE_SCREEN_MEAN_THRESHOLD` | `248` | 白屏检测亮度均值阈值 |
| `WHITE_SCREEN_STDDEV_THRESHOLD` | `3.5` | 白屏检测标准差阈值 |

---

### 功能F: 触发器系统

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `detector.py` | `src/trigger/detector.py` | 触发检测：关键词匹配 + @mention |

#### 触发条件类型

1. **@mention 触发**: 消息中 bot 被 @提及 → 总是触发
2. **关键词触发**: 消息内容匹配 `TRIGGER_KEYWORDS` 中任一关键词 → 触发总结

```python
class TriggerDetector:
    """两种触发条件:
    1. bot 被 @mention → 总是触发
    2. 消息内容匹配关键词列表 → 触发总结
    """
    def is_trigger(content, is_at_mentioned, sender_name) -> bool
```

#### 配置

| 参数 | 默认值 |
|---|---|
| `trigger_keywords` | `["总结一下", "之前发了什么", "错过了什么", "summarize", "what did i miss", "聊天总结", "帮我总结", "前面说了什么", "说了啥", "发生了什么"]` |
| `bot_display_name` | `"群聊小助手"` |

---

### 功能G: 待办事项

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `store.py` | `src/todo/store.py` | SQLite 存储 (CRUD + 自动清理) |
| `handler.py` | `src/todo/handler.py` | 命令解析与分发 |

#### 调用链

```
@bot 消息 → MessageRouter.handle(msg)
  │
  └── TodoHandler.handle(clean_content, chat_id, sender_id, sender_name, is_admin)
        │
        ├── 优先级1: 查看活跃待办 → list_active()
        ├── 优先级2: 查看已完成 → list_completed()
        ├── 优先级3: 查看已删除 → list_deleted()
        ├── 优先级4: 清空列表 (admin) → clear_completed() / clear_deleted()
        ├── 优先级5: 添加 (前缀匹配) → add()
        ├── 优先级6: 完成 (前缀匹配) → complete()
        ├── 优先级7: 删除 (前缀匹配) → delete()
        └── 优先级8: 恢复 (前缀匹配, admin) → restore()
```

#### 数据库表结构

```sql
CREATE TABLE todos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    display_order   INTEGER NOT NULL,  -- 每群单调递增, 永不改变
    status          TEXT    NOT NULL DEFAULT 'active',  -- active|completed|deleted
    creator_id      TEXT    NOT NULL DEFAULT '',
    creator_name    TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    completed_by_id   TEXT    DEFAULT '',
    completed_by_name TEXT    DEFAULT '',
    completed_at      REAL    DEFAULT 0,
    deleted_by_id     TEXT    DEFAULT '',
    deleted_by_name   TEXT    DEFAULT '',
    deleted_at        REAL    DEFAULT 0
);
```

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `todo_enabled` | `True` | 待办功能主开关 |
| `todo_groups` | `["*"]` | 启用待办的群组("*"=全部) |
| `todo_max_per_group` | `50` | 每群最大活跃待办数 |
| `todo_completed_retention_days` | `30` | 已完成保留天数(0=永久) |
| `todo_deleted_retention_days` | `30` | 已删除保留天数(0=永久) |

---

### 功能H: 飞书集成

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `client.py` | `src/integrations/feishu/client.py` | 飞书 OpenAPI 客户端 (纯 stdlib HTTP) |
| `exporter.py` | `src/integrations/feishu/exporter.py` | 导出服务 (协调消息→AI→飞书) |
| `knowledge.py` | `src/integrations/feishu/knowledge.py` | 知识库资源管理 + 内容分类器 |

#### 调用链

```
MessageRouter.handle(msg) → [飞书导出命令]
  │
  ├── FeishuExportService.is_export_command(content)
  │     └── 匹配 feishu_export_trigger_keywords
  │
  ├── [手动导出] FeishuExportService.export_recent_chat(trigger_msg)
  │     ├── _validate_target() — 验证配置完整性
  │     ├── _recent_messages() — 获取时间窗口内消息
  │     ├── _summarizer.summarize() — AI 总结
  │     ├── 按 mode 分支:
  │     │     ├── knowledge → _export_knowledge()
  │     │     │     ├── _ensure_knowledge_resources() — 创建/复用 Bitable
  │     │     │     └── KnowledgeClassifier.classify() — 结构化分类
  │     │     ├── spreadsheet → append_spreadsheet_rows()
  │     │     ├── bitable → create_bitable_record()
  │     │     └── docx → create_docx_with_markdown()
  │     └── 返回 FeishuExportResult
  │
  └── [自动同步] FeishuExportService.maybe_auto_export(msg)
        ├── 检查: auto_sync_enabled + mode == "knowledge"
        ├── 冷却: cooldown_sec 内不重复触发
        ├── 最低消息数: >= auto_sync_min_messages
        └── 同上 knowledge 导出流程
```

#### 4种导出模式

| 模式 | 目标 | 所需配置 |
|---|---|---|
| `knowledge` | 飞书多维表格知识库(5张表) | `app_id` + `app_secret` (自动创建) |
| `spreadsheet` | 飞书电子表格追加行 | `spreadsheet_token` + `spreadsheet_range` |
| `bitable` | 飞书多维表格单记录 | `bitable_app_token` + `bitable_table_id` |
| `docx` | 飞书文档 (markdown→段落) | `doc_folder_token` (可选) |

#### 知识库5张表

| 表 | key | 字段 |
|---|---|---|
| 群聊摘要 | `summary` | 同步时间,群聊,请求人,消息数,开始时间,结束时间,主题,摘要 |
| 待办 | `todo` | 创建时间,群聊,事项,负责人,截止时间,状态,来源 |
| 需求 | `requirement` | 创建时间,群聊,需求,提出人,优先级,状态,来源 |
| 日常记录 | `daily` | 记录时间,群聊,记录,分类,参与人,来源 |
| 项目 | `project` | 创建时间,群聊,项目,阶段,负责人,协作人,角色分工,提出人,来源 |

---

### 功能I: 语音识别

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `pipeline.py` | `src/voice/pipeline.py` | 端到端管道 (定位→解码→ASR→缓存) |
| `file_locator.py` | `src/voice/file_locator.py` | 语音文件定位 (.silk/.amr) |
| `decoder.py` | `src/voice/decoder.py` | SILK/AMR 解码 (pysilk + ffmpeg) |
| `asr.py` | `src/voice/asr.py` | ASR 后端 (LocalWhisper / OpenAI Whisper) |

#### 调用链

```
VoicePipeline.process(msg)
  │
  ├── 检查: voice_asr_enabled?
  │
  ├── VoiceCache.get(msg_svr_id) → 命中则直接返回
  │
  ├── VoiceFileLocator.find_voice_file(msg)
  │     ├── 搜索: {wxid}/msg/voice/{msg_svr_id}/*.silk
  │     ├── 搜索: {wxid}/msg/voice/{msg_svr_id}/*.amr
  │     ├── 搜索: {wxid}/msg/attach/{msg_svr_id}/*.silk
  │     └── 搜索: {wxid}/msg/attach/{msg_svr_id}/*.amr
  │
  ├── SilkDecoder.decode(audio_path) → .wav
  │     ├── .silk → _strip_wechat_silk_header() + pysilk.decode() → PCM → WAV
  │     └── .amr → ffmpeg subprocess → WAV
  │
  ├── AbstractASR.transcribe(wav_path, language)
  │     ├── LocalWhisperASR: faster_whisper.WhisperModel.transcribe()
  │     └── OpenAiWhisperASR: client.audio.transcriptions.create()
  │
  ├── VoiceCache.set(msg_svr_id, text, confidence)
  └── 清理临时 WAV 文件
```

#### ASR 后端对比

| 特性 | LocalWhisper (faster-whisper) | OpenAI Whisper API |
|---|---|---|
| 成本 | 免费 | $0.006/分钟 |
| 网络 | 离线 | 需要联网 |
| 内存 | ~1 GB (small 模型) | 0 |
| 模型大小 | tiny/base/small/medium | whisper-1 |
| 初次加载 | 下载 ~500MB | 即时 |
| 繁→简 | opencc t2s | opencc t2s |

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `voice_asr_enabled` | `False` | 语音识别主开关 |
| `voice_asr_backend` | `"local_whisper"` | ASR 后端选择 |
| `voice_asr_language` | `"zh"` | 识别语言 ("zh-en"/"auto"=自动) |
| `voice_local_model` | `"small"` | 本地 Whisper 模型大小 |
| `voice_asr_to_simplified` | `True` | 繁体→简体转换 |
| `_LOW_CONFIDENCE_THRESHOLD` | `0.6` | 低置信度打标 "[可能不准确]" |

---

### 功能J: 聊天记忆

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `consolidator.py` | `src/memory/consolidator.py` | 记忆合并触发和编排 |
| `store.py` | `src/db/store.py` | `group_memory` 表 CRUD |
| `schema.py` | `src/db/schema.py` | `group_memory` 表定义 |
| `prompts.py` | `src/summarize/prompts.py` | `MEMORY_CONSOLE_PROMPT` 模板 |

#### 数据库表结构

```sql
CREATE TABLE group_memory (
    chat_id           TEXT PRIMARY KEY,
    memory_text       TEXT NOT NULL DEFAULT '',
    message_count     INTEGER NOT NULL DEFAULT 0,
    last_message_id   TEXT,
    last_consolidated REAL,
    created_at        REAL NOT NULL DEFAULT (unixepoch()),
    updated_at        REAL NOT NULL DEFAULT (unixepoch())
);
```

#### 调用链

```
MessageRouter.handle(msg)
  │
  └── MemoryConsolidator.check_and_consolidate(chat_id)
        │
        ├── 获取当前记忆: MessageStore.get_group_memory(chat_id)
        ├── 统计新消息: MessageStore.get_new_message_count(chat_id, last_id)
        │
        ├── 触发条件 (任一满足):
        │     ├── 新消息数 >= CONSOLIDATE_MSG_THRESHOLD (50)
        │     └── 距上次合并 >= CONSOLIDATE_TIME_THRESHOLD_SEC (3600s)
        │
        ├── 获取新消息: MessageStore.get_messages_since_id()
        │
        ├── AI 合并: summarizer.consolidate_memory(existing, new_messages)
        │     └── 30s 超时 (ThreadPoolExecutor)
        │
        └── 持久化: MessageStore.upsert_group_memory()
```

#### 记忆格式

记忆采用**第一人称日记体**:
- "我"是 AI 助手在群中的身份
- 记录群友特点、口头禅、互动情况
- 记录群聊氛围和潜规则
- 限制 2000 字以内

#### 关键参数

| 参数 | 值 | 说明 |
|---|---|---|
| `CONSOLIDATE_MSG_THRESHOLD` | `50` | 触发合并的消息数阈值 |
| `CONSOLIDATE_TIME_THRESHOLD_SEC` | `3600` | 触发合并的时间阈值(1小时) |
| `MAX_NEW_MSGS_PER_CONSOLIDATION` | `400` | 每次合并最多处理的消息数 |
| 超时 | `30s` | AI 合并单次超时 |

---

### 功能K: macOS适配

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `desktop_mac.py` | `desktop_mac.py` | macOS 桌面入口 (WebView Cocoa) |
| `mac_ui_backend.py` | `src/wechat/mac_ui_backend.py` | macOS 界面自动化后端 |
| `mac_hybrid_backend.py` | `src/wechat/mac_hybrid_backend.py` | macOS 混合后端 (WCDB直读+自动化发送) |
| `mac_weflow_client.py` | `src/wechat/mac_weflow_client.py` | macOS WCDB 直读客户端 (WeFlow风格) |

#### MacHybridBackend 架构

```
MacHybridBackend
  │
  ├── 读路径: MacWeFlowClient
  │     ├── _WCDBSQLiteReader — ctypes 加载 libWCDB.dylib
  │     ├── 读取 session.db → get_sessions()
  │     ├── 读取 contact.db → get_contacts() → 显示名映射
  │     ├── 读取 message/*.db → get_new_messages()
  │     └── 密钥: all_keys.json (由 WeFlow 生成)
  │
  └── 写路径: MacUIAutomation
        ├── AppleScript (System Events) — 微信激活/搜索/发送
        ├── CoreGraphics (CGEventPost) — 屏幕点击
        ├── Vision OCR (VNRecognizeTextRequest) — 搜索匹配/标题验证
        └── 聊天标题映射缓存 — group_names.json + 日志行解析
```

#### MacUIBackend (纯界面自动化)

- 无 WCDB 访问 — 完全依赖 UI 文本读取
- 通过 AppleScript `System Events` 读取可见文本
- 使用 Vision OCR 识别当前聊天标题和搜索结果
- 通过 CoreGraphics 模拟鼠标点击进行界面操作

#### macOS 特有配置

| 环境变量 | 说明 |
|---|---|
| `WEBOT_APP_HOME` | 应用数据目录 (默认 `~/Library/Application Support/webot`) |
| `MAC_WECHAT_APP_NAME` | 微信应用名 (默认 `"WeChat"`) |
| `MAC_WECHAT_SEND_SHORTCUT` | 发送快捷键 (`"enter"` 或 `"cmd_enter"`) |
| `MAC_CHAT_TITLE_MAP` | 聊天标题映射 (`{"username": "display_name", ...}` JSON) |
| `MAC_CHAT_TITLE_CACHE_FILE` | 聊天标题缓存文件路径 |
| `MAC_WEFLOW_WCDB_LIB_DIR` | WeFlow WCDB 库目录 |
| `MAC_WEFLOW_DATA_DIR` | WeChat 数据目录 |
| `MACOS_CODESIGN_IDENTITY` | macOS 代码签名身份 (构建时) |

#### macOS 构建 (`build-macos.spec`)

- 输出: `dist/webot.app`
- 捆绑 `libWCDB.dylib` → `native/macos/`
- hiddenimports 包含 `AppKit`, `Quartz`, `objc` 等 macOS 框架
- excludes 排除 Windows 专用模块 (`uiautomation`, `win32api`, `comtypes` 等)
- 权限声明: `NSAppleEventsUsageDescription` + `NSScreenCaptureUsageDescription`

---

### 功能L: 媒体消息（表情/图片/appmsg）

#### 涉及文件

| 文件 | 路径 | 角色 |
|---|---|---|
| `pipeline.py` | `src/media/pipeline.py` | 统一入口 (归约 + 表情/图片视觉描述) |
| `blob.py` | `src/media/blob.py` | BLOB 消息体解码 (base64 → zstd → 文本) |
| `appmsg.py` | `src/media/appmsg.py` | appmsg/sysmsg XML → 一行文本 |
| `emoji.py` | `src/media/emoji.py` | 表情图片定位 (本地 Persist / CDN 下载) |
| `image.py` | `src/media/image.py` | 图片定位 (`msg/attach/{md5(session)}/`) + 解密 + 缓存 |
| `dat_decrypt.py` | `src/media/dat_decrypt.py` | `.dat` 四段容器解析 (AES-128-ECB 头 + 明文 + XOR 尾) |
| `wxgf.py` | `src/media/wxgf.py` | `wxgf`/HEVC 容器解包 (PyAV 可选) |
| `keys.py` | `src/media/keys.py` | 图片密钥来源解析 (`env` / `file`) |
| `vision.py` | `src/media/vision.py` | 图片描述 (OpenAI 兼容接口) |

#### 调用链

```
WcdbBackend._standardize(msg)
  │
  ├── is_binary(msg)?  → content = ""（base64 绝不进正文）
  │
  ├── localType == 34  → _try_voice()
  │
  ├── localType in {3,43,47,48,49}
  │     └── _try_media(msg, talker) or format_nontext_content(localType)
  │           ├── 3  (图片)   → MediaPipeline._describe_image(msg, chat_id)
  │           │     ├── ImageStore.fetch(msg)
  │           │     │     ├── 定位 msg/attach/{md5(chat_id)}/{yyyy-MM}/Img/*.dat
  │           │     │     │     匹配键: 基名（剥 _h/_hd/_t 后缀）+ file_size == length + 31
  │           │     │     ├── decrypt_dat(data, xor_key, aes_key_text)
  │           │     │     │     [15B header][AES-128-ECB 头段][明文段][XOR 尾段]
  │           │     │     ├── unwrap_wxgf(data)  → HEVC → JPEG（无 PyAV 则 None）
  │           │     │     └── 缓存 data/image_cache/{md5}
  │           │     └── VisionClient.describe(image) → "[图片] 猫在键盘上"
  │           ├── 47 (表情)   → MediaPipeline._describe_emoji()
  │           │     ├── EmojiStore.fetch(xml)
  │           │     │     ├── 本地 {wxid}/business/emoticon/Persist/{md5[:2]}/{md5}
  │           │     │     └── 否则 CDN 下载 cdnurl → data/emoji_cache/{md5}
  │           │     └── VisionClient.describe(image) → "[表情] 猫在摇头"
  │           └── 其他        → "[视频]" / "[链接]" 等占位符
  │
  └── 其他 BLOB（localType=1 文本、10000 系统、49-family appmsg）
        └── _try_media(msg) or _blob_text(msg)
              ├── appmsg XML  → "[链接] 标题"（reduce_appmsg）
              ├── sysmsg XML  → None（丢弃）
              └── 纯文本      → 原文（"哈哈哈哈哈哈哈"）
```

#### 各类型支持状态

| localType | 类型 | 状态 | 产出 |
|---|---|---|---|
| 3 | 图片 | ✅ 可用（需密钥 + 视觉） | `[图片] <视觉描述>`，缺任一项降级 `[图片]` |
| 47 | 表情/贴纸 | ✅ 可用 | `[表情] <视觉描述>` |
| 49-family | 链接/文件/小程序/视频 | ✅ 可用 | `[链接] 标题` 等 |
| 10000 | 系统提示 | — 丢弃 | 无 |
| 1 | 文本（BLOB 存储） | ✅ 可用 | 原文 |

图片链路的三个前提（任一缺失即降级 `[图片]`，绝不输出半解密的乱码）：

1. **AES 密钥** — 每台机器不同，不在消息 XML 里、也无法推导，必须 `IMAGE_AES_KEY` 手动填（XOR 密钥可自动推导，见下）
2. **容器结构** — `.dat` 是四段拼接，不是整段 AES：只有头部一段是 AES-128-ECB，中段明文，尾段单字节 XOR
3. **codec** — `wxgf` 载荷是 HEVC；打包版为控制体积不含 PyAV，这类图片改走**同图 JPEG 副本兜底**（`_h`/`_b`/`_t` 同基名 `.dat`），实测 1683 组真实图片 0 失败（其中 587 组靠兜底救回）。兜底也没有时才降级 `[图片]`

**XOR 密钥不需要提取。** 尾段是单字节 XOR，而图片有固定结尾符（JPEG `FF D9`、PNG `IEND®B\`\x82`），所以密钥可以从文件本身反推。`ImageStore._resolve_xor_key()` 在未配置时遍历附件目录、对每个 V2 文件的尾段投票，选出唯一能让最多文件正确收尾的字节。实测本机 3807 个文件：真值 `0xED` 得 2650 票，次高仅 1 票，前 10 个文件即可稳定得出，耗时 0.15s，结果（含推导失败）每次运行只算一次。

早前“整段 AES 全矩阵 0 命中 / XML `aeskey` 0/5 / CDN 直连 400”的结论，是因为把四段容器当成单一密码体；`aeskey` 签名的是 CDN 下载，与本地文件无关。

#### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `media_vision_enabled` | `False` | 视觉识别主开关；关闭时仍做文本归约 |
| `media_vision_model` | `""` | 视觉模型覆盖；空 = 复用 `AI_BACKEND` 的模型 |
| `image_xor_key` | `""` | 单字节 XOR 密钥；接受 `0x37` / `55`。**留空则自动从本地文件推导** |
| `image_aes_key` | `""` | AES 密钥取字符串**前 16 个 ASCII 字符**（非 hex 解码） |
| `image_key_provider` | `"env"` | `env` 读 `.env`；`file` 读 `data/image_keys.json`；`auto` 在微信运行时读 `GetImageKey`（按账号，结果另存 `data/image_keys.json`）。`.env` 已填时始终优先 |

#### 视觉后端支持

| `AI_BACKEND` | 视觉 | 说明 |
|---|---|---|
| `deepseek` | ✅ | 自动关闭 thinking；`max_tokens` 需 ≥ 800 否则 `content` 为空 |
| `openai` | ✅ | 走 `OPENAI_MODEL` |
| `claude` | ❌ | 接口形状不同，跳过（表情降级 `[表情]`） |

---

## 5. 配置文件和环境变量

### 5.1 `.env` 文件完整配置项

```ini
# === AI 后端 ===
AI_BACKEND=deepseek                           # "deepseek" | "claude" | "openai"

# === DeepSeek ===
DEEPSEEK_API_KEY=sk-xxx                        # DeepSeek API 密钥
DEEPSEEK_MODEL=deepseek-v4-flash               # 模型 ID
DEEPSEEK_BASE_URL=https://api.deepseek.com     # API 地址

# === OpenAI 兼容 (GLM / Moonshot / Qwen 等) ===
OPENAI_API_KEY=sk-xxx                          # OpenAI 兼容 API 密钥
OPENAI_BASE_URL=https://api.openai.com/v1      # GLM: https://open.bigmodel.cn/api/paas/v4/
OPENAI_MODEL=gpt-4o-mini                       # 模型 ID (如 glm-4-flash)

# === Claude (Anthropic) ===
ANTHROPIC_API_KEY=sk-ant-xxx                   # Anthropic API 密钥
ANTHROPIC_BASE_URL=https://api.anthropic.com   # API 地址
SUMMARIZE_MODEL=claude-haiku-4-5-20251001      # Claude 模型

# === 微信后端 ===
WECHAT_BACKEND=wcdb                            # "wcdb" / "mac_hybrid" / "mac_ui"
WECHAT_GROUPS=*                                # 群名(逗号分隔, URL编码, "*"=全部)
WECHAT_DATA_DIR=                               # 微信数据目录(留空自动检测)

# === 机器人身份 ===
BOT_DISPLAY_NAME=群聊小助手                     # 机器人显示名
ADMIN_WXID=                                    # 管理员 wxid

# === 触发关键词 ===
TRIGGER_KEYWORDS=总结一下,之前发了什么,错过了什么

# === 摘要配置 ===
SUMMARIZE_ENABLED=true
FALLBACK_WINDOW_HOURS=8

# === 主动发言 ===
PROACTIVE_ENABLED=false
PROACTIVE_RATE_WINDOW_SEC=120
PROACTIVE_RATE_QUIET=1.5
PROACTIVE_RATE_CASUAL=4.0
PROACTIVE_RATE_LIVELY=6.5
PROACTIVE_RATE_BURST=8.5

# === 欢迎新成员 ===
WELCOME_ENABLED=false

# === 粘性 @mention ===
STICKY_MENTION_ENABLED=true
STICKY_MENTION_TTL_SEC=60

# === 待办事项 ===
TODO_ENABLED=true
TODO_GROUPS=*
TODO_MAX_PER_GROUP=50
TODO_COMPLETED_RETENTION_DAYS=30
TODO_DELETED_RETENTION_DAYS=30

# === 调优 ===
POLL_INTERVAL_SEC=1.0
DEDUP_WINDOW_SEC=60
MAX_MESSAGES_FOR_SUMMARY=5000
CHUNK_SIZE=400

# === 日志 ===
LOG_LEVEL=INFO
LOG_FILE=data/bot.log

# === 数据库 ===
DB_PATH=data/messages.db

# === 趣味功能 ===
FUN_ENABLED=true

# === 语音识别 ===
VOICE_ASR_ENABLED=false
VOICE_ASR_BACKEND=local_whisper
VOICE_ASR_LANGUAGE=zh
VOICE_LOCAL_MODEL=small
VOICE_ASR_TO_SIMPLIFIED=true

# === 飞书导出 ===
FEISHU_EXPORT_ENABLED=false
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_EXPORT_MODE=knowledge
FEISHU_EXPORT_WINDOW_HOURS=8
FEISHU_AUTO_SYNC_ENABLED=false
FEISHU_AUTO_SYNC_MIN_MESSAGES=20
FEISHU_AUTO_SYNC_COOLDOWN_SEC=1800
FEISHU_KNOWLEDGE_BASE_NAME=webot 群聊沉淀
FEISHU_EXPORT_TRIGGER_KEYWORDS=同步到飞书,导出到飞书,写到飞书,沉淀到飞书

# === 引导流程 ===
ONBOARDING_DONE=false
WCDB_KEY=
```

### 5.2 `build.spec` 打包配置 (Windows)

```python
# 入口: desktop.py
# 输出: dist/webot.exe
# 二进制:
#   - WebView2Loader.dll → runtimes/win-x64/native
#   - WebBrowserInterop.x64.dll → lib/
#   - wcdb_api.dll → native/windows/   (自建替代版, 见 5.4)
#   - WCDB.dll → native/windows/
#   - MSVCP140.dll → native/windows/
#   - VCRUNTIME140.dll → native/windows/
#   - VCRUNTIME140_1.dll → native/windows/
#   - wx_key.dll → native/windows/
# 数据:
#   - ui/dist → ui/dist (前端构建产物)
#   - .env.example → . (示例配置)
# hiddenimports: 显式列出 src.* 各模块 + 第三方包 (dotenv/anthropic/openai/
#   pydantic/webview/psutil/PIL/comtypes/uiautomation) + src.media 与 zstandard
#   + Crypto (Crypto.Cipher.AES, 用于 .dat 图片头段 AES-128-ECB)
# 排除: faster_whisper, ctranslate2, numpy, onnxruntime, pysilk, tkinter, matplotlib, scipy
```

`src/media` 与 `zstandard` 必须留在 `hiddenimports` 里：`MediaPipeline` 是在
`WcdbBackend._get_media()` 内部延迟导入的，PyInstaller 的静态分析看不到这条
路径，漏掉会让打出的 EXE 在运行时抛 `ImportError`。

图片解密新增的 `src.media.dat_decrypt` / `wxgf` / `image` / `keys` 与 `Crypto`
同理必须显式列出。**PyAV 被刻意排除**：`wxgf` 载荷是 HEVC，带上 PyAV 会让
EXE 增大约 100 MB；不带时这类图片降级为 `[图片]` 占位符，其余链路不受影响。

### 5.3 `requirements.txt` 依赖说明

| 包名 | 用途 | 必需 |
|---|---|---|
| `python-dotenv>=1.0.0` | 加载 .env 环境变量 | 是 |
| `anthropic>=0.45.0` | Claude API SDK | 按后端需求 |
| `openai>=1.0.0` | DeepSeek API (OpenAI 兼容) | 按后端需求 |
| `ddgs>=9.0` | DuckDuckGo 搜索 (AI 上下文增强) | 是 |
| `pydantic>=2.0.0` | 结构化输出模型 (SummaryResult) | 是 |
| `uiautomation>=2.0` | Windows UI 自动化 (窗口操控) | Windows 必需 |
| `pywin32>=300` | Windows API 绑定 (窗口/剪贴板/进程) | Windows 必需 |
| `comtypes>=1.4` | COM 类型库 (UIA) | Windows 必需 |
| `pywebview>=5.0` | 桌面 WebView 窗口 | 是 |
| `Pillow>=10.0` | 图像处理 (白屏检测) | 是 |
| `psutil>=5.0` | 进程管理 | 是 |
| `pyperclip>=1.8` | 剪贴板操作 | 是 |
| `silk-python>=0.2.8` | SILK v3 音频解码 | 语音功能 |
| `faster-whisper>=1.0` | 本地语音识别 | 语音功能 |
| `opencc-python-reimplemented>=0.1.7` | 繁简中文转换 | 语音功能 |
| `zstandard>=0.22` | 解压微信非文本消息体 (base64 → zstd → 文本) | 媒体功能 |
| `pycryptodome>=3.20` | `.dat` 图片容器头段 AES-128-ECB 解密 | 图片功能 |

### 5.4 前端依赖 (`ui/package.json`)

| 包名 | 用途 |
|---|---|
| `react` / `react-dom` `^19.2.7` | UI 框架 |
| `framer-motion` `^12.40.0` | 动画库 (Spring 过渡) |
| `@phosphor-icons/react` `^2.1.10` | 图标库 |
| `@tailwindcss/vite` `^4.3.0` | Tailwind CSS v4 集成 |
| `@vitejs/plugin-react` `^6.0.2` | Vite React 插件 |
| `tailwindcss` `^4.3.0` | CSS 框架 |
| `vite` `^8.0.16` | 构建工具 |

### 5.5 数据文件 (运行时生成)

| 文件路径 | 生成者 | 内容 |
|---|---|---|
| `data/messages.db` | `src/db/schema.py` | SQLite 消息数据库 |
| `data/bot.log` | `src/utils/logging_config.py` | 运行日志 |
| `data/bot_status.json` | `src/bot.py:HealthMonitor` | 状态 JSON (外部监控) |
| `data/nicknames.json` | `src/nickname.py` | wxid → 昵称映射 |
| `data/group_names.json` | `src/wechat/wcdb_backend.py` | chat_id → {name, member_count} |
| `data/group_members.json` | `src/wechat/wcdb_backend.py` | chat_id → {wxid: display_name} |
| `data/welcome_templates.json` | `src/welcome.py` | 欢迎模板 + 群映射 |
| `data/lots.json` | `src/fun.py` | 自定义抽签配置 |
| `data/feishu_resources.json` | `src/integrations/feishu/knowledge.py` | 飞书知识库资源 ID |
| `data/voice_cache.json` | `src/voice/pipeline.py` | 语音识别缓存 |
| `data/send_failures.log` | `src/wechat/window_controller.py` | 发送失败记录 |
| `data/crash.log` | `desktop.py` / `desktop_mac.py` | 崩溃日志 |

### 5.6 自建 `wcdb_api.dll` 替代品 (Windows)

**为什么需要**：仓库原先捆绑的 `native/windows/wcdb_api.dll` 来自 WeFlow，`wcdb_init()` 里写死了一个授权到期检查，到期后无条件返回 `-1000`，导致 webot 一点启动就报 `wcdb_init failed:-1000`。反汇编证据（`wcdb_init`，RVA `0xe4120`）：

```asm
0x1800e4439  mov  dword [rsp+0x6c], 0x7e   ; tm_year = 126 → 2026
0x1800e4441  mov  dword [rsp+0x68], 0x08   ; tm_mon  = 8   → 9 月
0x1800e4449  mov  dword [rsp+0x64], 0x1e   ; tm_mday = 30
0x1800e4451  mov  dword [rsp+0x60], 0x17   ; tm_hour = 23
0x1800e4459  mov  dword [rsp+0x5c], 0x3b   ; tm_min  = 59
0x1800e4461  mov  dword [rsp+0x58], 0x3b   ; tm_sec  = 59
0x1800e4469  call _mktime64                ; → 到期时间戳
0x1800e447e  call _time64                  ; → 当前时间
0x1800e4484  cmp  rax, rbx
0x1800e4487  jle  0x1800e45bd              ; 未过期 → return 0
0x1800e45b1  call 0x18006cb70              ; 过期 → 自毁分支
0x1800e45b6  mov  eax, 0xfffffc18          ; return -1000
```

到期时间为 **2026-09-30 23:59:59 UTC**。`0xfffffc18` 在整个 `.text` 节只出现一次，返回路径唯一。

**替代方案**：`native/src/wcdb_api_replacement.c` 用 SQLCipher 重新实现 webot 实际使用的 10 个 C ABI 导出，无任何授权检查。

| 项 | 说明 |
|---|---|
| 源码 | `native/src/wcdb_api_replacement.c` |
| 构建脚本 | `tools/build_wcdb_api.py`（离线，无需网络） |
| 编译器 | gcc（已验证 Strawberry Perl 13.2；MSVC 亦可） |
| 加密库 | SQLCipher 4.5.7 amalgamation，取自 cargo registry 的 `libsqlite3-sys-*/sqlcipher/` |
| OpenSSL | 链接 **Python 自带的** `libcrypto-3.dll`（`<prefix>/DLLs/`），无需额外分发文件 |
| 产物 | `native/windows/wcdb_api.dll`（约 1.2 MB） |

**导出函数**（12 个）：

| 导出 | 说明 |
|---|---|
| `InitProtection` | 恒返回 0（无授权检查） |
| `wcdb_init` / `wcdb_shutdown` | 引擎生命周期，幂等 |
| `wcdb_open_account` | 先用 key 探测 session.db 再注册账号；非法路径 `-3`、空 key `-2`、NULL `-1` |
| `wcdb_close_account` | 释放账号槽位与 contact 句柄 |
| `wcdb_free_string` | 释放返回的 JSON 缓冲 |
| `wcdb_get_sessions` | `SELECT * FROM SessionTable ORDER BY sort_timestamp DESC, last_timestamp DESC` |
| `wcdb_get_messages` | 按 `Msg_<md5(talker)>` 定位分库，`LEFT JOIN Name2Id` 解析 `real_sender_id` |
| `wcdb_get_display_names` | 按 remark → nick_name → alias 优先级解析，未知名回显 wxid |
| `wcdb_get_contacts_compact` | 关键字过滤通讯录 |
| `wcdb_get_group_members` | 经 `name2id` + `chatroom_member` 关联群成员 |
| `webot_wcdb_api_replacement` | 标记函数，供 `wcdb_client.py` 识别替代品 |

**关键 schema 差异**（易错点）：`contact.db` 的 `name2id` 用 `username` 列，而 `message_*.db` 的 `Name2Id` 用 `user_name` 列。

**关键密钥投递方式**（最易错，踩过）：微信是把 32 字节原始密钥交给 `sqlite3_key()`，SQLCipher 会把它当**口令**过 PBKDF2，**不是**当 raw key。用 `PRAGMA key = "x'<64hex>'"` 派生出的密钥完全不同，合法密钥会被判成 `SQLITE_NOTADB`（`wcdb_open_account` 返回 `-3`）。实测同一个真实密钥对 `session.db` / `contact.db` / `message_0.db` 的三种投递方式：

| 投递方式 | session.db | contact.db | message_0.db |
|---|---|---|---|
| `sqlite3_key(db, raw32, 32)`（微信自己的用法） | ✅ 18 objects | ✅ 29 | ✅ 307 |
| `PRAGMA key = "x'<64hex>'"`（raw key） | ❌ not a database | ❌ | ❌ |
| `PRAGMA key = '<64hex>'`（hex 当口令） | ❌ | ❌ | ❌ |

因此 `open_encrypted()` 按 `sqlite3_key(raw32)` → raw-key → hex 口令 → 纯口令 的顺序依次尝试，第一个能解密 `sqlite_master` 的胜出。改这里前务必用真实库回归，别只信自建 fixture——自建库用哪种方式写就能用哪种方式读，测不出这个坑。

**构建与校验**：

```bash
python tools/build_wcdb_api.py           # 构建并安装到 native/windows/
python tools/build_wcdb_api.py --check   # 只校验已安装的 DLL
```

**与 DRM patch 的关系**：`wcdb_client.py` 通过 `webot_wcdb_api_replacement` 标记识别替代品；识别到就**跳过** `_apply_drm_patch`——原版补丁 RVA `0x6e1f6` 在替代品里并不存在，强行写入只会破坏代码。此外 `_apply_drm_patch` 现在会先校验字节，不匹配时拒绝写入。

