"""
AIVLE School 학습 도우미 (미션 3·4)

- 01번 노트북의 rag_pipeline()을 그대로 사용 (PDF 읽기 → 정제 → 구조 기반 분할 → Chroma → 검색 → 답변)
- 01번과 달라진 점 (미션 4 고도화)
    ① 시스템 메시지로 역할 지정 : 문서 근거 + 도전을 응원하는 선배·학습 코치
    ② 대화 기록 반영 : '그럼 언제 받아요?' 같은 후속 질문도 이해
    ③ collection_name 인자 : 업로드한 문서마다 따로 저장해 다른 사용자의 문서와 섞이지 않음
"""
import hashlib
import os
import random
import re
import tempfile

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

pymupdf.set_messages(path=os.devnull)
st.set_page_config(page_title="AIVLE School 학습 도우미", page_icon="🎓", layout="centered",
                   initial_sidebar_state="expanded")
st.logo("https://raw.githubusercontent.com/Jangrae/img/master/aivle.png", size="large")   # 강의 제공 투명 로고

# Streamlit Cloud의 Secrets에 넣은 키를 환경변수로 연결
try:
    if not os.environ.get("OPENAI_API_KEY") and "OPENAI_API_KEY" in st.secrets:
        os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
except Exception:
    pass

# ── 설정 ───────────────────────────────────────────────
DEFAULT_PDF = "AIVLE School 백서.pdf"     # GitHub 저장소에 함께 올린 백서
CHROMA_DIR = "chroma_db"
EMBED_MODEL = "text-embedding-3-small"
LLM_MODEL = "gpt-4o-mini"
TOP_K = 5                 # 검색할 청크 수
HISTORY_TURNS = 6         # 답변할 때 참고할 최근 대화 수(질문+답변)


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
    s = line.replace("**", "").replace("<br>", " ")
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


# 시스템 메시지 (미션 4) : 역할 + 답변 원칙
SYSTEM_PROMPT = """당신은 'AIVLE School 학습 도우미'입니다.
KT AIVLE School을 먼저 경험한 선배이자 학습 코치처럼, 지원을 고민하는 사람과 교육생에게
정확한 정보와 도전할 용기를 함께 전합니다.

[답변 원칙]
1. 사실은 반드시 [참고 문서]에서만 가져옵니다. 문서에 없는 내용은 지어내지 말고
   "제가 가진 문서에서는 확인되지 않아요."라고 말한 뒤 AIVLE School 공식 홈페이지(aivle.kt.co.kr)나
   AIVLE-EDU 1:1 문의를 안내합니다.
2. 근거로 쓴 문장 끝에 [1], [2]처럼 참고 문서 번호를 붙입니다.
3. 첫 문장에서 질문에 바로 답합니다(두괄식). 항목이 여러 개면 짧은 글머리표로 정리합니다.
4. 지원 자격·일정·금액·출결·수료 기준은 기수마다 달라질 수 있으니 최신 모집 공고를 확인하라고 한 줄 덧붙입니다.
5. 수료생 후기는 '한 선배의 경험'으로 소개하고, 모든 사람에게 해당하는 사실처럼 단정하지 않습니다.
6. 답변 마지막 줄에 질문자가 바로 해 볼 수 있는 행동 하나를 "💡 다음 한 걸음:"으로 제안합니다.
   망설임이나 걱정이 보이면 문서 근거를 들어 따뜻하게 응원하되, 합격·취업을 보장하는 표현은 쓰지 않습니다.
7. 이전 대화를 참고해 '그럼', '그건' 같은 후속 질문의 맥락을 이해합니다.
8. 에이블스쿨·학습과 관계없는 질문이면 정중하게 도울 수 있는 범위를 알려 주고, 물어볼 만한 질문을 하나 제안합니다.

[참고 문서]
{context}"""

FOLLOW_UP_WORDS = ("그럼", "그러면", "그건", "그거", "그게", "이건", "이거", "거기", "그때", "그 ", "더 ", "또 ", "왜")


def to_inputs(x):
    """'질문' 문자열 또는 {'question': ..., 'history': [...]} → 체인 입력 형식으로 통일"""
    if isinstance(x, dict):
        return {"question": x["question"], "history": x.get("history") or []}
    return {"question": x, "history": []}


def make_search_query(inputs):
    """짧은 후속 질문이면 직전 질문을 붙여 검색 (예: '그럼 언제 받아요?' → '훈련장려금은 얼마예요? 그럼 언제 받아요?')"""
    question, history = inputs["question"], inputs.get("history") or []
    last_q = next((m.content for m in reversed(history) if isinstance(m, HumanMessage)), "")
    if last_q and (len(question) <= 15 or question.startswith(FOLLOW_UP_WORDS)):
        return f"{last_q} {question}"
    return question


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
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder("history"),
        ("human", "{question}"),
    ])

    # RAG 체인 구성 : 문자열 질문과 {"question": ..., "history": [...]} 모두 받을 수 있음
    rag_chain = (
        RunnableLambda(to_inputs)
        | RunnablePassthrough.assign(context=RunnableLambda(make_search_query) | retriever | format_docs)
        | prompt
        | llm
        | StrOutputParser()
    )

    return rag_chain


# ════════════════════════════════════════════════════════
# 2. 앱 준비
# ════════════════════════════════════════════════════════
# 벡터화가 반복되지 않도록 두 겹으로 막음
#   ① st.session_state.chain : 질문할 때마다 스크립트가 다시 실행돼도 저장된 체인을 그대로 사용
#   ② st.cache_resource      : 같은 문서를 여러 사용자가 열어도 rag_pipeline()은 문서당 한 번만 실행
@st.cache_resource(show_spinner=False)
def build_assistant(pdf_path, collection_name):
    """rag_pipeline()으로 체인을 만들고, 근거 표시용 저장소 핸들도 함께 반환"""
    chain = rag_pipeline(pdf_path, collection_name)
    store = Chroma(collection_name=collection_name, embedding_function=OpenAIEmbeddings(model=EMBED_MODEL),
                   persist_directory=CHROMA_DIR)
    return chain, store, store._collection.count()


def doc_info(path, name, is_default):
    digest = hashlib.md5(open(path, "rb").read()).hexdigest()[:12]
    return {"name": name, "path": path, "collection": f"doc_{digest}", "hash": digest, "default": is_default}


def default_doc():
    try:
        path = resolve_path(DEFAULT_PDF)
    except FileNotFoundError:
        return None
    return doc_info(path, os.path.basename(path), True)


RECOMMENDED = {
    "🎯 지원 준비": ["비전공자도 커리큘럼을 따라갈 수 있을까요?",
                  "AI 개발자 트랙과 DX 컨설턴트 트랙은 어떻게 달라요?",
                  "지원 전에 미리 공부해 두면 좋은 건 뭐예요?"],
    "💳 지원금·제도": ["훈련장려금은 얼마나, 언제 받을 수 있나요?",
                   "국민내일배움카드는 꼭 있어야 하나요?",
                   "아르바이트를 하면서 교육을 들어도 되나요?"],
    "📚 교육 생활": ["하루 수업 시간표는 어떻게 되나요?",
                  "수료하려면 출석을 얼마나 해야 하나요?",
                  "진도를 놓쳤을 때는 뭐부터 해야 하나요?"],
    "🚀 프로젝트·취업": ["팀 프로젝트 주제는 어떻게 정하면 좋을까요?",
                    "프로젝트 경험을 포트폴리오로 어떻게 정리하나요?",
                    "수료 후 취업 연계는 어떻게 도와주나요?"],
}
UPLOAD_QUESTIONS = {
    "📄 이 문서 살펴보기": ["이 문서의 핵심 내용을 요약해 주세요.",
                      "이 문서에서 꼭 알아야 할 3가지는 뭐예요?",
                      "이 문서를 바탕으로 1주 학습 계획을 세워 주세요."],
}


def question_pool(doc):
    groups = RECOMMENDED if doc["default"] else UPLOAD_QUESTIONS
    return [q for qs in groups.values() for q in qs]


# 세션 상태
ss = st.session_state
ss.setdefault("messages", [])          # [{"role", "content", "sources"}]
ss.setdefault("uploader_key", 0)
ss.setdefault("pending", None)
ss.setdefault("chain", None)           # 현재 문서의 RAG 체인
ss.setdefault("document_name", None)   # 체인을 만든 문서 이름
if "doc" not in ss:
    ss.doc = default_doc()


def ask(question):
    ss.pending = question


def clear_chat():
    ss.messages = []


def back_to_default():
    ss.doc = default_doc()
    ss.messages = []
    ss.uploader_key += 1               # 업로드 위젯 비우기


def chat_markdown():
    lines = ["# AIVLE School 학습 도우미 대화 기록\n"]
    for m in ss.messages:
        lines.append(f"**{'🙋 질문' if m['role'] == 'user' else '🎓 답변'}**\n\n{m['content']}\n")
        for s in m.get("sources", []):
            lines.append(f"- 근거 {s['label']}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════
# 3. 화면
# ════════════════════════════════════════════════════════
st.markdown("""
<style>
.hero {background: linear-gradient(135deg, #0b1f2a 0%, #0f5f5a 55%, #2fb3a6 100%);
       padding: 26px 28px; border-radius: 18px; margin-bottom: 14px;}
.hero .badge {display: inline-block; background: rgba(255,255,255,.16); color: #ccfbf1;
              padding: 3px 12px; border-radius: 999px; font-size: .78rem; margin-bottom: 10px;}
.hero h1 {color: #ffffff; font-size: 1.85rem; margin: 0 0 6px 0; padding: 0;}
.hero p {color: #d5f5f0; margin: 0; font-size: .98rem; line-height: 1.55;}
.stat {display: flex; gap: 8px; flex-wrap: wrap; margin: 2px 0 10px 0;}
.stat span {font-size: .8rem; padding: 3px 10px; border-radius: 999px;
            background: rgba(47,179,166,.10); border: 1px solid rgba(47,179,166,.35);}
.src {font-size: .86rem; border-left: 3px solid #2fb3a6; padding: 2px 10px; margin: 8px 0 2px 0; font-weight: 600;}
.src-body {font-size: .82rem; opacity: .8; padding-left: 13px; white-space: pre-wrap;}
</style>
""", unsafe_allow_html=True)

# ── 사이드바 : 문서 업로드 / 설정 ──
with st.sidebar:
    st.markdown("#### 📁 관리자 · 파일 업로드")
    doc_slot = st.container()
    uploaded = st.file_uploader("PDF를 올리면 그 문서를 기준으로 답해요", type=["pdf"],
                                key=f"uploader_{ss.uploader_key}")
    if uploaded is not None:
        data = uploaded.getvalue()
        digest = hashlib.md5(data).hexdigest()[:12]
        if not ss.doc or ss.doc["hash"] != digest:
            # ① 임시 폴더에 .pdf 파일 생성 ② 업로드 내용 저장 ③ 파일 경로 확보 ④ 경로를 rag_pipeline()에 전달
            #    (원래 파일 이름을 유지해 청크 머리표가 '[강의자료]'처럼 읽기 좋게 남도록 함)
            folder = os.path.join(tempfile.gettempdir(), "aivle_uploads", digest)
            os.makedirs(folder, exist_ok=True)
            safe_name = re.sub(r"[^\w가-힣.\- ]", "_", os.path.splitext(uploaded.name)[0]) + ".pdf"
            path = os.path.join(folder, safe_name)
            with open(path, "wb") as f:
                f.write(data)
            ss.doc = doc_info(path, uploaded.name, False)
            ss.messages = []
            st.toast(f"'{uploaded.name}' 문서로 새 대화를 시작해요", icon="📄")

    st.divider()
    st.markdown("#### 💡 추천 질문")
    with st.expander("질문 목록 펼치기"):
        for q in (question_pool(ss.doc) if ss.doc else []):
            st.button(q, key=f"side_{q}", on_click=ask, args=(q,), width="stretch", type="tertiary")

    st.divider()
    st.markdown("#### 💬 대화 관리")
    show_sources = st.toggle("답변 근거 보기", value=True)
    c1, c2 = st.columns(2)
    c1.button("대화 삭제", icon="🗑️", on_click=clear_chat, width="stretch", disabled=not ss.messages)
    c2.download_button("대화 저장", data=chat_markdown(), file_name="aivle_chat.md", icon="💾",
                       mime="text/markdown", width="stretch", disabled=not ss.messages)
    st.divider()
    st.caption("답변은 업로드된 문서(기본: AIVLE School 백서)를 근거로 합니다. "
               "모집 조건·일정은 기수마다 달라질 수 있으니 공식 모집 공고를 꼭 확인하세요.")

# ── 문서 준비 ──
st.markdown("""
<div class="hero">
  <div class="badge">KT AIVLE School · 문서 기반 RAG</div>
  <h1>🎓 AIVLE School 학습 도우미</h1>
  <p>지원 준비부터 교육 생활, 프로젝트와 취업까지.<br>백서를 근거로 답하고, 당신의 다음 한 걸음을 함께 고민할게요.</p>
</div>
""", unsafe_allow_html=True)

if not os.environ.get("OPENAI_API_KEY"):
    st.error("OPENAI_API_KEY가 없습니다. Streamlit Cloud라면 앱 Settings → Secrets에 `OPENAI_API_KEY = \"sk-...\"`를 추가하세요.")
    st.stop()
if ss.doc is None:
    st.info(f"기본 문서 `{DEFAULT_PDF}`가 저장소에 없어요. 왼쪽에서 PDF를 업로드해 주세요.")
    st.stop()

doc = ss.doc
if ss.chain is None or ss.document_name != doc["hash"]:          # 문서가 바뀔 때만 체인 준비
    try:
        with st.spinner(f"'{doc['name']}' 문서를 처리하고 있습니다… (문서마다 처음 한 번만 걸려요)"):
            ss.chain, ss.store, ss.n_chunks = build_assistant(doc["path"], doc["collection"])
        ss.document_name = doc["hash"]
    except Exception as e:
        st.error(f"문서를 준비하지 못했어요: {e}")
        st.stop()
chain, store, n_chunks = ss.chain, ss.store, ss.n_chunks

with doc_slot:
    st.success(f"**{doc['name']}**", icon="📘" if doc["default"] else "📄")
    st.caption(f"검색 단위 {n_chunks}개로 나눠 저장했어요.")
    if not doc["default"] and default_doc():
        st.button("백서로 돌아가기", icon="↩️", on_click=back_to_default, width="stretch")

st.markdown(
    f'<div class="stat"><span>📘 {doc["name"]}</span><span>🔎 검색 단위 {n_chunks}개</span>'
    f'<span>💬 질문 {sum(m["role"] == "user" for m in ss.messages)}개</span></div>',
    unsafe_allow_html=True,
)


def render_sources(sources):
    if not show_sources or not sources:
        return
    with st.expander(f"📎 답변 근거 {len(sources)}개"):
        for s in sources:
            st.markdown(f'<div class="src">[{s["n"]}] {s["label"]}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="src-body">{s["snippet"]}</div>', unsafe_allow_html=True)


def to_history(messages):
    return [HumanMessage(m["content"]) if m["role"] == "user" else AIMessage(m["content"])
            for m in messages[-HISTORY_TURNS:]]


AVATAR = {"user": "🙋", "assistant": "🎓"}

# ── 지난 대화 ──
for m in ss.messages:
    with st.chat_message(m["role"], avatar=AVATAR[m["role"]]):
        st.markdown(m["content"])
        if m["role"] == "assistant":
            render_sources(m.get("sources", []))

# ── 첫 화면 : 추천 질문 ──
if not ss.messages and not ss.pending:
    st.markdown("#### 무엇이 궁금하세요?")
    groups = RECOMMENDED if doc["default"] else UPLOAD_QUESTIONS
    cols = st.columns(2)
    for i, (title, qs) in enumerate(groups.items()):
        with cols[i % 2].container(border=True):
            st.markdown(f"**{title}**")
            for q in qs:
                st.button(q, key=f"rec_{title}_{q}", on_click=ask, args=(q,), width="stretch")

# ── 질문 처리 ──
question = st.chat_input("에이블스쿨에 대해 무엇이든 물어보세요")
if not question and ss.pending:
    question, ss.pending = ss.pending, None

if question:
    history = to_history(ss.messages)
    ss.messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=AVATAR["user"]):
        st.markdown(question)

    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        try:
            answer = st.write_stream(chain.stream({"question": question, "history": history}))
            docs = store.similarity_search(make_search_query({"question": question, "history": history}), k=TOP_K)
            sources = [{"n": i, "label": d.metadata.get("source_label", ""),
                        "snippet": d.page_content.split("\n", 1)[-1][:300]}
                       for i, d in enumerate(docs, start=1)]
        except Exception as e:
            answer, sources = f"⚠️ 답변을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요. ({e})", []
            st.markdown(answer)
        render_sources(sources)
    ss.messages.append({"role": "assistant", "content": answer, "sources": sources})
    st.rerun()      # 사이드바(대화 저장·삭제)와 질문 수를 최신 상태로 다시 그림

# ── 이어서 물어보기 ──
if ss.messages and ss.messages[-1]["role"] == "assistant":
    asked = {m["content"] for m in ss.messages if m["role"] == "user"}
    rest = [q for q in question_pool(doc) if q not in asked]
    picks = random.Random(len(ss.messages)).sample(rest, min(3, len(rest)))
    if picks:
        st.caption("이어서 물어보기")
        cols = st.columns(len(picks))
        for col, q in zip(cols, picks):
            col.button(q, key=f"next_{q}", on_click=ask, args=(q,), width="stretch", wrap=True)

