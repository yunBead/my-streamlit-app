
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import streamlit as st
from langchain_chroma import Chroma
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_pymupdf4llm import PyMuPDF4LLMLoader
from operator import itemgetter


st.set_page_config(
    page_title="AIVLE School 학습 도우미",
    page_icon="📘",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+KR:wght@400;500;600;700;800&display=swap');
    :root { --coral:#F47F70; --coral-dark:#D95F52; --mint:#BFE9D8; --mint-dark:#4D9D82; --ink:#26353A; --muted:#718087; --paper:#FBFCFA; }
    html, body, [class*="css"] { font-family:'DM Sans','Noto Sans KR',sans-serif; color:var(--ink); }
    .stApp { background:radial-gradient(ellipse at 8% 0%, #FFF0EC 0, transparent 32%), radial-gradient(ellipse at 95% 12%, #E9F8F1 0, transparent 28%), var(--paper); }
    [data-testid="stSidebar"] { background:linear-gradient(180deg,#F0FAF5 0%,#F8FBF9 100%); border-right:1px solid #E2EEE8; }
    [data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3 { color:#263D3A; }
    .hero { position:relative; overflow:hidden; padding:2.2rem 2.4rem; border-radius:28px; background:linear-gradient(115deg,#FFF0EC 0%,#FFF7F3 48%,#E8F7F0 100%); border:1px solid #F3E4DE; box-shadow:0 14px 40px rgba(66,81,75,.07); margin:.4rem 0 1.5rem; }
    .hero:after { content:'✳'; position:absolute; right:5%; top:-38px; font-size:170px; color:rgba(77,157,130,.12); transform:rotate(18deg); }
    .eyebrow { color:var(--coral-dark); letter-spacing:.13em; text-transform:uppercase; font-size:.76rem; font-weight:800; margin-bottom:.7rem; }
    .hero h1 { color:#263D3A; font-size:clamp(2rem,4vw,3rem); line-height:1.18; letter-spacing:-.04em; margin:0 0 .7rem; }
    .hero p { color:#5D706F; font-size:1.04rem; line-height:1.7; max-width:680px; margin:0; }
    .section-label { color:#7E8E8B; text-transform:uppercase; letter-spacing:.12em; font-size:.72rem; font-weight:800; margin:.2rem 0 .55rem; }
    .soft-card { background:rgba(255,255,255,.84); border:1px solid #E7EEEA; border-radius:20px; padding:1.1rem 1.2rem; box-shadow:0 8px 26px rgba(53,75,67,.045); }
    .doc-pill { display:inline-block; border-radius:99px; padding:.38rem .72rem; color:#34775F; background:#E5F5EC; font-weight:700; font-size:.82rem; }
    .stButton>button { border-radius:13px; border:1px solid #DCEBE4; background:#FFFFFF; color:#36564D; font-weight:650; transition:all .18s ease; }
    .stButton>button:hover { border-color:#ECA095; color:#B9564C; background:#FFF6F3; }
    .stButton>button[kind="primary"] { background:var(--coral); border-color:var(--coral); color:white; }
    .stChatMessage { background:rgba(255,255,255,.82); border:1px solid #E9EFEC; border-radius:18px; padding:.7rem 1rem; }
    [data-testid="stChatInput"] textarea { border-radius:16px; }
    [data-testid="stFileUploader"] section { border:1.5px dashed #A8D6C3; border-radius:17px; background:rgba(255,255,255,.72); }
    .stCaption { color:#7D8A8F; }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_api_key() -> str | None:
    try:
        secret = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        secret = None
    return secret or os.getenv("OPENAI_API_KEY")


def format_docs(docs) -> str:
    parts = []
    for doc in docs:
        source = Path(str(doc.metadata.get("source", "AIVLE School 백서"))).name
        page = doc.metadata.get("page")
        citation = f"출처: {source}" + (f", p.{page}" if page is not None else "")
        parts.append(f"[{citation}]\n{doc.page_content}")
    return "\n\n---\n\n".join(parts)


def rag_pipeline(filepath: str):
    """PDF 경로를 받아 인덱싱 여부를 확인하고 RAG 체인을 반환합니다."""
    pdf_path = Path(filepath).expanduser().resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("RAG 파이프라인에는 존재하는 PDF 파일의 경로를 전달해야 합니다.")

    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    store_dir = Path("chroma_db") / source_hash
    store_dir.mkdir(parents=True, exist_ok=True)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(
        collection_name="aivle_docs",
        embedding_function=embeddings,
        persist_directory=str(store_dir),
    )

    stored = vectorstore.get(include=["metadatas"])
    ids = stored.get("ids", [])
    metadatas = stored.get("metadatas", []) or []
    expected_count = next(
        (m.get("source_chunk_count") for m in metadatas if m and m.get("source_hash") == source_hash),
        None,
    )
    already_indexed = bool(ids) and len(ids) == expected_count and all(
        m and m.get("source_hash") == source_hash for m in metadatas
    )

    if not already_indexed:
        docs = PyMuPDF4LLMLoader(str(pdf_path)).load()
        docs = [doc for doc in docs if doc.page_content and doc.page_content.strip()]
        if not docs:
            raise ValueError("PDF에서 검색할 텍스트를 읽지 못했습니다. 텍스트가 포함된 PDF를 확인해 주세요.")
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=600,
            chunk_overlap=100,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        chunks = splitter.split_documents(docs)
        for chunk in chunks:
            chunk.metadata.update({"source_hash": source_hash, "source": "AIVLE School 백서", "source_chunk_count": len(chunks)})
        vectorstore.reset_collection()
        vectorstore.add_documents(
            chunks,
            ids=[f"{source_hash[:16]}-{i}" for i in range(len(chunks))],
        )

    retriever = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={"k": 6, "fetch_k": 24, "lambda_mult": 0.5},
    )
    system_prompt = """당신은 KT AIVLE School 학습 도우미입니다. 한국어로 친절하고 구체적으로 답합니다.
아래 검색 문맥에 담긴 정보만 사실로 설명하고, 공식 정보와 일반적인 학습 제안은 구분합니다.
문맥에서 확인할 수 없는 내용은 추측하거나 공식 사실처럼 말하지 말고 '현재 업로드된 백서에서 확인되지 않습니다.'라고 밝힌 뒤 공식 공지 또는 담당자 확인을 안내합니다.
절차나 계획을 물으면 문서 근거를 먼저 요약하고, 바로 실행할 수 있는 작은 다음 단계를 제안합니다. 짧은 답에도 근거가 있으면 문맥의 출처 표기를 함께 제시합니다.

[검색된 백서 내용]
{context}"""
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ]
    )
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, streaming=True)
    return (
        {
            "context": itemgetter("input") | retriever | format_docs,
            "history": itemgetter("history"),
            "input": itemgetter("input"),
        }
        | prompt
        | llm
        | StrOutputParser()
    )


@st.cache_resource(show_spinner=False, max_entries=4)
def get_cached_chain(pdf_path: str, pdf_hash: str):
    # Hash가 캐시 키에 포함되어 문서가 바뀌면 새 체인을 만들고, 같은 문서면 재사용합니다.
    return rag_pipeline(pdf_path)


def initialize_state():
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("active_document_hash", None)
    st.session_state.setdefault("suggested_question", None)


initialize_state()
api_key = get_api_key()
if api_key:
    os.environ["OPENAI_API_KEY"] = api_key

with st.sidebar:
    st.markdown("## 📗 AIVLE 학습 도우미")
    st.caption("백서를 근거로 학습과 프로젝트 준비를 함께 정리해요.")
    st.markdown('<div class="section-label">학습 백서 업로드</div>', unsafe_allow_html=True)
    uploaded_pdf = st.file_uploader(
        "AIVLE School 백서 PDF",
        type=["pdf"],
        accept_multiple_files=False,
        help="PDF를 올리면 문서를 색인한 뒤 질문에 답합니다.",
    )
    st.caption("PDF 파일만 지원합니다. 문서 내용은 이 앱의 RAG 검색에 사용됩니다.")
    st.divider()
    if uploaded_pdf is not None:
        st.markdown('<span class="doc-pill">● 백서 업로드됨</span>', unsafe_allow_html=True)
        if st.button("대화 내용 지우기", use_container_width=True):
            st.session_state.messages = []
            st.rerun()
    else:
        st.info("먼저 백서 PDF를 업로드해 주세요.")
    if not api_key:
        st.warning("관리자는 Streamlit Secrets에 OPENAI_API_KEY를 설정해야 합니다.")
    st.markdown("\n")
    st.caption("AIVLE Whitepaper · 학습 근거 기반 응답")

st.markdown(
    """
    <section class="hero">
      <div class="eyebrow">AIVLE SCHOOL · YOUR LEARNING COMPANION</div>
      <h1>배움의 다음 단계,<br>함께 찾아볼까요?</h1>
      <p>백서 내용을 바탕으로 과정과 학습 전략을 정리하고, 지금 바로 시작할 수 있는 다음 한 걸음을 제안합니다.</p>
    </section>
    """,
    unsafe_allow_html=True,
)

if uploaded_pdf is None:
    st.markdown('<div class="soft-card">왼쪽에서 백서 PDF를 업로드하면 대화를 시작할 수 있어요. 업로드한 백서에 없는 내용은 확인되지 않았다고 분명히 안내합니다.</div>', unsafe_allow_html=True)
    active_path = None
    chain = None
else:
    uploaded_bytes = uploaded_pdf.getvalue()
    if not uploaded_bytes.startswith(b"%PDF-"):
        st.error("파일 내용이 정상적인 PDF 형식인지 확인해 주세요.")
        active_path = None
        chain = None
    elif not api_key:
        st.error("OPENAI_API_KEY가 설정되지 않아 RAG 체인을 준비할 수 없습니다.")
        active_path = None
        chain = None
    else:
        document_hash = hashlib.sha256(uploaded_bytes).hexdigest()
        upload_dir = Path("data") / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        active_path = (upload_dir / f"{document_hash}.pdf").resolve()
        if not active_path.exists():
            active_path.write_bytes(uploaded_bytes)
        if st.session_state.active_document_hash != document_hash:
            st.session_state.messages = []
            st.session_state.active_document_hash = document_hash
        try:
            with st.spinner("백서를 확인하고 검색 준비를 하고 있어요…"):
                chain = get_cached_chain(str(active_path), document_hash)
            st.markdown(f'<span class="doc-pill">✓ {uploaded_pdf.name} · 검색 준비 완료</span>', unsafe_allow_html=True)
        except Exception as exc:
            chain = None
            st.error(f"백서 검색 준비 중 문제가 발생했습니다: {exc}")

st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
st.markdown('<div class="section-label">이런 질문부터 시작해 보세요</div>', unsafe_allow_html=True)
suggestions = [
    "AI 트랙과 DX 트랙은 어떤 차이가 있나요?",
    "이번 주 학습 계획을 어떻게 세우면 좋을까요?",
    "프로젝트에서 협업을 잘하려면 무엇부터 정할까요?",
]
cols = st.columns(3)
for col, question in zip(cols, suggestions):
    with col:
        if st.button(question, use_container_width=True, disabled=chain is None):
            st.session_state.suggested_question = question

for message in st.session_state.messages:
    role = "user" if isinstance(message, HumanMessage) else "assistant"
    with st.chat_message(role):
        st.markdown(message.content)

prompt_text = st.chat_input(
    "백서에 대해 궁금한 점을 적어 주세요…",
    disabled=chain is None,
)
user_question = prompt_text or st.session_state.suggested_question
st.session_state.suggested_question = None

if user_question and chain is not None:
    history = list(st.session_state.messages)
    st.session_state.messages.append(HumanMessage(content=user_question))
    with st.chat_message("user"):
        st.markdown(user_question)
    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(
                chain.stream({"input": user_question, "history": history})
            )
        except Exception as exc:
            answer = "응답을 만드는 중 문제가 발생했습니다. 잠시 후 다시 질문해 주세요."
            st.error(f"요청 처리 오류: {exc}")
            st.markdown(answer)
    st.session_state.messages.append(AIMessage(content=answer))
