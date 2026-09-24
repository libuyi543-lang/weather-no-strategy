# 连续三桶概率校准与交叉验证

> 概率只使用其他目标日期估计；全部结果为历史shadow研究。

- 独立日期：15；原始候选事件：88。

## 交叉验证组合结果

| 规则 | 日期 | 事件 | 净ROI | 净PnL | 5%下界 |
|---|---:|---:|---:|---:|---:|
| `market_anchor|5/15/5|ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `market_anchor|5/15/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `market_anchor|5/20/5|ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `market_anchor|5/20/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `global|5/15/5|ev_positive` | 15 | 15 | 28.4% | 37.619 | 0.199 |
| `global|5/15/5|lower_ev_positive` | 2 | 2 | -37.2% | -5.929 | -3.117 |
| `global|5/20/5|ev_positive` | 15 | 15 | 33.9% | 55.743 | 0.407 |
| `global|5/20/5|lower_ev_positive` | 6 | 6 | 16.0% | 9.667 | -4.173 |
| `center_bin|5/15/5|ev_positive` | 15 | 15 | 19.3% | 25.925 | -0.550 |
| `center_bin|5/15/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `center_bin|5/20/5|ev_positive` | 15 | 15 | 22.8% | 38.103 | -0.759 |
| `center_bin|5/20/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `gap_bin|5/15/5|ev_positive` | 14 | 14 | 26.1% | 32.057 | -0.136 |
| `gap_bin|5/15/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |
| `gap_bin|5/20/5|ev_positive` | 14 | 14 | 21.1% | 32.171 | -1.271 |
| `gap_bin|5/20/5|lower_ev_positive` | 0 | 0 | N/A% | 0.000 | N/A |

## 概率质量

| 模型/结构 | Brier | 平均log score |
|---|---:|---:|
| `market_anchor|5/15/5` | 0.1493 | -0.4593 |
| `market_anchor|5/20/5` | 0.1493 | -0.4593 |
| `global|5/15/5` | 0.1383 | -0.4400 |
| `global|5/20/5` | 0.1383 | -0.4400 |
| `center_bin|5/15/5` | 0.1421 | -0.4460 |
| `center_bin|5/20/5` | 0.1421 | -0.4460 |
| `gap_bin|5/15/5` | 0.1493 | -0.4765 |
| `gap_bin|5/20/5` | 0.1493 | -0.4765 |

## Global校准相对市场锚

- Brier平均改善：0.0110；日期块5%下界：-0.0030。
- Log score平均改善：0.0193；日期块5%下界：-0.0172。
