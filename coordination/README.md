# coordination 目录说明

本目录只保存**真实开放 PR 的当前协作状态**与通用模板。

```text
coordination/
├── README.md
├── coordination.yaml
├── TEMPLATE/
└── PR-<真实开放 PR 号>/
```

- 不预建虚构 PR 目录。
- `任务.md` 是累积合同；`agent汇报.md` 与 `chatgpt解惑.md` 是当前快照。
- PR 合并后从 HEAD 删除对应 `PR-N/`；历史通过 Git commit 继续可读。
- Dashboard 的历史节点可直接从 Git 对象读取已删除目录在过去 commit 中的 `任务.md`，因此无需为了 UI 保留已合并 PR 目录。
