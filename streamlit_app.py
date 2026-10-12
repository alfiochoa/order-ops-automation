"""Streamlit Community Cloud entry point. Self-seeds synthetic data on first run; no network, no secrets."""
import io

import pandas as pd
import streamlit as st

from opsauto.pipeline import COLS
from opsauto.seed import Demo

st.set_page_config(page_title="Order Ops Automation", layout="wide")


@st.cache_resource(show_spinner="Seeding synthetic traffic (webhook ~60% flaky)...")
def get_demo():
    d = Demo()
    return d, d.seed(200)


demo, tally = get_demo()
p = demo.pipeline
s = p.store.stats()

st.title("Order Ops Automation")
st.caption("Webhook automation for a small online store: validate → enrich → YAML rules → SQLite/CSV → Slack/Discord alert. "
           "**Demo on synthetic data** · notifications go to a mocked webhook.")

c = st.columns(5)
c[0].metric("Events", s["total"])
c[1].metric("Notified", s["notified"])
c[2].metric("Avg risk", s["avg_risk"])
c[3].metric("Duplicates ignored", tally["duplicate"], help="Client retries with the same Idempotency-Key")
c[4].metric("Dead letters open", s["dlq_open"], delta_color="inverse")

left, right = st.columns(2)
with left:
    st.subheader("Rule flags")
    if s["flags"]:
        st.bar_chart(pd.Series(s["flags"], name="events").sort_values(ascending=False))
with right:
    st.subheader("Dead-letter queue")
    st.write("Notifications that exhausted their retries are parked here, not lost.")
    dlq = pd.DataFrame(p.store.dlq_open())
    if dlq.empty:
        st.success("Queue empty: nothing waiting for replay.")
    else:
        st.dataframe(dlq[["id", "stage", "idem_key", "attempts", "error"]], hide_index=True, height=200, width="stretch")
    if st.button("Recover webhook & replay dead letters", type="primary", disabled=dlq.empty):
        st.session_state["replay"] = demo.recover()
        st.rerun()
    if "replay" in st.session_state:
        st.info(f"Last replay: {st.session_state['replay']}")

st.subheader("Idempotency: send the same order twice")
if st.button("POST order with Idempotency-Key: demo-1 (twice)"):
    order = {"order_id": "A1", "email": "x@acme.test", "payment_method": "cod",
             "items": [{"sku": "S", "qty": 1, "price": 600}],
             "billing": {"line1": "1 Main", "city": "austin", "country": "USA"},
             "shipping": {"line1": "1 Main", "city": "lagos", "country": "Nigeria"}}
    first, second = demo.post("order", order, "demo-1").json(), demo.post("order", order, "demo-1").json()
    st.json({"first call": first, "second call (same key)": second})

st.subheader("Recent events")
st.dataframe(pd.DataFrame(s["recent"]), hide_index=True, width="stretch")
with st.expander("Audit trail (append-only)"):
    st.dataframe(pd.DataFrame(p.store.audit_tail(15))[["ts", "actor", "action", "idem_key"]], hide_index=True, width="stretch")

rows = list(reversed(p.store.events()))
buf = io.StringIO()
pd.DataFrame(rows)[COLS].to_csv(buf, index=False)
st.download_button("Download events (CSV)", buf.getvalue(), "orders.csv", "text/csv")
