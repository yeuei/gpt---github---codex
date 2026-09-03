# coordination 目录说明

本目录只保存**开放 PR 的当前协作状态**。

标准结构：

```text
coordination/
├── README.md
├── coordination.yaml
├── TEMPLATE/
│   ├── 任务.md
│   ├── agent汇报.md
│   └── chatgpt解惑.md
└── PR-<N>/
    ├── 任务.md
    ├── agent汇报.md
    └── chatgpt解惑.md
```

## 新 PR

复制 `TEMPLATE/` 到 `PR-<N>/`。

## 开放 PR

只保留当前有效三文件，不创建版本号副本。

## PR 合并后

删除对应 `PR-<N>/` 目录。Git 历史已经保存全过程。

## 三文件语义

- `任务.md`：累积，只增不减；ChatGPT 新增任务，Agent 改状态。
- `agent汇报.md`：即时快照；Agent 每次重要更新完整覆盖。
- `chatgpt解惑.md`：即时决策；ChatGPT 每次新的有效指导完整覆盖。
