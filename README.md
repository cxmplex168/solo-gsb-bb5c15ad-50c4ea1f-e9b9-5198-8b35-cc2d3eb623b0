# 抄本核对 API

供档案员核对三份独立抄录的 Web API:对三段文本做 NFC 规范化后,用三维全局动态规划对齐,生成共识文本,并报告对齐列与总成本。

## 启动

只需一条命令(依赖随镜像构建自动安装):

```bash
docker compose up --build
```

- 服务地址: <http://localhost:8000>
- 交互式文档(Swagger UI): <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>

## 接口

### `POST /reconcile/adjudicate`

人工裁决接口。请求在三段文本之外可携带 `anchors`(同 `/reconcile/anchored`)与 `decisions`(裁决列表,可省略)。服务端先按本次请求**重算对齐**,再一次性校验全部裁决,全部有效才生效:

- **坐标签名定位**:每项裁决以 `signature` 给出该列在三份文本中的**原始字素簇位置**(0 起始,与 `/reconcile` 响应中 `uncertain[].sources[].position` 一致;该列未消费的来源记 `null`)。按原始坐标而非列序号定位,锚点导致列序/列数变化时裁决不会错落到其他位置。
- **二选一给出采用值**:`source` 为 `0/1/2` 时采用该列该来源的**非空字素**;`replacement` 为替换字符串时,服务端做 NFC 规范化,要求规范化后**恰好为一个字素簇**(如 `👨‍👩‍👧‍👦` 合法,`甲乙` 非法)。
- **一次性原子校验**:目标列必须存在、目标列仍为 `uncertain`、同一签名不得重复裁决、`source` 不得指向空位;任一项失效返回 **409**(响应 `detail` 列出每项失效裁决的全部原因),**不产出部分结果**。请求结构问题(签名长度/负值、`source` 越界、同时/均未给 `source` 与 `replacement`、替换值非单字素簇、锚点非法、超出 120 簇等)返回 **422**。
- 成功响应在 `/reconcile/anchored` 结构上增加 `audits`;`consensus` 为裁决后的完整文本,`uncertain` 仅保留**尚未裁决**的列,列内 `consensus` 同步更新;`audits` 按请求顺序记录每项裁决的 `signature`(坐标签名)、`original`(裁决前三来源原字素,空位 `null`)、`adopted`(采用值)与 `method`(`source:0/1/2` 或 `replacement`)。

```json
{
  "texts": ["甲丙", "甲丁", "甲乙"],
  "decisions": [
    {"signature": [1, 1, 1], "source": 0}
  ]
}
```

```bash
curl -X POST http://localhost:8000/reconcile/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲丙", "甲丁", "甲乙"], "decisions": [{"signature": [1, 1, 1], "source": 0}]}'
```

响应示例:

```json
{
  "consensus": "甲丙",
  "total_cost": 3,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲",
      "anchored": false
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 3,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "丙",
      "anchored": false
    }
  ],
  "uncertain": [],
  "audits": [
    {
      "signature": [1, 1, 1],
      "original": ["丙", "丁", "乙"],
      "adopted": "丙",
      "method": "source:0"
    }
  ]
}
```

使用人工替换字素、并对只消费单个来源的列(签名含 `null`)裁决的示例:

```bash
curl -X POST http://localhost:8000/reconcile/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲乙", "", ""], "decisions": [{"signature": [1, null, null], "replacement": "丁"}]}'
```

裁决失效时(下例中目标列不存在、裁决已确定列、重复裁决同时发生)返回 409 且无任何部分结果:

```json
{
  "detail": [
    {"decision": 0, "signature": [9, 9, 9], "reasons": ["坐标签名在重算后的对齐中不存在"]},
    {"decision": 1, "signature": [0, 0, 0], "reasons": ["目标列已达成共识,并非 uncertain 列"]},
    {"decision": 3, "signature": [1, 1, 1], "reasons": ["裁决重复:同一坐标签名被多次裁决"]}
  ]
}
```

### `POST /reconcile`

请求体为恰好三段文本,每段不超过 **120 个 Unicode 字素簇**(按 UAX #29 切分,如 `👨‍👩‍👧‍👦` 计 1 个;先 NFC 规范化再对齐):

```json
{
  "texts": ["甲丙", "甲丁", "甲乙"]
}
```

```bash
curl -X POST http://localhost:8000/reconcile \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲丙", "甲丁", "甲乙"]}'
```

响应示例:

```json
{
  "consensus": "甲�",
  "total_cost": 3,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲"
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 3,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "�"
    }
  ],
  "uncertain": [
    {
      "column": 1,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ]
    }
  ]
}
```

### `POST /reconcile/anchored`

与 `/reconcile` 相同,但可额外提供**锚点**:每个锚点给出三份文本各自的字素簇位置(0 起始),强制这三个字素出现在同一对齐列。对齐时以锚点切分前后区间,各区间分别求全局最小成本后拼接;锚点列照常参与成本与共识计算。响应每列增加 `anchored` 标记,所有 `position` 仍为原文绝对位置。

锚点需满足:各来源位置**严格递增**、**不越界**、**不重复**,否则返回 422;`anchors` 可省略或为空,此时等价于 `/reconcile`(仅多出 `anchored` 标记)。

```json
{
  "texts": ["甲乙", "甲丙", "甲乙"],
  "anchors": [{"positions": [1, 1, 1]}]
}
```

```bash
curl -X POST http://localhost:8000/reconcile/anchored \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲乙", "甲丙", "甲乙"], "anchors": [{"positions": [1, 1, 1]}]}'
```

响应示例(第二列为锚点强制对齐列):

```json
{
  "consensus": "甲乙",
  "total_cost": 2,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲",
      "anchored": false
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 2,
      "sources": [
        {"grapheme": "乙", "position": 1},
        {"grapheme": "丙", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "乙",
      "anchored": true
    }
  ],
  "uncertain": []
}
```

## 对齐规则

- 每列可消费任意非空来源组合,以位掩码表示:bit0/1/2 分别对应 `texts[0]/texts[1]/texts[2]`(如 `mask=5` 表示该列消费来源 0 与 2)。
- 单列成本:两两配对中,字符不等或字符与空位配对各计 1,空位与空位计 0;总成本为各列之和,取全局最小。
- 平局时依次偏好:消费来源数更多的掩码、位掩码更小的掩码(优先级 `7 > 3 > 5 > 6 > 1 > 2 > 4`)。
- 共识:某列至少两份相同非空字素时写入该字素,否则写入 `�`,并在 `uncertain` 中列出该列各来源的字素与原位置(0 起始,空位为 `null`)。
- 锚点(仅 `/reconcile/anchored` 与 `/reconcile/adjudicate`):每个锚点强制三份文本指定位置的字素同列(掩码 7);锚点把序列切分为若干区间,各区间独立求全局最小成本后拼接,锚点列照常计入成本与共识。
- 人工裁决(仅 `/reconcile/adjudicate`):以三来源原始位置签名定位 `uncertain` 列,采用某非空来源字素或单个 NFC 字素簇替换值;服务端重算对齐后一次性原子校验全部裁决,任一失效返回 409 且无部分结果,成功则返回裁决后文本、未裁决列与审计项。

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://localhost:8000
python -m pytest tests/              # 运行测试(含与纯 Python 参考实现的随机对拍)
```
