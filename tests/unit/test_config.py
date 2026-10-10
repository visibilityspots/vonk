from __future__ import annotations

import pytest

from vonk.config import DEFAULT_DB_PATH, Settings, _secret
from vonk.models import Tak

FULL_ENV = {
    "BAIKAL_URL": "https://baikal.example/dav.php/",
    "BAIKAL_USER": "vonk",
    "BAIKAL_PASSWORD": "pw-secret",
    "BAIKAL_CALENDAR": "vonk-test",
    "GOTIFY_URL": "https://gotify.example",
    "GOTIFY_TOKEN": "gotify-secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "VONK_API_TOKEN": "api-secret",
    "VONK_API_URL": "https://vonk.example/api",
    "VONK_DB_PATH": "/data/vonk.db",
    "VONK_PUBLIC_URL": "https://vonk.example",
    "CHILD_NAMES": '{"kabouters": "Kind A", "jeevees": "Kind B"}',
    "TAKKEN": "kabouters,jeevees",
}


@pytest.mark.parametrize("raw", [None, "", "   ", "\n", "<no value>", "  <no value>  "])
def test_secret_treats_empty_and_no_value_as_missing(raw):
    assert _secret(raw) is None


def test_secret_strips_real_value():
    assert _secret("  abc \n") == "abc"


def test_full_env_is_not_degraded():
    s = Settings.from_env(FULL_ENV)
    assert s.missing_secrets("serve") == []
    assert s.missing_secrets("poll") == []
    assert not s.degraded
    assert s.errors == ()
    assert s.child_names == {Tak.KABOUTERS: "Kind A", Tak.JEEVEES: "Kind B"}
    assert s.takken == (Tak.KABOUTERS, Tak.JEEVEES)
    assert s.vonk_db_path == "/data/vonk.db"


def test_empty_env_degraded_and_defaults():
    s = Settings.from_env({})
    assert s.degraded
    assert s.vonk_db_path == DEFAULT_DB_PATH
    assert "BAIKAL_PASSWORD" in s.missing_secrets("serve")
    assert "CHILD_NAMES" in s.missing_secrets("serve")
    assert s.missing_secrets("poll") == [
        "ANTHROPIC_API_KEY",
        "VONK_API_TOKEN",
        "VONK_API_URL",
        "TAKKEN",
    ]


@pytest.mark.parametrize("bad", ["", "   ", "<no value>"])
def test_no_value_secret_counts_as_missing(bad):
    s = Settings.from_env({**FULL_ENV, "GOTIFY_TOKEN": bad})
    assert s.gotify_token is None
    assert s.missing_secrets("serve") == ["GOTIFY_TOKEN"]
    assert s.degraded


def test_poll_role_does_not_need_caldav_or_gotify():
    env = {k: FULL_ENV[k] for k in ("ANTHROPIC_API_KEY", "VONK_API_TOKEN", "VONK_API_URL")}
    env["TAKKEN"] = "kabouters"
    s = Settings.from_env(env)
    assert s.missing_secrets("poll") == []
    assert s.degraded  # the serve role is incomplete


def test_no_value_child_names_is_missing():
    s = Settings.from_env({**FULL_ENV, "CHILD_NAMES": "<no value>"})
    assert s.child_names == {}
    assert s.missing_secrets("serve") == ["CHILD_NAMES"]


@pytest.mark.parametrize(
    "raw",
    ["not json", "[1, 2]", '{"nope": "Kind"}', '{"kabouters": ""}', '{"kabouters": 3}'],
)
def test_invalid_child_names_is_error_and_missing(raw):
    s = Settings.from_env({**FULL_ENV, "CHILD_NAMES": raw})
    assert s.child_names == {}
    assert s.errors
    assert s.degraded
    assert "CHILD_NAMES" in s.missing_secrets("serve")


def test_partially_invalid_child_names_keeps_valid_and_reports():
    s = Settings.from_env({**FULL_ENV, "CHILD_NAMES": '{"kabouters": "Kind A", "x": "Kind B"}'})
    assert s.child_names == {Tak.KABOUTERS: "Kind A"}
    assert len(s.errors) == 1
    assert "Kind" not in s.errors[0]
    assert s.degraded


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("kabouters,jeevees", (Tak.KABOUTERS, Tak.JEEVEES)),
        ("kabouters jeevees", (Tak.KABOUTERS, Tak.JEEVEES)),
        (" Kabouters , JEEVEES ,", (Tak.KABOUTERS, Tak.JEEVEES)),
        ('["welpen", "jogis"]', (Tak.WELPEN, Tak.JOGIS)),
        ("kabouters,kabouters", (Tak.KABOUTERS,)),
    ],
)
def test_takken_parsing(raw, expected):
    s = Settings.from_env({**FULL_ENV, "TAKKEN": raw})
    assert s.takken == expected
    assert s.errors == ()


def test_unknown_tak_in_takken_is_error():
    s = Settings.from_env({**FULL_ENV, "TAKKEN": "kabouters,scouts"})
    assert s.takken == (Tak.KABOUTERS,)
    assert s.errors


def test_repr_hides_secrets_and_child_names():
    text = repr(Settings.from_env(FULL_ENV))
    for secret in ("pw-secret", "gotify-secret", "sk-ant-secret", "api-secret", "Kind A"):
        assert secret not in text


def test_unknown_role_rejected():
    with pytest.raises(ValueError):
        Settings.from_env(FULL_ENV).missing_secrets("other")  # type: ignore[arg-type]


def test_from_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("BAIKAL_CALENDAR", "<no value>")
    monkeypatch.setenv("GOTIFY_URL", "https://gotify.example")
    s = Settings.from_env()
    assert s.baikal_calendar is None
    assert s.gotify_url == "https://gotify.example"
