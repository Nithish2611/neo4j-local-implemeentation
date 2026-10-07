"""Word-level matching for task-to-code retrieval. Dependency-free: the hooks import it on every prompt.

This is LEXICAL matching on split identifier words (`chargeInvoice` -> charge, invoice), not semantic
understanding: "payment" will not find `charge_invoice` unless the words also appear in the code.
"""
import math
import re

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Words that carry no code meaning in a task description.
STOPWORDS = frozenset("""
a an and are as at be been but by can could do does did for from had has have how i if in into is it its
me my no not of on or our please should so than that the their them then there these this to too up us was
we were what when where which who why will with would you your also just need needs want fix bug bugs add
change changes make update updated use using get set code function functions file files project issue
problem error errors wrong work works working implement feature check look find show tell explain
rename variable variables something clearer better simpler clean cleaner thing things value values really
maybe actually sometimes anything everything whatever name names
""".split())


def stem(word: str) -> str:
    """A deliberately small suffix stripper, applied the same way to code words and task words, so
    `charging`, `charge` and `charges` share one form (`charg`). It is not a linguistic stemmer."""
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def split_words(text: str) -> list[str]:
    """`chargeInvoice`, `charge_invoice`, `HTTPServer2` -> lower-case words (not stemmed)."""
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", text):
        words += [m.group(0).lower() for m in _CAMEL.finditer(part)]
    return [w for w in words if len(w) >= 2]


def split_identifier(text: str) -> list[str]:
    """Stemmed words of an identifier or path, for matching against task terms."""
    return [stem(w) for w in split_words(text)]


def task_terms(text: str) -> set[str]:
    """Meaningful stemmed words of a plain-language task: split identifiers, drop stopwords and short words."""
    out: set[str] = set()
    for token in _WORD.findall(text):
        for word in split_words(token) or [token.lower()]:
            if len(word) >= 3 and word not in STOPWORDS:  # stopwords are checked before stemming
                out.add(stem(word))
    return out


def search_query(text: str) -> str:
    """Full-text query for the task: the original words AND their stems, OR-ed. The index holds stems
    (search_text) and original identifiers (raw code), so both forms are needed to match either.
    Only [a-z0-9] tokens are used, so no Lucene escaping is needed."""
    forms: set[str] = set()
    for token in _WORD.findall(text):
        for word in split_words(token) or [token.lower()]:
            if len(word) >= 3 and word not in STOPWORDS:
                forms.add(word)
                forms.add(stem(word))
    return " OR ".join(sorted(f for f in forms if f.isalnum()))[:400]


def score(terms: set[str], name_terms: set[str], path_terms: set[str], text_terms: set[str]) -> dict:
    name_hits = terms & name_terms
    path_hits = (terms & path_terms) - name_hits
    text_hits = (terms & text_terms) - name_hits - path_hits
    distinct = len(name_hits | path_hits | text_hits)
    value = 3 * len(name_hits) + 2 * len(path_hits) + len(text_hits)
    if not value:
        return {"score": 0, "accepted": False}
    # Two different task words must agree, or one long distinctive name word. Evidence that is only body
    # text needs three different words: a common pair ("invoice", "retry") appears in plenty of unrelated code.
    text_only = not name_hits and not path_hits
    accepted = (distinct >= 3 if text_only else distinct >= 2) or any(len(w) >= 6 for w in name_hits)
    why = []
    if name_hits:
        why.append("name: " + ", ".join(sorted(name_hits)))
    if path_hits:
        why.append("path: " + ", ".join(sorted(path_hits)))
    if text_hits:
        why.append("text: " + ", ".join(sorted(text_hits)[:4]))
    strong = bool(name_hits) and distinct >= 2
    return {"score": value, "accepted": accepted, "strong": strong, "why": "; ".join(why),
            "distinct": distinct}


def is_test_file(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return path.replace("\\", "/").startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


# Relative full-text thresholds: a candidate's full-text score divided by the best candidate's score.
# Body-only evidence (the words appear only in function source) must clear BODY_MIN_RELATIVE; evidence from a
# function or file name must clear NAME_MIN_RELATIVE. Chosen with benchmarks/run_retrieval_benchmark.py.
BODY_MIN_RELATIVE = 0.0
NAME_MIN_RELATIVE = 0.0
_BM25_K1, _BM25_B = 1.2, 0.75


def bm25_relative(query: set[str], docs: list[list[str]]) -> list[float]:
    """BM25 over the candidate rows, scaled so the best candidate is 1.0. Used when the full-text index score is
    not available (offline benchmark); the database path passes Neo4j's own score in `row["ft"]`."""
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    avg = sum(len(d) for d in docs) / n or 1.0
    df = {q: sum(1 for d in docs if q in d) for q in query}
    raw = []
    for d in docs:
        total = 0.0
        for q in query:
            tf = d.count(q)
            if tf and df[q]:
                idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
                total += idf * tf * (_BM25_K1 + 1) / (tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(d) / avg))
        raw.append(total)
    best = max(raw) or 1.0
    return [r / best for r in raw]


def rank(terms: set[str], rows: list[dict], limit: int, body_min: float | None = None,
         name_min: float | None = None) -> list[dict]:
    """Rank candidate rows (keys: file, name, code, optional ft = full-text score). Labels each kept row `strong`,
    `weak` or `ambiguous`.

    Evidence is first counted from words (name > path > body). Then each candidate's full-text score, relative to
    the best candidate, must clear a threshold: BODY_MIN_RELATIVE for body-only evidence, NAME_MIN_RELATIVE
    otherwise. `ambiguous` means the runner-up is within 20% of the best score."""
    body_min = BODY_MIN_RELATIVE if body_min is None else body_min
    name_min = NAME_MIN_RELATIVE if name_min is None else name_min
    asks_about_tests = "test" in terms  # test code is only relevant when the task is about tests
    candidates = []
    for row in rows:
        if not asks_about_tests and is_test_file(row["file"]):
            continue
        name_terms = set(split_identifier(row["name"]))
        path_terms = set(split_identifier(row["file"].replace("/", " ").replace(".", " ")))
        text_terms = set(split_identifier(row.get("code") or ""))
        result = score(terms, name_terms, path_terms, text_terms)
        if result["accepted"]:
            candidates.append((row, result, name_terms | path_terms | text_terms))
    if not candidates:
        return []
    if all("ft" in row for row, _, _ in candidates):
        best = max(row["ft"] for row, _, _ in candidates) or 1.0
        relative = [row["ft"] / best for row, _, _ in candidates]
    else:
        relative = bm25_relative(terms, [sorted(all_terms) for _, _, all_terms in candidates])
    scored = []
    for (row, result, _), rel in zip(candidates, relative):
        structural = bool(result["why"]) and (result["why"].startswith("name") or "path:" in result["why"])
        if result["why"].startswith("name") or "path:" in result["why"]:
            if rel < name_min:
                continue
        elif rel < body_min:
            continue  # body-only evidence without a strong full-text match
        scored.append({**row, **result, "relative": round(rel, 3), "structural": structural})
    scored.sort(key=lambda r: (-r["score"], -r["relative"], r["file"], r["start_line"] or 0))
    top = scored[:limit]
    for i, row in enumerate(top):
        row["label"] = "strong" if row.pop("strong") else "weak"
        if i == 0 and len(top) > 1 and top[1]["score"] >= 0.8 * row["score"]:
            row["label"] = "ambiguous"
        elif i == 1 and top[0]["score"] >= 0.8 * row["score"]:
            row["label"] = "ambiguous"
    return top
