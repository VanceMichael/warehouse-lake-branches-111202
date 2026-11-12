# lake-branches

按日维护影像分类索引，记录分类版本并统计标签分布。

## 当前数据组织

- `classifications`：`scene TEXT PRIMARY KEY, day TEXT NOT NULL, label TEXT NOT NULL, model_version TEXT NOT NULL`

`examples/source.json` 为本地合成输入，`examples/expected.json` 是当前查询的公开样例结果。`schema.json` 描述结构，运行时定义在 `warehouse/definition.py`。业务计算位于 `warehouse/domain.py`，事务装载位于 `warehouse/catalog.py`，内容寻址 JSONL 对象位于 `warehouse/store.py`。

输入批次以内容 SHA-256 固定；相同批次重投不重复写入，相同批次内容不同则报错。装载以完整输入包为事务，主键冲突不留下半批记录。关系数据保存在 SQLite，精确数量用十进制文本；导出对象按内容哈希命名，读取会重新校验，来源不在运行时修改。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。时区相关开发使用系统 IANA 时区库。数据库和对象目录均由入口参数指定。源码和合成数据可以直接复制到 Linux 环境运行。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

## 运行

每次使用全新临时目录执行真实导入、查询、导出和内容校验：

```bash
python3 scripts/demo.py
```

分步调用可用：

```bash
python3 -m warehouse import --db /tmp/lake-branches.sqlite3 --input examples/source.json --batch first
python3 -m warehouse inspect --db /tmp/lake-branches.sqlite3
python3 -m warehouse export --db /tmp/lake-branches.sqlite3 --objects /tmp/lake-branches-objects
```

`scripts/demo.py` 打印实际命令和完整输出，验证首次写入、重复导入、查询和导出往返。小规模样例在数秒内结束，失败返回非零退出码，不回放预制日志。手工重复使用数据库路径时应留意已存在的业务主键。

## 轻量数据分支

在内容寻址对象存储之上为文件索引提供分区级数据分支，全部状态保存在本地文件，不依赖远端 Git 保存数据状态。实现位于 `warehouse/branching.py`，命令入口为 `python3 -m warehouse <子命令>`（原 `import/inspect/export` 保持可用）。

### 数据模型

- 提交是不可变内容对象：只保存相对父提交的**变更分区**（`{分区: {object, rows}}`，删除用 `null` 墓碑）与结构化 Schema 对象；未变更分区沿第一父链继承。
- 分支引用 `refs/heads/<name>` 指向提交；创建分支时在 `refs/bases/<name>` 固定共同祖先（公共基线），之后不随生产推进改变。
- 生产 `main` 只能通过合入改变，不能直接写入或删除分区。
- 所有写操作经 `locks/repo.lock` 文件锁串行化，引用通过临时文件原子替换切换。

### 合入与冲突

- `preview` 基于共同祖先做三方比较（祖先 / main / 分支），输出每分区状态与结构化冲突；预览本身也是内容对象，ID 即其正文哈希。
- 不同分区自动合入；同分区两侧不同修改报 `partition-divergent`，删除对修改（双向）报 `partition-delete-modify`，不允许最后写入者胜。
- Schema 与分区数据联合判断：生产已退役字段被分支带回时报 `schema-resurrection`，且只能 `keep-main`；同字段两侧不同修改报 `schema-divergent`；分支数据不符合合并后 Schema 报 `data-schema-mismatch`。
- `merge` 校验预览基于的 main 版本与分支 tip：生产或分支已推进则拒绝并要求重新预览（`StalePreviewError`），不会覆盖另一成功合入。
- 冲突必须用 `resolutions` 显式选择 `keep-branch` 或 `keep-main`（即显式放弃某侧变更），每次裁决写入 `resolution-record` 对象并进入 `audit.log`，可追溯。

### 删除、GC、读者与恢复

- `rm-branch` 是软删除：移除 head 但写入 `refs/tombstones/<name>`，墓碑仍是 GC 存活根，可 `restore` 恢复。
- `purge-branch` 才清除墓碑与基线；之后 `gc` 回收不可达对象。存活根为：生产、所有分支 head、墓碑、有效读者租约、预览及其解决记录。
- 读者通过 `reader open --ref <引用>` 获得租约并固定快照提交，`read` 校验租约有效后读取分区对象。
- `verify` 沿所有存活根遍历对象图，发现缺失对象或悬空引用即失败。
- 还提供 `branch`（列表/创建）、`show`、`log`、`diff`、`deleted`、`restore`、`revert`（生产引用回退到历史提交，不销毁对象）等查询与恢复入口。

### 分支演示

`scripts/demo_branches.py` 用全新临时目录端到端演示：独立分区、同区冲突与显式放弃、Schema 退役/复活、删除对修改、两个审核者并发合入（陈旧预览拒绝后重新预览再合）、删分支后 GC 与生产读者共享对象读取、悬空引用校验、分支恢复与硬清除回收。

```bash
python3 scripts/demo_branches.py
python3 -m unittest tests.test_branching -v
```

合成夹具位于 `examples/branching/`，不含真实遥感产品。

