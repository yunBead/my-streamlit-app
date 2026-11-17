"""
AIVLE School 학습 도우미 (미션 3·4)

[필수 기능]
- 업로드된 문서 자동 벡터화 → 01번 노트북의 rag_pipeline()으로 RAG 체인 구성
- 관리자 영역 : 백서 업로드·버전 변경(모든 사용자에게 적용), 질문·평가 기록 CSV 내려받기
- 시스템 메시지 : 백서 근거로 명확히 답하고, 마지막에 '도전 한 걸음'으로 성장 행동 제안

[추가 기능]
- 로그인 : 관리자 / 사용자 구분 (로그인 없이 게스트로 바로 질문 가능)
- 채팅 입력 : 텍스트 · 음성 · 텍스트 파일(.txt, .md)
- 답변 아래 각주 : [1], [2]가 어느 자료(FAQ 번호·절·쪽)에서 왔는지 표시 + 근거 원문(표 포함) 보기
- 좋아요/아쉬워요 평가 + 백서에 없는 질문 자동 수집 → 관리자 화면에서 CSV로 내려받아 백서 개선
- 백서에 없는 질문이면 공식 채널(홈페이지·모집 공고·FAQ·AIVLE 1:1 문의) 버튼 표시
- 개인 맞춤 학습 지원 탭 : 학습 계획 달력(구글 캘린더 파일 저장), 교안 PDF 학습 노트, 개념 퀴즈
"""
import calendar
import hashlib
import html
import json
import os
import random
import re
import tempfile
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pandas as pd
import pymupdf
import streamlit as st
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_pymupdf4llm import PyMuPDF4LLMLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

pymupdf.set_messages(path=os.devnull)
st.set_page_config(page_title="AIVLE School 학습 도우미", page_icon=":material/school:", layout="wide",
                   initial_sidebar_state="expanded")
st.logo("https://raw.githubusercontent.com/Jangrae/img/master/aivle.png", size="large")   # 강의 제공 투명 로고


def secret(name, default=None):
    """Streamlit Cloud의 Secrets → 환경변수 → 기본값 순서로 읽기"""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


if secret("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = secret("OPENAI_API_KEY")

# ── 설정 ───────────────────────────────────────────────
DEFAULT_PDF = "AIVLE School 백서.pdf"          # GitHub 저장소에 함께 올린 기본 백서
DATA_DIR = "app_data"                          # 관리자가 올린 백서·질문 기록 (앱이 재시작되면 초기화될 수 있음)
CHROMA_DIR = "chroma_db"
EMBED_MODEL = "text-embedding-3-small"
LLM_MODEL = "gpt-4o-mini"
STT_MODELS = ("gpt-4o-mini-transcribe", "whisper-1")   # 음성 인식 모델 (앞의 것이 실패하면 다음 것)
TOP_K = 5
HISTORY_TURNS = 6
MAX_FILE_CHARS = 3000                          # 채팅에 첨부한 텍스트 파일에서 읽을 최대 글자 수
ADMIN_PASSWORD = secret("ADMIN_PASSWORD", "admin1234")
NOT_FOUND = "AIVLE School 백서와 관련된 내용이 아니에요"   # 백서로 답할 수 없을 때 이 한 문장만 답함
KST = timezone(timedelta(hours=9))
OFFICIAL_LINKS = [                             # AIVLE School 공식 채널
    ("공식 홈페이지", "https://aivle.kt.co.kr/", ":material/home:"),
    ("모집 공고", "https://aivle.kt.co.kr/home/brd/bbs/listAtclMain?mcd=MC00000061&bbsCd=NOTICE", ":material/campaign:"),
    ("공식 FAQ", "https://aivle.kt.co.kr/home/brd/faq/main?mcd=MC00000056", ":material/help:"),
    ("AIVLE 1:1 문의", "https://aivle.kt.co.kr/home/brd/qna/main?mcd=MC00000057", ":material/forum:"),
]


# ════════════════════════════════════════════════════════
# 1. RAG 파이프라인 (01번 노트북 코드)
# ════════════════════════════════════════════════════════

# 전처리 & 구조 기반 분할 도구
HEADER_FOOTER = re.compile(r"^(#+\s*)?(AIVLE SCHOOL(\s*\|\s*학습과 성장 가이드)?|AIVLE School 백서\s*·\s*\d+)$")
STRAY_LINE  = re.compile(r"^[가-힣 ]{1,25}$")                  # 쪽 끝에 남은 제목 조각 : 짧고 한글만 있음
HEADING     = re.compile(r"^(#{1,3})\s+(.+)$")                 # 마크다운 제목 (#, ##, ###)
NUMBERED    = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")        # '1. 제목', '2.3.1 제목'
FAQ_Q       = re.compile(r"^(?:#+\s*)?(?:-\s*)?Q(\d+)\s*[.)]?\s*(.*)$")
FAQ_RANGE   = re.compile(r"^\|([^|]+)\|\s*Q(\d*)\s*~\s*Q(\d*)\s*\|")   # FAQ 분류표의 한 행
PLACEHOLDER = re.compile(r"입력해\s*주세요")
# 영문·숫자 뒤에 잘못 들어간 공백 제거
UNIT_SPACE     = re.compile(r"(\d[\d,.]*)\s+(?=(?:개월|시간|학기|만원|퍼센트|권역)"                  # '20 만원'→'20만원'
                            r"|(?:년|월|일|세|기|회|원|주|분|명|점|개)(?:[^가-힣]|$|[의은는이가을를과와로에도만부까차제째간씩동]))")  # '10 기'→'10기'
PARTICLE_SPACE = re.compile(r"(?<=[A-Za-z0-9%)])\s+(?=(?:의|은|는|이|가|을|를|과|와|로|으로|에|에서|도|만)(?![가-힣]))")  # 'KT 의'→'KT의'


def resolve_path(filepath):
    """'백서.pdf'와 '백서.PDF'처럼 확장자 대소문자가 달라도 파일을 찾음"""
    if os.path.exists(filepath):
        return filepath
    folder, name = os.path.split(filepath)
    for f in os.listdir(folder or "."):
        if f.lower() == name.lower():
            return os.path.join(folder, f)
    raise FileNotFoundError(f"파일이 없습니다: {filepath}")


def normalize_line(line):
    """마크다운 강조(**)·<br>·빈칸 밑줄을 지우고 문장부호 주변의 불필요한 공백 정리"""
    s = line.replace("**", "")
    s = re.sub(r"(?<=[가-힣])<br>(?=[가-힣])", "", s)   # 표 칸 안에서 단어 중간 줄바꿈 : '서비<br>스' → '서비스'
    s = s.replace("<br>", " ")
    s = re.sub(r"^#{4,}\s*", "", s)               # ####부터는 제목이 아닌 본문으로 취급
    s = re.sub(r"_{3,}", "", s)
    s = re.sub(r"\s+([.,?!:;)\]’”」])", r"\1", s)  # 닫는 부호 앞 공백 :  '입니다 .' → '입니다.'
    s = re.sub(r"([(\[‘“「])\s+", r"\1", s)        # 여는 부호 뒤 공백 :  '( 단' → '(단'
    s = UNIT_SPACE.sub(lambda m: m[0] if "." in m[1] else m[1], s)   # '6.3 주간' 같은 절 번호는 그대로 둠
    s = PARTICLE_SPACE.sub("", s)
    s = re.sub(r"(?<=[A-Za-z]-)\s+", "", s)       # 'K- 디지털' → 'K-디지털'
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def clean_page(text):
    """한 쪽의 텍스트 → 정제된 줄 목록 (머리말·꼬리말·쪽 끝 제목 조각 제거)"""
    lines = [normalize_line(l) for l in text.split("\n")]
    lines = [l for l in lines if l and not HEADER_FOOTER.match(l)]
    while lines and STRAY_LINE.match(lines[-1]):
        lines.pop()
    return lines


def to_blocks(pages, doc_title="AIVLE School 백서"):
    """쪽들을 이어 붙이면서 제목·FAQ 질문을 경계로 블록(절 1개 또는 FAQ 1개)을 나눔"""
    blocks, chapter, faq_set, in_faq = [], doc_title, "", False
    cur = {"type": "section", "chapter": chapter, "title": chapter, "page": 1, "lines": []}
    for p in pages:
        page_no = p.metadata.get("page", 0) + 1
        for line in clean_page(p.page_content):
            q = FAQ_Q.match(line) if in_faq else None
            h = HEADING.match(line)
            if q:                                    # FAQ 질문 → 새 FAQ 블록
                blocks.append(cur)
                cur = {"type": "faq", "chapter": chapter, "title": faq_set, "q_no": int(q[1]),
                       "question": q[2], "page": page_no, "lines": []}
            elif h:                                  # 장·절 제목 → 새 절 블록
                blocks.append(cur)
                title, num = h[2], NUMBERED.match(h[2])
                is_chapter = (num and "." not in num[1]) or (not num and len(h[1]) == 1)
                if is_chapter:                       # '1. ~', '# 참고자료' 같은 장 제목
                    chapter, in_faq = title, "FAQ" in title
                elif in_faq:                         # '4.1 ~ FAQ', '4.2 ~ FAQ'
                    faq_set = title
                cur = {"type": "section", "chapter": chapter, "title": title, "page": page_no, "lines": []}
            else:
                cur["lines"].append(line)
    blocks.append(cur)
    return blocks


def faq_category(q_no, ranges):
    """FAQ 분류표 [(시작번호, 끝번호, 분류명), ...]에서 Q번호의 분류를 찾음"""
    for i, (start, end, name) in enumerate(ranges):
        end = end or (ranges[i + 1][0] - 1 if i + 1 < len(ranges) else 999)
        if start <= q_no <= end:
            return name
    return "기타"


def load_and_split(filepath, chunk_size=800, chunk_overlap=100, verbose=True):
    """PDF 읽기 → 정제 → 구조 기반 청크(Document) 목록"""
    pages = PyMuPDF4LLMLoader(filepath).load()
    blocks = to_blocks(pages, doc_title=os.path.splitext(os.path.basename(filepath))[0])
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, separators=["\n\n", "\n", ".", " ", ""]
    )

    # FAQ 분류표 읽기 → {'4.1 AIVLE School 공식 홈페이지 FAQ': [(1, 21, '모집/선발'), ...]}
    ranges = {}
    for b in blocks:
        for line in b["lines"]:
            m = FAQ_RANGE.match(line)
            if m and m[2]:
                ranges.setdefault(b["title"], []).append((int(m[2]), int(m[3]) if m[3] else None, m[1].strip()))

    chunks, issues, seen = [], [], {}
    for b in blocks:
        text = "\n".join(b["lines"]).strip()
        if b["type"] == "faq":
            name = f"{b['title']} Q{b['q_no']}"
            if not b["question"] or not text or PLACEHOLDER.search(text):
                issues.append(f"{name} : 질문 또는 답변이 비어 있거나 임시 문구 → 제외")
                continue
            key = (b["title"], re.sub(r"\W", "", text))
            if key in seen:
                issues.append(f"{name} : 답변이 Q{seen[key]}와 완전히 같음 → 백서 확인 필요")
            seen.setdefault(key, b["q_no"])
            category = faq_category(b["q_no"], sorted(ranges.get(b["title"], [])))
            chunks.append(Document(
                page_content=f"[{b['title']} > {category}]\nQ{b['q_no']}. {b['question']}\nA. {text}",
                metadata={"source": filepath, "page": b["page"], "type": "faq",
                          "source_label": f"{name} ({category}, p.{b['page']})"},
            ))
        elif text:
            header = b["chapter"] if b["title"] == b["chapter"] else f"{b['chapter']} > {b['title']}"
            for piece in splitter.split_text(text):
                chunks.append(Document(
                    page_content=f"[{header}]\n{piece}",
                    metadata={"source": filepath, "page": b["page"], "type": "section",
                              "source_label": f"{b['title']} (p.{b['page']})"},
                ))

    if verbose:
        n_faq = sum(c.metadata["type"] == "faq" for c in chunks)
        print(f"청크 {len(chunks)}개 (FAQ {n_faq}개, 본문 {len(chunks) - n_faq}개)")
        for issue in issues:
            print("  [백서 점검]", issue)
    return chunks


# 문서 포맷터 : 검색된 청크마다 번호와 출처를 붙여 하나의 문자열로 합침
def format_docs(docs):
    return "\n\n".join(
        f"[{i}] (출처: {d.metadata.get('source_label', '백서')})\n{d.page_content}"
        for i, d in enumerate(docs, start=1)
    )


# 시스템 메시지 : 역할 지정 (문서 근거 + 도전하게 만드는 답변)
SYSTEM_PROMPT = """당신은 'AIVLE School 학습 도우미'입니다.
AIVLE School을 먼저 경험한 선배이자 성장 코치로서, ① 백서에 근거한 정확한 정보와
② 질문자가 에이블스쿨에서 성장하기 위해 지금 바로 도전할 수 있는 행동을 함께 전합니다.

[답변 구성]
1. 첫 문장 : 질문에 대한 결론을 바로 말합니다(두괄식).
2. 근거 설명 : [참고 문서]의 내용만 사용하고, 근거로 쓴 문장 끝에 [1], [2]처럼 참고 문서 번호를 붙입니다.
   두 가지 이상을 비교하거나 항목이 많으면 마크다운 표나 글머리표로 정리합니다. 표 앞뒤에는 빈 줄을 둡니다.
3. 마지막 줄 : "도전 한 걸음:"으로 시작해, 질문자가 오늘이나 이번 주에 해 볼 구체적인 행동 하나를 제안합니다.

[백서로 답할 수 없는 질문]
- [참고 문서]로 답할 수 없는 질문(에이블스쿨과 관계없는 질문 포함)에는 정확히 "{not_found}." 한 문장만 출력합니다.
- 이때 사과, '하지만', 다른 주제 제안, 일반 상식 설명, 도전 한 걸음 등 어떤 말도 덧붙이지 않습니다.
- 문서에 일부만 있으면 있는 내용만 답하고, 없는 부분은 추측하지 않습니다.

[원칙]
- 지원 자격·일정·금액·출결·수료 기준은 기수마다 달라질 수 있으니 최신 모집 공고 확인을 한 줄로 덧붙입니다.
- 수료생 후기는 '한 선배의 경험'으로 소개하고, 모든 사람에게 해당하는 사실처럼 단정하지 않습니다.
- 망설임이나 걱정이 보이면 백서 근거(지원 제도, 학습 지원, 선배 경험)를 들어 따뜻하고 구체적으로 응원합니다.
  단, 합격·취업을 보장하는 표현은 쓰지 않습니다.
- 존댓말로 군더더기 없이 답합니다. 이모지, 제목(#), 과한 굵은 글씨, "물론이죠!"·"좋은 질문이에요" 같은
  상투적인 표현은 쓰지 않고, 상담 선배가 말하듯 담백하게 씁니다.
- 이전 대화를 참고해 '그럼', '그건' 같은 후속 질문의 맥락을 이해합니다.

[참고 문서]
{context}"""

FOLLOW_UP_WORDS = ("그럼", "그러면", "그건", "그거", "그게", "이건", "이거", "거기", "그때", "그 ", "더 ", "또 ", "왜")


def to_inputs(x):
    """'질문' 문자열 또는 {'question', 'history', 'docs'(선택)} → 체인 입력 형식으로 통일"""
    if isinstance(x, dict):
        return {"question": x["question"], "history": x.get("history") or [], "docs": x.get("docs")}
    return {"question": x, "history": [], "docs": None}


def make_search_query(inputs):
    """짧은 후속 질문이면 직전 질문을 붙여 검색 (예: '그럼 언제 받아요?' → '훈련장려금은 얼마예요? 그럼 언제 받아요?')"""
    question, history = inputs["question"], inputs.get("history") or []
    last_q = next((m.content for m in reversed(history) if isinstance(m, HumanMessage)), "")
    if last_q and (len(question) <= 15 or question.startswith(FOLLOW_UP_WORDS)):
        return f"{last_q} {question}"[:2000]
    return question[:2000]


# RAG 파이프라인 함수 (01번 노트북과 같은 순서)
def rag_pipeline(filepath, collection_name="aivle_docs"):

    # PDF 파일 읽기 + 전처리 + 구조 기반 분할 (FAQ는 Q&A 1개 = 청크 1개)
    chunks = load_and_split(resolve_path(filepath))

    # Embedding 모델
    embedding = OpenAIEmbeddings(model=EMBED_MODEL)

    # 벡터저장소 만들기
    vectorstore = Chroma(
        collection_name=collection_name,
        embedding_function=embedding,
        persist_directory=CHROMA_DIR
    )

    # 기존 데이터 비우기 (여러 번 호출해도 데이터가 중복되지 않음)
    vectorstore.reset_collection()

    # 문서 추가
    vectorstore.add_documents(chunks)

    # 검색기 구성
    retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": TOP_K})

    # LLM 선언
    llm = ChatOpenAI(model=LLM_MODEL, temperature=0.3)

    # 프롬프트 구성 : 시스템 메시지 + 이전 대화 + 질문
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT.replace("{not_found}", NOT_FOUND)),
        MessagesPlaceholder("history"),
        ("human", "{question}"),
    ])

    # 검색 : 앱이 미리 찾은 문서(docs)가 있으면 그대로 쓰고, 없으면 직접 검색
    #        → 화면의 각주 [1], [2]와 LLM이 본 문서 번호가 정확히 일치함
    def retrieve(inputs):
        return inputs["docs"] if inputs.get("docs") is not None else retriever.invoke(make_search_query(inputs))

    # RAG 체인 구성 : rag_chain.invoke("질문") 또는 {"question", "history", "docs"} 모두 가능
    rag_chain = (
        RunnableLambda(to_inputs)
        | RunnablePassthrough.assign(context=RunnableLambda(retrieve) | format_docs)
        | prompt
        | llm
        | StrOutputParser()
    )

    return rag_chain


# ════════════════════════════════════════════════════════
# 2. 서버 공용 상태 : 관리자가 올린 백서 버전 · 질문/평가 기록 (모든 사용자 공유)
# ════════════════════════════════════════════════════════
def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def file_digest(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:12]


def _save_json(name, data):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _load_json(name, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@st.cache_resource
def shared():
    """앱 프로세스 전체에서 하나만 존재하는 공용 저장소"""
    state = {"lock": threading.Lock(), "versions": [], "active": None, "logs": _load_json("logs.json", [])}
    try:                                                    # 저장소에 올려 둔 기본 백서 = v1
        path = resolve_path(DEFAULT_PDF)
        state["versions"].append({"hash": file_digest(path), "name": os.path.basename(path), "path": path,
                                  "uploaded_at": "기본 문서", "uploaded_by": "GitHub 저장소"})
    except FileNotFoundError:
        pass
    saved = _load_json("versions.json", {"versions": [], "active": None})
    for v in saved["versions"]:                             # 관리자가 올렸던 버전 복원
        if os.path.exists(v["path"]) and v["hash"] not in {x["hash"] for x in state["versions"]}:
            state["versions"].append(v)
    hashes = [v["hash"] for v in state["versions"]]
    state["active"] = saved["active"] if saved["active"] in hashes else (hashes[0] if hashes else None)
    return state


def versions():
    return shared()["versions"]


def version_label(v):
    i = next((n for n, x in enumerate(versions(), start=1) if x["hash"] == v["hash"]), 0)
    return f"v{i} · {v['name']}"


def active_version():
    S = shared()
    return next((v for v in S["versions"] if v["hash"] == S["active"]), None)


def register_version(data, filename, user):
    """관리자 업로드 PDF 저장 → 버전 등록 → 활성 문서로 지정"""
    digest = hashlib.md5(data).hexdigest()[:12]
    folder = os.path.join(DATA_DIR, "docs", digest)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w가-힣.\- ]", "_", os.path.splitext(filename)[0]) + ".pdf"
    path = os.path.join(folder, safe)
    with open(path, "wb") as f:
        f.write(data)
    S = shared()
    with S["lock"]:
        if digest not in {v["hash"] for v in S["versions"]}:
            S["versions"].append({"hash": digest, "name": filename, "path": path,
                                  "uploaded_at": now_kst(), "uploaded_by": user})
        S["active"] = digest
        _persist_versions()
    return digest


def set_active(digest):
    S = shared()
    with S["lock"]:
        S["active"] = digest
        _persist_versions()


def _persist_versions():
    S = shared()
    _save_json("versions.json", {"versions": [v for v in S["versions"] if v["uploaded_at"] != "기본 문서"],
                                 "active": S["active"]})


def log_answer(entry):
    S = shared()
    with S["lock"]:
        S["logs"].append(entry)
        _save_json("logs.json", S["logs"])


def update_log(entry_id, **fields):
    S = shared()
    with S["lock"]:
        for e in S["logs"]:
            if e["id"] == entry_id:
                e.update(fields)
        _save_json("logs.json", S["logs"])


# 벡터화가 반복되지 않도록 두 겹으로 막음
#   ① st.session_state.chain : 질문할 때마다 스크립트가 다시 실행돼도 저장된 체인을 그대로 사용
#   ② st.cache_resource      : 같은 문서를 여러 사용자가 열어도 rag_pipeline()은 문서당 한 번만 실행
@st.cache_resource(show_spinner=False)
def build_assistant(pdf_path, digest):
    collection = f"doc_{digest}"
    chain = rag_pipeline(pdf_path, collection)
    store = Chroma(collection_name=collection, embedding_function=OpenAIEmbeddings(model=EMBED_MODEL),
                   persist_directory=CHROMA_DIR)
    return chain, store, store._collection.count()


# ════════════════════════════════════════════════════════
# 3. 입력 처리 · 출력 정리 도우미
# ════════════════════════════════════════════════════════
def read_text_file(uploaded):
    """첨부한 텍스트 파일 → 문자열 (UTF-8 / CP949 자동 판별)"""
    data = uploaded.getvalue()
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return data.decode(enc).strip()[:MAX_FILE_CHARS]
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore").strip()[:MAX_FILE_CHARS]


def transcribe(audio):
    """녹음한 음성(wav) → 텍스트 (OpenAI 음성 인식)"""
    from openai import OpenAI
    client, last_error = OpenAI(), None
    for model in STT_MODELS:
        try:
            result = client.audio.transcriptions.create(
                model=model, file=("question.wav", audio.getvalue(), "audio/wav"), language="ko")
            return result.text.strip()
        except Exception as e:
            last_error = e
    raise last_error


def parse_submission(sub):
    """채팅 입력값(텍스트 / 음성 / 파일) → (LLM에 보낼 질문, 화면 표시용 정보)"""
    if sub is None:
        return None, {}
    if isinstance(sub, str):
        return (sub.strip() or None), {"kind": "text", "display": sub.strip()}
    text = (getattr(sub, "text", "") or "").strip()
    meta = {"kind": "text", "display": text}
    audio = getattr(sub, "audio", None)
    if audio is not None:
        heard = transcribe(audio)
        text = f"{text} {heard}".strip()
        meta = {"kind": "voice", "display": text}
    files = getattr(sub, "files", None) or []
    if files:
        f = files[0]
        content = read_text_file(f)
        preview = content[:300] + ("…" if len(content) > 300 else "")
        meta = {"kind": "file", "file": f.name, "display": text or preview}
        text = f"{text}\n\n[첨부 파일 '{f.name}' 내용]\n{content}" if text else content
    return (text or None), meta


REFUSAL_RE = re.compile(                            # '백서/문서에 없다', '관련된 내용이 아니다' 류의 표현만
    r"관련(된|이 있는)?\s*(내용|정보|질문)이\s*아(니|닙|닌)|관련이\s*없는\s*(질문|내용)"   # 아니에요·아닙니다·아닌
    r"|(백서|문서|자료)(에서는|에서|에는|에)?\s*.{0,12}(확인되지\s*않|확인할\s*수\s*없|포함되어\s*있지\s*않"
    r"|나와\s*있지\s*않|다루지\s*않|언급되어\s*있지\s*않|찾을\s*수\s*없)"
    r"|(정보|내용)(가|이|는|은)?\s*(없|포함되어\s*있지\s*않|확인되지\s*않|나와\s*있지\s*않)")
SENTENCE_END = re.compile(r"[.!?。]|\n")


def guard_not_found(tokens):
    """LLM 답변 스트림 감시 : 첫 문장이 '백서에 없다/관련 없다'는 내용이면
    고정 문장 한 줄만 내보내고 뒤('하지만…', 도전 한 걸음 등)는 화면에 나오기 전에 버림"""
    buf, decided = "", False
    for tok in tokens:
        if decided:
            yield tok
            continue
        buf += tok
        end = SENTENCE_END.search(buf)
        if end or len(buf) > 150:                     # 첫 문장이 완성될 때까지만 잠시 모아 둠
            if REFUSAL_RE.search(buf[:end.end()] if end else buf):
                yield NOT_FOUND + "."
                return
            decided = True
            yield buf
    if not decided and buf:
        yield NOT_FOUND + "." if REFUSAL_RE.search(buf) else buf


def fix_markdown(text):
    """표 앞뒤에 빈 줄을 넣어 마크다운 표가 깨지지 않게 함"""
    out = []
    for line in text.split("\n"):
        is_row = line.lstrip().startswith("|")
        prev = out[-1] if out else ""
        prev_row = prev.lstrip().startswith("|")
        if prev.strip() and is_row != prev_row and (is_row or line.strip()):
            out.append("")
        out.append(line)
    return "\n".join(out)


def cited_numbers(answer, n_sources):
    nums = sorted({int(x) for x in re.findall(r"\[(\d+)\]", answer)})
    return [n for n in nums if 1 <= n <= n_sources]


def get_llm(temperature=0.3):
    return ChatOpenAI(model=LLM_MODEL, temperature=temperature)


def ask_structured(schema, system, human, temperature=0.4):
    return get_llm(temperature).with_structured_output(schema).invoke([("system", system), ("human", human)])


# ════════════════════════════════════════════════════════
# 4. 개인 맞춤 학습 지원 : 학습 계획 · 학습 노트 · 개념 퀴즈
# ════════════════════════════════════════════════════════
class StudyEvent(BaseModel):
    date: str = Field(description="YYYY-MM-DD 형식. 반드시 주어진 학습 날짜 중 하나")
    category: Literal["학습", "실습", "복습", "회고"]
    title: str = Field(description="18자 이내 일정 제목")
    detail: str = Field(description="그날 할 일을 구체적으로 1~2문장")


class StudyPlan(BaseModel):
    summary: str = Field(description="계획 전체 흐름을 설명하는 2~3문장")
    events: list[StudyEvent]


class Concept(BaseModel):
    name: str
    explanation: str = Field(description="2~3문장 설명")
    where: str = Field(description="근거가 된 쪽·절 표시, 모르면 빈 문자열")


class StudyNote(BaseModel):
    title: str
    summary: list[str] = Field(description="핵심 3줄 요약")
    concepts: list[Concept] = Field(description="핵심 개념 4~8개")
    questions: list[str] = Field(description="스스로 점검할 질문 3~5개")
    next_actions: list[str] = Field(description="다음에 할 일 3~5개, 구체적인 행동으로")


class QuizItem(BaseModel):
    question: str
    options: list[str] = Field(description="보기 4개")
    answer: int = Field(description="정답 보기 번호 1~4")
    explanation: str = Field(description="정답 해설 1~2문장")
    source: str = Field(description="근거 자료 표시 (예: 4.1 FAQ Q52, p.23)")


class Quiz(BaseModel):
    items: list[QuizItem]


WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
CAT_STYLE = {"학습": "mint", "실습": "deep", "복습": "mint-soft", "회고": "gray"}


def study_dates(start, weeks, rest_days):
    return [start + timedelta(days=i) for i in range(weeks * 7) if WEEKDAYS[(start + timedelta(days=i)).weekday()] not in rest_days]


def make_plan(profile, dates, guide):
    system = ("당신은 AIVLE School 학습 코치입니다. 백서의 학습 전략을 반영해 날짜별 학습 일정을 만듭니다.\n"
              "- 주어진 학습 날짜마다 정확히 1개의 일정을 만들고 날짜는 주어진 형식 그대로 씁니다.\n"
              "- 매주 마지막 학습일은 category '회고'(주간 회고·다음 주 조정)로 합니다.\n"
              "- 하루 학습 시간 안에 끝낼 수 있는 분량으로, 보완하고 싶은 부분을 우선 배치합니다.\n"
              "- 학습 → 실습 → 복습 흐름이 반복되게 하고, 제목은 짧고 구체적으로 씁니다.")
    human = (f"[학습자 정보]\n{profile}\n\n[학습 날짜]\n" + ", ".join(d.isoformat() for d in dates) +
             f"\n\n[백서의 학습 전략 참고]\n{guide or '(없음)'}")
    plan = ask_structured(StudyPlan, system, human, temperature=0.5).model_dump()
    valid = {d.isoformat() for d in dates}
    events = sorted([e for e in plan["events"] if e["date"] in valid], key=lambda e: e["date"])
    return {"summary": plan["summary"], "events": events}


def calendar_html(events, start, end):
    by_day = defaultdict(list)
    for e in events:
        by_day[e["date"]].append(e)
    today, parts, y, m = date.today(), [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        parts.append(f"<div class='cal-month'><div class='cal-title'>{y}년 {m}월</div><div class='cal-grid'>")
        parts.extend(f"<div class='cal-head'>{w}</div>" for w in WEEKDAYS)
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(y, m):
            for d in week:
                cls = "cal-day" + (" out" if d.month != m else "") + (" today" if d == today else "")
                chips = "".join(
                    f"<div class='chip {CAT_STYLE.get(e['category'], 'mint')}' title='{html.escape(e['detail'])}'>"
                    f"{html.escape(e['title'])}</div>" for e in by_day.get(d.isoformat(), []))
                parts.append(f"<div class='{cls}'><div class='num'>{d.day}</div>{chips}</div>")
        parts.append("</div></div>")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return "".join(parts)


def plan_to_ics(events):
    def esc(s):
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AIVLE School Learning Helper//KO", "CALSCALE:GREGORIAN"]
    for e in events:
        d = date.fromisoformat(e["date"])
        lines += ["BEGIN:VEVENT", f"UID:{uuid.uuid4()}@aivle-helper", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{esc('[' + e['category'] + '] ' + e['title'])}", f"DESCRIPTION:{esc(e['detail'])}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode("utf-8")


def make_note(source_name, text):
    system = ("당신은 AIVLE School 학습 코치입니다. 주어진 학습 자료만 근거로 복습용 학습 노트를 만듭니다. "
              "자료에 없는 내용은 만들지 않고, 개념 설명은 비전공자도 이해하도록 쉽게 씁니다.")
    human = f"[자료 이름]\n{source_name}\n\n[자료 내용]\n{text}"
    return ask_structured(StudyNote, system, human, temperature=0.3).model_dump()


def note_markdown(note, source_name):
    md = [f"# {note['title']}", f"_자료: {source_name}_", "", "## 3줄 요약"] + [f"- {s}" for s in note["summary"]]
    md += ["", "## 핵심 개념"] + [f"- **{c['name']}** : {c['explanation']}" + (f" ({c['where']})" if c["where"] else "")
                                for c in note["concepts"]]
    md += ["", "## 스스로 점검할 질문"] + [f"- {q}" for q in note["questions"]]
    md += ["", "## 다음 할 일"] + [f"- [ ] {a}" for a in note["next_actions"]]
    return "\n".join(md)


def make_quiz(context, n, level):
    system = ("당신은 AIVLE School 학습 코치입니다. 주어진 자료만 근거로 4지선다 확인 문제를 만듭니다.\n"
              "- 보기는 정확히 4개, 정답은 하나, 오답도 그럴듯하게 만듭니다.\n"
              "- 해설에는 왜 정답인지 자료 근거를 쓰고, source에는 자료의 출처 표시를 그대로 씁니다.")
    human = f"[문항 수] {n}개\n[난이도] {level}\n\n[자료]\n{context}"
    items = ask_structured(Quiz, system, human, temperature=0.5).model_dump()["items"]
    return [q for q in items if len(q["options"]) == 4 and 1 <= q["answer"] <= 4][:n]


def keyword_pick(chunks, topic, k):
    """주제어가 많이 등장하는 청크 고르기 (교안용 간단 검색)"""
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", topic.lower())]
    scored = sorted(chunks, key=lambda c: -sum(c["text"].lower().count(w) for w in words))
    return scored[:k]


@st.cache_data(show_spinner=False)
def material_chunks(path, digest):
    """교안 PDF → [{'label', 'text'}] (같은 파일은 한 번만 읽음)"""
    return [{"label": d.metadata["source_label"], "text": d.page_content} for d in load_and_split(path, verbose=False)]


def store_chunks(store):
    got = store.get(include=["documents", "metadatas"])
    return [{"label": (m or {}).get("source_label", ""), "text": t} for t, m in zip(got["documents"], got["metadatas"])]


def join_chunks(chunks, limit):
    out, total = [], 0
    for c in chunks:
        piece = f"(출처: {c['label']})\n{c['text']}"
        if total + len(piece) > limit:
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(out)


# ════════════════════════════════════════════════════════
# 5. 세션 상태 · 공통 동작
# ════════════════════════════════════════════════════════
ss = st.session_state
for key, value in {"messages": [], "pending": None, "chain": None, "store": None, "n_chunks": 0,
                   "document_name": None, "user": {"name": "게스트", "role": "user"}, "logged_in": False,
                   "login_error": "", "plan": None, "note": None, "quiz": None, "quiz_id": 0,
                   "quiz_graded": False, "material": None}.items():
    ss.setdefault(key, value)

RECOMMENDED = {
    "지원 준비": ["비전공자도 커리큘럼을 따라갈 수 있을까요?",
                  "AI 개발자 트랙과 DX 컨설턴트 트랙은 어떻게 달라요?",
                  "지원 전에 미리 공부해 두면 좋은 건 뭐예요?"],
    "지원금·제도": ["훈련장려금은 얼마나, 언제 받을 수 있나요?",
                   "국민내일배움카드는 꼭 있어야 하나요?",
                   "아르바이트를 하면서 교육을 들어도 되나요?"],
    "교육 생활": ["하루 수업 시간표는 어떻게 되나요?",
                  "수료하려면 출석을 얼마나 해야 하나요?",
                  "진도를 놓쳤을 때는 뭐부터 해야 하나요?"],
    "프로젝트·취업": ["팀 프로젝트 주제는 어떻게 정하면 좋을까요?",
                    "프로젝트 경험을 포트폴리오로 어떻게 정리하나요?",
                    "수료 후 취업 연계는 어떻게 도와주나요?"],
}


def is_admin():
    return ss.logged_in and ss.user["role"] == "admin"


def ask(question):
    ss.pending = question


def clear_chat():
    ss.messages = []


def login():
    name = (ss.get("login_name") or "").strip() or "에이블러"
    role = ss.get("login_role") or "사용자"
    if role == "관리자" and ss.get("login_pw") != ADMIN_PASSWORD:
        ss.login_error = "관리자 비밀번호가 맞지 않아요."
        return
    ss.user = {"name": name, "role": "admin" if role == "관리자" else "user"}
    ss.logged_in, ss.login_error = True, ""


def logout():
    ss.user, ss.logged_in = {"name": "게스트", "role": "user"}, False


def on_feedback(mid):
    rating = {1: "👍", 0: "👎"}.get(ss.get(f"fb_{mid}"), "")
    for m in ss.messages:
        if m.get("id") == mid:
            m["rating"] = rating
    update_log(mid, rating=rating)


def ensure_chain():
    """현재 활성 백서의 체인을 session_state에 준비 (문서가 바뀔 때만 다시 준비)"""
    v = active_version()
    if v is None:
        return None
    if ss.chain is None or ss.document_name != v["hash"]:
        updated = ss.document_name is not None
        with st.spinner(f"'{v['name']}' 문서를 처리하고 있어요… (문서마다 처음 한 번만 걸려요)"):
            ss.chain, ss.store, ss.n_chunks = build_assistant(v["path"], v["hash"])
        ss.document_name = v["hash"]
        if updated:
            st.toast(f"백서가 {version_label(v)}(으)로 바뀌었어요", icon=":material/menu_book:")
    return v


def to_history(messages):
    hist = []
    for m in messages[-HISTORY_TURNS:]:
        content = m["content"][:1500]
        hist.append(HumanMessage(content) if m["role"] == "user" else AIMessage(content))
    return hist


def plain_text(text):
    """마크다운 기호를 빼서 메모장에서 읽기 쉽게 정리"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)               # **굵게** → 굵게
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)            # # 제목 → 제목
    text = re.sub(r"^\|?\s*:?-{3,}.*$\n?", "", text, flags=re.M)   # 표 구분선 |---|---| 삭제
    text = re.sub(r"^\|(.*)\|\s*$", lambda m: " | ".join(c.strip() for c in m[1].split("|")), text, flags=re.M)  # |a|b| → a | b
    return text.strip()


def chat_text():
    """대화 기록 → 텍스트 파일(.txt) 내용"""
    v = active_version()
    lines = ["AIVLE School 학습 도우미 대화 기록",
             f"저장 시각 : {now_kst()}  ·  사용자 : {ss.user['name']}",
             f"참고 백서 : {version_label(v) if v else '없음'}", "=" * 50, ""]
    for m in ss.messages:
        if m["role"] == "user":
            how = {"voice": " (음성)", "file": f" (파일 첨부: {m.get('file', '')})"}.get(m.get("kind"), "")
            lines += [f"[질문]{how}", plain_text(m.get("display") or m["content"]), ""]
        else:
            lines += ["[답변]", plain_text(m["content"])]
            sources = m.get("sources", [])
            cited = cited_numbers(m["content"], len(sources))
            shown = [x for x in sources if x["n"] in cited] or ([] if m.get("not_found") else sources)
            if shown:
                lines.append("")
                lines += [f"  각주 [{x['n']}] {x['label']}" for x in shown]
            if m.get("rating"):
                lines.append(f"  평가 : {m['rating']}")
            lines += ["", "-" * 50, ""]
    return "\n".join(lines).encode("utf-8-sig")       # BOM 포함 → 윈도우 메모장에서도 한글이 깨지지 않음


# ════════════════════════════════════════════════════════
# 6. 화면
# ════════════════════════════════════════════════════════
CHALLENGE_RE = re.compile(r"\n*[ \t]*(?:🚀\s*)?(?:\*\*)?도전 한 걸음(?:\*\*)?\s*[:：]\s*(.+?)\s*$", re.S)
AVATAR = {"user": ":material/person:", "assistant": ":material/school:"}


def split_challenge(answer):
    """답변 끝의 '도전 한 걸음'을 떼어 내 강조 박스로 보여 줌"""
    m = CHALLENGE_RE.search(answer)
    if not m:
        return answer, ""
    return answer[:m.start()].rstrip(), m[1].replace("**", "").strip()


st.markdown("""
<style>
@import url("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css");
/* KT AIVLE School 로고 민트(#30A4A0) 한 가지 색 + 회색 계열 */
:root {--mint:#2FA4A0; --mint-dark:#1F7F7B; --mint-deep:#176663; --mint-mid:#8FD0CC; --mint-soft:#E9F6F5; --mint-line:#C3E5E3;
       --ink:#1F2937; --muted:#6B7280; --line:#E5E9EC; --gray-soft:#F2F4F6; --paper:#F6FAFA;}
html, body, p, li, label, input, textarea, button, h1, h2, h3, h4, h5, h6, td, th {
  font-family:"Pretendard", "Source Sans Pro", -apple-system, sans-serif;}
.block-container {padding-top:3.4rem;}
section[data-testid="stSidebar"] {background:var(--paper); border-right:1px solid var(--line);}

/* Streamlit 기본 빨강 → 민트 (config.toml이 없어도 적용) */
button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] {
  background:var(--mint) !important; border-color:var(--mint) !important; color:#FFFFFF !important;}
button[data-testid="stBaseButton-primary"]:hover, button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
  background:var(--mint-dark) !important; border-color:var(--mint-dark) !important;}
button[data-testid="stBaseButton-secondary"]:hover, a[data-testid^="stBaseLinkButton"]:hover {
  border-color:var(--mint) !important; color:var(--mint-dark) !important;}
div[data-testid="stButtonGroup"] button[aria-checked="true"] {background:var(--mint-soft) !important; border-color:var(--mint) !important; color:var(--mint-dark) !important;}
div[data-testid="stButtonGroup"] button[aria-checked="true"] * {color:var(--mint-dark) !important;}
div[data-testid="stTab"][aria-selected="true"], div[data-testid="stTab"][aria-selected="true"] * {color:var(--mint-dark) !important;}
div[data-testid="stTab"][aria-selected="true"] > div:not([data-testid]) {background:var(--mint) !important;}
div[data-testid="stCheckbox"][data-selected="true"] label > div:not([data-testid]) {background:var(--mint) !important;}
div[data-testid="stSlider"] div[role="group"] > div > div:first-child {filter:hue-rotate(178deg) saturate(.62) brightness(.9);}
div[data-testid="stSlider"] div[role="group"] > div > div:has(> div[data-testid="stSliderThumbValue"]) {background:var(--mint) !important;}
div[data-testid="stSliderThumbValue"], div[data-testid="stSliderThumbValue"] * {color:var(--mint-dark) !important; border-color:transparent !important;}
div[data-testid="stMultiSelectTagsContainer"] span[data-tag] {background:var(--mint) !important;}
div[data-testid="stChatInput"] > div {border-color:var(--mint-line) !important;}
div[data-testid="stChatInput"] > div:focus-within {border-color:var(--mint) !important;}
div[data-testid="stTextInputRootElement"]:focus-within {border-color:var(--mint) !important;}
div[data-testid="stMarkdownContainer"] a {color:var(--mint-dark);}

/* 머리말 */
.app-head {display:flex; align-items:flex-end; justify-content:space-between; gap:16px; flex-wrap:wrap;
           padding-bottom:14px; margin-bottom:4px; border-bottom:1px solid var(--line);}
.app-head .overline {font-size:.75rem; letter-spacing:.08em; color:var(--mint-dark); font-weight:700; margin-bottom:4px;}
.app-head h1 {font-size:1.7rem; font-weight:800; margin:0; padding:0; color:var(--ink); letter-spacing:-.02em;}
.app-head h1 .accent {color:var(--mint);}
.app-head h1::after {content:""; display:block; width:44px; height:4px; border-radius:2px; margin-top:8px; background:var(--mint);}
.app-head p {margin:8px 0 0 0; color:var(--muted); font-size:.93rem;}
.chips {display:flex; gap:6px; flex-wrap:wrap;}
.chips span {font-size:.76rem; padding:4px 11px; border-radius:999px; background:#FFFFFF; color:var(--ink); border:1px solid var(--line);}
.chips span.mint {background:var(--mint-soft); color:var(--mint-dark); border-color:var(--mint-line);}
.chips span.solid {background:var(--mint); color:#FFFFFF; border-color:var(--mint);}

/* 사이드바 */
.side-label {font-size:.75rem; font-weight:700; color:var(--muted); letter-spacing:.06em; margin:14px 0 6px 0;}
.doc-card {background:#FFFFFF; border:1px solid var(--mint-line); border-left:4px solid var(--mint); border-radius:10px; padding:10px 12px;}
.doc-card .name {font-weight:700; color:var(--ink); font-size:.92rem;}
.doc-card .sub {font-size:.78rem; color:var(--muted); margin-top:2px;}
.doc-card.empty {border-color:var(--line); border-left-color:#9CA3AF;}
.who {display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px;}
.role-badge {font-size:.72rem; padding:2px 9px; border-radius:999px; background:var(--mint-soft); color:var(--mint-dark);}
.role-badge.admin {background:var(--mint); color:#FFFFFF;}

/* 패널 · 안내 */
.panel-title {font-weight:700; color:var(--ink); font-size:.95rem; margin:0 0 8px 0; padding-left:9px; border-left:3px solid var(--mint);}
.welcome {background:var(--mint-soft); border:1px solid var(--mint-line); border-radius:12px; padding:14px 16px; color:var(--ink); line-height:1.65;}
.welcome .muted, .muted {color:var(--muted); font-size:.88rem;}
.notice {background:var(--gray-soft); border:1px solid var(--line); border-left:4px solid #9CA3AF; border-radius:12px;
         padding:12px 16px; color:var(--ink); line-height:1.65;}

/* 대화 : 메신저형 말풍선 (내 질문 = 오른쪽 연민트, 답변 = 흰 카드 + 민트 선) */
div[data-testid="stElementContainer"]:has(.role-user), div[data-testid="stElementContainer"]:has(.role-bot) {display:none;}
div[data-testid="stChatMessage"] {background:transparent !important; padding:2px 0 !important; gap:10px;}
div[data-testid="stChatMessage"]:has(.role-user) {
  display:flex !important; flex-direction:row-reverse !important;
  justify-content:flex-start !important; align-items:flex-start !important;
  width:100% !important; box-sizing:border-box;}
div[data-testid="stChatMessage"]:has(.role-user) div[data-testid="stChatMessageContent"] {
  flex:0 1 auto !important; width:fit-content !important; min-width:0 !important; max-width:78% !important; margin:0 !important; text-align:left; overflow-wrap:anywhere; background:var(--mint-soft); border:1px solid var(--mint-line);
  border-radius:16px 4px 16px 16px; padding:10px 14px;}
div[data-testid="stChatMessage"]:has(.role-bot) div[data-testid="stChatMessageContent"] {
  background:#FFFFFF; border:1px solid var(--line); border-left:3px solid var(--mint);
  border-radius:4px 16px 16px 16px; padding:12px 16px;}
.challenge {margin-top:10px; background:#FFFFFF; border:1px solid var(--mint); border-radius:10px;
            padding:10px 14px; color:var(--ink); line-height:1.6;}
.challenge .label {font-size:.74rem; font-weight:800; color:var(--mint-dark); letter-spacing:.06em; margin-bottom:2px;}
.fn {font-size:.8rem; color:var(--muted); margin-top:10px; padding-top:8px; border-top:1px dashed var(--line); line-height:1.75;}
.fn .t {font-weight:700; color:var(--ink); font-size:.78rem; letter-spacing:.04em;}
.fn b {color:var(--mint-dark);}
.src-label {font-weight:700; font-size:.86rem; color:var(--ink); border-left:3px solid var(--mint); padding-left:8px; margin:12px 0 4px 0;}
.ok {color:var(--mint-dark); font-weight:700;} .ng {color:var(--muted); font-weight:700;}
.badge-on {font-size:.72rem; padding:2px 8px; border-radius:999px; background:var(--mint); color:#FFFFFF; margin-left:6px;}

/* 달력 */
.cal-month {margin:8px 0 18px 0;}
.cal-title {font-weight:700; color:var(--ink); margin-bottom:6px;}
.cal-grid {display:grid; grid-template-columns:repeat(7, 1fr); gap:4px;}
.cal-head {text-align:center; font-size:.76rem; color:var(--muted); padding:4px 0;}
.cal-day {min-height:86px; border:1px solid var(--line); border-radius:8px; padding:4px; background:#FFFFFF; overflow:hidden;}
.cal-day.out {background:#FAFBFB; opacity:.55;}
.cal-day.today {border:2px solid var(--mint);}
.cal-day .num {font-size:.74rem; color:var(--muted); margin-bottom:2px;}
.chip {font-size:.71rem; line-height:1.3; border-radius:6px; padding:2px 5px; margin-bottom:2px; color:#FFFFFF;}
.chip.mint {background:var(--mint);} .chip.deep {background:var(--mint-deep);}
.chip.mint-soft {background:var(--mint-soft); color:var(--mint-dark);} .chip.gray {background:var(--gray-soft); color:var(--ink);}
</style>
""", unsafe_allow_html=True)

version = active_version()
has_key = bool(os.environ.get("OPENAI_API_KEY"))
esc = html.escape


def notice(msg, target=st):
    """오류·경고도 빨강/노랑 대신 회색 안내 박스로 표시"""
    target.markdown(f"<div class='notice'>{esc(msg)}</div>", unsafe_allow_html=True)

# ── 사이드바 : 계정 / 현재 백서 / 대화 ──
with st.sidebar:
    st.markdown('<div class="side-label">계정</div>', unsafe_allow_html=True)
    with st.container(border=True):
        if not ss.logged_in:
            st.text_input("이름", key="login_name", placeholder="예: 에이블러")
            st.segmented_control("역할", ["사용자", "관리자"], default="사용자", key="login_role")
            if ss.get("login_role") == "관리자":
                st.text_input("관리자 비밀번호", type="password", key="login_pw")
            st.button("입장하기", type="primary", icon=":material/login:", on_click=login, width="stretch")
            if ss.login_error:
                st.markdown(f"<div class='notice'>{esc(ss.login_error)}</div>", unsafe_allow_html=True)
            st.caption("로그인하지 않아도 게스트로 바로 질문할 수 있어요.")
        else:
            role = "관리자" if is_admin() else "사용자"
            st.markdown(f"<div class='who'><b>{esc(ss.user['name'])} 님</b>"
                        f"<span class='role-badge {'admin' if is_admin() else ''}'>{role}</span></div>", unsafe_allow_html=True)
            st.button("로그아웃", icon=":material/logout:", on_click=logout, width="stretch")

    st.markdown('<div class="side-label">현재 백서</div>', unsafe_allow_html=True)
    if version:
        sub = (f"검색 단위 {ss.n_chunks}개 · {esc(version['uploaded_at'])}" if ss.document_name == version["hash"]
               else "문서를 준비하고 있어요…")
        st.markdown(f"<div class='doc-card'><div class='name'>{esc(version_label(version))}</div>"
                    f"<div class='sub'>{sub}</div></div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='doc-card empty'><div class='name'>등록된 백서가 없어요</div>"
                    "<div class='sub'>관리자가 백서를 등록하면 질문할 수 있어요.</div></div>", unsafe_allow_html=True)

    st.markdown('<div class="side-label">대화</div>', unsafe_allow_html=True)
    show_sources = st.toggle("근거 원문 함께 보기", value=False)
    c1, c2 = st.columns(2)
    c1.button("대화 삭제", icon=":material/delete:", on_click=clear_chat, width="stretch", disabled=not ss.messages)
    c2.download_button("대화 저장", data=chat_text(), file_name=f"aivle_chat_{datetime.now(KST):%Y%m%d_%H%M}.txt",
                       icon=":material/download:", mime="text/plain", width="stretch", disabled=not ss.messages)
    st.caption("답변은 관리자가 등록한 AIVLE School 백서를 근거로 합니다. "
               "모집 조건·일정은 기수마다 달라질 수 있으니 공식 모집 공고를 꼭 확인하세요.")

# ── 머리말 ──
n_q = sum(m["role"] == "user" for m in ss.messages)
chips = [f"<span class='mint'>{esc(version_label(version)) if version else '백서 미등록'}</span>",
         f"<span>질문 {n_q}개</span>",
         f"<span class='solid'>{'관리자' if is_admin() else esc(ss.user['name'])}</span>"]
st.markdown(
    "<div class='app-head'><div><div class='overline'>KT AIVLE SCHOOL · 백서 기반 학습 도우미</div>"
    "<h1>AIVLE School <span class='accent'>학습 도우미</span></h1>"
    "<p>백서에 근거해 정확하게 답하고, 에이블스쿨에서 성장하기 위한 다음 도전을 함께 제안합니다.</p></div>"
    f"<div class='chips'>{''.join(chips)}</div></div>", unsafe_allow_html=True)

tab_names = [":material/forum: 학습 도우미", ":material/event_note: 개인 맞춤 학습 지원"] + \
            ([":material/admin_panel_settings: 관리자"] if is_admin() else [])
tabs = st.tabs(tab_names)


def render_answer(content):
    body, challenge = split_challenge(content)
    st.markdown(fix_markdown(body))
    if challenge:
        st.markdown(f"<div class='challenge'><div class='label'>도전 한 걸음</div>{esc(challenge)}</div>",
                    unsafe_allow_html=True)


def render_assistant_extras(m):
    """답변 아래 : 각주(출처) · 공식 채널 버튼 · 근거 원문 · 평가"""
    sources = m.get("sources", [])
    cited = cited_numbers(m["content"], len(sources))
    shown = [s for s in sources if s["n"] in cited] or ([] if m.get("not_found") else sources)
    if shown:
        title = "각주" if cited else "참고한 자료"
        items = "".join(f"<div><b>[{s['n']}]</b> {esc(s['label'])}</div>" for s in shown)
        st.markdown(f"<div class='fn'><div class='t'>{title}</div>{items}</div>", unsafe_allow_html=True)
    if m.get("not_found"):                            # 한 문장 답변 + 공식 채널 버튼만
        cols = st.columns(len(OFFICIAL_LINKS))
        for col, (label, url, icon) in zip(cols, OFFICIAL_LINKS):
            col.link_button(label.replace("공식 홈페이지", "홈페이지").replace("AIVLE ", ""), url, icon=icon, width="stretch")
    if show_sources and sources and not m.get("not_found"):
        with st.expander(f"근거 원문 {len(sources)}개", icon=":material/manage_search:"):
            for s in sources:
                st.markdown(f"<div class='src-label'>[{s['n']}] {esc(s['label'])}</div>", unsafe_allow_html=True)
                st.markdown(fix_markdown(s["text"]))
    if m.get("id"):
        st.feedback("thumbs", key=f"fb_{m['id']}", on_change=on_feedback, args=(m["id"],))


def role_marker(role):
    """말풍선 모양(내 질문 = 오른쪽 코랄, 답변 = 민트 카드)을 CSS로 구분하기 위한 표시"""
    st.markdown(f"<span class='role-{'user' if role == 'user' else 'bot'}'></span>", unsafe_allow_html=True)


# ── 탭 1 : 학습 도우미 채팅 ──
with tabs[0]:
    col_chat, col_side = st.columns([2.4, 1], gap="large")
    with col_chat:
        status_slot = st.empty()
        box = st.container(height=600, border=True, autoscroll=True)
        with box:
            if not ss.messages:
                if not has_key:
                    st.markdown("<div class='notice'><b>OpenAI API 키가 설정되지 않았어요.</b><br>"
                                "관리자에게 앱 Secrets 설정을 요청해 주세요.</div>", unsafe_allow_html=True)
                elif version is None:
                    msg = ("<b>아직 학습 도우미가 참고할 백서가 등록되지 않았어요.</b><br>"
                           + ("'관리자' 탭에서 AIVLE School 백서 PDF를 업로드하면 바로 대화를 시작할 수 있어요."
                              if is_admin() else "관리자에게 백서 업로드를 요청해 주세요. 백서가 등록되면 바로 질문할 수 있어요."))
                    st.markdown(f"<div class='notice'>{msg}</div>", unsafe_allow_html=True)
                else:
                    st.markdown(
                        f"<div class='welcome'><b>안녕하세요, {esc(ss.user['name'])}님.</b><br>"
                        "AIVLE School 백서를 바탕으로 지원 준비, 교육 생활, 프로젝트와 취업 질문에 답해 드려요.<br>"
                        "<span class='muted'>입력창에서 직접 묻거나, 마이크로 녹음하거나, 질문이 담긴 .txt 파일을 첨부할 수 있어요.</span></div>",
                        unsafe_allow_html=True)
            for m in ss.messages:
                with st.chat_message(m["role"], avatar=AVATAR[m["role"]]):
                    role_marker(m["role"])
                    if m["role"] == "user":
                        st.markdown(m.get("display") or m["content"])
                        if m.get("kind") == "voice":
                            st.caption("음성으로 질문")
                        elif m.get("kind") == "file":
                            st.caption(f"첨부 파일 · {m['file']}")
                    else:
                        render_answer(m["content"])
                        render_assistant_extras(m)
        ready_to_chat = has_key and version is not None
        submission = st.chat_input(
            "질문을 입력하세요 · 음성 녹음과 .txt 파일 첨부도 가능해요" if ready_to_chat else "백서가 등록되면 질문할 수 있어요",
            accept_file=True, file_type=["txt", "md"], accept_audio=True, disabled=not ready_to_chat)

    with col_side:
        with st.container(border=True):
            st.markdown('<div class="panel-title">추천 질문</div>', unsafe_allow_html=True)
            group = st.pills("분야", list(RECOMMENDED), default=list(RECOMMENDED)[0], required=True,
                             key="rec_group", label_visibility="collapsed")
            for q in RECOMMENDED.get(group or list(RECOMMENDED)[0], []):
                st.button(q, key=f"rec_{q}", on_click=ask, args=(q,), width="stretch", disabled=not ready_to_chat)
        with st.container(border=True):
            st.markdown('<div class="panel-title">공식 채널</div>', unsafe_allow_html=True)
            for label, url, icon in OFFICIAL_LINKS:
                st.link_button(label, url, icon=icon, width="stretch", type="secondary")

# ── 탭 2 : 개인 맞춤 학습 지원 ──
with tabs[1]:
    st.markdown('<div class="panel-title">나에게 맞춘 학습 계획 · 학습 노트 · 개념 퀴즈</div>', unsafe_allow_html=True)
    with st.container(border=True):
        material = st.file_uploader("내 교안 PDF (학습 노트·퀴즈에 사용, 선택)", type=["pdf"], key="material_pdf")
        if material is not None:
            digest = hashlib.md5(material.getvalue()).hexdigest()[:12]
            if not ss.material or ss.material["hash"] != digest:
                folder = os.path.join(tempfile.gettempdir(), "aivle_materials", digest)
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, re.sub(r"[^\w가-힣.\- ]", "_", os.path.splitext(material.name)[0]) + ".pdf")
                with open(path, "wb") as f:
                    f.write(material.getvalue())
                ss.material = {"name": material.name, "path": path, "hash": digest}
        elif ss.material:                                   # 교안을 지우면 백서로 돌아감
            ss.material = None
        st.caption(f"현재 교안: **{ss.material['name']}**" if ss.material else "교안을 올리지 않으면 백서를 자료로 사용해요.")

    t_plan, t_note, t_quiz = st.tabs([":material/calendar_month: 학습 계획 달력", ":material/edit_note: 학습 노트 자동 정리",
                                      ":material/quiz: 개념 퀴즈"])

    with t_plan:
        with st.form("plan_form"):
            c1, c2, c3 = st.columns(3)
            start = c1.date_input("시작일", value=date.today())
            weeks = c2.select_slider("기간", options=[1, 2, 3, 4, 6, 8], value=4, format_func=lambda w: f"{w}주")
            hours = c3.select_slider("하루 학습 시간", options=["30분", "1시간", "2시간", "3시간 이상"], value="1시간")
            c4, c5 = st.columns([2, 1])
            goal = c4.text_input("학습 목표", placeholder="예: 미니프로젝트에서 데이터 분석 파트를 맡을 수 있게 되기")
            track = c5.selectbox("트랙", ["AI 개발자", "DX 컨설턴트", "아직 고민 중"])
            weak = st.multiselect("보완하고 싶은 부분", ["Python 기초", "데이터 분석(Pandas)", "시각화", "머신러닝",
                                                     "딥러닝", "생성형 AI·LLM", "클라우드", "문제 정의·기획", "발표·문서 작성"])
            rest = st.multiselect("쉬는 요일", WEEKDAYS, default=["일"])
            make = st.form_submit_button("학습 계획 만들기", type="primary", icon=":material/event_available:", disabled=not has_key)
        if make:
            dates = study_dates(start, weeks, rest)
            if not dates:
                notice("학습할 날짜가 없어요. 쉬는 요일을 줄여 주세요.")
            else:
                guide = ""
                if ensure_chain():
                    guide = join_chunks([{"label": d.metadata.get("source_label", ""), "text": d.page_content}
                                         for d in ss.store.similarity_search("학습 루틴 복습 주간 회고 뒤처졌을 때 학습 계획", k=4)], 3000)
                profile = (f"- 트랙: {track}\n- 목표: {goal or '에이블스쿨 과정을 꾸준히 따라가기'}\n- 하루 학습 시간: {hours}\n"
                           f"- 보완하고 싶은 부분: {', '.join(weak) or '특별히 없음'}")
                with st.spinner("백서의 학습 전략을 반영해 일정을 짜고 있어요…"):
                    try:
                        ss.plan = {**make_plan(profile, dates, guide), "start": start.isoformat(),
                                   "end": dates[-1].isoformat()}
                    except Exception as e:
                        notice(f"계획을 만들지 못했어요: {e}")
        if ss.plan:
            plan = ss.plan
            st.markdown(f"<div class='welcome'>{esc(plan['summary'])}</div>", unsafe_allow_html=True)
            st.markdown(calendar_html(plan["events"], date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])),
                        unsafe_allow_html=True)
            table = pd.DataFrame([{"날짜": e["date"], "요일": WEEKDAYS[date.fromisoformat(e["date"]).weekday()],
                                   "분류": e["category"], "일정": e["title"], "할 일": e["detail"]} for e in plan["events"]])
            st.dataframe(table, hide_index=True, width="stretch")
            d1, d2 = st.columns(2)
            d1.download_button("구글 캘린더용 파일(.ics)", plan_to_ics(plan["events"]), file_name="aivle_study_plan.ics",
                               mime="text/calendar", icon=":material/calendar_add_on:", width="stretch")
            d2.download_button("표로 저장(.csv)", table.to_csv(index=False).encode("utf-8-sig"),
                               file_name="aivle_study_plan.csv", mime="text/csv", icon=":material/table_view:", width="stretch")
            st.caption("구글 캘린더 → 설정 → 가져오기에서 .ics 파일을 올리면 일정이 한 번에 등록돼요.")

    with t_note:
        src = "교안" if ss.material else "백서"
        st.caption(f"자료: **{ss.material['name'] if ss.material else '현재 백서'}** — 핵심 개념, 스스로 점검할 질문, 다음 할 일을 정리해요.")
        if st.button("학습 노트 만들기", type="primary", icon=":material/description:",
                     disabled=not has_key or (src == "백서" and version is None)):
            with st.spinner("자료를 읽고 학습 노트를 정리하고 있어요…"):
                try:
                    if ss.material:
                        chunks, name = material_chunks(ss.material["path"], ss.material["hash"]), ss.material["name"]
                    else:
                        ensure_chain()
                        chunks, name = store_chunks(ss.store), version_label(version)
                    ss.note = {"source": name, **make_note(name, join_chunks(chunks, 60000))}
                except Exception as e:
                    notice(f"학습 노트를 만들지 못했어요: {e}")
        if ss.note:
            note = ss.note
            st.markdown(f"#### {note['title']}")
            st.markdown("<div class='welcome'>" + "<br>".join(f"· {esc(s)}" for s in note["summary"]) + "</div>",
                        unsafe_allow_html=True)
            st.markdown('<div class="panel-title" style="margin-top:14px">핵심 개념</div>', unsafe_allow_html=True)
            cols = st.columns(2)
            for i, c in enumerate(note["concepts"]):
                with cols[i % 2].container(border=True):
                    st.markdown(f"**{c['name']}**" + (f"  ·  _{c['where']}_" if c["where"] else ""))
                    st.write(c["explanation"])
            q1, q2 = st.columns(2)
            with q1:
                st.markdown('<div class="panel-title">스스로 점검할 질문</div>', unsafe_allow_html=True)
                for q in note["questions"]:
                    st.markdown(f"- {q}")
            with q2:
                st.markdown('<div class="panel-title">다음 할 일</div>', unsafe_allow_html=True)
                for i, a in enumerate(note["next_actions"]):
                    st.checkbox(a, key=f"todo_{i}_{a[:20]}")
            st.download_button("학습 노트 저장(.md)", note_markdown(note, note["source"]), file_name="aivle_study_note.md",
                               mime="text/markdown", icon=":material/download:")

    with t_quiz:
        q1, q2, q3 = st.columns([2, 1, 1])
        topic = q1.text_input("주제 (비우면 자료 전체에서 골고루)", placeholder="예: 훈련장려금, 결측치 처리")
        n_items = q2.select_slider("문항 수", options=[3, 5, 7, 10], value=5)
        level = q3.segmented_control("난이도", ["기초", "보통", "도전"], default="보통")
        if st.button("퀴즈 만들기", type="primary", icon=":material/quiz:",
                     disabled=not has_key or (not ss.material and version is None)):
            with st.spinner("자료에서 확인 문제를 만들고 있어요…"):
                try:
                    if ss.material:
                        chunks = material_chunks(ss.material["path"], ss.material["hash"])
                        picked = keyword_pick(chunks, topic, 8) if topic else random.sample(chunks, min(8, len(chunks)))
                    else:
                        ensure_chain()
                        if topic:
                            picked = [{"label": d.metadata.get("source_label", ""), "text": d.page_content}
                                      for d in ss.store.similarity_search(topic, k=8)]
                        else:
                            allc = store_chunks(ss.store)
                            picked = random.sample(allc, min(8, len(allc)))
                    ss.quiz = make_quiz(join_chunks(picked, 12000), n_items, level or "보통")
                    ss.quiz_id += 1
                    ss.quiz_graded = False
                except Exception as e:
                    notice(f"퀴즈를 만들지 못했어요: {e}")
        if ss.quiz:
            with st.form(f"quiz_form_{ss.quiz_id}"):
                for i, item in enumerate(ss.quiz, start=1):
                    st.radio(f"**Q{i}. {item['question']}**", item["options"], index=None, key=f"quiz_{ss.quiz_id}_{i}")
                graded = st.form_submit_button("채점하기", type="primary", icon=":material/fact_check:")
            if graded:
                ss.quiz_graded = True
            if ss.quiz_graded:
                score = 0
                for i, item in enumerate(ss.quiz, start=1):
                    picked = ss.get(f"quiz_{ss.quiz_id}_{i}")
                    right = item["options"][item["answer"] - 1]
                    ok = picked == right
                    score += ok
                    with st.container(border=True):
                        mark = "<span class='ok'>정답</span>" if ok else "<span class='ng'>오답</span>"
                        mine = "" if ok else f" · 내 답: {esc(picked or '선택 안 함')}"
                        st.markdown(f"{mark} &nbsp;Q{i}. 정답은 <b>{esc(right)}</b>{mine}", unsafe_allow_html=True)
                        st.caption(f"{item['explanation']}  ·  근거: {item['source']}")
                st.metric("점수", f"{score} / {len(ss.quiz)}")

# ── 탭 3 : 관리자 ──
if is_admin():
    with tabs[2]:
        if ADMIN_PASSWORD == "admin1234":
            st.markdown("<div class='notice'><b>기본 관리자 비밀번호를 쓰고 있어요.</b> "
                        "Streamlit Cloud 앱 Settings → Secrets에 ADMIN_PASSWORD를 설정하세요.</div>", unsafe_allow_html=True)
        a1, a2 = st.columns([1, 1.3], gap="large")
        with a1:
            st.markdown('<div class="panel-title">백서 관리</div>', unsafe_allow_html=True)
            with st.container(border=True):
                new_pdf = st.file_uploader("새 백서 PDF 업로드 (자동 벡터화 후 모든 사용자에게 적용)", type=["pdf"], key="admin_pdf")
                if new_pdf is not None and st.button("이 파일을 현재 백서로 등록", type="primary", icon=":material/upload:",
                                                     width="stretch"):
                    digest = register_version(new_pdf.getvalue(), new_pdf.name, ss.user["name"])
                    v = next(x for x in versions() if x["hash"] == digest)
                    try:
                        with st.spinner("새 백서를 벡터화하고 RAG 체인을 구성하고 있어요…"):
                            build_assistant(v["path"], v["hash"])
                        st.rerun()                      # 모든 사용자에게 새 버전 적용 (화면에 '백서가 바뀌었어요' 알림)
                    except Exception as e:
                        notice(f"벡터화에 실패했어요: {e}")
            st.markdown('<div class="panel-title" style="margin-top:14px">버전 기록</div>', unsafe_allow_html=True)
            for v in reversed(versions()):
                with st.container(border=True):
                    is_active = version is not None and v["hash"] == version["hash"]
                    st.markdown(f"<b>{esc(version_label(v))}</b>" + ("<span class='badge-on'>사용 중</span>" if is_active else ""),
                                unsafe_allow_html=True)
                    st.caption(f"{v['uploaded_at']} · {v['uploaded_by']}")
                    if not is_active:
                        st.button("이 버전으로 변경", key=f"use_{v['hash']}", on_click=set_active, args=(v["hash"],))
            if not versions():
                st.info("아직 등록된 백서가 없어요. 위에서 PDF를 올려 주세요.")

        with a2:
            st.markdown('<div class="panel-title">질문 · 평가 현황</div>', unsafe_allow_html=True)
            logs = shared()["logs"]
            df = pd.DataFrame(logs) if logs else pd.DataFrame(
                columns=["id", "time", "user", "question", "input", "answer", "not_found", "rating", "sources", "doc"])
            need = df[(df["rating"] == "👎") | (df["not_found"] == True)] if len(df) else df  # noqa: E712
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("전체 질문", len(df))
            m2.metric("좋아요", int((df["rating"] == "👍").sum()) if len(df) else 0)
            m3.metric("아쉬워요", int((df["rating"] == "👎").sum()) if len(df) else 0)
            m4.metric("백서 미확인", int(df["not_found"].sum()) if len(df) else 0)
            st.markdown("**백서 개선이 필요한 질문** <span class='muted'>(아쉬워요 평가 또는 백서에 없는 질문)</span>",
                        unsafe_allow_html=True)
            if len(need):
                view = need.assign(사유=need.apply(lambda r: "아쉬워요 평가" if r["rating"] == "👎" else "백서에 없음", axis=1))
                st.dataframe(view[["time", "user", "사유", "question", "answer"]].rename(
                    columns={"time": "시간", "user": "사용자", "question": "질문", "answer": "답변"}), hide_index=True, width="stretch")
            else:
                st.caption("아직 개선이 필요한 질문이 없어요.")
            e1, e2 = st.columns(2)
            e1.download_button("개선 필요 질문 CSV", need.to_csv(index=False).encode("utf-8-sig"), icon=":material/download:",
                               file_name="aivle_improve_questions.csv", mime="text/csv", width="stretch", disabled=not len(need))
            e2.download_button("전체 질문 기록 CSV", df.to_csv(index=False).encode("utf-8-sig"), icon=":material/download:",
                               file_name="aivle_all_questions.csv", mime="text/csv", width="stretch", disabled=not len(df))
            st.markdown("<div class='welcome'><b>백서 개선 루프</b><br>① 개선 필요 질문 CSV 내려받기 → ② 백서에 답변 보완 → "
                        "③ 왼쪽에서 새 버전 업로드 → ④ 같은 질문으로 다시 확인</div>", unsafe_allow_html=True)

# ════════════════════════════════════════════════════════
# 7. 질문 처리 (화면을 먼저 그린 뒤 실행 → 첫 접속 때도 채팅창이 바로 보임)
# ════════════════════════════════════════════════════════
question, meta = None, {}
try:
    question, meta = parse_submission(submission)
except Exception as e:
    notice(f"음성/파일을 읽지 못했어요: {e}", status_slot)
if not question and ss.pending:
    question, meta = ss.pending, {"kind": "text", "display": ss.pending}
    ss.pending = None

if question and ready_to_chat:
    with status_slot.container():
        v = ensure_chain()
    history = to_history(ss.messages)
    ss.messages.append({"role": "user", "content": question, **meta})
    with box:
        with st.chat_message("user", avatar=AVATAR["user"]):
            role_marker("user")
            st.markdown(meta.get("display") or question)
        with st.chat_message("assistant", avatar=AVATAR["assistant"]):
            role_marker("assistant")
            mid = uuid.uuid4().hex[:10]
            try:
                docs = ss.store.similarity_search(make_search_query({"question": question, "history": history}), k=TOP_K)
                answer = st.write_stream(guard_not_found(
                    ss.chain.stream({"question": question, "history": history, "docs": docs})))
                sources = [{"n": i, "label": d.metadata.get("source_label", ""), "text": d.page_content.split("\n", 1)[-1]}
                           for i, d in enumerate(docs, start=1)]
            except Exception as e:
                answer, sources = f"답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요. ({e})", []
                st.markdown(answer)
    not_found = answer.strip().startswith(NOT_FOUND)
    ss.messages.append({"role": "assistant", "content": answer, "sources": sources, "id": mid,
                        "not_found": not_found, "rating": ""})
    log_answer({"id": mid, "time": now_kst(), "user": ss.user["name"], "question": question[:1000],
                "input": meta.get("kind", "text"), "answer": answer[:2000], "not_found": not_found, "rating": "",
                "sources": " / ".join(s["label"] for s in sources), "doc": version_label(v) if v else ""})
    st.rerun()
elif ready_to_chat and (ss.chain is None or ss.document_name != version["hash"]):
    try:
        with status_slot.container():                   # 첫 접속 : 화면을 먼저 보여 준 뒤 백서 준비
            ensure_chain()
        st.rerun()
    except Exception as e:
        notice(f"백서를 준비하지 못했어요: {e}", status_slot)
