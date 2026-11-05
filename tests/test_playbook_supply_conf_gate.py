"""供应侧 conf 门退役 (2026-11-05 用户裁「conf 没有判断力 ⇒ 正常成交, 供应标的量」)。

背景证据 (A10 口径, PLAYBOOK_SIM n=758, 2026-07-01 起):
  · IC(confidence, $pnl) = +0.043 ⇒ 无可用的预测力;
  · conf<40 占 727/758 = 96% (笔均 +$2.23, 胜率 71.3%) ⇒ 分层退化成一个桶, 没有"高 conf"人群可比;
  · 门自 2026-08-25 起 0 命中 (全史仅 13 行 SKIP) ⇒ 已在事实上空转, 只留下误导性日志与口径歧义。
处置: 阈值键改 0 = 关闭, conf 仍随指令落库供事后归因。

本文件钉的是 **"关掉"这件事本身**, 而不是 helper 内部的比较符:
  · 最危险的回归是**配置被悄悄改回 55** (门重新长出牙齿, 而没有任何提交说明);
    ⇒ test_yaml_config_is_disabled 直接读 YAML 真源断言两键为 0。
  · 其次是"0 被当成普通阈值"的语义歧义: 当前比较式 `confidence < 0` 恰好恒假,
    所以 0 只是"顺带安全"; 一旦有人把判据改成 `<=` 或把 fallback 调成 >0,
    0 就会变成"全拦"。⇒ 断言 helper 对 <=0 恒返回 False, 给未来改判据的人一条红线。
  · 变异验证记录: 把 helper 改回裸比较式, 本文件仍全绿 (= 该 mutation 与 0 阈值行为等价,
    不构成有效变异); 把 YAML 改回 55 ⇒ test_yaml_config_is_disabled 转红 (= 有效变异)。
"""
from app.playbook.pipeline import _supply_conf_blocked


def test_zero_threshold_never_blocks():
    for conf in (0, 12, 35, 55, 99):
        assert _supply_conf_blocked(conf, 0) is False, f"阈值 0 必须等于关闭 (conf={conf})"


def test_negative_threshold_never_blocks():
    assert _supply_conf_blocked(10, -1) is False


def test_positive_threshold_still_filters():
    assert _supply_conf_blocked(35, 55) is True
    assert _supply_conf_blocked(54.9, 55) is True
    # 边界: 等于阈值通过 (与退役前的 `<` 语义一致, 不因本批改动而变严)
    assert _supply_conf_blocked(55, 55) is False
    assert _supply_conf_blocked(80, 55) is False


def test_bad_input_fails_open():
    """缺值/脏值不得变成全拦 (fail-open: 与"门已退役"一致, 且不会静默掐供给)。"""
    assert _supply_conf_blocked(None, 55) is False
    assert _supply_conf_blocked("abc", 55) is False


def test_yaml_config_is_disabled():
    """配置真源也必须是 0 —— 代码支持关闭 ≠ 部署关闭, 两者要分别钉。"""
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parent.parent
    text = (root / "config" / "unified_config.yaml").read_text(encoding="utf-8")
    for key in ("supply_conf_gate_buy", "supply_conf_gate_short"):
        m = re.search(rf"^\s+{key}:\s*(\S+)", text, re.M)
        assert m, f"YAML 缺 {key}"
        assert float(m.group(1)) == 0, f"{key} 应为 0 (门关闭), 实为 {m.group(1)}"