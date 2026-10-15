# Roast Recap：安装、试用与调度

产品名为 Roast Recap（挨骂总结）；安装目录与调用标识继续使用 `ai-scold-review`，已有台账和定时任务可直接沿用。v0.2.1 统一显示名称及总结文案。

需要 Python 3.9+。当前适配本机 Codex 和 Claude Code，不把 Claude 消费者网页/桌面聊天冒充为已支持来源。

v0.2.0 增加第二阶段的主要原因分析及对话证据，保留v0.1.1的localImage兼容修复。升级只新增原因及上下文表，保留启用时间、事件ID和既有分类；原先确认的事件会进入原因待分析。仍只分析文字，不分析图片内容。

在解压的 skill 目录运行：

```sh
python3 scripts/install.py install --platform both --timezone Asia/Shanghai --dry-run
python3 scripts/install.py install --platform both --timezone Asia/Shanghai
```

只装一个宿主时，将 `both` 改为 `codex` 或 `claude-code`。默认尊重 `CODEX_HOME` 与 `CLAUDE_CONFIG_DIR`，也可显式指定 `--codex-root` / `--claude-root`。安装器写入对应根目录的 `skills/ai-scold-review` 与独立的 `ai-scold-review` 数据目录；不修改原聊天、不修改原有设置、不设置任何 hook 信任。

沙箱中的定时任务需要可写台账目录时，可加 `--data-base <已授权的工作目录>`；安装器会为每个平台创建独立子目录并写入 host.json。后续更新自动保留该路径，不要求扩大定时任务权限。

首次安装以实际启用时刻为统计起点；重复安装保留台账与起点。安装成功输出 skill/data/source 的实际位置。更新旧版时，在数据目录 `install-backups` 保留旧 skill。已有同名但非本安装器管理的目录会拒绝覆盖。

安装后新建聊天或刷新宿主的技能列表，调用“使用 ai-scold-review 汇总今天的记录”。Codex 可显式输入 `$ai-scold-review`；Claude Code 可使用 `/ai-scold-review`。没有出现时先检查宿主是否加载安装位置，不能据此声称技能已运行。

## 自动每日、每周、每月总结

安装脚本不自行创建定时任务。宿主支持定时任务时，使用其官方调度入口设置：

> 每天21:00，按Asia/Shanghai时区，调用已安装的ai-scold-review，采集本平台启用后的新增用户消息并完成pending分类，再处理reason-pending，给确认事件保存有原话证据的主要原因。生成今日截至采集时的概览，同时结算前一自然日，覆盖晚间21:00之后的消息。周一同时汇总前一周；每月1日同时汇总上月。停机后补跑时补齐尚未结算的日期。总结显示来源覆盖、已确认次数、原话、时间、主要原因分布和未决项，区分用户明确表达、上下文推测与无法判断。失败要说明缺口。不评价骂后效果、不生成第三阶段行动方案。

Codex 中使用应用的定时任务工具创建，不用另写系统 cron。Claude Code 单独安装时，可手动调用；要实现无人值守每日总结，仍需宿主提供可持续运行的调度。本机有 Codex 调度时，可明确授权一个任务分别执行两套安装的 skill 并汇总，用户无需导出记录。不要宣称 Claude Code 单独安装就已经自动安排每日总结。

本地任务到点运行依赖电脑及宿主处于可运行状态；总结应保留最近成功采集时间。新增记录可以在下一次运行时回填，但这不代表漏掉的总结已经准时送达。

## 更正与移除

对误判可说“把 event_id 为……的记录改成非辱骂”；skill 对原记录改判，重新生成总结。

```sh
python3 scripts/install.py uninstall --platform both --dry-run
python3 scripts/install.py uninstall --platform both
```

卸载仅移除本安装器管理的 skill，保留台账。相关定时任务需要通过原宿主调度工具停用。未经用户明确要求，不删除历史台账。
