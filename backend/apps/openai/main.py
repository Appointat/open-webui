import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import List, Optional

import aiohttp
import requests
from apps.webui.models.models import Models
from config import (
    CACHE_DIR,
    ENABLE_MODEL_FILTER,
    ENABLE_OPENAI_API,
    MODEL_FILTER_LIST,
    OPENAI_API_BASE_URLS,
    OPENAI_API_KEYS,
    SRC_LOG_LEVELS,
    AppConfig,
)
from constants import ERROR_MESSAGES
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from utils.task import prompt_template
from utils.utils import (
    get_admin_user,
    get_verified_user,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OPENAI"])

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.state.config = AppConfig()

app.state.config.ENABLE_MODEL_FILTER = ENABLE_MODEL_FILTER
app.state.config.MODEL_FILTER_LIST = MODEL_FILTER_LIST

app.state.config.ENABLE_OPENAI_API = ENABLE_OPENAI_API
app.state.config.OPENAI_API_BASE_URLS = OPENAI_API_BASE_URLS
app.state.config.OPENAI_API_KEYS = OPENAI_API_KEYS

app.state.MODELS = {}


@app.middleware("http")
async def check_url(request: Request, call_next):
    if len(app.state.MODELS) == 0:
        await get_all_models()
    else:
        pass

    response = await call_next(request)
    return response


@app.get("/config")
async def get_config(user=Depends(get_admin_user)):
    return {"ENABLE_OPENAI_API": app.state.config.ENABLE_OPENAI_API}


class OpenAIConfigForm(BaseModel):
    enable_openai_api: Optional[bool] = None


@app.post("/config/update")
async def update_config(form_data: OpenAIConfigForm, user=Depends(get_admin_user)):
    app.state.config.ENABLE_OPENAI_API = form_data.enable_openai_api
    return {"ENABLE_OPENAI_API": app.state.config.ENABLE_OPENAI_API}


class UrlsUpdateForm(BaseModel):
    urls: List[str]


class KeysUpdateForm(BaseModel):
    keys: List[str]


@app.get("/urls")
async def get_openai_urls(user=Depends(get_admin_user)):
    return {"OPENAI_API_BASE_URLS": app.state.config.OPENAI_API_BASE_URLS}


@app.post("/urls/update")
async def update_openai_urls(form_data: UrlsUpdateForm, user=Depends(get_admin_user)):
    await get_all_models()
    app.state.config.OPENAI_API_BASE_URLS = form_data.urls
    return {"OPENAI_API_BASE_URLS": app.state.config.OPENAI_API_BASE_URLS}


@app.get("/keys")
async def get_openai_keys(user=Depends(get_admin_user)):
    return {"OPENAI_API_KEYS": app.state.config.OPENAI_API_KEYS}


@app.post("/keys/update")
async def update_openai_key(form_data: KeysUpdateForm, user=Depends(get_admin_user)):
    app.state.config.OPENAI_API_KEYS = form_data.keys
    return {"OPENAI_API_KEYS": app.state.config.OPENAI_API_KEYS}


@app.post("/audio/speech")
async def speech(request: Request, user=Depends(get_verified_user)):
    idx = None
    try:
        idx = app.state.config.OPENAI_API_BASE_URLS.index("https://api.openai.com/v1")
        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = Path(CACHE_DIR).joinpath("./audio/speech/")
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f"{name}.mp3")
        file_body_path = SPEECH_CACHE_DIR.joinpath(f"{name}.json")

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        headers = {}
        headers["Authorization"] = f"Bearer {app.state.config.OPENAI_API_KEYS[idx]}"
        headers["Content-Type"] = "application/json"
        if "openrouter.ai" in app.state.config.OPENAI_API_BASE_URLS[idx]:
            headers["HTTP-Referer"] = "https://openwebui.com/"
            headers["X-Title"] = "Open WebUI"
        r = None
        try:
            r = requests.post(
                url=f"{app.state.config.OPENAI_API_BASE_URLS[idx]}/audio/speech",
                data=body,
                headers=headers,
                stream=True,
            )

            r.raise_for_status()

            # Save the streaming content to a file
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

            with open(file_body_path, "w") as f:
                json.dump(json.loads(body.decode("utf-8")), f)

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)
            error_detail = "Open WebUI: Server Connection Error"
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        error_detail = f"External: {res['error']}"
                except:
                    error_detail = f"External: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500, detail=error_detail
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.OPENAI_NOT_FOUND)


async def fetch_url(url, key):
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        headers = {"Authorization": f"Bearer {key}"}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=headers) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f"Connection error: {e}")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


def merge_models_lists(model_lists):
    log.debug(f"merge_models_lists {model_lists}")
    merged_list = []

    for idx, models in enumerate(model_lists):
        if models is not None and "error" not in models:
            merged_list.extend(
                [
                    {
                        **model,
                        "name": model.get("name", model["id"]),
                        "owned_by": "openai",
                        "openai": model,
                        "urlIdx": idx,
                    }
                    for model in models
                    if "api.openai.com"
                    not in app.state.config.OPENAI_API_BASE_URLS[idx]
                    or "gpt" in model["id"]
                ]
            )

    return merged_list


async def get_all_models(raw: bool = False):
    log.info("get_all_models()")

    if (
        len(app.state.config.OPENAI_API_KEYS) == 1
        and app.state.config.OPENAI_API_KEYS[0] == ""
    ) or not app.state.config.ENABLE_OPENAI_API:
        models = {"data": []}
    else:
        # Check if API KEYS length is same than API URLS length
        if len(app.state.config.OPENAI_API_KEYS) != len(
            app.state.config.OPENAI_API_BASE_URLS
        ):
            # if there are more keys than urls, remove the extra keys
            if len(app.state.config.OPENAI_API_KEYS) > len(
                app.state.config.OPENAI_API_BASE_URLS
            ):
                app.state.config.OPENAI_API_KEYS = app.state.config.OPENAI_API_KEYS[
                    : len(app.state.config.OPENAI_API_BASE_URLS)
                ]
            # if there are more urls than keys, add empty keys
            else:
                app.state.config.OPENAI_API_KEYS += [
                    ""
                    for _ in range(
                        len(app.state.config.OPENAI_API_BASE_URLS)
                        - len(app.state.config.OPENAI_API_KEYS)
                    )
                ]

        tasks = [
            fetch_url(f"{url}/models", app.state.config.OPENAI_API_KEYS[idx])
            for idx, url in enumerate(app.state.config.OPENAI_API_BASE_URLS)
        ]

        responses = await asyncio.gather(*tasks)
        log.debug(f"get_all_models:responses() {responses}")

        if raw:
            return responses

        models = {
            "data": merge_models_lists(
                list(
                    map(
                        lambda response: (
                            response["data"]
                            if (response and "data" in response)
                            else (response if isinstance(response, list) else None)
                        ),
                        responses,
                    )
                )
            )
        }

        import time

        # Add Moonshot models
        current_time = int(time.time())
        moonshot_models = [
            {
                "id": "moonshot-v1-8k",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "moonshot-v1-8k",
                "openai": {
                    "id": "moonshot-v1-8k",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
                "urlIdx": 0,
            },
            {
                "id": "moonshot-v1-32k",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "moonshot-v1-32k",
                "openai": {
                    "id": "moonshot-v1-32k",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "moonshot-v1-64k",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "moonshot-v1-64k",
                "openai": {
                    "id": "moonshot-v1-64k",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
        ]

        models["data"].extend(moonshot_models)

        # Add Qianfan models
        current_time = int(time.time())
        qianfan_models = [
            {
                "id": "ERNIE-4.0-8K",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K",
                "openai": {
                    "id": "ERNIE-4.0-8K",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
                "urlIdx": 0,
            },
            {
                "id": "ERNIE-4.0-8K-Preview",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K-Preview",
                "openai": {
                    "id": "ERNIE-4.0-8K-Preview",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-8K-Preview-0518",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K-Preview-0518",
                "openai": {
                    "id": "ERNIE-4.0-8K-Preview-0518",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-8K-Latest",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K-Latest",
                "openai": {
                    "id": "ERNIE-4.0-8K-Latest",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-8K-0329",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K-0329",
                "openai": {
                    "id": "ERNIE-4.0-8K-0329",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-8K-0613",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-8K-0613",
                "openai": {
                    "id": "ERNIE-4.0-8K-0613",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-Turbo-8K",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-Turbo-8K",
                "openai": {
                    "id": "ERNIE-4.0-Turbo-8K",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-4.0-Turbo-8K-Preview",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-4.0-Turbo-8K-Preview",
                "openai": {
                    "id": "ERNIE-4.0-Turbo-8K-Preview",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
            {
                "id": "ERNIE-3.5-8K",
                "object": "model",
                "created": current_time,
                "owned_by": "openai",
                "name": "ERNIE-3.5-8K",
                "openai": {
                    "id": "ERNIE-3.5-8K",
                    "object": "model",
                    "created": current_time,
                    "owned_by": "system",
                },
            },
        ]

        models["data"].extend(qianfan_models)

        # Add leagent models
        # current_time = int(time.time())
        # leagent_models = [
        #     {
        #         "id": "leagent-lesson-planning",
        #         "object": "model",
        #         "created": current_time,
        #         "owned_by": "openai",
        #         "name": "leagent-lesson-planning",
        #         "openai": {
        #             "id": "leagent-lesson-planning",
        #             "object": "model",
        #             "created": current_time,
        #             "owned_by": "system",
        #         },
        #         "urlIdx": 0,
        #     },
        #     {
        #         "id": "leagent-qa",
        #         "object": "model",
        #         "created": current_time,
        #         "owned_by": "openai",
        #         "name": "leagent-qa",
        #         "openai": {
        #             "id": "leagent-qa",
        #             "object": "model",
        #             "created": current_time,
        #             "owned_by": "system",
        #         },
        #         "urlIdx": 0,
        #     },
        #     {
        #         "id": "leagent-evaluation",
        #         "object": "model",
        #         "created": current_time,
        #         "owned_by": "openai",
        #         "name": "leagent-evaluation",
        #         "openai": {
        #             "id": "leagent-evaluation",
        #             "object": "model",
        #             "created": current_time,
        #             "owned_by": "system",
        #         },
        #         "urlIdx": 0,
        #     },
        # ]

        # models["data"].extend(leagent_models)

        log.debug(f"models: {models}")
        app.state.MODELS = {model["id"]: model for model in models["data"]}

    return models


@app.get("/models")
@app.get("/models/{url_idx}")
async def get_models(url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if url_idx == None:
        models = await get_all_models()
        if app.state.config.ENABLE_MODEL_FILTER:
            if user.role == "user":
                models["data"] = list(
                    filter(
                        lambda model: model["id"] in app.state.config.MODEL_FILTER_LIST,
                        models["data"],
                    )
                )
                return models
        return models
    else:
        url = app.state.config.OPENAI_API_BASE_URLS[url_idx]
        key = app.state.config.OPENAI_API_KEYS[url_idx]

        headers = {}
        headers["Authorization"] = f"Bearer {key}"
        headers["Content-Type"] = "application/json"

        r = None

        try:
            r = requests.request(method="GET", url=f"{url}/models", headers=headers)
            r.raise_for_status()

            response_data = r.json()
            if "api.openai.com" in url:
                response_data["data"] = list(
                    filter(lambda model: "gpt" in model["id"], response_data["data"])
                )

            return response_data
        except Exception as e:
            log.exception(e)
            error_detail = "Open WebUI: Server Connection Error"
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        error_detail = f"External: {res['error']}"
                except:
                    error_detail = f"External: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500,
                detail=error_detail,
            )


@app.post("/chat/completions")
@app.post("/chat/completions/{url_idx}")
async def generate_chat_completion(
    form_data: dict,
    url_idx: Optional[int] = None,
    user=Depends(get_verified_user),
):
    idx = 0
    payload = {**form_data}

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)

    if model_id.lower().startswith("leagent"):
        return await handle_leagent_request(payload, user)
    if model_id.lower().startswith("moonshot"):
        return await handle_moonshot_request(payload, user)
    if model_id.lower().startswith("ernie"):
        return await handle_qianfan_request(payload, user)

    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id

        model_info.params = model_info.params.model_dump()

        if model_info.params:
            if model_info.params.get("temperature", None) is not None:
                payload["temperature"] = float(model_info.params.get("temperature"))

            if model_info.params.get("top_p", None):
                payload["top_p"] = int(model_info.params.get("top_p", None))

            if model_info.params.get("max_tokens", None):
                payload["max_tokens"] = int(model_info.params.get("max_tokens", None))

            if model_info.params.get("frequency_penalty", None):
                payload["frequency_penalty"] = int(
                    model_info.params.get("frequency_penalty", None)
                )

            if model_info.params.get("seed", None):
                payload["seed"] = model_info.params.get("seed", None)

            if model_info.params.get("stop", None):
                payload["stop"] = (
                    [
                        bytes(stop, "utf-8").decode("unicode_escape")
                        for stop in model_info.params["stop"]
                    ]
                    if model_info.params.get("stop", None)
                    else None
                )

        system = model_info.params.get("system", None)
        if system:
            system = prompt_template(
                system,
                **(
                    {
                        "user_name": user.name,
                        "user_location": (
                            user.info.get("location") if user.info else None
                        ),
                    }
                    if user
                    else {}
                ),
            )
            # Check if the payload already has a system message
            # If not, add a system message to the payload
            if payload.get("messages"):
                for message in payload["messages"]:
                    if message.get("role") == "system":
                        message["content"] = system + message["content"]
                        break
                else:
                    payload["messages"].insert(
                        0,
                        {
                            "role": "system",
                            "content": system,
                        },
                    )

    else:
        pass

    model = app.state.MODELS[payload.get("model")]
    idx = model["urlIdx"]

    if "pipeline" in model and model.get("pipeline"):
        payload["user"] = {
            "name": user.name,
            "id": user.id,
            "email": user.email,
            "role": user.role,
        }

    # Check if the model is "gpt-4-vision-preview" and set "max_tokens" to 4000
    # This is a workaround until OpenAI fixes the issue with this model
    if payload.get("model") == "gpt-4-vision-preview":
        if "max_tokens" not in payload:
            payload["max_tokens"] = 4000
        log.debug("Modified payload:", payload)

    # Convert the modified body back to JSON
    payload = json.dumps(payload)

    log.debug(payload)

    url = app.state.config.OPENAI_API_BASE_URLS[idx]
    key = app.state.config.OPENAI_API_KEYS[idx]

    headers = {}
    headers["Authorization"] = f"Bearer {key}"
    headers["Content-Type"] = "application/json"

    r = None
    session = None
    streaming = False

    try:
        session = aiohttp.ClientSession(trust_env=True)
        r = await session.request(
            method="POST",
            url=f"{url}/chat/completions",
            data=payload,
            headers=headers,
        )

        r.raise_for_status()

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            response_data = await r.json()
            return response_data
    except Exception as e:
        log.exception(e)
        error_detail = "Open WebUI: Server Connection Error"
        if r is not None:
            try:
                res = await r.json()
                print(res)
                if "error" in res:
                    error_detail = f"External: {res['error']['message'] if 'message' in res['error'] else res['error']}"
            except:
                error_detail = f"External: {e}"
        raise HTTPException(status_code=r.status if r else 500, detail=error_detail)
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    idx = 0

    body = await request.body()

    url = app.state.config.OPENAI_API_BASE_URLS[idx]
    key = app.state.config.OPENAI_API_KEYS[idx]

    target_url = f"{url}/{path}"

    headers = {}
    headers["Authorization"] = f"Bearer {key}"
    headers["Content-Type"] = "application/json"

    r = None
    session = None
    streaming = False

    try:
        session = aiohttp.ClientSession(trust_env=True)
        r = await session.request(
            method=request.method,
            url=target_url,
            data=body,
            headers=headers,
        )

        r.raise_for_status()

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            response_data = await r.json()
            return response_data
    except Exception as e:
        log.exception(e)
        error_detail = "Open WebUI: Server Connection Error"
        if r is not None:
            try:
                res = await r.json()
                print(res)
                if "error" in res:
                    error_detail = f"External: {res['error']['message'] if 'message' in res['error'] else res['error']}"
            except:
                error_detail = f"External: {e}"
        raise HTTPException(status_code=r.status if r else 500, detail=error_detail)
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()

async def handle_moonshot_request(payload, user):
    # Call Moonshot API
    import os

    from openai import OpenAI

    api_key = os.getenv("MOONSHOT_API_KEY", "")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.moonshot.cn/v1",
    )

    async def event_generator():
        stream = client.chat.completions.create(
            model=payload.get("model"),
            messages=payload.get("messages", []),
            temperature=0.3,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Type": "text/event-stream"},
    )


async def handle_qianfan_request(payload, user):
    # Call Moonshot API
    import os

    from openai import OpenAI

    api_key = os.getenv("QIANFAN_API_KEY", "")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.moonshot.cn/v1",
    )

    async def event_generator():
        stream = client.chat.completions.create(
            model=payload.get("model"),
            messages=payload.get("messages", []),
            temperature=0.3,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Type": "text/event-stream"},
    )


async def handle_leagent_request(payload, user):
    model_id = payload.get("model")
    user_message = payload["messages"][-1]["content"] if payload["messages"] else ""
    messages = payload.get("messages", [])
    print(f"messages:\n{messages}")

    async def event_generator():
        async for message in leagent_processing(model_id, user_message, messages, user):
            if message == "TASK_DONE":
                yield f"data: {json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'TASK_DONE'}}]})}\n\n"
                break
            yield f"data: {json.dumps({'choices': [{'delta': {'content': message}}]})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Type": "text/event-stream"},
    )


async def leagent_processing(model_id: str, content: str, messages, user):
    leagent_server_url = "http://localhost:8101/process"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            leagent_server_url,
            json={
                "model": model_id,
                "content": content,
                "messages": messages,
                "user": user.dict(),
            },
        ) as response:
            async for line in response.content:
                if line:
                    message = line.decode("utf-8").strip()
                    if message == "TASK_DONE":
                        yield message + "\n"
                        break
                    yield message + "\n"
