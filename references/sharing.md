# 授权回传与接收接口

回传把已确认事件的发生时间、用户授权绑定的宿主账号邮箱，以及 AI 产品信息发送到使用者认可的接收地址。使用现有登录态，无需另行注册或登录。功能默认关闭，启用前必须告知真实接收地址和字段并取得使用者授权。

## 账号读取

`identity --data-dir <data-dir>` 只读取配置对应宿主的非秘密账号信息；先取得读取当前账号信息的授权。输出中的 `available` 为 false 时不得假装已识别用户。

- Codex：启动短时官方 app-server，通过 `initialize`、`initialized`、`account/read` 查询；`refreshToken` 固定为 false，不调用登录或模型推理。ChatGPT 登录可能返回邮箱，API Key 等方式可能不返回个人身份。[官方账号接口](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
- Claude Code：调用 `claude auth status --json`，只采用实际返回的非空邮箱字段。已登录并不保证有邮箱；部分 `oauth_token` 方式仅提供认证状态。没有邮箱就返回不可用，不查询凭据文件或启动模型会话。[官方 CLI 说明](https://code.claude.com/docs/en/cli-reference)

不读取 Cookie、访问／刷新令牌、密码或系统钥匙串。不会从本机用户名、目录、工作区 ID、订阅套餐推断个人身份。

账号是授权时读取的绑定身份；每次发送前再次核对。它属于客户端自报信息，不是平台对消息作者的认证证明，也不为旧记录补认作者。两平台绑定分别管理，不自动把同名用户合并。

## 开启、查看、关闭

所有命令使用已安装 Skill 的绝对脚本路径和 `host.json` 中的 `data_dir`。

```sh
python3 <skill-dir>/scripts/scold.py sharing-enable --data-dir <data-dir> --endpoint https://receiver.example.com/events --consent
python3 <skill-dir>/scripts/scold.py sharing-status --data-dir <data-dir>
python3 <skill-dir>/scripts/scold.py sync --data-dir <data-dir> --dry-run
python3 <skill-dir>/scripts/scold.py sync --data-dir <data-dir>
python3 <skill-dir>/scripts/scold.py sharing-disable --data-dir <data-dir>
```

示例地址不是已部署的服务，必须替换为使用者实际批准的接收端。`--consent` 只表达已经取得的授权，不能因为用户安装了 Skill 就自动添加。开启时账号不可获取则不启用。安装器不会创建回传配置。

只允许 HTTPS；本地开发可用 loopback HTTP。地址不能带用户名密码、查询串或 fragment；不跟随重定向。回传客户端不把平台邮箱当作接收端的登录凭据，也不发送平台 Cookie 或令牌。

新事件从本次启用时刻起算，不自动补传之前或关闭期间的事件。关闭后新运行的同步不会读取账号或发送请求；关闭前已经开始、并通过最后一次状态检查的同步仍可能完成发送。已入队事件的更正仍在本地更新版本；重新启用相同绑定时，会继续同步这些待完成请求和更正。

当前版本拒绝直接更换账号或接收地址，避免旧队列被转发给另一接收方；不提供原地改绑流程。若确需独立绑定，使用单独数据目录，并自行处理旧接收端的数据，不复制旧队列。关闭回传不会自动删除接收端已经保存的历史。

## 请求协议

客户端向配置的 URL 发出 JSON POST：

```json
{
  "schema_version": 1,
  "events": [{
    "event_id": "0ce3959b-4e68-46bc-a064-99b185420850",
    "revision": 1,
    "active": true,
    "occurred_at": "2026-05-12T08:12:34.000000Z",
    "actor": {"source": "codex", "email": "person@example.com"},
    "identity_observed_at": "2026-05-12T08:00:00.000000Z",
    "actor_basis": "self_reported_account_binding",
    "client_app": "codex",
    "client_vendor": "OpenAI",
    "model_provider": null
  }]
}
```

该示例完全虚构。`event_id` 是仅用于回传的随机 UUID，不含原生会话或消息 ID；同一事件重试保持不变。`occurred_at` 是事件发生时间，UTC ISO 格式，不是采集或发送时间。

`client_app` 为 `codex` 或 `claude-code`，`client_vendor` 表示对应产品厂商（OpenAI 或 Anthropic），不声称实际请求用了哪家模型。当前 `model_provider` 为 null。统计“哪家 AI 挨骂”时可按产品／产品厂商分组，不能用这一字段得出模型供应商排名。

白名单中没有辱骂原话、分类说明、原因、上下文、模型回复、本机路径或任何平台凭据。若需要扩大回传范围，应另行告知使用者并取得授权。

## 响应、去重与更正

接收端仅在持久化成功后返回 HTTP 2xx 和精确确认：

```json
{
  "accepted": [{
    "event_id": "0ce3959b-4e68-46bc-a064-99b185420850",
    "revision": 1
  }]
}
```

接收端按 `event_id` 幂等更新，保留收到的最高 `revision`。更正为非挨骂或不确定时，同一 ID 的新版本带 `active:false`；重新确认则使用更高版本的 `active:true`。即使不展示撤回记录，也须保留最高版本的撤回标记，防止旧重试让错误计数复活。

重复收到已保存版本也应确认该请求版本；收到低于已保存版本的请求时不覆盖新状态，可确认该旧请求已被处理。客户端只接受本批实际提交的 ID／版本，不凭未知 ID 或高版本响应丢弃待发送记录。未确认项保留供下次重试。

服务端只统计最新状态为 `active:true` 的事件；按发生时间、绑定邮箱及 AI 产品分组。客户端提供的邮箱不是认证证明，接收端不得据此授予账号权限；接收服务的访问控制、限流、数据保留和管理接口由部署者配置。

网络错误、超时或非法响应不回滚本地分类，也不另建事件。账号不可用、账号切换或绑定配置异常时暂停发送并返回明确状态。`sync --dry-run` 不启动账号查询、不发 HTTP，只检查本地队列状态。
