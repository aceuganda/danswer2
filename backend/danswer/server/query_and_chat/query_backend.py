from fastapi import APIRouter, Request
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse 
import aiohttp

from danswer.auth.users import current_admin_user
from danswer.auth.users import current_user
from danswer.configs.constants import DocumentSource
from danswer.db.engine import get_session
from danswer.db.models import User
from danswer.db.tag import get_tags_by_value_prefix_for_source_types
from danswer.document_index.factory import get_default_document_index
from danswer.document_index.vespa.index import VespaIndex
from danswer.one_shot_answer.answer_question import stream_search_answer
from danswer.one_shot_answer.models import DirectQARequest
from danswer.search.access_filters import build_access_filters_for_user
from danswer.search.danswer_helper import recommend_search_flow
from danswer.search.models import IndexFilters
from danswer.search.models import SearchDoc
from danswer.search.search_runner import chunks_to_search_docs
from danswer.secondary_llm_flows.query_validation import get_query_answerability
from danswer.secondary_llm_flows.query_validation import stream_query_answerability
from danswer.server.query_and_chat.models import AdminSearchRequest
from danswer.server.query_and_chat.models import AdminSearchResponse
from danswer.server.query_and_chat.models import HelperResponse
from danswer.server.query_and_chat.models import QueryValidationResponse
from danswer.server.query_and_chat.models import SimpleQueryRequest
from danswer.server.query_and_chat.models import SourceTag
from danswer.server.query_and_chat.models import TagResponse
from danswer.utils.logger import setup_logger

logger = setup_logger()

admin_router = APIRouter(prefix="/admin")
basic_router = APIRouter(prefix="/query")


@admin_router.post("/search")
def admin_search(
    question: AdminSearchRequest,
    user: User | None = Depends(current_admin_user),
    db_session: Session = Depends(get_session),
) -> AdminSearchResponse:
    query = question.query
    logger.info(f"Received admin search query: {query}")

    user_acl_filters = build_access_filters_for_user(user, db_session)
    final_filters = IndexFilters(
        source_type=question.filters.source_type,
        document_set=question.filters.document_set,
        time_cutoff=question.filters.time_cutoff,
        access_control_list=user_acl_filters,
    )
    document_index = get_default_document_index()
    if not isinstance(document_index, VespaIndex):
        raise HTTPException(
            status_code=400,
            detail="Cannot use admin-search when using a non-Vespa document index",
        )

    matching_chunks = document_index.admin_retrieval(query=query, filters=final_filters)

    documents = chunks_to_search_docs(matching_chunks)

    # Deduplicate documents by id
    deduplicated_documents: list[SearchDoc] = []
    seen_documents: set[str] = set()
    for document in documents:
        if document.document_id not in seen_documents:
            deduplicated_documents.append(document)
            seen_documents.add(document.document_id)
    return AdminSearchResponse(documents=deduplicated_documents)


@basic_router.get("/valid-tags")
def get_tags(
    match_pattern: str | None = None,
    # If this is empty or None, then tags for all sources are considered
    sources: list[DocumentSource] | None = None,
    allow_prefix: bool = True,  # This is currently the only option
    _: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> TagResponse:
    if not allow_prefix:
        raise NotImplementedError("Cannot disable prefix match for now")

    db_tags = get_tags_by_value_prefix_for_source_types(
        tag_value_prefix=match_pattern,
        sources=sources,
        db_session=db_session,
    )
    server_tags = [
        SourceTag(
            tag_key=db_tag.tag_key, tag_value=db_tag.tag_value, source=db_tag.source
        )
        for db_tag in db_tags
    ]
    return TagResponse(tags=server_tags)


@basic_router.post("/search-intent")
def get_search_type(
    simple_query: SimpleQueryRequest, _: User = Depends(current_user)
) -> HelperResponse:
    logger.info(f"Calculating intent for {simple_query.query}")
    return recommend_search_flow(simple_query.query)


@basic_router.post("/query-validation")
def query_validation(
    simple_query: SimpleQueryRequest, _: User = Depends(current_user)
) -> QueryValidationResponse:
    # Note if weak model prompt is chosen, this check does not occur and will simply return that
    # the query is valid, this is because weaker models cannot really handle this task well.
    # Additionally, some weak model servers cannot handle concurrent inferences.
    logger.info(f"Validating query: {simple_query.query}")
    reasoning, answerable = get_query_answerability(simple_query.query)
    return QueryValidationResponse(reasoning=reasoning, answerable=answerable)


@basic_router.post("/stream-query-validation")
def stream_query_validation(
    simple_query: SimpleQueryRequest, _: User = Depends(current_user)
) -> StreamingResponse:
    # Note if weak model prompt is chosen, this check does not occur and will simply return that
    # the query is valid, this is because weaker models cannot really handle this task well.
    # Additionally, some weak model servers cannot handle concurrent inferences.
    logger.info(f"Validating query: {simple_query.query}")
    return StreamingResponse(
        stream_query_answerability(simple_query.query), media_type="application/json"
    )


@basic_router.post("/stream-answer-with-quote")
def get_answer_with_quote(
    query_request: DirectQARequest,
    user: User = Depends(current_user),
    db_session: Session = Depends(get_session),
) -> StreamingResponse:
    query = query_request.messages[0].message
    logger.info(f"Received query for one shot answer with quotes: {query}")
    packets = stream_search_answer(
        query_req=query_request, user=user, db_session=db_session
    )
    return StreamingResponse(packets, media_type="application/json")


@basic_router.post("/generate-luganda")
async def generate(req:Request) -> StreamingResponse:
    logger.info(f"Received request for Luganda translation: {req}")

    try:
        input_data = await req.json()   # Assuming query is in query parameter
        if not input_data:
            return JSONResponse({"error": "Query is required"}, status_code=400)

        async def stream_words():
            async with aiohttp.ClientSession() as session:  # Use aiohttp for asynchronous requests
                async with session.post("http://65.108.33.93:5000/generate", json={"prompt": input_data['prompt'], "stream": True}) as response:
                    if response.status != 200:
                        raise Exception(f"Error fetching response: {response.status}")

                    async for chunk in response.content.iter_chunked(1024):  # Stream in chunks
                        decoded_chunk = chunk.decode("utf-8")
                        lines = decoded_chunk.splitlines()
                        for line in lines:
                            if line.startswith("data: "):
                                word = line[6:].strip()
                                yield word + " "

        return StreamingResponse(stream_words(), media_type="text/event-stream")

    except Exception as e:
        logger.exception(e)
        return JSONResponse({"error": "Failed to process translation"}, status_code=500)