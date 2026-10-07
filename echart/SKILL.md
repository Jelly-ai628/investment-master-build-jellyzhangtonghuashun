---
name: echart
description: 生成数据可视化图表，支持词云、折线图、柱状图、饼图、面积图及混合图表。当用户要求根据数据生成图表，或回复中的数据适合用图表呈现时，使用此skill。
---

# ECharts 图表生成

## 基本原则

1. **用户需求优先**：用户明确要求的样式/配置优先于本 skill 的默认规范
2. **严格遵循约束**：无特殊要求时，必须遵循本 skill 的配置规范（grid、legend、颜色等）
3. **输出合法 JSON**：禁止函数、注释、尾逗号等非法语法

## 输出方式

直接在回复中输出 ECharts option JSON，使用 `echarts` 代码块：

~~~
```echarts
{
  "xAxis": { "type": "category", "data": ["A", "B", "C"] },
  "yAxis": { "type": "value" },
  "series": [{ "type": "bar", "data": [...] }]
}
```
~~~

- 输出纯 JSON，不要有注释或 JavaScript 代码
- 一个代码块 = 一个图表
- 用户要求多个图表时，输出多个代码块
- 直接输出代码块，不要在前面加"XX配置"等标题
- **禁止**将 echarts json 作为单独文件输出

如需在 md 文件中嵌入图表：直接嵌入 echarts 代码块即可

如需在 html 中嵌入图表：使用 JavaScript 方式调用 `echarts.init()` + `setOption()`

## 图表类型

| 场景 | 推荐 |
|------|------|
| 时间序列趋势 | 折线图 |
| 分类比较 | 柱状图 |
| 占比≤5类 | 饼图 |
| 占比>5类 | 排序柱状图 |
| 文本频次 | 词云 |

**多指标对比**：

| 情况 | 选择 |
|------|------|
| 同单位、量级相近（如各部门销售额） | 多线图 |
| 量 + 率（如销量 + 增长率） | 柱状 + 折线 |
| 量级差异大（如收入万元 + 占比%） | 双Y轴（谨慎）或拆成两图 |

- 禁止自动生成雷达图、甘特图、热力图（除非用户明确要求）

## 样式配置

### 标题

应传达洞察而非仅描述数据：
- 避免："月度销售数据"
- 推荐："Q3销售额增长40%"

### 颜色

**在 series 层级设置颜色**，legend 自动继承：

```json
{
  "legend": { "data": ["2024年"] },
  "series": [{
    "name": "2024年",
    "type": "bar",
    "itemStyle": { "color": "#ee6666" },
    "data": [120, 230, 180]
  }]
}
```

**例外**：饼图每个扇区需不同颜色，在 data 项中指定（不要用函数）：
```json
"data": [
  { "value": 100, "name": "A", "itemStyle": { "color": "#d97757" } },
  { "value": 80, "name": "B", "itemStyle": { "color": "#6a9bcc" } }
]
```
词云在 data 项中用 `textStyle` 指定颜色。

**按数据项区分颜色**（正负值、突出某项等）：在 data 项中指定，不要用函数
```json
"data": [
  { "value": -12, "itemStyle": { "color": "#ee6666" } },
  { "value": 5, "itemStyle": { "color": "#91cc75" } }
]
```

**默认色板**（温暖简约）：`["#d97757", "#6a9bcc", "#788c5d", "#c4a35a", "#8b7cb6", "#c97b84", "#5d8c8c", "#a67c52", "#b0aea5"]`

**商务风格（备用）**：`["#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de", "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc"]`

**聚焦用色**：需突出关键数据时，其他数据用灰色 `#b0aea5`，关键数据用亮色

### 布局

**笛卡尔坐标系图表**（柱状图、折线图等）推荐设置 grid，防止标签被裁切：

```json
{ "grid": { "left": "6%", "right": "6%", "bottom": "3%", "containLabel": true } }
```

**图例位置**：默认放顶部 `"legend": { "top": "top" }`，避免与 X 轴标签重叠

**坐标轴颜色**：设置为深灰 `#666`，需在 xAxis/yAxis 中单独配置：
```json
{
  "xAxis": { "axisLabel": { "color": "#666" }, "axisLine": { "lineStyle": { "color": "#666" } } },
  "yAxis": { "axisLabel": { "color": "#666" }, "axisLine": { "lineStyle": { "color": "#666" } } }
}
```

**数值显示**：
- 数据点 ≤6：可用 `"label": { "show": true }` 直接显示
- 数据点较多：用 `"tooltip": { "trigger": "axis" }` 悬浮显示（饼图用 `"trigger": "item"`）

### 格式化

- 数值最多2位小数，百分比加 `%`
- 模板：`{a}` 系列名、`{b}` 类别、`{c}` 值、`{d}` 百分比（仅饼图）
- 使用 `{b}` 时 data 每项必须有 `name` 字段
- 模板占位符用 `: ` 分隔，禁止 `\n`（如 `"{b}: {d}%"`）

**数据预处理**（标签/数值过长会导致显示比例失调或遮挡，输出前应先简化）：
- 长标签缩写："2024财年xx电子公司Q3华东区域销售业绩" → "Q3华东"
- 大数值：保持数值类型，换算后单位写在轴名称（如 `"yAxis": { "name": "销售额(万)" }`，数据用 `[10, 20]` 而非 `[100000, 200000]`）

**注意**：不要把数值转成带单位的字符串

## 图表技巧

**柱状图方向**：
- 类别 ≤6 个或时间序列 → 纵向
- 类别 >6 个或标签较长 → 横向（数据预排序 + `yAxis.inverse: true` 实现降序）

**饼图**：必须独占 series，不能与其他类型混合
- 默认用实心饼图：`"radius": "70%"`
- 环形图（用户要求时）：`"radius": ["40%", "70%"]`

**双 Y 轴**：折线 Y 轴使用 `"scale": true` 避免数据波动被压平

**混合图表**：折线+柱状图时，`xAxis.boundaryGap` 保持默认 `true`，避免柱子贴边

**X 轴标签过多**：标签超过 8 个时，考虑旋转 `"axisLabel": { "rotate": 45 }`

**时间轴数据**：
- 数据点 >15 个时，考虑按时段聚合（半小时/小时），或使用 `"xAxis": { "type": "time" }`
- time 类型会按真实时间比例显示，需完整时间格式：`["2024-12-11 09:30", 27]`

**数据点较多（>20）**：可添加缩放条方便用户查看细节：
```json
{ "dataZoom": [{ "type": "slider" }] }
```

**参考线**：需提供上下文时（目标、平均值等）：
```json
{ "markLine": { "data": [{ "type": "average", "name": "平均" }] } }
```

**面积图**：添加 `areaStyle` 实现渐变（顶部半透明→底部全透明）：
```json
"areaStyle": {
  "color": { "type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
    "colorStops": [{ "offset": 0, "color": "rgba(84,112,198,0.5)" }, { "offset": 1, "color": "rgba(84,112,198,0)" }]
  }
}
```

**词云**：
```json
{
  "type": "wordCloud",
  "width": "100%", "height": "100%",
  "gridSize": 5, "sizeRange": [60, 80],
  "rotationRange": [0, 0], "drawOutOfBound": false
}
```

## 禁止项

**输出必须是严格合法的 JSON**，以下写法会导致渲染失败：

**错误 - 函数（最常见错误）**：
```javascript
"formatter": function(params) { return params[0].name; }
"color": function() { return colors[Math.random()...] }  // 词云随机颜色也不行
```

**正确替代**：
```json
"formatter": "{b}: {c}"
// 词云颜色：在每个 data 项中指定 "textStyle": { "color": "#xxx" }
```

**其他禁止项**：
- 尾逗号：`{"a": 1,}` 
- 注释：`// comment` 或 `/* comment */`
- 单引号：`{'a': 1}`
- 变量引用：`"data": myData`
- 多 grid 布局
- tooltip.formatter 中获取颜色
