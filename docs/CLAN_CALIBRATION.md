# 公会战校准格式

## 能力与版本

生产校准只从安装目录的 `assets/resource/clan-battle-calibrations/index.json` 加载。Python 和 GUI 固定校验索引 SHA-256，再核对所选 profile 的摘要。玩家作业不能提供校准路径或“已验证”标志。

索引保留 `version` 和 `entries`。每项原有字段为 `calibration_id`、`server`、`content_version`、`frame_size`、`profile_path` 和 `profile_sha256`。同一服务器、内容版本及画面尺寸只能有一项。

索引版本 2 增加 `capabilities`，值为无重复的字符串数组：

| 值 | 用途 |
| --- | --- |
| `simulation` | 单次模拟。 |
| `timed` | 按剩余秒数切换 SET／AUTO，必须同时通过真实时序测量。 |
| `formal` | 正式普通挑战。 |
| `carryover` | 补偿挑战，同时要求 `formal`。 |

空数组不授予执行能力。实际战斗先检查对应模式；非空秒数轴还要求 `timed`。`timed` 不能单独作为战斗模式。

索引版本 1 只支持纯 AUTO 模拟。profile 版本 1 即使放入版本 2 索引，也不能声明 `timed`、`formal` 或 `carryover`。玩家作业和三刀请求继续使用原版本 1。

## 页面与材料

profile 保留既有页面、动作、编队、支援和保存帧字段。版本 2 另支持 `boss_targets`、`phase_labels`、`support_modes` 和 `daily`，具体页面约束由原生校准器校验。保存帧、模板和 profile 均须与索引摘要一致；路径必须留在安装目录内。模板列表的每一项都须单独绑定摘要。

`support_modes: ["none"]` 将执行范围限制为全部自有角色，并允许省略支援布局及相关动作。缺少这项明确声明时，仍要求完整支援校准。

版本 2 的全自有配置可在 `formation` 中声明 `star_source: "account_snapshot"`。此时星级来自作业绑定的账号快照 `battle_rarity`，不以已解锁的 `rarity` 替代，也不依赖编队卡片的星级／徽标动画。快照沿用账号资料的 24 小时有效期，账号、区服、内容版本、文件摘要和五个出战星级均须匹配；未知或不符时停止。GUI 与 Python 使用同一限制。画面仍逐次核对实际五人、支援标记、目标与模式；当前不支持用自有账号快照证明支援星级。

协议同步可能使游戏客户端断线，应在空闲时读取，再回到游戏核验账号。读取本身不修改角色星级；游戏中手动调整星级后，须重新同步并保存作业确认。

`daily` 的只读页面校准不能授予正式出刀资格。正式及补偿还要求完整的执行页面、动作、已用角色和补偿槽位校准；额外样本同样由安装内路径和摘要绑定。

模拟难度页没有真实轮次时，读取结果使用 `round: null` 和 `round_source: selected_difficulty`，并附 `selected_difficulty` 的实际阶段、Boss 编号和模拟标记。三项均与当前目标匹配时，才接受这项结果。正式战仍核对实际轮次。

## 秒数轴与开场

声明 `timed` 的 profile 必须提供 `timing_measurements`，其中仅有安装内相对 `path` 和 `sha256`。文件摘要、JSON 格式及完整测量内容验证后，才交给原生执行器。测量文件使用既有 `owner_native_clan_timing` 类型，绑定校准编号和 HUD 版本。

测量必须覆盖完整 `full_read_tick` 观察和 `toggle_dispatch_and_full_team_readback` 输入读回。观察间隔、输入耗时及游戏秒时长各至少 20 条。`battle_speed` 必须是整数 1；观察间隔不超过 250 ms，输入读回不超过 500 ms。整组操作预算必须严格小于 1,000 ms 和最短实测游戏秒，两项取较小值。

执行轴时，每次观察和开关读回都必须确认 1 倍速。倍速未知或变化、漏秒、状态不明均停止，不补点。版本 2 的开场首帧须在起始秒数时，确认 SET／AUTO 已与作业一致。普通战斗为 90 秒，补偿使用执行器已核对的实际起始秒数；不能在战斗开始后补设，再把后来的正确设置记作开场成功。

格式检查和离线回放不代表实机验收。实际索引只登记已经审核的能力，正式次数与补偿消费仍须各自的真实验证。
