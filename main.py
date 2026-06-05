import json
import os
import time
import httpx
import uuid
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Claude GitLab Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_PAT = os.environ.get("GITLAB_PAT")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "my-proxy-key-123")

TOKEN_FILE = ".gitlab_token.json"
OAUTH_STATES = set()

def save_token(token_data: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)

def load_token() -> Optional[dict]:
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None

async def get_api_key(request: Request) -> str:
    api_key = request.headers.get("x-api-key")
    auth_header = request.headers.get("authorization", "")
    print(f"[DEBUG] Received x-api-key: {api_key}")
    print(f"[DEBUG] Received authorization: {auth_header}")
    
    if not api_key:
        api_key = auth_header.replace("Bearer ", "")
    if api_key:
        api_key = api_key.strip()
        
    # If the user provides the PROXY_API_KEY or dummy-bypass-token, use the local GITLAB_PAT
    if api_key in [PROXY_API_KEY, "dummy-bypass-token"]:
        if GITLAB_PAT:
            print(f"[DEBUG] Authenticated via PROXY_API_KEY or dummy token. Using GITLAB_PAT from environment.")
            return GITLAB_PAT
        else:
            raise HTTPException(status_code=500, detail="GITLAB_PAT is not set in the proxy .env file.")
    
    # If the user provides a real PAT directly, use it
    if api_key and not api_key.startswith("sk-ant-") and "dummy" not in api_key.lower() and api_key.lower() != "oauth":
        print(f"[DEBUG] Using direct API Key: {api_key}")
        return api_key
        
    # Fallback to local token from OAuth
    token_data = load_token()
    if token_data and "access_token" in token_data:
        return token_data["access_token"]
        
    raise HTTPException(
        status_code=401, 
        detail="Authentication required. Please provide a valid PROXY_API_KEY or visit http://localhost:8001/login."
    )

@app.get("/login")
async def login():
    """Redirects the user to GitLab OAuth authorization page"""
    client_id = os.environ.get("GITLAB_CLIENT_ID")
    if not client_id:
        return HTMLResponse(
            "<h2>Configuration Error</h2>"
            "<p><code>GITLAB_CLIENT_ID</code> is not set in the environment.</p>"
            "<p>Please create an OAuth application in GitLab with the redirect URI <code>http://localhost:8001/callback</code> "
            "and scope <code>api</code>, then set the Client ID and Secret in your environment.</p>", 
            status_code=500
        )
    
    state = str(uuid.uuid4())
    OAUTH_STATES.add(state)
    
    redirect_uri = "http://localhost:8001/callback"
    auth_url = f"{GITLAB_URL}/oauth/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&state={state}&scope=api"
    
    return RedirectResponse(auth_url)

@app.get("/callback")
async def callback(code: str, state: str):
    """Handles the OAuth callback and fetches the access token"""
    if state not in OAUTH_STATES:
        return HTMLResponse("Error: Invalid state parameter. CSRF attempt or session expired.", status_code=400)
    
    OAUTH_STATES.remove(state)
    
    client_id = os.environ.get("GITLAB_CLIENT_ID")
    client_secret = os.environ.get("GITLAB_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return HTMLResponse("<h2>Configuration Error</h2><p>GITLAB_CLIENT_ID or GITLAB_CLIENT_SECRET missing.</p>", status_code=500)
        
    redirect_uri = "http://localhost:8001/callback"
    
    token_url = f"{GITLAB_URL}/oauth/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(token_url, data=payload)
            res.raise_for_status()
            token_data = res.json()
            save_token(token_data)
            
            return HTMLResponse("""
            <html>
                <body style="font-family: sans-serif; text-align: center; margin-top: 50px;">
                    <h1 style="color: #2e7d32;">Authentication Successful! ✅</h1>
                    <p>The proxy has securely saved your access token.</p>
                    <p style="color: #666;">You can now close this window and use Claude Code.</p>
                </body>
            </html>
            """)
        except Exception as e:
            error_msg = f"Error fetching token: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f"<br>Response: {e.response.text}"
            return HTMLResponse(f"<h2>Authentication Failed</h2><p>{error_msg}</p>", status_code=500)

@app.get("/v1/models")
async def list_models():
    """Mock models endpoint for Claude Code"""
    return {
        "data": [
            {
                "type": "model",
                "id": "claude-3-5-sonnet-20241022",
                "display_name": "Claude 3.5 Sonnet",
                "created_at": "2024-10-22T00:00:00Z"
            },
            {
                "type": "model",
                "id": "claude-3-haiku-20240307",
                "display_name": "Claude 3 Haiku",
                "created_at": "2024-03-07T00:00:00Z"
            },
            {
                "type": "model",
                "id": "claude-opus-4-8",
                "display_name": "Claude Opus 4.8",
                "created_at": "2024-06-05T00:00:00Z"
            }
        ],
        "has_more": False
    }

def build_gitlab_prompt(payload: dict) -> str:
    """Format Anthropic messages into a single prompt for GitLab Duo Chat"""
    system_prompt = payload.get("system", "")
    messages = payload.get("messages", [])
    
    formatted_prompt = ""
    if system_prompt:
        # If it's a list (Claude Code sometimes sends a list of blocks)
        if isinstance(system_prompt, list):
            system_text = "\n".join([b.get("text", "") for b in system_prompt if b.get("type") == "text"])
        else:
            system_text = str(system_prompt)
        formatted_prompt += f"System: {system_text}\n\n"
        
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        
        if isinstance(content, list):
            # Parse complex content blocks
            text_blocks = []
            for block in content:
                if block.get("type") == "text":
                    text_blocks.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    text_blocks.append(f"[Tool Call: {block.get('name')}]")
                elif block.get("type") == "tool_result":
                    text_blocks.append(f"[Tool Result: {block.get('content')}]")
            content_str = "\n".join(text_blocks)
        else:
            content_str = str(content)
            
        formatted_prompt += f"{role.capitalize()}: {content_str}\n\n"
        
    # Append instructions to act as an assistant
    formatted_prompt += "Assistant: "
    return formatted_prompt

async def stream_gitlab_response(prompt: str, api_key: str):
    """Simulate streaming an Anthropic response using the GitLab response"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "intent": "generation",
        "prompt_version": 2,
        "current_file": {
            "file_name": "conversation.txt",
            "content_above_cursor": prompt,
            "content_below_cursor": ""
        }
    }
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{GITLAB_URL}/api/v4/code_suggestions/completions",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                response_text = data["choices"][0].get("text", "No response text found.")
            else:
                response_text = "No response received from GitLab Duo"
        except Exception as e:
            error_msg = f"Error from GitLab API: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f"\nResponse: {e.response.text}"
            response_text = error_msg
            
    # Simulate SSE Stream
    msg_id = f"msg_{int(time.time())}"
    
    # Message Start
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': 'claude-3-5-sonnet-20241022', 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 1}}})}\n\n"
    
    # Content Block Start
    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
    
    # Chunk the text to simulate streaming
    chunk_size = 20
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i:i+chunk_size]
        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"
        # Small sleep to simulate generation
        time.sleep(0.01)
        
    # Content Block Stop
    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
    
    # Message Stop
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

@app.post("/v1/messages")
async def messages_proxy(request: Request, api_key: str = Depends(get_api_key)):
    """Proxy Anthropic messages to GitLab Duo"""
    payload = await request.json()
    is_stream = payload.get("stream", False)
    
    prompt = build_gitlab_prompt(payload)
    
    if is_stream:
        return StreamingResponse(
            stream_gitlab_response(prompt, api_key),
            media_type="text/event-stream"
        )
    else:
        # Non-streaming response
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        req_payload = {
            "intent": "generation",
            "prompt_version": 2,
            "current_file": {
                "file_name": "conversation.txt",
                "content_above_cursor": prompt,
                "content_below_cursor": ""
            }
        }
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                res = await client.post(
                    f"{GITLAB_URL}/api/v4/code_suggestions/completions",
                    json=req_payload,
                    headers=headers
                )
                res.raise_for_status()
                data = res.json()
                if "choices" in data and len(data["choices"]) > 0:
                    response_text = data["choices"][0].get("text", "No response text found.")
                else:
                    response_text = "No response received"
            except Exception as e:
                error_msg = f"Error from GitLab API: {str(e)}"
                if hasattr(e, 'response') and e.response is not None:
                    error_msg += f"\nResponse: {e.response.text}"
                response_text = error_msg
                
        return JSONResponse({
            "id": f"msg_{int(time.time())}",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": response_text
                }
            ],
            "model": payload.get("model", "claude-3-5-sonnet-20241022"),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 10
            }
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
