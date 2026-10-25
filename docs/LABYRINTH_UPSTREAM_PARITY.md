# 黎明界上游差距补齐

本轮按上游固定提交 `b809b196988b019571caf5679ccf8d7ebb7f072d` 补齐复活后继和商店规则。当前为本地源码改动，尚未发布，也没有覆盖现用安装。

## 遗物复活后继

“星辉吐息”等效果改判胜利后，游戏仍可能显示“战斗失败”，但右下方变为“下一步”，没有“结束”按钮。

程序现在同时核验失败标题、伤害报告、下一步和同帧按钮颜色布局，再将结果记录为独立的 `revived`。它不增加失败次数、重试次数或失败阵容。只有本助手刚完成的战斗能推进这条后继；点击前会重新核对布局。实际到达遗物结果后，再沿用原结果关闭与地图读回。整局通关仍需原终态校验。

普通三按钮战败、变暗弹窗、缺少必要文字或身份不匹配均不能进入复活分支。新增的页面读取仅在识别到战败页后执行。

参考：[两按钮布局识别](https://github.com/wbero/limingjie-assistant/blob/b809b196988b019571caf5679ccf8d7ebb7f072d/android/app/src/main/java/com/landosol/toolbox/labyrinth/vision/LabyrinthBattleFailureRecognition.kt)、[会话中的推进与重试区分](https://github.com/wbero/limingjie-assistant/blob/b809b196988b019571caf5679ccf8d7ebb7f072d/android/app/src/main/java/com/landosol/toolbox/labyrinth/LabyrinthEntryRecognitionSession.kt)。

## 商店规则

默认仍在第四区节省金币。当玩家启用刷新，并把起始区域设为第四区或更早时，第四区会按配置购买和刷新；关闭购买或刷新时保留原有行为。

完整的三个按钮已确认不可购买，或三格都能确认是职能印记时，可以恢复本轮遗物售罄状态。标题、价格或购买条件不足的商品不会因此获得购买资格。刷新仍需独立核验 300 金币报价、确认和实际扣费。

刷新按钮未识别和明确不可刷新分别记录。货架仍有未定商品且没有可先购买的可信遗物时，继续只读观察，沿用原有 30 秒预算和更短父预算。等待期间不买印记、不刷新、不离店；超时、停止和未知结果保持停止，不补发操作。

参考：[上游商店决策](https://github.com/wbero/limingjie-assistant/blob/b809b196988b019571caf5679ccf8d7ebb7f072d/android/app/src/main/java/com/landosol/toolbox/labyrinth/LabyrinthShopPolicy.kt)。

## 验证范围

新增回归覆盖复活推进、普通战败、缺字、变暗、页面变化后的点击守卫、局号绑定、第四区配置、售罄恢复、货架未定、停止与父预算。原生回放使用上游实际两按钮截图；后继遗物结果图为合成夹具，地图来自另一张保存帧，不作为真实游戏后继或通关证明。

本轮未新增真实游戏、实机时序和整局通关验收。最终检查结果、来源摘要及未覆盖项见工程内 `.runtime/upstream-parity-20261025/`。
