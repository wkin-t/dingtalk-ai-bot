from app.gemini_client import _convert_openai_to_gemini


def test_string_system_extracted():
    msgs = [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_openai_to_gemini(msgs)
    assert sys_inst == "you are helpful"


def test_list_blocks_system_concatenated():
    blocks = [
        {"type": "text", "text": "stable"},
        {"type": "text", "text": "dynamic"},
    ]
    msgs = [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_openai_to_gemini(msgs)
    assert "stable" in sys_inst
    assert "dynamic" in sys_inst
    assert sys_inst == "stable\n\ndynamic"


def test_no_system_returns_none():
    msgs = [{"role": "user", "content": "hi"}]
    sys_inst, contents = _convert_openai_to_gemini(msgs)
    assert sys_inst is None
