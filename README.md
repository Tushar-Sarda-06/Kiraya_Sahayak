# Kiraya Sahayak — an AI helper for Bengaluru tenancy problems

A chat assistant for ordinary tenants in Bengaluru. You describe your problem in English, Hindi, Hinglish or Kannada; it tells you where you stand under the actual text of the law, what your options are, what it doesn't know, and when you need a real lawyer.

**Live app:** https://kirayasahayak-gchckksxu2s4vcza5wdwtl.streamlit.app/  ·  **Build notebook:** `Cellwise_KukuFM.ipynb`  ·  **Test cases + raw outputs:** `eval_*.jsonl / *.csv`

---

## 1. Scope

**Covers (residential tenancy, Karnataka):** water/electricity cut-offs, eviction threats and grounds, repairs (who pays for what), rent receipts, landlord refusing rent, notice periods, holding over, agreement registration and stamp duty, free legal aid.

**Does not cover:** commercial leases, ownership/title disputes, criminal matters, other states, court judgments/case law. Out-of-scope questions are refused and pointed to the District Legal Services Authority (DLSA).

**Laws indexed** (details and provenance in `data/sources.md`): Karnataka Rent Act 1999 (70 sections + schedules), Transfer of Property Act 1882 ss.105–108, 111, 116 (plain-language summary — marked as such in every answer), Registration Act 1908 s.17, Karnataka Stamp Act 1957, Limitation Act 1963, Legal Services Authorities Act 1987. Model Tenancy Act 2021 is indexed but excluded from answers (not adopted law).

## 2. How it works — what is AI, what is code, where humans come in

```
message → [fake-section check] → [intake] → [red-flag rules] → [retrieval] → [answer] → [citation check] → [fact-check] → reply
             code                  AI          code                code          AI         code              AI
```

| Step | Done by | Why |
|---|---|---|
| "Does Section N exist?" | **Code** — checked against the indexed sections before any AI call | Models invent sections (our intake model once wrote a whole fake "Section 999") |
| Story → facts, timeline, language, topic | AI (gpt-oss-20b) | Messy multilingual text |
| Invented-date guard | **Code** — removes years the user never wrote | Limitation periods depend on dates |
| Threats, lockouts, court/police notices, amounts ≥ ₹5 lakh → "see a lawyer" | **Code rules** (+ AI flags), code has the final say | Safety must not depend on the model noticing |
| Finding the law | Hybrid search: multilingual embeddings + keyword (BM25) | Works across English / Hindi / Kannada |
| Plain-language answer | AI (gpt-oss-120b), may only use retrieved text, must cite chunk IDs like `[KRA-s49-p1]` | Grounding |
| Citation check | **Code** — every cited ID / section / schedule must be in the retrieved text; no case law | Catches invented citations |
| Fact-check | AI — does the cited section actually apply *to these facts*? | Catches real sections wrongly applied (see §4) |
| Fallback | **Code** — if checks fail twice, show the relevant provisions + DLSA instead of an answer | Never ship an unverified legal claim |
| Human in the loop | User sees the exact law text behind every answer; lawyer/DLSA banner on red flags; manual audit of answers during evaluation | The tool prepares; people decide |

## 3. How it avoids inventing law

1. **Closed corpus.** Only statute text we parsed and checked; no case law.
2. **Source checking before indexing.** Two of our first seven PDFs were wrong — a *repealed* 1961 Rent Act and a *Bangladesh* edition of the TPA — plus study notes posing as statute and an unamended 1882 scan. Parse diagnostics (section counts, gaps, titles) caught them.
3. **Cite-by-ID + code verification.** The model cites chunk IDs; code rejects any ID, section or schedule not retrieved, and any case-law pattern.
4. **Applicability fact-check.** A second model call checks each cited claim against the user's facts.
5. **Fake-section guard in code** before any model runs.
6. **Visible evidence.** Every answer shows the retrieved law text, with cited sections marked.

## 4. Evaluation (27 test cases, raw outputs in `eval_*.jsonl / *.csv`)

Test set: 11 grounded questions, 3 coverage gaps / myths, 4 escalation cases, 2 fake sections, 3 out-of-scope, 4 multilingual (Hindi, Kannada, Hinglish). Each has gold labels and an expected-behaviour sentence.

| Layer | Metric | Result |
|---|---|---|
| **Safety** | Answers with invented section numbers / case law / forbidden claims | **0% / 0% / 0%** |
| | Citations pointing to retrieved text | **100%** |
| | Correct section cited (when it answers) | **100%** |
| | Fake sections handled without AI · out-of-scope refused | **100% · 100%** |
| | Escalation precision / recall (code rules) | **100% / 100%** |
| Understanding | Topic · language · scope accuracy | 100% · 100% · 100% |
| Retrieval | hit@1 · hit@5 · MRR (realistic queries) | 67% · 94% · 0.79 |
| Language | Replied in user's script | 95% |
| Answer quality (LLM judge, 1–5) | faithfulness · helpfulness · clarity · honesty | 3.69 · 3.38 · 3.85 · 4.54 |
| | Expected-behaviour pass rate | 54% |
| **Helpfulness** | Fell back to "can't verify" | **52%** |
| Consistency (3 runs) | same mode · cited-section overlap | 60% · 0.67 |
| Operations | latency p50 / p95 · cost per conversation | 44 s / 90 s · $0.002 |

**Manual checks against the Act text** (examples): water cut-off answer — s.49(1) "just and sufficient cause", s.49(5)(b) compensation up to ₹50, s.54(ix) penalty — all correct. A deposit question first produced "refund within 90 days with 12% interest" citing s.28(2) — a real section, but it applies only after court-ordered eviction. The fact-checker caught it; that case is why the fact-check layer exists.

### Honest failures
- **Too cautious.** The fact-checker sometimes rejects valid claims (e.g. "you can apply to the Controller" because the user hasn't applied yet). With all-or-nothing fallback, 52% of questions get "can't verify" instead of an answer. Safe, but often unhelpful. *Designed fix (not shipped):* drop only failing claims and recalibrate the fact-check prompt.
- **Over-escalation.** The lawyer banner appears on 84% of non-urgent questions (fallbacks force it).
- **Inconsistency.** The same question can pass on one run and fall back on the next.
- **Language gap.** The fallback message is fixed English text, so one Kannada user got an English reply. Kannada retrieval also has the lowest similarity scores — non-English users are more likely to get a weaker answer.
- **Coverage gap.** No indexed section governs deposit refunds after voluntary move-out; the bot says so rather than guessing.
- **Engineering:** an intake JSON failure crashed the eval run (fixed: code check before intake + safe defaults); faiss + torch OpenMP clash crashed the app on macOS (fixed: numpy search — equivalent at 450 chunks).
- **Evaluation limits.** 27 cases written by the builder; LLM judge is the same model family as the answerer (self-preference bias possible).

## 5. Cost and scale

~3.3 LLM calls and ~7,500 tokens per conversation → **≈ $0.002 per conversation** at Groq paid rates (≈ $200 per 100,000 conversations). Retrieval runs locally; embeddings are computed once. Free tier caps daily tokens per model (hit after ~30 conversations during testing) — real use needs the paid tier. Latency (≈45 s) is dominated by the answer + fact-check calls; lower reasoning effort or a single combined call would cut it.

 “Live demo runs on Groq’s free tier; heavy use may hit the daily limit.”

## 6. Limitations
- Rent Act text is the original 2001 version — later amendments (e.g. 2025 decriminalisation of offences) are not reflected.
- TPA is covered by a plain-language summary, not the bare Act.
- Not legal advice; no case law; single-turn reasoning (the app passes the last few messages for context).

## 7. Designed, not built
Legal-notice drafter · voice input (Whisper on Groq) · practice mode (role-play the landlord) · deadline calculator · claim-level pruning.

## 8. Run locally
```bash
pip install -r requirements.txt
echo 'GROQ_API_KEY=your_key' > .env
streamlit run app.py
```
Optional: `MODEL_BIG=llama-3.3-70b-versatile streamlit run app.py` to switch models when a free quota runs out.
