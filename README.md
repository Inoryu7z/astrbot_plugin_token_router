[![TokenRouter Counter](https://count.getloli.com/get/@Inoryu7z.token_router?theme=miku)](https://github.com/Inoryu7z/-astrbot_plugin_token_router)

# 🔀 TokenRouter · Token 用量路由器

一个按对话窗口追踪 token 用量的模型路由插件。
它会在每次 LLM 响应后累计当前模型的当日用量，当某个模型达到每日限额时，自动把该窗口切换到路由链中的下一个模型；当所有模型都用尽时，回退到框架默认模型。每天 0 点自动重置计数。

插件全程静默运行，不提供命令、不发送提醒，只在后台完成用量统计与模型切换。

---

## ✨ 功能概览

### 📊 按窗口追踪用量

插件以 UMO（Unified Message Origin，统一消息来源）为单位记录每个对话窗口的 token 用量。
不同窗口的用量彼此独立，互不影响。

例如：窗口 1 今天用了 19 万 token 的模型 A，窗口 2 今天用了 1 万 token 的模型 A，两者各自计数，不会因为合计超限而误触发切换。

### 🧬 基于人格的独立路由

v1.1.0 新增，v1.2.0 升级为选择式配置。每个窗口配置可额外绑定一个 `persona_id`（人格），绑定后该窗口的路由链仅对指定人格生效。

v1.2.0 起，`persona_id` 在配置面板中为下拉选择列表，选项由框架 `PersonaManager` 实时提供，无需手动输入人格 ID。新增/删除人格时下拉列表自动更新。

典型场景：一个群聊窗口里有多个 bot 人格（通过 `/persona` 命令切换），每个人格可配置独立的模型路由链，用量计数与「已用尽」标记也各自独立，互不干扰。

`persona_id` 留空时该窗口对所有人格生效（向后兼容 v1.0.0）。窗口匹配优先级：
1. UMO + 人格ID 完全匹配
2. UMO + 空人格ID（通用窗口，回退）

### 🔀 限额触发路由

每个窗口可配置一条有序的模型路由链：模型 1 → 模型 2 → 模型 3 ……
当模型 1 的当日用量达到限额后，下一次请求会自动切换到模型 2，以此类推。

切换时机是「达到限额后的下一次请求」，而不是达到限额的当次请求——当次请求仍由原模型完成。

### 🪂 用尽回退默认模型

当路由链中所有模型都达到当日限额时，插件会把该窗口回退到框架默认模型，并标记当天已用尽。
标记后当天不再重复处理该窗口，避免反复切换；次日 0 点自动清除标记并恢复路由链。

### 🕛 每日自动重置

用量计数按本地时间每天 0 点重置。
重置通过日期字符串比较实现：每次记录用量时会检查该 provider 的记录日期，若与今天不符则归零。

### 📈 两种统计模式

插件支持两种全局统计模式，通过 `stats_mode` 配置项切换：

| 模式 | 值 | 说明 |
|------|-----|------|
| 窗口统计 | `window` | 每个窗口独立计数，互不影响。窗口 1 用了 19 万 A 不影响窗口 2 的 A 计数。 |
| 全局统计 | `global` | 所有窗口共享同一 provider 的用量计数。任一窗口的请求都会累加该 provider 的全局用量，达到限额后所有使用该 provider 的窗口都会触发切换。 |

全局统计适合多个窗口共用同一额度池的场景（例如多个群聊共用同一个 API Key 的额度）。

### 🧷 Provider ID 标识

每个模型条目包含 `provider_id`：
- `provider_id` 用于切换 AstrBot 的 Provider（决定走哪个提供商）

---

## 🛠️ 配置结构

配置提供 10 个窗口：外层是 `windows` 对象，内含 `window_1` 到 `window_10`，每个窗口内有一个 `models` 列表（可自由添加模型条目）。

同一 UMO 可配置在多个窗口中，搭配不同的 `persona_id` 实现多人格各自独立路由。

### 全局配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `stats_mode` | 统计模式：`window`（窗口统计）/ `global`（全局统计） | `window` |

### 窗口配置（window_1 ~ window_10）

| 字段 | 说明 |
|------|------|
| `umo` | 对话窗口 UMO 号，格式 `platform_id:message_type:session_id`。留空则不启用此窗口。 |
| `persona_id` | 人格。配置面板中为下拉选择列表（由框架 `PersonaManager` 动态提供）。留空则对所有人格生效（向后兼容）；选择某个具体人格则仅对该人格生效，可实现同一窗口内多人格各自独立路由。 |

### 模型配置（每个窗口内的 models 列表）

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `provider_id` | AstrBot WebUI 中配置的提供商 ID | — |
| `daily_limit` | 每日用量限额（token） | `200000` |

### 配置示例

#### 示例 1：单窗口单人格（最简）

以窗口 1（`aiocqhttp:GroupMessage:123456`）为例，路由链为 模型 A → 模型 B → 模型 C：

```
stats_mode: window
windows
└─ window_1
   ├─ umo: aiocqhttp:GroupMessage:123456
   ├─ persona_id: (留空，对所有人格生效)
   └─ models
      ├─ 模型 1: provider_id=provider_a, daily_limit=200000
      ├─ 模型 2: provider_id=provider_b, daily_limit=200000
      └─ 模型 3: provider_id=provider_c, daily_limit=200000
```

#### 示例 2：同窗口多人格独立路由

一个群聊窗口里有三个 bot 人格，各自使用独立的路由链：

```
stats_mode: window
windows
├─ window_1
│  ├─ umo: aiocqhttp:GroupMessage:123456
│  ├─ persona_id: bot_a
│  └─ models
│     ├─ 模型 1: provider_id=provider_a, daily_limit=200000
│     └─ 模型 2: provider_id=provider_b, daily_limit=200000
├─ window_2
│  ├─ umo: aiocqhttp:GroupMessage:123456
│  ├─ persona_id: bot_b
│  └─ models
│     ├─ 模型 1: provider_id=provider_c, daily_limit=300000
│     └─ 模型 2: provider_id=provider_d, daily_limit=300000
└─ window_3
   ├─ umo: aiocqhttp:GroupMessage:123456
   ├─ persona_id: bot_c
   └─ models
      └─ 模型 1: provider_id=provider_e, daily_limit=500000
```

模型 1 应与框架为该窗口设置的默认模型保持一致，这样在未触发限额时插件不会干扰默认行为。

---

## 📌 使用建议

**最简上手**：在 `window_1` 中填入 UMO 号，添加一个与框架默认模型一致的模型条目即可。此时插件只会记录用量，不会触发切换。

| 需求 | 配置方式 |
|------|----------|
| 仅统计用量 | 只配置模型 1（与默认模型一致） |
| 单次备用切换 | 配置模型 1 + 模型 2 |
| 多级路由链 | 按优先级顺序配置多个模型 |
| 多窗口共用额度 | `stats_mode` 设为 `global` |
| 多人格独立路由 | 同一 UMO 配置多个窗口，各填不同 `persona_id` |

- 路由链按从上到下的顺序消费，模型 1 最先用，最后一个用尽后回退默认模型。
- 每个模型的 `daily_limit` 可以不同，按各模型的实际额度填写即可。
- 如果某窗口不需要路由，UMO 留空即可，插件不会干预未配置的窗口。
- 全局统计模式下，不同窗口如果配置了同一个 provider，会共享用量计数；但各窗口的路由链和切换目标仍然独立。
- 人格独立路由下，每个人格的用量计数与「已用尽」标记独立，互不影响。

---

## ⚠️ 注意事项

1. 插件仅在 `on_llm_response` 中累计 `usage.total`（含输入与输出 token），不包含流式过程中的中间统计。
2. 用量数据持久化在插件数据目录的 `usage_data.json` 中，重启 AstrBot 不会丢失当日计数。
3. 插件通过 `event.set_extra("selected_provider")` 指定 provider，仅影响对应窗口的当次请求，不改变会话级 provider。
4. 当所有模型用尽并回退默认模型后，当天该 (UMO, 人格) 不再参与路由；次日 0 点自动恢复。
5. 插件不主动设置初始模型，仅在限额触发时切换；模型 1 的初始状态由框架配置决定。
6. 若配置的 `provider_id` 在 AstrBot 中不存在，切换会失败并记录警告日志，不影响其他流程。
7. 全局统计模式下，用量计数按 provider 共享，但「已用尽」标记按 (UMO, 人格) 独立——因为每个 (UMO, 人格) 有自己的路由链。
8. 人格 ID 通过框架 `PersonaManager.resolve_selected_persona` 解析，解析优先级：UMO 级强制人格 > 会话级人格 > 默认人格。
9. v1.1.0 升级后会自动迁移 v1.0.0 的用量数据到人格嵌套格式，旧数据归入空人格 ID 作用域，不影响已有计数。

---

## 📝 版本记录

详细更新见 `CHANGELOG.md`。
