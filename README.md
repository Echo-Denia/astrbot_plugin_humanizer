# astrbot_plugin_humanizer

让 AstrBot 的回复更接近真人行为。仅在 **aiocqhttp (NapCat/OneBot v11)** 平台生效。

## 功能

### 1. 回复前随机延迟
LLM 回复后、真正发送消息前，随机等待一段时间，模拟人类打字/思考。

- 支持三种分布：
  - **`mixed`**（默认，推荐）：对数正态分布为主，偶发（8% 概率）触发"走神/被打断"长延迟，最像真人
  - **`lognormal`**：纯对数正态分布，延迟集中在中位数附近，尾部自然
  - **`triangular`**：三角分布，延迟集中在峰值
- 可按消息字数加权延迟（长消息多等一会）
- 群聊/私聊可分别开关
- 命令回复默认不加延迟

配置（在 `main.py` 顶部）：
```python
DELAY_DISTRIBUTION = "mixed"        # "mixed" | "lognormal" | "triangular"
DELAY_MIN = 1.0                     # 最小延迟（秒，triangular 用）
DELAY_MAX = 4.0                     # 最大延迟（秒，triangular 用）
DELAY_PEAK = 2.0                    # 三角分布峰值
DELAY_PER_CHAR = 0.015              # 每字额外延迟

# 对数正态参数（lognormal / mixed 生效）
DELAY_LOGNORMAL_MU = 0.65           # 位置参数，影响中位数
DELAY_LOGNORMAL_SIGMA = 0.45        # 形状参数，越大尾部越长

# mixed 模式：偶发长延迟
DELAY_MIXED_LONG_PROB = 0.08        # 8% 概率触发长延迟
DELAY_MIXED_LONG_MIN = 4.0
DELAY_MIXED_LONG_MAX = 9.0

DELAY_FOR_COMMAND = False
DELAY_ENABLE_GROUP = True
DELAY_ENABLE_PRIVATE = True
```

### 2. 去除尾随句号 + 拆分换行
人类发消息并不总加句号，也不会在一条消息里写好几行。

- 自动去掉 LLM 回复末尾的 `. 。 ！ ! ？ ?` 和空白
- **把含 `\n` 的文本拆成多条独立消息**逐条发送，更像真人一句一句发
- 只处理 LLM 结果，不处理命令回复
- 默认只对纯文本消息生效（含图片/@ 的不处理）

配置：
```python
TRIM_TRAILING_PERIOD = True
TRIM_PLAIN_ONLY = True
```

### 3. 已读不回 / 回复时机决策
人类不会每条消息都秒回。这个功能让机器人在收到消息时决定：**回不回、等多久再回**。

支持两种模式：

- **`probability`**：按时间段随机决定。白天概率低，深夜概率高。
- **`llm`**（默认）：调用 astrbot 当前使用的 LLM，根据消息内容、时间、对方是否连发等因素智能判断。LLM 返回 `{"reply": bool, "delay_seconds": int, "reason": str}`。LLM 超时或失败时自动回退到概率模式。

共同规则：
- 命令（`/` 开头）、@机器人永远回复
- 连续忽略上限：同一个人连续被忽略 N 条后，下一条一定回复
- 群聊/私聊可分别开关

配置（`main.py` 顶部）：
```python
READ_NO_REPLY_ENABLE = True
READ_NO_REPLY_MODE = "llm"                  # "probability" | "llm"
READ_NO_REPLY_PRIVATE = True
READ_NO_REPLY_GROUP = False
READ_NO_REPLY_MAX_SKIP = 8

# 概率模式
READ_NO_REPLY_PROB_DAY = 0.08
READ_NO_REPLY_PROB_NIGHT = 0.35
READ_NO_REPLY_DAY_HOURS = (8, 23)

# LLM 模式
READ_NO_REPLY_LLM_PROVIDER_ID = ""          # 指定 provider ID，留空用当前会话默认
READ_NO_REPLY_LLM_TIMEOUT = 8
READ_NO_REPLY_LLM_FALLBACK_TO_PROB = True
```

> **如何选择 LLM 模型：**
> 1. 打开 AstrBot WebUI → **模型管理** 页面
> 2. 每个模型卡片上会显示它的 **ID**
> 3. 把想用的模型 ID 填到 `READ_NO_REPLY_LLM_PROVIDER_ID`
> 4. 留空 `""` 则使用当前聊天会话默认的模型
>
> 建议：决策用便宜/快的模型，不要用主力对话模型。

### 4. 加群请求处理（默认关闭）
监听 OneBot v11 的加群请求事件（`request_type=group`）。

模式：
- `"off"` —— 关闭（默认）
- `"auto_accept"` —— 自动同意所有加群请求
- `"auto_reject"` —— 自动拒绝所有加群请求
- `"notify_admin"` —— 通知管理员，由管理员手动处理

配置：
```python
GROUP_REQUEST_MODE = "off"
ADMIN_IDS = ["123456789"]  # notify_admin 模式下通知的管理员 QQ
```

### 5. 好友申请处理
监听 OneBot v11 的好友申请事件（`request_type=friend`）。

模式：
- `"manual"` —— 通知管理员，由管理员手动处理
- `"auto"` —— 自动同意所有好友申请
- `"llm"`（默认）—— LLM 根据验证消息判断，`accept` / `reject` / `ask`（不确定时询问管理员）

LLM 默认策略：**默认拒绝**，只有明确说出共同身份/共同朋友的才同意；含糊其辞、营销、搭讪一律拒绝。

配置：
```python
FRIEND_REQUEST_MODE = "llm"
FRIEND_REQUEST_LLM_PROVIDER_ID = ""
FRIEND_REQUEST_LLM_TIMEOUT = 10
```

### 6. 好友管理 LLM 工具
向 LLM 暴露一组好友管理工具，让 LLM 可以代你查看/操作好友列表。

| 工具名 | 功能 |
|--------|------|
| `friend_list` | 获取好友列表（QQ 号 + 昵称） |
| `friend_stranger_info` | 查询陌生人信息（昵称、性别、年龄） |
| `friend_pending_requests` | 查看待处理的好友申请 |
| `friend_accept` | 同意好友申请 |
| `friend_reject` | 拒绝好友申请 |
| `friend_delete` | 删除好友 |

**权限控制**：只有 `ADMIN_IDS` 中配置的管理员才能调用这些工具，其他用户调用会被拒绝。未配置管理员时默认全部拒绝。

```python
ADMIN_IDS = ["123456789"]  # 你的管理员 QQ 号
```

## 安装
将本目录放入 astrbot 的 `data/plugins/` 下，重启 astrbot 即可。

## 配置说明
所有配置都在 `main.py` 顶部，修改后需重启 astrbot 生效。

## 平台限制
- 延迟、去句号、好友管理等功能只影响 **aiocqhttp** 平台（NapCat）
- 其他平台（QQ 官方机器人等）不受影响

## License
MIT
