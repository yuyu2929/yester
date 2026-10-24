# Yester 开发日志

> 项目：Yester v0.1.0 · 本地优先 Git 文件版本化工具
> 记录区间：2026-10-21（单日冲刺，UTC+8）
> 约定：只记录真实发生的事——决策、踩坑、修复、数据。不美化。

---

## Day 1 · 2026-10-21

### 阶段 0：立项与命名（17:00–17:10）

- 确定产品内核：把"用 git 给普通文件夹做版本化 + 加密同步 + 发布"的社区创意，做成**独立可发布的开源产品**，要求个人愿意传播、企业愿意付费。
- 命名调研（GitHub 实测）：
  - `ChronoVault` —— 撞名，已有时间胶囊 Web 应用、Web3 钱包、C++ 加密工具等多个项目。放弃。
  - `Kairos` —— 撞名，已有 CNCF 沙箱 immutable OS、P2P 文件共享、Claude 会话日历。放弃。
  - **`Yester`** 采用。含义"昨天"——你的文件随时回得去；短、可念、中英文都顺；CLI 命令 `yester`。类别内无同名工具。

### 阶段 1：架构与模块划分（17:10–17:30）

决定单仓库模型（而非社区原案的"子文件夹各一仓库"），理由：
1. 搜索/审计/UI 全部大幅简化；
2. 现代托管对单仓库友好；
3. 用户心智负担低（目录约定代替仓库拓扑）。

模块清单（11 个 Python 模块 + Flask UI + 7 个测试文件）：

```
cli / config / gitops / watcher / crypto / publisher /
search / archive / audit / license / timemachine
```

### 阶段 2：核心实现（17:30–18:40）

按依赖序实现：`config → gitops → watcher → crypto → publisher → search → archive → audit → license → timemachine → cli`。

关键设计落点：
- 提交消息约定 `yester(<kind>): <subject>`，审计模块直接从历史解析，零额外状态。
- 分支模型：设备在 `devices/<设备>` 工作，提交后同步进 `main`。
- 加密：AES-256-GCM 单文件封装 + scrypt 包缠口令 + 恢复码双路解包。
- 大文件：内容寻址 blob 存储 + `.yblob` 指针 + `.git/info/exclude`。

### 阶段 3：测试驱动的第一轮修复（18:40–19:00）

首轮全量测试：**18 通过 / 11 失败**。逐个定位：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 中文搜索 snippet 抛 `TypeError` | `_snippet` 里 `best` 先被赋成字符串，下一次循环与 int 比较 | 改 `None` 哨兵 |
| 2 | 审计/log 只看到 init 一条提交 | 提交都在设备分支，而审计读 `main` | 新增 `_sync_main()`：每次提交后把设备分支快进/合并进 `main`（多设备同步的真实语义） |
| 3 | `promote` 测试断言错误 | promote 后切回设备分支是设计行为 | 修正测试为验证 main 与设备分支同源 + 备份分支存在 |
| 4 | Markdown 渲染断言失败 | `toc` 扩展给 `<h1>` 加了 id 属性 | 放宽断言 |
| 5 | CLI 全流程测试断言失败 | `capsys.readouterr()` 在一条 assert 里被消费两次 | 先取到变量再断言 |

### 阶段 4：一个隐藏很深的 bug（19:00–19:20）

修复后剩 4 个失败，其中 3 个 crypto 测试呈现"单独跑能过、整文件跑必挂"的幽灵现象。

**根因**：`config._deep_merge` 用了浅拷贝（`out = dict(base)`），导致 `VaultConfig` 实例与模块级 `DEFAULT_CONFIG` **共享嵌套字典**。某测试调用 `init_encryption` → `config.set("crypto.enabled", True)` → 写进了共享的默认配置 → 同进程内后续创建的所有保险库都"加密已启用"。

**修复**：`_deep_merge` 改为 `copy.deepcopy`。这个 bug 单测永远抓不到，只有跨测试的状态污染能暴露它——记下来提醒自己：**模块级可变默认值 + 浅拷贝 = 定时炸弹**。

### 阶段 5：冒烟测试逼出两个真 bug（19:20–19:50）

用真实感演示库（酸面包日记 / 云南旅行计划 / 设计评审笔记 + 120KB 样本文件）跑 CLI 全流程，暴露：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 6 | `large_file_mb: 0.05` 导致**所有文件**进 blob 存储 | `int(0.05) == 0`，阈值变 0 字节 | `large_file_bytes` 改 `float()` |
| 7 | **严重**：文件被 blob 化管理后，用户再编辑它，`yester sync` 永远"nothing to commit" | blob 化会把原文件写入 `.git/info/exclude`，此后 `git status` 不再报告它的改动——用户的编辑静默丢失版本 | `stage()` 增加 `_refresh_blobs()`：显式扫描所有 `.yblob` 指针，比对 sha256，变化则重新入 blob 并更新指针；`auto_commit` 改为以 `git diff --cached` 为准而非 status |
| 8 | `.yester/config.yml` 自己被 blob 化了 | 阈值调小后状态目录文件也超阈值 | `stage()` 跳过 `.yester/` 前缀 |

为 #7 补了回归测试 `test_blob_managed_file_edits_are_still_committed`（含"未改动的 blob 文件不产生空提交"的反向用例）。

### 阶段 6：测试收敛（19:50–20:00）

**31/31 全部通过。** 期间还为 CLI 补了 `config` 子命令（get/set/list）和 `init --device`——冒烟时发现没有它们，演示很不顺。

### 阶段 7：端到端冒烟 + 截图（20:00–20:35）

演示库（`life-vault`，设备名 `annas-macbook`）全流程实录：

```
init → sync（7 文件） → snapshot "vault kickoff"
     → 编辑 → sync → 编辑 → sync → snapshot "best loaf yet"
     → encrypt-init（恢复码 6EF6-D0E0-…） → mirror（8 文件加密）
     → publish --no-push（站点生成 index/about）
     → search --reindex（59 文档）→ "防晒霜" ✓ → "under-proofed" ✓
     → activate team key → audit csv 导出 ✓
     → scan：植入 AKIAIOSFODNN7EXAMPLE + ghp_ token → 双双命中、脱敏、exit 1 ✓
     → lock（留存模式）✓
```

> 插曲：secret scan 一开始显示"clean"，排查发现是 `cp` 的相对路径落到了当前 shell 目录而非保险库——工具没错，是用法错了。用 `YESTER_VAULT` 环境变量定位保险库后一切正常。（该环境变量能力本就是为脚本化设计的。）

时光机 UI 实测：Flask 服务于 127.0.0.1:8787，无头 Chromium 截图成功（`docs/screenshots/timemachine.png`）——时间线按提交类型着色、状态 chips（branch/head/snapshots/blobs/encryption/retention）、文件树均正常。

### 阶段 8：文档与打包（20:35–）

- `docs/architecture.svg`：手写数据流架构图（与 UI 同款深色主题）
- `README.md`：英文为主（GitHub 全球传播）+ 中文快速开始
- `docs/design.md`：六大方向论证、对"Git 不是备份工具"批评的回应、open-core 模型
- 本日志

---

## 附：本次修复的问题总账

| # | 严重度 | 问题 | 来源 |
|---|---|---|---|
| 1 | 中 | `_snippet` 类型错误 | 测试 |
| 2 | 高 | 审计读不到设备分支提交 | 测试 |
| 3–5 | 低 | 三个测试自身断言/用法问题 | 测试 |
| 6 | 高 | `_deep_merge` 浅拷贝污染全局默认配置 | 测试（幽灵失败） |
| 7 | 中 | `int()` 截断小数配置 | 冒烟 |
| 8 | **致命** | blob 化文件后续编辑不同步（版本静默丢失） | 冒烟 |
| 9 | 中 | `.yester/` 被 blob 化 | 冒烟调试 |

## 附：待办（v0.2）

- [ ] systemd / launchd / Windows Service 常驻封装
- [ ] 多设备同步的端到端测试（双 vault 互推）
- [ ] 可选 embedding 语义检索后端
- [ ] 移动端只读伴侣（响应式优先）
- [ ] 企业私有化一键部署脚本
