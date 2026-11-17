
# RAG 파이프라인 함수 
def rag_pipeline(filepath):

    # PDF 파일 읽기
    loader = PyMuPDF4LLMLoader(filepath)
    docs = loader.load()

    # 텍스트 분할
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
    chunks = splitter.split_documents(docs)

    # Embedding 모델
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

    # 벡터저장소 만들기
    vectorstore = Chroma(
        persist_directory=path + "chroma_db",
        collection_name="aivle_docs",
        embedding_function=embeddings
    )

    # 기존 데이터 비우기 , 중복 방지
    vectorstore.reset_collection()

    # 문서 추가
    vectorstore.add_documents(chunks)

    # 검색기 구성
    retriever = vectorstore.as_retriever()

    # LLM 선언
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # 프롬프트 구성
    s_msg = """
    1. 반드시 제공된 문서 내용(context)에 근거하여 답변하세요.
    2. 문서에 없는 내용은 추측하지 말고 "업로드된 문서에서 확인되지 않습니다."라고 답하세요.
    3. 답변은 질문자가 성장을 위해 적극적으로 도전하게 만드세요
    [문서]
    {context}"""
    h_msg = "{input}"
    prompt = ChatPromptTemplate.from_messages([("system", s_msg), ("human", h_msg)])

    # RAG 체인 구성
    rag_chain = (
        {"context": itemgetter("input") | retriever | format_docs, "input": itemgetter("input")}
        | prompt
        | llm
        | StrOutputParser()
    )

    return rag_chain
