# DESIGN.md — 客情评估智能体

> 本项目 UI 采用红白企业风格：白底 + 黑字 + 红色 CTA（`#E60012`）+ 中性灰分层。任何涉及 UI 的改动，请先阅读本文件，保持 token、几何与质感一致。

## 视觉主题

- 白画布 + 浅灰侧边栏 + 发丝线：主内容区底色 `#FFFFFF`，侧边栏 `#F5F5F5`，边框 `#E5E5E5`（1px hairline）。弱阴影、低装饰，靠留白与层次取胜。
- 单一主色：红色 `#E60012` 只用于主 CTA、主动作与选中态，**不用作正文或大面积背景**；非主操作（状态胶囊、聊天气泡、导航激活文字）用中性灰，避免红色泛滥。
- 几何克制：按钮 8px 圆角（矩形，不是胶囊），卡片 12px 圆角，徽章/状态点用胶囊。
- 结构去盒化：筛选/搜索工具栏直接悬浮在画布上（无卡片外框）；主表格为无外框行列表（仅行分隔线）；统计用一条带分隔线的统计条，而非卡片矩阵。

## 颜色 Token（frontend/src/index.css `@theme`）

| Token | 值 | 用途 |
|---|---|---|
| `--color-brand` / `--color-accent` | `#E60012` | 主 CTA / 主动作 |
| `--color-accent-hover` | `#C5000F` | CTA hover/pressed |
| `--color-accent-soft` | `#FDEBEC` | 选中态浅红底、标签底 |
| `--color-bg` | `#FFFFFF` | 主画布（白） |
| `--color-surface` | `#FFFFFF` | 卡片、表格 |
| `--color-surface-2` | `#F5F5F5` | 表头、次级面板、chip 底 |
| `--color-surface-3` | `#FAFAFA` | 表格/列表行 hover 底 |
| `--color-sidebar` | `#F5F5F5` | 侧边栏底 |
| `--color-border` | `#E5E5E5` | hairline 边框 |
| `--color-border-soft` | `#EFEFEF` | 更淡的分隔线 |
| `--color-border-strong` | `#D4D4D4` | 输入框边框 |
| `--color-ink` | `#1A1A1A` | 主文本 |
| `--color-ink-2` | `#333333` | 次文本 |
| `--color-muted` | `#666666` | 辅助/弱文本 |
| `--color-success` / `soft` | `#1AAE39` / `#E4F4E8` | 成功、上升 |
| `--color-warning` / `soft` | `#DD5B00` / `#FAEDE1` | 警告、一般 |
| `--color-danger` / `soft` | `#C62828` / `#FBE9E9` | 错误、风险 |
| `--color-info` / `soft` | `#0075DE` / `#E4F0FB` | 信息、低优先级 |

> 等级颜色（优秀/良好/一般/风险）由 `backend/scoring_config.yaml` 的 `levels` 配置驱动，**不要**在前端写死；前端兜底值见 `frontend/src/lib/ui.tsx` 的 `DEFAULT_LEVELS`，与 YAML 保持一致。

## 排版

- 字体：系统栈（Inter → PingFang SC / Microsoft YaHei），无需引入 webfont。
- 标题：600 字重、紧凑行高（1.2–1.3）、`tracking-tight`；正文 400、行高 1.5–1.85。
- 数字（分数、趋势）用 600 字重 + 等级色，表格内数字建议等宽对齐。

## 组件规则

- 主按钮：`bg-accent text-white rounded-lg`，hover `bg-accent-hover`，不加光晕阴影。
- 次按钮：白底 + `border-border` 1px + `text-ink-2`；hover 边框变红。
- 输入框：白底、`border-border-strong`、focus 红色边框/ring（`focus:border-accent focus:ring-2 focus:ring-accent/20`）。
- 卡片/统计条/列表容器：白底 + `border-border` + `rounded-xl`（12px），平铺无重阴影。
- 表格：无外框，表头 `text-muted` + 底部 hairline，行间 `border-border-soft`，行 hover `bg-surface-3`。
- 工具栏：搜索/筛选控件无卡片外框，输入框用 `border-border-strong`，focus 红色 ring。
- 徽章（等级/预警）：胶囊 + 语义色 12% 透明底 + 同色 1px 边，文字用语义色。
- 顶栏：白底 + 底部 hairline，左侧红色 logo 块，右侧 LLM 状态胶囊（正常=浅灰底灰字 + 绿点，降级=浅橙底橙字）。
- 侧边栏：`bg-sidebar` 浅灰，激活项白卡 + 黑色文字 + 左侧红色指示条，hover `#EBEBEB`。
- 抽屉/弹窗：白底、12px 圆角、`rgba(15,15,15,.16)` 柔和阴影，遮罩 `rgba(15,15,15,.38)`。
- Markdown（AI 回复）：标题/加粗用 `--color-ink`，引用块左侧 3px 红色竖线 + 浅灰底。

## Do / Don't

- ✅ 红色 `#E60012` 只做 CTA 与选中态；状态传达交给红/橙/绿语义色（预警红 `#C62828` 与之区分）。
- ✅ “红而不刺眼”：红色 `#E60012` 保持小面积点缀——CTA 按钮、细红杠、hover 文字变红、小数字强调；**禁止**大面积红色块（气泡、横幅、整块背景）。
- ✅ 按钮保持 8px 直角矩形；卡片 12px；徽章才用胶囊。
- ✅ 中性灰底 + 白卡，靠 hairline 分层。
- ❌ 不要用蓝色/深蓝科技风色板（旧 `#1A3A5C` / `#0066FF` 体系已废弃）。
- ❌ 不要使用紫色（旧 Notion 体系已废弃）。
- ❌ 不要给按钮加光晕/重阴影，不要滥用渐变。
- ❌ 不要在前端硬编码等级颜色；走 `levelColor()` / `--color-*` token。

## 响应式

- 桌面：左侧 260px 侧边栏 + 内容区；移动端：顶部条 + 底部 Tab，侧边栏变抽屉。
- 表格在 `md` 以下切换为卡片列表（已实现，勿回退）。
