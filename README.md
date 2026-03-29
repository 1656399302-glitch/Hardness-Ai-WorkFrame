# Harness — 高标准工程型 AI 工作框架

[English](README_EN.md) | 中文

这是一个面向长时开发任务的、工件驱动的 AI 工作框架。设计目标包括：

- 先合同，后开发
- 评审独立，且必须真实操作
- 单项不过线即失败
- 长任务必须留下 handoff artifact
- 上下文策略是工程配置，不是写死常量

参考来源：
- Anthropic: [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## 框架概览

### 1. 工件体系

每个 workspace 都会生成结构化工件目录：

```text
<workspace>/
  spec.md
  contract.md
  feedback.md
  progress.md
  .ai-harness/
    product-spec/
      spec-v1.md
    sprint-contracts/
      sprint-01.md
      sprint-02.md
    qa-reports/
      sprint-01-qa.md
      sprint-02-qa.md
    handoffs/
      round-01.json
      latest.json
    decision-log/
      decisions.md
    runbooks/
      setup.md
      test.md
      release.md
    runtime/
      ...
```

根目录文件仍然保留，方便 agent 读写；结构化目录则负责审计、恢复和跨轮追踪。

### 2. 角色拆分

- Planner: 产出完整 Product Spec
- Contract Proposer / Reviewer: 先协商 Sprint Contract，再允许 Builder 开工
- Builder: 只负责实现，不拥有发布批准权
- Evaluator: 独立 QA，按合同逐条验收，必须给出运行/浏览器证据

### 3. 更严格的 QA 门禁

Evaluator 现在按六维评分：

1. Feature Completeness
2. Functional Correctness
3. Product Depth
4. UX / Visual Quality
5. Code Quality
6. Operability

默认硬门槛：

- 平均分 `< 9.0`：FAIL
- Functional Correctness `< 9.0`：FAIL
- Operability `< 9.0`：FAIL
- 其他核心维度 `< 8.5`：FAIL
- 任一验收项未测：FAIL
- 缺浏览器证据：FAIL
- placeholder / fake completion：FAIL

### 4. 结构化 handoff 与 runtime 状态

- Agent reset 不再只写散文总结，而是写结构化 handoff JSON
- runtime 会记录 phase、round、active agent、compaction 次数、reset 次数
- 这些状态同时服务于 CLI 和 dashboard

### 5. 本地 HTML Dashboard

支持：

- 编辑 `.env`
- 启动 / 停止 harness
- 查看当前 phase / round / agent / PID
- 查看 compaction / reset 计数
- 实时 tail 日志

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium

cp .env.template .env
# 填写 API 参数
```

直接跑 CLI：

```bash
python harness.py "Build a release-ready browser app with real QA evidence."
```

继续已有 workspace：

```bash
python harness.py \
  --resume-dir workspace/20260328-114410_ai-ai-ui-ai \
  --skip-planner \
  "Remediation only. Close every blocker in feedback.md and progress.md."
```

启动 dashboard：

```bash
./start-dashboard.sh
```

也可以直接用 Python：

```bash
python harness.py dashboard
```

默认地址：

```text
http://127.0.0.1:8765
```

## 关键文件

- [harness.py](./harness.py): orchestrator、门禁、CLI 入口
- [prompts.py](./prompts.py): Planner / Builder / Evaluator / Contract prompts
- [artifacts.py](./artifacts.py): 工件目录、同步、decision log、handoff
- [context.py](./context.py): compaction / reset / structured checkpoint
- [runtime_state.py](./runtime_state.py): 运行时状态与日志切片
- [dashboard_server.py](./dashboard_server.py): dashboard API
- [dashboard.html](./dashboard.html): 本地控制台页面
- [config.py](./config.py): `.env` schema + runtime config

## 使用原则

1. 没有 Sprint Contract，不允许开发。
2. Builder 的自检不是验收。
3. Evaluator 必须像真实用户一样操作系统。
4. Handoff artifact 是一级交付物，不是可选总结。
5. 如果文档声称“已完成”，但浏览器路径没过，该轮就是 FAIL。

## 验证

当前仓库至少应通过：

```bash
python3 -m py_compile harness.py config.py prompts.py agents.py context.py logger.py tools.py artifacts.py runtime_state.py dashboard_server.py
python3 -m unittest tests.test_harness_guards -v
```
