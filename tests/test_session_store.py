import pytest

from pico.state_store import StateStore


def test_sessions_save_load_and_latest(tmp_path):
    store=StateStore(tmp_path)
    assert store.latest_session() is None
    store.save_session({'id':'first','history':[]})
    store.save_session({'id':'second','history':[{'role':'user','content':'hello'}]})
    assert store.latest_session()=='second'
    assert store.load_session('second')['history'][0]['content']=='hello'


def test_serialization_failure_preserves_prior_session(tmp_path):
    store=StateStore(tmp_path)
    store.save_session({'id':'atomic','history':['original']})
    with pytest.raises(TypeError):store.save_session({'id':'atomic','history':[object()]})
    assert store.load_session('atomic')['history']==['original']
