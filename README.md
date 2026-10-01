# 本地数据导出风险审查 API

用 Python / FastAPI 实现的本地 HTTP 服务。输入一张**可乱序、至多 60 个节点**的数据加工图和一个
导出节点，服务沿依赖关系逐列追踪**敏感来源**，对导出节点的每一列给出审查结论：

* **内容影响（content）**：敏感源列的值流入了该列；
* **行影响（row）**：敏感源列影响了结果里“出现哪些行”，该影响会落到结果的**每一列、每一行**。

两类原因只要存在其一，该导出列即判 `blocked: true`，并分别列出源头节点 ID 与列名。

服务还提供 `POST /api/revise`：在审查结论基础上把图修订成一张**可再次提交、复审判安全**的
导出图（只允许在导出末尾追加一个投影节点），详见下文“安全修订”。

## 风险传播规则

| 节点 | 行为 | 内容影响 | 行影响 |
|---|---|---|---|
| `source` | 声明互异列名与 `sensitive` 标记 | 敏感列自身的来源 `(节点, 列)` | 初始为空 |
| `projection` | 只保留明确列出的输出列；每列 `copy` 上游列或以 `constant` 常量替换 | `copy` 继承上游列；`constant` **清除**该列内容来源 | 原样保留（投影/替换不能消除行影响） |
| `filter` | 声明条件 `references` 引用的列，保留全部输入列 | 输入列原样保留 | 并入所有被引用列的内容来源；并继承上游行影响 |
| `join` | 等值连接：左右输入、`left_key`/`right_key`、输出列与互异别名 | 每个输出列继承对应侧列的内容来源 | 合并两侧已有行影响，**并并入左右连接键的敏感内容来源** |

要点：筛选引用敏感列、或连接键带敏感来源时，敏感来源影响结果的**每一行**；之后的投影、
常量替换都无法消除；连接时两侧行影响取并集。

### 整次请求拒绝（HTTP 400）

出现以下情况拒绝整次请求，不给出部分审查结果：

* 节点 ID 重复（`DUPLICATE_NODE`）；
* 列名冲突：source 列名、projection 输出列名、filter 引用名、join 输出别名在各自节点内重复（`COLUMN_CONFLICT`）；
* 无效引用：输入节点/被复制列/筛选列/连接键/连接输出列/导出节点不存在（`INVALID_REFERENCE`）；
* 依赖图含环，包括自环（`CYCLE`）。

请求体不符合 JSON Schema（未知字段、同时写 `copy` 与 `constant`、节点数超过 60 等）返回 HTTP 422。

`POST /api/revise` 复用上述全部校验（重复 ID、列名冲突、无效引用、环、超过 60 节点等同样拒绝），
额外把 `allowed_replacements` 的列名重复判为 422，把允许列不存在及各类不可修订情形判为 400。

## 单条命令启动

> 端口：**8080**

```bash
docker compose up --build
```

后台运行：

```bash
docker compose up --build -d
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## HTTP 调用示例

### 请求

```
POST /api/review
Content-Type: application/json
```

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {
      "id": "users",
      "type": "source",
      "columns": [
        {"name": "uid", "sensitive": false},
        {"name": "email", "sensitive": true},
        {"name": "country", "sensitive": false}
      ]
    },
    {
      "id": "only_cn",
      "type": "filter",
      "input": "users",
      "references": ["country"]
    },
    {
      "id": "export_users",
      "type": "projection",
      "input": "only_cn",
      "columns": [
        {"name": "user_id", "copy": "uid"},
        {"name": "mail", "copy": "email"},
        {"name": "bucket", "constant": "ALL"}
      ]
    }
  ],
  "export": "export_users"
}'
```

也可以直接用仓库里的示例：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

### 响应

```json
{
  "export": "export_users",
  "blocked": true,
  "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {
      "name": "mail",
      "blocked": true,
      "content_reasons": [{"node_id": "users", "column": "email"}],
      "row_reasons": []
    },
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

### 行影响示例：筛选敏感列后，连常量列也被阻断

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e"
}'
```

`score`（非敏感复制列）与 `lit`（常量列）的 `row_reasons` 都包含
`{"node_id":"s","column":"id_card"}`，均被阻断。

### 连接示例：敏感连接键影响连接结果每一行

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "orders", "type": "source",
     "columns": [{"name": "uid_enc", "sensitive": true}, {"name": "amount"}]},
    {"id": "users", "type": "source",
     "columns": [{"name": "uid"}, {"name": "name"}]},
    {"id": "j", "type": "join",
     "left_input": "orders", "right_input": "users",
     "left_key": "uid_enc", "right_key": "uid",
     "outputs": [
       {"alias": "amt", "side": "left", "column": "amount"},
       {"alias": "uname", "side": "right", "column": "name"}
     ]}
  ],
  "export": "j"
}'
```

两个输出列本身均不含敏感内容，但都因敏感连接键 `orders.uid_enc` 的行影响而阻断。

## 安全修订：POST /api/revise

把审查结论转成一张**可再次提交的安全导出图**。请求体在审查请求基础上增加
`allowed_replacements`：允许被替换为常量 `null` 的导出列名列表（**必须互异**，可传空数组；
列名非空）。整张图仍按 `/api/review` 的原规则校验，原有错误行为不变。

修订规则：

1. **原图已安全**：原样返回 `nodes`、原 `export`，**不新增节点**，`replaced_columns` 为空。
2. **原图被阻断且可修订**：只允许在导出节点**末尾追加恰好一个 `projection` 节点**，
   * 列名、列顺序、行集合完全保持（输入即原导出节点，不做筛选/连接）；
   * 仅把**带有敏感内容来源且在允许列表内**的列置为常量 `null`；
   * 其余列（含获准但本身干净的列、原常量列）一律按同名 `copy` 复制；
   * **不修改任何原节点**，追加的投影 ID 不会与原节点冲突（冲突时自动换名）。
3. **不可修订**（返回 HTTP 400，**不返回部分改好的图**）：
   * 导出存在**行影响**（投影无法消除行影响）：`UNREVISABLE_ROW_INFLUENCE`；
   * 任一阻断列（带敏感内容来源）**不在允许列表**：`UNREVISABLE_COLUMN_NOT_ALLOWED`；
   * 追加投影后节点数**超过 60**：`UNREVISABLE_NODE_LIMIT`；
   * 允许列表中的列名在导出节点上**不存在**（原图安全时同样检查）：
     `REPLACEMENT_COLUMN_NOT_FOUND`。

允许列表列名重复、为空字符串、字段类型不符等 schema 问题返回 HTTP 422。

成功响应包含完整 `nodes`、更新后的 `export`、实际替换列 `replaced_columns`
（只含真正被置空的列，按列顺序）、以及用原规则对新导出节点**重审**的结果 `review`；
该图可直接再 POST 到 `/api/review` 并判安全。

### 修订并复审（可直接执行）

```bash
# 1) 发起修订（示例文件：敏感列 mail 获准替换；filter 只引用非敏感列 country，无行影响）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json
```

返回（节选）：末尾追加了 `export_users__safe_export`，`mail` 被置为 `null`，其余列同名复制：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "mail", "copy": "email"},
      {"name": "bucket", "constant": "ALL"}]},
    {"id": "export_users__safe_export", "type": "projection",
     "input": "export_users", "columns": [
       {"name": "user_id", "copy": "user_id"},
       {"name": "mail", "constant": null},
       {"name": "bucket", "copy": "bucket"}]}
  ],
  "export": "export_users__safe_export",
  "revised": true,
  "replaced_columns": ["mail"],
  "review": {"export": "export_users__safe_export", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用原审查接口复审返回的图（需要 jq；应得 "blocked": false）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

无 `jq` 时可分两步保存中间结果：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' --data @examples/revise_request.json > /tmp/revised.json
python3 -c "import json; d=json.load(open('/tmp/revised.json')); \
  json.dump({'nodes': d['nodes'], 'export': d['export']}, open('/tmp/safe.json','w'))"
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @/tmp/safe.json
```

行影响不可修订的例子（对敏感列筛选后，允许替换全部列也无法消除行影响）：

```bash
curl -s -i -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e",
  "allowed_replacements": ["score", "lit"]
}'
# HTTP/1.1 400 ...  {"error":{"code":"UNREVISABLE_ROW_INFLUENCE", ...
#   "row_reasons":[{"node_id":"s","column":"id_card"}]}}
```

## 节点 JSON 结构

```jsonc
// source
{"id": "s", "type": "source",
 "columns": [{"name": "col", "sensitive": true}]}

// projection：copy 与 constant 二选一（constant 允许为 null）
{"id": "p", "type": "projection", "input": "s",
 "columns": [{"name": "out", "copy": "col"}]}

// filter：保留全部输入列，references 为条件引用到的列
{"id": "f", "type": "filter", "input": "s", "references": ["col"]}

// join：等值连接；outputs 别名必须互异，side 为 "left" 或 "right"
{"id": "j", "type": "join",
 "left_input": "a", "right_input": "b",
 "left_key": "ka", "right_key": "kb",
 "outputs": [{"alias": "x", "side": "left", "column": "v"}]}
```

* 节点可以乱序给出，服务按依赖做拓扑排序；只审查导出节点可达的祖先链。
* 一个关系连到自身两侧的等值连接（左右输入相同）是合法的；自环依赖（节点输入是自己）按环拒绝。
* `content_reasons` / `row_reasons` 中的来源按 `(node_id, column)` 排序，保证输出确定。

## 开发与测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 文件说明

```
app/models.py     请求模型与结构约束（Pydantic；含修订请求 ReviseRequest）
app/engine.py     图校验、拓扑排序、来源传播、逐列审查与安全修订
app/main.py       FastAPI 入口（POST /api/review、POST /api/revise、GET /health）
examples/         请求示例（request.json、revise_request.json）
tests/            传播规则、拒绝场景与修订/复审测试
Dockerfile        容器镜像（python:3.11-slim，暴露 8080）
docker-compose.yml 单命令编排
```
