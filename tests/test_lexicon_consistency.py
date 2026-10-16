"""词典↔注册表一致性守卫 (W9) 钉子测试 (2026-10-16, 用户令「推送随源头调整自动体现」).

钉死: ①终态注册项的活跃措辞必被暴露 ②终态且文案带终态标记的合规项不误报
③守卫函数异常时主流程不崩 (返回空=不漏面的保守向)。
"""
from app.playbook.shadow_registry import ShadowStatus


def test_terminal_with_active_wording_is_flagged(monkeypatch):
    import app.review.shadow_lexicon as lex

    class _S:
        def __init__(self, status):
            self.status = status

    monkeypatch.setattr(lex, "SHADOW_LEXICON", {
        "fake_terminal_active": {"plain": "x", "conclusion": "攒样中, 还差 5 笔到门",
                                 "cls": "WINDOW"},
        "fake_terminal_honest": {"plain": "x", "conclusion": "已退役（KILLED）归档",
                                 "cls": "AUTO_WATCH"},
    })
    import app.playbook.shadow_registry as reg
    monkeypatch.setattr(reg, "REGISTRY", [
        type("Item", (), {"id": "fake_terminal_active",
                          "status": ShadowStatus.KILLED})(),
        type("Item", (), {"id": "fake_terminal_honest",
                          "status": ShadowStatus.KILLED})(),
    ])
    # check 函数从 app.playbook.shadow_registry 再 import REGISTRY — 须 patch 到它读的那层
    out = lex.check_lexicon_registry_consistency()
    ids = [o["id"] for o in out]
    assert "fake_terminal_active" in ids, "终态+活跃措辞必须暴露"
    assert "fake_terminal_honest" not in ids, "终态+终态文案不得误报"


def test_real_repo_lexicon_registry_zero_mismatch():
    """真仓库零容忍 (2026-10-16 清零后): 任何新矛盾 = 注册表进了终态而词典没跟上 ⇒
    本钉当场红, 强迫同日补文案 (这正是「推送随源头调整自动体现」的 enforcement)。"""
    from app.review.shadow_lexicon import check_lexicon_registry_consistency
    mm = check_lexicon_registry_consistency()
    assert mm == [], f"词典与注册表矛盾: {[(m['id'], m['registry_status']) for m in mm]}"
