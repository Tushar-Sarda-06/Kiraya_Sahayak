import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import streamlit as st

st.set_page_config(page_title="Kiraya Sahayak — Bengaluru Tenancy Help", page_icon="🏠", layout="wide")

# ---------- API key: Streamlit secrets (cloud) or .env (local) ----------
try:
    os.environ["GROQ_API_KEY"] = st.secrets["GROQ_API_KEY"]
except Exception:                      # no secrets file locally → use .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

import engine

@st.cache_resource(show_spinner="Loading the law library (first time takes ~1 minute)…")
def load_engine():
    return engine.init()

info = load_engine()

# ---------- Sidebar: scope and honesty ----------
with st.sidebar:
    st.title("🏠 Kiraya Sahayak")
    st.caption("Plain-language help for **residential tenancy** problems in **Bengaluru / Karnataka**.")
    st.markdown("**Covers:** deposits, eviction threats, water/electricity cut-offs, repairs, "
                "rent receipts, notices, agreements & registration, free legal aid.")
    st.markdown("**Doesn't cover:** commercial leases, property ownership disputes, criminal cases, "
                "other states, court judgments.")
    st.markdown("**Laws used:** Karnataka Rent Act 1999 (original 2001 text — later amendments not included), "
                "Transfer of Property Act ss.105–108, 111, 116 (summary), Registration Act s.17, "
                "Karnataka Stamp Act 1957, Limitation Act 1963, Legal Services Authorities Act 1987.")
    st.warning("This is not legal advice. Every answer cites the law it relies on — check it, "
               "and talk to a lawyer before acting.", icon="⚖️")
    st.info("**Free legal help:** your District Legal Services Authority (DLSA) — "
            "Karnataka State Legal Services Authority.", icon="🤝")
    if st.button("Start a new conversation"):
        st.session_state.history = []
        st.rerun()

# ---------- Rendering one answer ----------
def render_answer(r):
    a, case = r["answer"], r["intake"]
    if a.get("needs_lawyer"):
        st.error(f"**Please talk to a lawyer or DLSA.** {a.get('lawyer_reason', '')}", icon="🚨")

    st.markdown(a.get("where_you_stand", ""))
    if a.get("options"):
        st.markdown("**Your options**")
        for o in a["options"]:
            st.markdown(f"- {o}")
    if a.get("next_steps"):
        st.markdown("**Next steps**")
        for i, s in enumerate(a["next_steps"], 1):
            st.markdown(f"{i}. {s}")
    if a.get("what_i_dont_know"):
        st.caption(f"**What I don't know:** {a['what_i_dont_know']}")
    if case.get("missing_info"):
        st.caption("**Telling me these would help:** " + " · ".join(case["missing_info"]))

    if r["sources"]:
        cited = set((r.get("verification") or {}).get("cited", []))
        with st.expander(f"📚 Law text used ({len(cited)} cited of {len(r['sources'])} retrieved)"):
            for s in r["sources"]:
                label = s["section"] if s["section"].startswith("Schedule") else f"Section {s['section']}"
                tag = "✅ cited" if s["chunk_id"] in cited else "retrieved"
                kind = " · *plain-language summary, not bare Act*" if s["status"] == "commentary" else ""
                st.markdown(f"**[{s['chunk_id']}] {s['act']}, {label} — {s['title']}** ({tag}){kind}")
                st.caption(s["text"][:1200] + ("…" if len(s["text"]) > 1200 else ""))
                if s.get("source_url"):
                    st.caption(f"Source: {s['source_url']}")

    with st.expander("🔍 How I handled this (for reviewers)"):
        st.json({"topic": case.get("topic"), "language": case.get("language"),
                 "urgent_flags": case.get("urgent_flags"), "escalation": case.get("escalate"),
                 "facts": case.get("facts"), "attempts": r.get("attempts"),
                 "citation_check": r.get("verification"),
                 "fact_check": r.get("support") and {"ok": r["support"]["ok"], "problems": r["support"]["problems"]}})

# ---------- Chat ----------
if "history" not in st.session_state:
    st.session_state.history = []          # list of {"role": "user"/"assistant", "text"/"result"}

st.title("Tell me what's happening with your rental")
st.caption("Write in English, हिंदी, Hinglish or ಕನ್ನಡ. Include dates, amounts and what proof you have.")

for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["text"])
        else:
            render_answer(msg["result"])

prompt = st.chat_input("e.g. My landlord cut off water 3 days ago after I refused a rent hike…")
if prompt:
    st.session_state.history.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Follow-ups ("it was ₹40,000") need earlier context: send the last few user messages together
    earlier = [m["text"] for m in st.session_state.history if m["role"] == "user"][-3:]
    full_text = "\n".join(earlier)

    with st.chat_message("assistant"):
        with st.spinner("Reading the law and checking every citation… (about 20–30 seconds)"):
            try:
                result = engine.ask(full_text)
            except Exception as e:
                st.error(f"Something went wrong ({type(e).__name__}). Please try again in a minute.")
                st.stop()
        render_answer(result)
    st.session_state.history.append({"role": "assistant", "result": result})
