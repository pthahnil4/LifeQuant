# 返回体结构与字段字典（response_fields）

## 统一外层信封

```json
{
  "code": 200,          // 返回码，200 成功；详见返回码表
  "msg": "成功",         // 返回码描述
  "taskNo": "6493...",  // 本次请求流水号
  "charge": true,       // 计费标志（⏳含义待确认：是否本次扣了一次调用量）
  "data": { "list": [...] }   // 或 "candle": [...]，随接口不同
}
```

## A股K线 `data.list[]` —— dict

| 字段 | 类型 | 说明 |
|------|------|------|
| day | String | 数据时间，如 `2024-07-31 13:30:00`（分钟/小时K带时分，日K仅日期）|
| open / high / low / close | String | 开/高/低/收（字符串，需转 float）|
| volume | String | 成交量（⏳单位待确认：股 / 手）|
| amount | String | 成交额（元）|
| ma_priceN | Number | N 条均价（传 `ma` 才有，如 `ma_price15`）|
| ma_volumeN | Number | N 条均量 |

## 沪深K线 `data.candle[]` —— 定长数组

顺序（实测推断）：

```
[ ts(秒), open, close, high, low, volume, amount, change, changeRate ]
```

- ts 为 Unix 秒；样例：`[1789628400, 11.64, 11.61, 11.65, 11.57, 173680, 201565013, -0.26, 0.69]`
- ⏳ 第 6 位 `volume` 与 A股K线的 `volume` 口径可能不同（一个像"手"一个像"股"），待文档确认。
- ⏳ 末两位是否 `涨跌额/涨跌幅%` 待确认。

## A股排行 `data.list[]` —— dict

| 字段 | 说明 |
|------|------|
| symbol | 代码（如 `sh601091`）|
| name | 名称 |
| price | 最新价 |
| change / changeRate | 涨跌额 / 涨跌幅% |
| open/high/low/preclose | 今开/最高/最低/昨收 |
| volume | 成交量 |
| value | 成交额 |
| amplitude | 振幅 |
| update_time | 更新时间（Unix 秒）|

## 返回码表

⏳ 待官方文档提供（目前仅确认成功码 `code:200`；HTTP 层 5xx=网关/上游超时，4xx=鉴权/参数/配额）。
