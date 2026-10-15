# Roast Recap · 挨骂总结

记录你什么时候骂了 AI、骂了什么，以及有对话证据支持的原因。按日、周、月生成总结。

Roast Recap 是安装在本机 AI 宿主中的 Skill。目前支持 **Codex 和 Claude Code**，直接读取本机原生聊天记录，无需手动导出。当前版本为 **v0.2.1**。

## 能做什么

- 记录明确针对 AI 的辱骂或强烈斥责，一条消息计一次。
- 排除普通修改意见、玩笑、引用、产品讨论和非真人消息；不确定的记录单独保留。
- 分析每次事件的一个主要原因，附可追溯的原话证据。
- 区分“用户明确表达”“根据上下文推测”和“无法判断”。用户表达的理由不自动等于已证实的客观错误。
- 生成每日总结、每周总结、每月总结，显示实际覆盖范围、待分类及待分析数量。
- 支持纠正误判、重复运行去重，以及保留已有记录的升级。

目前完成了第一阶段的记录统计、第二阶段的原因分析。第三阶段“被骂后 AI 表现变好还是变差，以及如何改进”尚未实现。

## 安装

需要 Python 3.9 或更高版本，运行脚本仅依赖 Python 标准库。

```sh
git clone https://github.com/IXYTYXI/AI-Roast-Recap.git
cd AI-Roast-Recap
python3 scripts/install.py install --platform both --timezone Asia/Shanghai --dry-run
python3 scripts/install.py install --platform both --timezone Asia/Shanghai
```

只安装一个平台时，将 `both` 改成 `codex` 或 `claude-code`。安装器支持 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`，也可显式指定宿主根目录。

产品显示名是 **Roast Recap（挨骂总结）**，安装目录和调用标识沿用 `ai-scold-review`，方便已安装用户直接升级。重复安装会保留启用时间、统计台账和原因分析。

## 使用

安装后刷新宿主技能列表或开启新聊天。可以说：

> 使用 ai-scold-review 生成今天的挨骂总结，列出时间、原话、原因和证据。

Codex 可显式调用 `$ai-scold-review`；Claude Code 可调用 `/ai-scold-review`。还可以要求每周、每月总结，或更正某条记录的分类与原因。

消息采集、去重、证据核验和统计由 Python 脚本处理；是否属于斥责、具体原因是什么，由运行 Skill 的宿主 AI 根据规则判断。

## 自动总结

**安装 Skill 本身不会创建后台服务或定时任务。** 自动总结需要另外配置宿主调度；本地任务到点运行依赖电脑及宿主处于可运行状态。

推荐每天 21:00 生成当天截至采集时的概况，同时结算前一天，覆盖前一晚的后续聊天；周一汇总上周，月初汇总上月。停机后可补跑，并明确实际覆盖范围。

本机有 Codex 调度时，可以授权同一任务分别运行 Codex 和 Claude Code 的安装并合并展示。Claude Code 单独安装不代表已经配置无人值守运行。

配置方法及可复用的调度提示见 [安装与调度](references/install.md)。

## 数据和范围

- 从首次启用时刻开始统计。启用前的消息不补计次数，仅可作为已计数事件的原因上下文。
- 台账及原因证据快照存放在本机。宿主 AI 执行分析时会读取相关内容；宿主的数据处理方式由其配置决定。
- 默认在当前聊天展示总结。配置飞书等输出位置后，总结中的原话和证据会写入指定位置；通用 Skill 不包含任何个人文件夹地址。
- 当前只处理文字。带图消息保留附带文字，纯图片、图片中的内容不参与分析。
- 当前不支持 Claude 普通网页或桌面聊天。
- 原生日志格式可能随宿主更新而改变。读取缺失或不支持时显示覆盖告警，不能将读取失败算作零次。

## 开发与验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'
```

当前包含 100 项测试，覆盖原生消息读取、分类台账、上下文边界、证据校验、原因统计、安装升级及 CLI 流程。测试使用合成数据。

- [Skill 工作流与分类规则](SKILL.md)
- [原因分析规则与证据格式](references/reasons.md)
- [安装、升级、调度及卸载](references/install.md)
