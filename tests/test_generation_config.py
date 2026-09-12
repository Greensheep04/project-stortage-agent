import pytest

from pi_market import explain


def _block_env_file(monkeypatch):
    """Prevent .env from injecting keys so missing-config behavior is deterministic."""
    monkeypatch.setattr(explain, "load_dotenv", lambda *args, **kwargs: None)


def test_missing_deepseek_key_raises_clear_error(monkeypatch):
    _block_env_file(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(explain.ExplainError) as exc:
        explain.DeepSeekGenerationClient()
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_defaults_point_to_deepseek(monkeypatch):
    _block_env_file(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("GENERATION_MODEL", raising=False)
    client = explain.DeepSeekGenerationClient()
    assert client.base_url == "https://api.deepseek.com"
    assert client.model == "deepseek-flash"


def test_explicit_config_is_respected(monkeypatch):
    _block_env_file(monkeypatch)
    client = explain.DeepSeekGenerationClient(
        api_key="sk-test-not-real",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
    )
    assert client.api_key == "sk-test-not-real"
    assert client.base_url == "https://api.deepseek.com"
    assert client.model == "deepseek-flash"


def test_generation_does_not_fall_back_to_dashscope(monkeypatch):
    # 只配置阿里云 Key 不应让生成链可用（两条链独立，禁止回退/共用）。
    _block_env_file(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-not-real")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(explain.ExplainError):
        explain.DeepSeekGenerationClient()


def test_explain_missing_key_returns_model_unavailable(monkeypatch):
    # 缺配置时 explain() 返回可观察的 model_unavailable，不抛到 CLI 外层。
    _block_env_file(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    facts = {
        "dataset_id": "ds-test",
        "order_id": "SO-TEST",
        "line_id": "001",
        "as_of": "2026-09-09T20:00:00+08:00",
        "lines": [
            {
                "order_id": "SO-TEST",
                "line_id": "001",
                "planned_quantity": 10,
                "matched_net_shipped_quantity": 10,
                "difference_quantity": 0,
                "status_tags": ["matched"],
                "evidence_ids": ["EV-1"],
            }
        ],
    }
    evidence = [
        {
            "evidence_id": "EV-1",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "说明。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 2},
        }
    ]
    result = explain.explain(facts, evidence)
    assert result["explanation_status"] == "model_unavailable"
    assert "DEEPSEEK_API_KEY" in result["model_error"]
