# lobsterai2api

配置通过项目根目录的 `.env` 文件或环境变量加载，模板见 `.env.example`；环境变量优先于 `.env`。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LB2A_API_KEY` | 空 | 聊天接口的 Bearer API Key，为空时不启用鉴权 |
| `LB2A_LOGIN_PORTAL` | `https://lobsterai.youdao.com` | LobsterAI 登录门户地址 |
| `LB2A_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接地址，auths 与 data 均写入 Redis |
| `LB2A_REDIS_PREFIX` | `lb2a` | Redis 键前缀 |
| `LB2A_UPSTREAM_BASE` | `https://lobsterai-server.youdao.com` | LobsterAI 上游 API 地址 |
| `LB2A_TOOL_MODEL` | 空 | Responses 工具调用使用的上游模型，为空时沿用普通模型映射 |
| `LB2A_TIMEOUT_SECONDS` | `180` | 上游 API 读取超时时间，单位为秒 |
| `LB2A_MAX_ACCOUNT_RETRIES` | `3` | 单次聊天请求最多尝试的账号数 |
| `LB2A_HARD_CREDIT` | `12h` | 余额不足后的冷却时间，支持纯数字秒数或 `60s`、`10m`、`12h`、`1d` |
| `LB2A_SOFT_RATE` | `60s` | 限流或资源不可用后的冷却时间 |
| `LB2A_ERR_THRESHOLD` | `3` | 连续错误达到多少次后冷却账号 |
| `LB2A_ERR_COOLDOWN` | `10m` | 连续错误后的冷却时间 |
| `LB2A_CHECKIN_HOURS` | `[9, 21]` | 自动签到小时列表，JSON 数组格式 |
| `LB2A_KEEPALIVE_HOURS` | `[22]` | 自动刷新 Token 小时列表，JSON 数组格式 |

## 启动运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
# 按需编辑 .env
uvicorn app.main:app --host 0.0.0.0 --port 8367
```

监听地址和端口由 Uvicorn 的 `--host`、`--port` 参数控制。本地进程内定时任务使用本地时间；部署到 Vercel 时不启动进程内定时器，改由 `vercel.json` 的 Cron 触发，Cron 使用 UTC。
