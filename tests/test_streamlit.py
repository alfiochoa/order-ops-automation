from streamlit.testing.v1 import AppTest

from opsauto.seed import Demo


def test_demo_seed_then_replay_leaves_no_dead_letters():
    d = Demo()
    tally = d.seed(60, 3)
    assert tally["duplicate"] + tally["rejected"] > 0
    assert d.pipeline.store.stats()["dlq_open"] > 0
    out = d.recover()
    assert out["failed"] == 0 and d.pipeline.store.stats()["dlq_open"] == 0


def test_streamlit_app_renders_without_exceptions():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60).run()
    assert not at.exception
    assert [m.label for m in at.metric][:2] == ["Events", "Notified"]
