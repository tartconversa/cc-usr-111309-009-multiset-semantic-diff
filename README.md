# 跨格式表格的语义多重集对账

本项目保存虚构实验设施的仓湖源数据与Python读取/对账工具。原始工具只加载文件并显示条数；`src/semantic_reconcile.py` 在此之上实现按字段契约的语义多重集对账。全部设备、样品、记录和数值均为合成。

## 字段与单位

CSV按UTF-8且带表头，JSONL是另一侧同字段源。amount保留十进制字符串以免浮点丢失，空CSV字段的NULL协议由contract明确。

material-index.json记录全部输入内容哈希；fixtures保存原始资料与配置，计算输出可放.workspace-output，原始资料不应被覆盖。

## 语义多重集对账器

原始文件哈希不同、但行数与总金额相同，不代表两表一致（fixture 即 `[a,a,b]` 对 `[a,b,b]` 陷阱）。对账器按 `contract.json` 的 field_id 契约把每一行规范成有类型、长度前缀的规范字节，再比较规范字节的多重集：

- 金额 Decimal 按固定 scale 精确规范；超精度拒绝，绝不自动舍入；JSON number 以 `parse_float=Decimal` 接收，不经过二进制 float。
- 时间必须带时区，统一换算为 UTC 精确时刻（`2026-01-01T08:00+08:00` 与 `00:00Z` 规范相等）；字符串可选 NFC；空串、NULL、缺列是三种互不等价的状态。
- 行顺序无意义、合法重复保留重数：先按规范行 SHA256 分桶粗筛（桶计数与无序多重集摘要，`coarse_authoritative=false`），再对每桶做外部排序后的逐字节精确核对，哈希相同从不作为相等证明。
- 内存超 `buffer_max_bytes` 即溢写带 CRC32 与 sha256 的运行文件，重复行逐条落盘、不进入去重 set；journal 支持崩溃恢复（孤儿运行收养、损坏运行丢弃、登记运行校验失败即报错）。
- 每条差异输出完整规范行（hex + 可读预览）、左右重数与原始行血缘（文件、记录号、物理行号、字节区间），不只报主键。
- `--mode probabilistic` 仅做 64 位指纹多重集预检，结果显式标注 `exact_verified=false`；同路径输入也完整跑流程，不做内容一致短路。

运行（标准库，无第三方依赖）：

    python -m src.semantic_reconcile \
        --left fixtures/left.csv --right fixtures/right.jsonl \
        --contract fixtures/contract.json \
        --work-dir .workspace-output/reconcile --keep-work-dir
    # 退出码：0 精确相等；2 存在差异

## 运行读取器与测试

Python 3.11以上，标准库即可，不需安装第三方依赖。

    python -m src.reconcile_reader fixtures/left.csv
    python -m compileall -q src tests
    python -m unittest discover -s tests -v

## 固定容器

    docker build -t multiset-semantic-diff:local .
    docker run --rm multiset-semantic-diff:local

容器镜像以完整digest固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记。
