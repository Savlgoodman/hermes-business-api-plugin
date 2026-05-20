# Hermes Business API 插件

Business API 是一个面向业务后端调用的 Hermes Gateway platform 插件。它在兼容 OpenAI Responses API 的基础上，额外提供会话上下文查询、token 用量统计以及文件上传/下载能力。

把 Hermes 部署在 Docker 或远程服务器时，你的业务服务通过 HTTP 调用这个插件即可与 Hermes agent 交互，并在业务侧完成用户管理、会话追踪、计费、文件处理等逻辑。

## 安装

### 从 Git 仓库安装

```bash
hermes plugins install https://github.com/Savlgoodman/hermes-business-api-plugin --enable
```

`--enable` 会同时将插件加入 `plugins.enabled` 列表，下次启动 Hermes 时自动加载。

### 手动安装

将 `plugin.yaml`、`__init__.py`、`adapter.py` 放到用户插件目录：

```bash
mkdir -p ~/.hermes/plugins/platforms/business_api
cp plugin.yaml __init__.py adapter.py ~/.hermes/plugins/platforms/business_api/
hermes plugins enable platforms/business_api
```

## 启用

安装时如果没有加 `--enable`，可以手动启用：

```bash
hermes plugins enable platforms/business_api
```

禁用：

```bash
hermes plugins disable platforms/business_api
```

启用后重启 gateway：

```bash
hermes gateway run
```

## 配置

### 必填环境变量

安装时如果设置了 `requires_env` 中的变量，会保存到 `~/.hermes/.env`，启动时自动加载。也可以手动设置：

| 变量 | 用途 |
| --- | --- |
| `BUSINESS_API_KEY` | 请求鉴权用的 Bearer token（必须配置） |

### 可选环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `BUSINESS_API_ENABLED` | 空 | 设置为 `true` 可通过环境变量启用插件 |
| `BUSINESS_API_HOST` | `127.0.0.1` | 监听地址 |
| `BUSINESS_API_PORT` | `8765` | 监听端口 |
| `BUSINESS_API_WORKSPACE_ROOT` | `/opt/workspace` | 文件上传和下载允许访问的根目录 |
| `BUSINESS_API_MAX_UPLOAD_BYTES` | `104857600` | 单文件上传大小上限（100MB） |

### 请求鉴权

所有接口都需要通过 Bearer token 鉴权：

```http
Authorization: Bearer <BUSINESS_API_KEY>
```

## 接口

### `POST /v1/responses`

调用 Hermes agent 的 Responses 接口，兼容 OpenAI Responses API 规范，支持通过 `previous_response_id` 延续上下文。

示例：

```bash
curl -X POST http://127.0.0.1:8765/v1/responses \
  -H "Authorization: Bearer $BUSINESS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"你好，请生成一份 123.txt 的内容。"}'
```

### `GET /api/responses/{response_id}/context`

根据 response id 查询该次响应的上下文快照、模型信息、本轮 token 用量，以及当前 Hermes session 的累计 token 用量。

示例：

```bash
curl "http://127.0.0.1:8765/api/responses/resp_xxx/context" \
  -H "Authorization: Bearer $BUSINESS_API_KEY"
```

不需要返回消息列表时：

```bash
curl "http://127.0.0.1:8765/api/responses/resp_xxx/context?include_messages=false" \
  -H "Authorization: Bearer $BUSINESS_API_KEY"
```

### `POST /api/files`

把文件上传到 Hermes 工作目录，供远端 Docker 内的 agent 读取或处理。

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 上传文件 |
| `target_path` | 否 | 目标目录，为空时写入 `BUSINESS_API_WORKSPACE_ROOT` |
| `overwrite` | 否 | `true` 时覆盖同名文件，默认自动改名 |
| `conversation_id` | 否 | 业务侧会话 id，仅记录在返回值中 |

示例：

```bash
curl -X POST http://127.0.0.1:8765/api/files \
  -H "Authorization: Bearer $BUSINESS_API_KEY" \
  -F "target_path=/opt/workspace/user-a" \
  -F "overwrite=true" \
  -F "file=@123.txt"
```

### `GET /api/files`

从 Hermes 工作目录下载文件。

查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `path` | 否 | 文件所在目录，为空时使用 `BUSINESS_API_WORKSPACE_ROOT` |
| `file_name` | 是 | 文件名，不允许带目录跳转字符 |

示例：

```bash
curl -OJ "http://127.0.0.1:8765/api/files?path=/opt/workspace/user-a&file_name=123.txt" \
  -H "Authorization: Bearer $BUSINESS_API_KEY"
```

## 安全注意事项

- `BUSINESS_API_KEY` 应使用强随机值，并置于内网、反向代理或 TLS 后面
- `BUSINESS_API_WORKSPACE_ROOT` 应设为专用目录（如 Docker 内的 `/opt/workspace`），不要指向 `/` 或用户 home
- 文件接口的租户隔离应由业务后端负责：维护用户、会话和工作目录的绑定关系
- 插件层的 workspace-root containment 是兜底防护，不应作为唯一的授权边界

## 本地 Smoke Test

启动 gateway 后运行：

```bash
python scripts/business_api_upload_smoke.py
```

脚本会上传一个内容为 `hello` 的 `123.txt`，再下载回来并校验内容一致。
