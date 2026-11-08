"""AI Lawyer engine: retrieval + intake + advisor + verifier + fact-check.
Same logic as notebook Cells 10, 14, 16-20. The index is built in the notebook; this only loads it."""
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import re, json, time, collections
from datetime import datetime
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi
from groq import Groq, BadRequestError

BASE_DIR = Path(__file__).parent
INDEX_DIR = BASE_DIR / "data" / "index"
if not (INDEX_DIR / "chunks.jsonl").exists():      # files uploaded flat at repo root
    INDEX_DIR = BASE_DIR

# ======================================================================
# Retrieval (Cell 10)
# ======================================================================
def tokenize(text):
    return re.findall(r"\w+", text.lower())

class LawRetriever:
    def __init__(self, index_dir=INDEX_DIR, model=None):
        self.chunks = [json.loads(l) for l in open(index_dir / "chunks.jsonl", encoding="utf-8")]
        self.by_id = {c["chunk_id"]: c for c in self.chunks}
        cfg = json.load(open(index_dir / "config.json"))
        if model is None:
            from sentence_transformers import SentenceTransformer
            import torch
            torch.set_num_threads(1)                      # single-threaded: stable on macOS
            model = SentenceTransformer(cfg["embed_model"], device="cpu")
        self.model = model
        # Plain numpy instead of faiss: ~500 vectors, so a matrix product is instant,
        # and it avoids the faiss/torch OpenMP clash that crashes Python on macOS.
        self.emb = np.load(index_dir / "embeddings.npy").astype("float32")
        self.bm25 = BM25Okapi([tokenize(c["embed_text"]) for c in self.chunks])

    def search(self, query, k=6, pool=50, acts=None, include_reference=False, rrf_k=60):
        q = self.model.encode(["query: " + query], normalize_embeddings=True).astype("float32")
        scores = self.emb @ q[0]
        dense_ids = [int(i) for i in np.argsort(-scores)[:pool]]
        bm = self.bm25.get_scores(tokenize(query))
        sparse_ids = [int(i) for i in np.argsort(-bm)[:pool] if bm[i] > 0]
        fused = collections.defaultdict(float)
        for r, i in enumerate(dense_ids):
            fused[i] += 1 / (rrf_k + r + 1)
        for r, i in enumerate(sparse_ids):
            fused[i] += 1 / (rrf_k + r + 1)
        results = []
        for i, s in sorted(fused.items(), key=lambda x: -x[1]):
            c = self.chunks[i]
            if acts and c["act_code"] not in acts:
                continue
            if not include_reference and c["status"] == "reference_only":
                continue
            cos = float(scores[i])
            results.append({**c, "rrf": round(s, 5), "dense": round(cos, 4)})
            if len(results) == k:
                break
        return results

retriever = None          # set by init()
KNOWN_SECTIONS = collections.defaultdict(set)

# ======================================================================
# Section-existence check (Cell 14)
# ======================================================================
ACT_ALIASES = {
    "KRA": ["rent act", "karnataka rent"],
    "TPA": ["transfer of property", "tpa"],
    "REG": ["registration act"],
    "KSA": ["stamp act"],
    "LIM": ["limitation act"],
    "LSA": ["legal services"],
}
SECTION_REF_RE = re.compile(r"\b(?:section|sec\.?|s\.)\s*(\d{1,3}[A-Z]?)\b", re.I)

def check_section_refs(text):
    low = text.lower()
    acts = [code for code, names in ACT_ALIASES.items() if any(n in low for n in names)]
    results = []
    for m in SECTION_REF_RE.finditer(text):
        sec = m.group(1).upper()
        targets = acts or list(ACT_ALIASES)
        found = [a for a in targets if sec in KNOWN_SECTIONS[a]]
        results.append({"section": sec, "acts_checked": targets, "in_corpus": found})
    return results

# ======================================================================
# LLM client (Cell 16)
# ======================================================================
client = None
MODEL_BIG = MODEL_SMALL = None
PRICES = {"openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30)}
USAGE_LOG = []

def _pick(available, preferences):
    for p in preferences:
        if p in available:
            return p
    raise ValueError(f"None of {preferences} available on Groq: {available}")

def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in model output: {text[:200]}")
    return json.loads(m.group(0))

def llm(messages, model, json_mode=False, temperature=0.2, max_tokens=3000, retries=3, tag=""):
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    for attempt in range(retries):
        try:
            t0 = time.time()
            r = client.chat.completions.create(**kwargs)
            break
        except BadRequestError:
            if "response_format" in kwargs and attempt < retries - 1:
                kwargs.pop("response_format")
                continue
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(4 * (2 ** attempt))
    u = r.usage
    pin, pout = PRICES.get(model, (0, 0))
    USAGE_LOG.append({"tag": tag, "model": model, "in_tok": u.prompt_tokens, "out_tok": u.completion_tokens,
                      "secs": round(time.time() - t0, 2),
                      "usd": (u.prompt_tokens * pin + u.completion_tokens * pout) / 1e6})
    text = r.choices[0].message.content or ""
    return parse_json(text) if json_mode else text

# ======================================================================
# Intake + rules (Cell 17)
# ======================================================================
TOPICS = ["deposit_refund", "eviction_threat", "essential_services_cut", "repairs",
          "rent_increase", "rent_payment_or_receipts", "agreement_registration_stamp",
          "notice_to_vacate", "holding_over", "legal_aid", "other_tenancy", "out_of_scope"]
FLAGS = ["violence_or_threat", "locked_out_or_belongings_removed", "court_or_police_notice",
         "deadline_within_7_days", "commercial_premises", "outside_karnataka"]

INTAKE_SYSTEM = f"""You are the intake step of a help tool for RESIDENTIAL tenancy problems in Bengaluru / Karnataka, India.
Read the user's message and extract facts. Do NOT give legal advice. Never invent dates, years, amounts or facts:
use null or empty lists when something is not stated. Write dates exactly as precise as the user gave them.
Return ONLY a JSON object with these keys:
- "language": "en", "hi" (Hindi in Devanagari), "hinglish" (Hindi in Roman letters), "kn", or "other"
- "topic": one of {TOPICS}
- "in_scope": true only if this is a residential landlord-tenant matter (Karnataka or location unstated)
- "role": "tenant", "landlord" or "unknown"
- "english_summary": 1-3 neutral English sentences describing the problem and what the user wants
- "facts": {{"timeline": [{{"date": str|null, "event": str}}], "amounts_inr": [{{"what": str, "amount": number}}],
             "evidence": [str], "agreement": "written registered" | "written unregistered" | "oral" | null}}
- "missing_info": up to 3 short questions whose answers would change the advice
- "urgent_flags": subset of {FLAGS}"""

RED_FLAG_RULES = {
    "violence_or_threat": r"\b(threat\w*|beat\w*|hit me|assault\w*|violen\w*|goons?|dhamki|maar\w*)\b",
    "locked_out_or_belongings_removed": r"\b(locked (me )?out|changed the locks?|threw (out )?my|removed my (things|belongings))\b",
    "court_or_police_notice": r"\b(summons|court notice|fir|police|legal notice|lawyer'?s? notice|vakil)\b",
}
LAWYER_FLAGS = {"violence_or_threat", "locked_out_or_belongings_removed", "court_or_police_notice"}
LARGE_AMOUNT_INR = 500_000

def fix_invented_years(case, user_text):
    for ev in (case.get("facts") or {}).get("timeline") or []:
        d = ev.get("date") or ""
        m = re.match(r"((?:19|20)\d{2})-(\d{2})-(\d{2})$", d)
        if m and m.group(1) not in user_text:
            ev["date"] = datetime.strptime(d, "%Y-%m-%d").strftime("%d %B") + " (year not stated)"

def guess_language(text):
    if re.search(r"[\u0C80-\u0CFF]", text): return "kn"
    if re.search(r"[\u0900-\u097F]", text): return "hi"
    return "en"

def default_case(user_text):
    return {"language": guess_language(user_text), "topic": "other_tenancy", "in_scope": True, "role": "unknown",
            "english_summary": user_text, "facts": {"timeline": [], "amounts_inr": [], "evidence": [], "agreement": None},
            "missing_info": [], "urgent_flags": [], "intake_failed": True}

def intake(user_text):
    try:
        data = llm([{"role": "system", "content": INTAKE_SYSTEM},
                    {"role": "user", "content": user_text}], MODEL_SMALL, json_mode=True, tag="intake")
        if not isinstance(data, dict) or "topic" not in data:
            raise ValueError("intake JSON missing keys")
    except Exception:
        data = default_case(user_text)
    fix_invented_years(data, user_text)
    flags = set(data.get("urgent_flags") or [])
    for flag, pattern in RED_FLAG_RULES.items():
        if re.search(pattern, user_text, re.I):
            flags.add(flag)
    data["urgent_flags"] = sorted(flags)
    data["section_refs"] = check_section_refs(user_text)
    amounts = [a.get("amount") or 0 for a in (data.get("facts") or {}).get("amounts_inr") or []]
    reasons = [f for f in data["urgent_flags"] if f in LAWYER_FLAGS]
    if amounts and max(amounts) >= LARGE_AMOUNT_INR:
        reasons.append(f"amount at stake ≥ ₹{LARGE_AMOUNT_INR:,}")
    data["escalate"] = {"needs_lawyer": bool(reasons), "reasons": reasons}
    return data

# ======================================================================
# Advisor (Cell 18)
# ======================================================================
LANG_NAMES = {"en": "English", "hi": "Hindi (Devanagari)",
              "hinglish": "Hinglish (Hindi in Roman letters, exactly as the user writes)",
              "kn": "Kannada", "other": "English"}

ADVISOR_SYSTEM = """You help ordinary people in Bengaluru understand residential tenancy problems.
STRICT RULES:
1. Use ONLY the SOURCES given. Every legal statement must end with its source ID in square brackets, e.g. [KRA-s49-p1].
2. Never mention any Act, section, schedule, rule, court case or judgment that is not in the SOURCES. Do not cite case law at all.
3. If the SOURCES do not answer the question, say so plainly in "what_i_dont_know". Never fill gaps from memory.
4. Sources marked "summary" are plain-language summaries, not the bare Act: say "(summary)" when you rely on them.
5. Remind the user that their rent agreement's terms matter and the Act may not apply to every rental.
6. Plain words, short sentences, no legal jargon. Write the answer in: {language}.
7. Many sections apply only in specific situations (e.g. only after a court eviction order, only to rent paid in advance).
   Before relying on a section, check its conditions against the user's facts. If the conditions don't match,
   do not use that section; say instead that the laws provided don't directly cover the situation.
Return ONLY a JSON object:
{{"where_you_stand": str, "options": [str], "next_steps": [str], "what_i_dont_know": str,
  "needs_lawyer": bool, "lawyer_reason": str}}"""

def format_sources(chunks):
    blocks = []
    for c in chunks:
        label = c["section"] if c["section"].startswith("Schedule") else f"Section {c['section']}"
        kind = " (summary, not bare text)" if c["status"] == "commentary" else ""
        blocks.append(f"[{c['chunk_id']}] {c['act']}, {label}{kind}: {c['title']}\n{c['text']}")
    return "\n\n".join(blocks)

def advise(user_text, case, sources, feedback=None):
    msgs = [{"role": "system", "content": ADVISOR_SYSTEM.format(language=LANG_NAMES.get(case.get("language"), "English"))},
            {"role": "user", "content":
                f"USER MESSAGE:\n{user_text}\n\nEXTRACTED FACTS:\n{json.dumps(case.get('facts'), ensure_ascii=False)}\n\n"
                f"SOURCES:\n{format_sources(sources)}"}]
    if feedback:
        msgs.append({"role": "user", "content": f"Your previous answer failed checks: {feedback}\n"
                                                 "Rewrite it following the rules exactly. Remove or correct the failing claims."})
    return llm(msgs, MODEL_BIG, json_mode=True, temperature=0.1, tag="advise")

# ======================================================================
# Verifier + support checker + fallback (Cell 19)
# ======================================================================
ORD = "FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH"
ORD_NUM = {w: i + 1 for i, w in enumerate(ORD.split("|"))}
ID_GROUP_RE = re.compile(r"\[([A-Z]{3}-[^\]]+)\]")
ID_RE = re.compile(r"[A-Z]{3}-(?:s\d{1,3}[A-Z]{0,3}|sch\d+)(?:-p\d+)?")
SEC_MENTION_RE = re.compile(r"\b(?:section|sec\.?|s\.)\s*(\d{1,3}[A-Z]?)\b", re.I)
SCHED_MENTION_RE = re.compile(rf"\b({ORD})\s+schedule\b", re.I)
CASE_LAW_RE = re.compile(r"\b(v\.|vs\.?|versus)\s+[A-Z]|\bAIR\s+\d{4}|\(\d{4}\)\s*\d+\s*SCC")

def answer_text(ans):
    return " ".join([ans.get("where_you_stand", ""), *ans.get("options", []),
                     *ans.get("next_steps", []), ans.get("what_i_dont_know", "")])

def verify(ans, sources):
    text = answer_text(ans)
    allowed_ids = {c["chunk_id"] for c in sources}
    allowed_secs = {c["section"] for c in sources}
    problems = []
    cited = {i for g in ID_GROUP_RE.findall(text) for i in ID_RE.findall(g)}
    if bad := sorted(cited - allowed_ids):
        problems.append(f"cites IDs not in SOURCES: {bad}")
    if not cited and ans.get("where_you_stand"):
        problems.append("no source IDs cited")
    if bad := sorted({s.upper() for s in SEC_MENTION_RE.findall(text)} - allowed_secs):
        problems.append(f"mentions sections not in SOURCES: {bad}")
    if bad := sorted({w.upper() for w in SCHED_MENTION_RE.findall(text)
                      if f"Schedule {ORD_NUM[w.upper()]}" not in allowed_secs}):
        problems.append(f"mentions schedules not in SOURCES: {bad}")
    if CASE_LAW_RE.search(text):
        problems.append("appears to cite case law")
    return {"ok": not problems, "problems": problems, "cited": sorted(cited)}

SUPPORT_SYSTEM = """You are a strict legal fact-checker. You get the user's FACTS, the SOURCES (law text),
and numbered CLAIMS from an answer, each with the source IDs it cites. For each claim decide:
- "supported": the cited source says this AND its conditions fit the user's facts
- "conditions_not_met": the source says something similar, but only for a situation that does not match
   the user's facts (e.g. applies only when the landlord recovers possession through court)
- "unsupported": the cited source does not say this
Return ONLY JSON: {"claims": [{"i": int, "verdict": str, "reason": str}]}"""

def split_claims(ans):
    pieces = (re.split(r"(?<=[.!?।\]])\s+", ans.get("where_you_stand", ""))
              + ans.get("options", []) + ans.get("next_steps", []))
    claims = []
    for p in pieces:
        ids = [i for g in ID_GROUP_RE.findall(p) for i in ID_RE.findall(g)]
        if ids:
            claims.append({"text": p.strip(), "ids": ids})
    return claims

def check_support(ans, sources, case):
    claims = split_claims(ans)
    if not claims:
        return {"ok": True, "problems": [], "verdicts": []}
    cited = {i for c in claims for i in c["ids"]}
    src = [s for s in sources if s["chunk_id"] in cited]
    payload = (f"FACTS:\n{json.dumps(case.get('facts'), ensure_ascii=False)}\n"
               f"USER SUMMARY: {case.get('english_summary')}\n\nSOURCES:\n{format_sources(src)}\n\nCLAIMS:\n" +
               "\n".join(f"{n}. {c['text']}  (cites {c['ids']})" for n, c in enumerate(claims)))
    out = llm([{"role": "system", "content": SUPPORT_SYSTEM}, {"role": "user", "content": payload}],
              MODEL_BIG, json_mode=True, temperature=0, tag="support")
    verdicts = out.get("claims", [])
    problems = [f"claim {v['i']} ('{claims[v['i']]['text'][:70]}…') is {v['verdict']}: {v['reason']}"
                for v in verdicts
                if v.get("verdict") != "supported" and isinstance(v.get("i"), int) and 0 <= v["i"] < len(claims)]
    return {"ok": not problems, "problems": problems, "verdicts": verdicts}

def safe_fallback(sources, lang):
    seen, lines = set(), []
    for c in sources:
        key = (c["act_code"], c["section"])
        if key in seen:
            continue
        seen.add(key)
        label = c["section"] if c["section"].startswith("Schedule") else "Section " + c["section"]
        lines.append(f"- {c['act']}, {label}: {c['title']}")
        if len(lines) == 4:
            break
    return {"where_you_stand": "I couldn't produce an answer I can fully back up with the law I have. "
                               "These provisions may be relevant — please read them or show them to a lawyer:\n" + "\n".join(lines),
            "options": [], "next_steps": ["Contact your District Legal Services Authority (DLSA) for free legal advice."],
            "what_i_dont_know": "A verified explanation for your specific situation.",
            "needs_lawyer": True, "lawyer_reason": "Automatic answer failed verification."}

# ======================================================================
# Pipeline (Cell 20)
# ======================================================================
OUT_OF_SCOPE_MSG = ("I can only help with residential tenancy problems in Bengaluru/Karnataka "
                    "(deposits, eviction, repairs, rent, notices, agreements). For other legal issues, "
                    "your District Legal Services Authority (DLSA) offers free legal advice.")

def section_not_found_answer(case, query):
    refs = case["section_refs"]
    secs = ", ".join(r["section"] for r in refs)
    cover = []
    for code in refs[0]["acts_checked"]:
        nums = sorted(int(re.match(r"\d+", s).group()) for s in KNOWN_SECTIONS[code] if re.match(r"\d+", s))
        if nums:
            cover.append(f"{code}: sections {nums[0]}–{nums[-1]}")
    seen, opts = set(), []
    for t in retriever.search(query, k=6):
        key = (t["act_code"], t["section"])
        if key in seen or t["section"].startswith("Schedule"):
            continue
        seen.add(key)
        opts.append(f"Did you mean {t['act']}, Section {t['section']}: {t['title']}?")
        if len(opts) == 3:
            break
    return {"where_you_stand": f"I couldn't find Section {secs} in the laws I have ({'; '.join(cover)}), "
                               "so I won't guess what it says.",
            "options": opts,
            "next_steps": ["Check the section number on your notice or document and ask again."],
            "what_i_dont_know": f"The text of Section {secs}.", "needs_lawyer": False, "lawyer_reason": ""}

def ask(user_text, k=6):
    refs = check_section_refs(user_text)                 # fake section → answered by code, no LLM at all
    if refs and all(not r["in_corpus"] for r in refs):
        case = {**default_case(user_text), "intake_failed": False, "topic": None,
                "section_refs": refs, "escalate": {"needs_lawyer": False, "reasons": []}}
        return {"input": user_text, "intake": case, "sources": [], "verification": None, "support": None,
                "attempts": 0, "answer": section_not_found_answer(case, user_text)}
    case = intake(user_text)
    result = {"input": user_text, "intake": case, "sources": [], "answer": None,
              "verification": None, "support": None, "attempts": 0}

    if not case.get("in_scope", True) or case.get("topic") == "out_of_scope":
        result["answer"] = {"where_you_stand": OUT_OF_SCOPE_MSG, "options": [], "next_steps": [],
                            "what_i_dont_know": "", "needs_lawyer": False, "lawyer_reason": ""}
        return result

    query = case.get("english_summary") or user_text
    sources = retriever.search(query, k=k)
    result["sources"] = sources
    feedback, check, support = None, None, None
    for attempt in range(2):
        result["attempts"] = attempt + 1
        ans = advise(user_text, case, sources, feedback)
        check = verify(ans, sources)
        support = check_support(ans, sources, case) if check["ok"] else None
        if check["ok"] and support["ok"]:
            break
        feedback = "; ".join(check["problems"] + (support["problems"] if support else []))
    else:
        ans = safe_fallback(sources, case.get("language"))

    if case["escalate"]["needs_lawyer"]:
        ans["needs_lawyer"] = True
        ans["lawyer_reason"] = "; ".join(case["escalate"]["reasons"]) + ". " + ans.get("lawyer_reason", "")
    result.update(answer=ans, verification=check, support=support)
    return result

# ======================================================================
# Startup
# ======================================================================
def init(api_key=None):
    """Load the index + embedding model and connect to Groq. Call once (cached by Streamlit)."""
    global retriever, client, MODEL_BIG, MODEL_SMALL
    retriever = LawRetriever(INDEX_DIR)
    KNOWN_SECTIONS.clear()
    for c in retriever.chunks:
        KNOWN_SECTIONS[c["act_code"]].add(c["section"])
    client = Groq(api_key=api_key or os.environ.get("GROQ_API_KEY"))
    available = sorted(m.id for m in client.models.list().data)
    MODEL_BIG = _pick(available, ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"])
    MODEL_SMALL = _pick(available, ["openai/gpt-oss-20b", "llama-3.1-8b-instant", MODEL_BIG])
    return {"chunks": len(retriever.chunks), "model_big": MODEL_BIG, "model_small": MODEL_SMALL}
