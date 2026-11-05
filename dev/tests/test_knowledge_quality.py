"""知识质量模型：场景/效果/证据、可读性与 legacy 标记。"""
from __future__ import annotations

from anvil.services import knowledge_card


def _fields(**extra):
    return {
        "observation": "X509 证书链校验缺失",
        "evidence": "任务 T1 的评审记录与复现命令",
        "lesson": "先校验证书链再下结论",
        "applicability": "OpenSSL X509 审计",
        "counterexamples": "纯内部测试证书可豁免",
        "use_when": "审计 X509 证书链时",
        "expected_effect": "避免漏报证书链问题，减少返工",
        **extra,
    }


def test_good_card_passes_quality_gate():
    quality = knowledge_card.evaluate_quality(
        "insight", "先校验证书链再下结论",
        "审计 X509 时先校验证书链，避免漏报。",
        "# 结论\n\n## 证据\n任务 T1。", _fields())
    assert quality["blocking"] == []
    assert quality["score"] >= 70
    assert quality["grade"] in ("A", "B")


def test_missing_scenario_effect_evidence_blocks():
    quality = knowledge_card.evaluate_quality(
        "insight", "证书链", "摘要", "正文", {})
    assert "use_when" in quality["blocking"]
    assert "expected_effect" in quality["blocking"]
    assert "evidence" in quality["blocking"]


def test_quality_aliases_accept_legacy_fields():
    quality = knowledge_card.evaluate_quality(
        "insight", "结论", "摘要",
        "正文",
        {
            "scope": "当前频道",
            "outcome": "减少返工",
            "evidence": "任务评审",
            "observation": "观察", "lesson": "结论",
            "applicability": "测试", "counterexamples": "无",
        })
    assert "use_when" not in quality["blocking"]
    assert "expected_effect" not in quality["blocking"]
    assert quality["resolved_fields"]["use_when"] == "scope"


def test_long_title_and_empty_body_block():
    quality = knowledge_card.evaluate_quality(
        "insight", "标" * 81, "摘要", "", _fields())
    assert "title_length" in quality["blocking"]
    assert "body" in quality["blocking"]
    assert quality["readable"] is False


def test_legacy_marker_is_advisory():
    quality = knowledge_card.evaluate_quality(
        "insight", "旧卡", "旧摘要", "旧正文", {},
        status="approved", legacy=True)
    assert quality["legacy"] is True
    assert any("历史已批准" in warning for warning in quality["warnings"])


def test_template_exposes_quality_fields():
    template = knowledge_card.template("insight")
    fields = {item["key"]: item for item in template["kinds"]["insight"]["fields"]}
    assert fields["use_when"]["required"] is True
    assert fields["expected_effect"]["required"] is True
