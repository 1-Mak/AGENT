import pytest

from sitewatch.config import ConfigError, Settings, load_dotenv, load_sources

ENV_KEYS = [
    "DEEPSEEK_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING",
    "SMTP_USER", "MAIL_FROM", "MAIL_TO", "SMTP_PORT", "SMTP_HOST",
]  # fmt: skip


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_defaults_target_cheap_deepseek_model():
    s = Settings.from_env()
    assert (s.llm_model, s.llm_base_url, s.llm_thinking) == ("deepseek-flash", "https://api.deepseek.com", False)
    assert s.llm_api_key == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_THINKING", "ON")
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example.com/")
    monkeypatch.setenv("SMTP_USER", "bot@x.ru")
    monkeypatch.setenv("MAIL_TO", "a@x.ru, b@x.ru ,")
    s = Settings.from_env()
    assert s.llm_api_key == "sk-1" and s.llm_model == "deepseek-v4-pro" and s.llm_thinking is True
    assert s.llm_base_url == "https://proxy.example.com"
    assert s.mail_from == "bot@x.ru" and s.mail_to == ("a@x.ru", "b@x.ru")
    assert s.missing_mail_settings() == ["SMTP_HOST"]


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "из-окружения")
    env = tmp_path / ".env"
    env.write_text('# комментарий\nLLM_MODEL=из-файла\nMAIL_TO="a@x.ru"\n', encoding="utf-8")
    load_dotenv(env)
    s = Settings.from_env()
    assert s.llm_model == "из-окружения" and s.mail_to == ("a@x.ru",)


def write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_sources_defaults_and_url_normalization(tmp_path):
    p = write(
        tmp_path,
        """
sources:
  - {id: a, type: list, url: "https://честныйзнак.рф/info/", link_pattern: "/x/"}
  - {id: b, type: page, url: "https://example.com/p"}
""",
    )
    a, b = load_sources(p)
    assert a.url == "https://xn--80ajghhoc2aj1c8b.xn--p1ai/info/" and a.initial_notify == 1 and a.name == "a"
    assert b.initial_notify == 0


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        ("sources: []", "нет списка sources"),
        ("sources:\n  - {id: a, type: list, url: 'https://x.ru'}", "link_pattern"),
        ("sources:\n  - {id: a, type: rss, url: 'https://x.ru'}", "type должен быть"),
        ("sources:\n  - {id: a, type: page, url: 'https://x.ru'}\n  - {id: a, type: page, url: 'https://y.ru'}", "повторяющийся"),
        ("sources:\n  - {id: a, type: list, url: 'https://x.ru', link_pattern: '('}", "регулярное выражение"),
        ("sources:\n  - {type: page, url: 'https://x.ru'}", "не задано поле id"),
    ],
)
def test_load_sources_rejects_bad_config(tmp_path, yaml_text, message):
    with pytest.raises(ConfigError, match=message):
        load_sources(write(tmp_path, yaml_text))


def test_load_sources_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="не найден"):
        load_sources(tmp_path / "нет.yaml")
