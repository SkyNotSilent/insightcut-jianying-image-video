import sqlite3

import pytest

from src.config import Config
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient
from src.draft.voice_catalog import (
    DOUBAO_DEFAULT_ENABLED_IDS,
    DOUBAO_PRESET_VOICES,
    MIMO_DEFAULT_ENABLED_IDS,
    build_voice_key,
    encode_segment_tts_override,
    normalize_tts_options,
    parse_voice_key,
    segment_tts_override,
    speed_instruction,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


def test_builds_and_parses_canonical_voice_keys():
    assert build_voice_key("mimo", "冰糖") == "mimo:冰糖"
    assert build_voice_key("doubao", "voice-1") == "doubao:voice-1"

    mimo = parse_voice_key("mimo:冰糖")
    assert (mimo.provider, mimo.voice_id, mimo.kind) == ("mimo", "冰糖", "preset")

    clone = parse_voice_key("mimo-clone:abc123")
    assert (clone.provider, clone.voice_id, clone.kind) == ("mimo", "abc123", "clone")


def test_infers_known_bare_voice_ids_without_breaking_legacy_tasks():
    assert parse_voice_key("茉莉").provider == "mimo"
    assert parse_voice_key("zh_male_qingchexiaoxin_moon_bigtts").provider == "doubao"
    assert parse_voice_key("unknown-legacy", default_provider="doubao").provider == "doubao"


def test_normalizes_provider_specific_tts_options():
    normal_doubao = normalize_tts_options(
        {"speed_level": "normal", "volume_ratio": 1.0},
        {},
        provider="doubao",
    )
    assert normal_doubao == {"speed_level": "normal", "speed_ratio": 1.0, "volume_ratio": 1.0}

    doubao = normalize_tts_options(
        {"speed_level": "fast", "volume_ratio": 9, "style_prompt": "ignored"},
        {"speed_level": "normal", "volume_ratio": 1.0},
        provider="doubao",
    )
    assert doubao == {"speed_level": "fast", "speed_ratio": 1.25, "volume_ratio": 2.0}

    mimo = normalize_tts_options(
        {"speed_level": "very_slow", "style_prompt": "温柔克制"},
        {"speed_level": "normal", "style_prompt": "自然清晰"},
        provider="mimo",
    )
    assert mimo == {
        "speed_level": "very_slow",
        "style_prompt": "温柔克制",
        "speed_instruction": "语速很慢，停顿充分，保持清晰。",
    }
    assert speed_instruction("normal") == ""


def test_segment_speed_override_distinguishes_user_choice_from_legacy_snapshot():
    task_options = {"speed_level": "normal", "volume_ratio": 1.0}

    inherited, inherited_active = segment_tts_override(
        '{"speed_level":"normal","volume_ratio":1.0}',
        segment_voice_type="doubao:voice-a",
        task_voice_type="doubao:voice-a",
        task_options=task_options,
    )
    assert inherited == {}
    assert inherited_active is False

    legacy_slow, legacy_active = segment_tts_override(
        '{"speed_level":"very_slow","volume_ratio":1.0}',
        segment_voice_type="doubao:voice-a",
        task_voice_type="doubao:voice-a",
        task_options=task_options,
    )
    assert legacy_slow["speed_level"] == "very_slow"
    assert legacy_active is True

    explicit = encode_segment_tts_override({"speed_level": "normal"})
    decoded, explicit_active = segment_tts_override(
        explicit,
        segment_voice_type="",
        task_voice_type="doubao:voice-a",
        task_options=task_options,
    )
    assert decoded == {"speed_level": "normal"}
    assert explicit_active is True


def test_normalizes_multi_provider_runtime_config():
    config = {
        "tts": {
            "provider": "mimo",
            "api_url": "https://doubao.test",
            "appid": "app",
            "token": "token",
            "default_voice": "zh_male_old",
            "mimo": {"api_key": "key", "default_voice": "茉莉"},
        }
    }

    Config._normalize_tts_config(config)

    assert config["tts"]["enabled_providers"] == ["doubao", "mimo"]
    assert config["tts"]["preview_text"].startswith("欢迎来到")
    assert config["tts"]["speed_level"] == "normal"
    assert config["tts"]["volume_ratio"] == 1.0
    assert config["tts"]["mimo"]["speed_level"] == "normal"
    assert config["tts"]["mimo"]["clone_model"] == "mimo-v2.5-tts-voiceclone"


def test_fresh_database_seeds_all_presets_and_default_visibility(temp_db):
    all_voices = temp_db.list_tts_voices(include_disabled=True)
    enabled = temp_db.list_tts_voices()

    assert len(all_voices) == 19
    assert len(enabled) == 6
    assert {row["voice_id"] for row in enabled if row["provider"] == "mimo"} == set(
        MIMO_DEFAULT_ENABLED_IDS
    )
    assert {row["voice_id"] for row in enabled if row["provider"] == "doubao"} == set(
        DOUBAO_DEFAULT_ENABLED_IDS
    )
    assert all(row["id"].startswith(f'{row["provider"]}:') for row in all_voices)


def test_default_doubao_voices_match_the_two_account_granted_legacy_voices():
    voice_ids = {voice[0] for voice in DOUBAO_PRESET_VOICES}

    assert DOUBAO_DEFAULT_ENABLED_IDS == (
        "zh_female_shuangkuaisisi_moon_bigtts",
        "zh_male_jieshuoxiaoming_moon_bigtts",
    )
    assert set(DOUBAO_DEFAULT_ENABLED_IDS) <= voice_ids


def test_migrates_legacy_voice_table_without_losing_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tts_voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voice_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            gender TEXT NOT NULL,
            description TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO tts_voices (voice_id, name, gender, description, is_enabled, sort_order)
        VALUES ('legacy-voice', '旧音色', 'male', '保留我', 1, 999);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", db_path)

    db = SQLiteClient()
    rows = db.list_tts_voices(include_disabled=True)

    legacy = next(row for row in rows if row["voice_id"] == "legacy-voice")
    assert legacy["provider"] == "doubao"
    assert legacy["description"] == "保留我"
    with sqlite3.connect(db_path) as migrated:
        indexes = migrated.execute("PRAGMA index_list(tts_voices)").fetchall()
        assert indexes


def test_updates_voice_availability_by_canonical_key(temp_db):
    changed = temp_db.set_voice_availability(
        ["mimo:冰糖", "doubao:zh_female_wanwanxiaohe_moon_bigtts"]
    )

    assert changed == 19
    enabled = temp_db.list_tts_voices()
    assert {row["id"] for row in enabled} == {
        "mimo:冰糖",
        "doubao:zh_female_wanwanxiaohe_moon_bigtts",
    }
