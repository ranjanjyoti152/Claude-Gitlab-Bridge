import json
import os
import re
import time
import httpx
import uuid
import asyncio
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

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# OAuth endpoints
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# 1. STRUCTURED PROMPT BUILDER  (fixes prompt flattening)
# ---------------------------------------------------------------------------

TOOL_INJECTION_PREAMBLE = """
You are a coding assistant with access to tools. When you need to use a tool, you MUST output a JSON block wrapped in <tool_call> tags.

FORMAT FOR TOOL CALLS:
<tool_call>
{"name": "TOOL_NAME", "id": "UNIQUE_ID", "input": {PARAMETERS}}
</tool_call>

You may output text before or after a tool call. You may make multiple tool calls in one response.
If you want to call a tool, you MUST use the exact <tool_call> XML tag format above. Do NOT output raw JSON without the tags.
When you are done and do not need any more tools, just respond with normal text without any <tool_call> tags.

AVAILABLE TOOLS:
"""

def build_gitlab_prompt(payload: dict) -> str:
    """
    Build a structured prompt that preserves multi-turn conversation history,
    system prompts, tool definitions, and tool results.
    
    Applies smart truncation to fit within GitLab's payload limits.
    """
    MAX_PROMPT_LENGTH = 100000  # GitLab code_suggestions has a size limit
    
    # ---- Tool definitions (inject into prompt) ----
    tools = payload.get("tools", [])
    tools_section = ""
    if tools:
        tool_parts = [TOOL_INJECTION_PREAMBLE]
        for tool in tools:
            tool_name = tool.get("name", "unknown")
            tool_desc = tool.get("description", "No description")
            # Compact schema (no indent) to save space
            tool_schema = json.dumps(tool.get("input_schema", {}))
            tool_parts.append(f"- {tool_name}: {tool_desc}\n  Schema: {tool_schema}")
        tools_section = "\n".join(tool_parts)
    
    # ---- System prompt (truncate if needed) ----
    system_section = ""
    system_prompt = payload.get("system", "")
    if system_prompt:
        if isinstance(system_prompt, list):
            system_text = "\n".join([b.get("text", "") for b in system_prompt if b.get("type") == "text"])
        else:
            system_text = str(system_prompt)
        # Truncate system prompt to leave room for messages
        max_system = 8000
        if len(system_text) > max_system:
            system_text = system_text[:max_system] + "\n... [system prompt truncated for brevity]"
        system_section = f"System: {system_text}"
    
    # ---- Messages (preserving multi-turn structure) ----
    messages = payload.get("messages", [])
    message_parts = []
    for msg in messages:
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        
        if isinstance(content, list):
            block_texts = []
            for block in content:
                btype = block.get("type", "text")
                
                if btype == "text":
                    block_texts.append(block.get("text", ""))
                    
                elif btype == "tool_use":
                    tool_call_json = json.dumps({
                        "name": block.get("name", ""),
                        "id": block.get("id", ""),
                        "input": block.get("input", {})
                    })
                    block_texts.append(f"<tool_call>\n{tool_call_json}\n</tool_call>")
                    
                elif btype == "tool_result":
                    tool_id = block.get("tool_use_id", "unknown")
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_parts = []
                        for rc in result_content:
                            if isinstance(rc, dict):
                                result_parts.append(rc.get("text", str(rc)))
                            else:
                                result_parts.append(str(rc))
                        result_content = "\n".join(result_parts)
                    # Truncate very long tool results
                    if isinstance(result_content, str) and len(result_content) > 5000:
                        result_content = result_content[:5000] + "\n... [truncated]"
                    is_error = block.get("is_error", False)
                    prefix = "ERROR" if is_error else "RESULT"
                    block_texts.append(f"<tool_result tool_use_id=\"{tool_id}\">\n[{prefix}]: {result_content}\n</tool_result>")
                    
                elif btype == "image":
                    block_texts.append("[Image content provided]")
                else:
                    block_texts.append(str(block))
                    
            content_str = "\n".join(block_texts)
        else:
            content_str = str(content)
            
        message_parts.append(f"{role}: {content_str}")
    
    # ---- Assemble final prompt ----
    # Priority: recent messages > tools > system prompt
    final_parts = []
    if system_section:
        final_parts.append(system_section)
    if tools_section:
        final_parts.append(tools_section)
    final_parts.extend(message_parts)
    final_parts.append("Assistant:")
    
    prompt = "\n\n".join(final_parts)
    
    # If still too long, aggressively trim older messages (keep last 6)
    if len(prompt) > MAX_PROMPT_LENGTH:
        print(f"[WARN] Prompt too long ({len(prompt)} chars), trimming older messages...")
        # Keep system + tools + last 6 messages + "Assistant:"
        trimmed_parts = []
        if system_section:
            trimmed_parts.append(system_section)
        if tools_section:
            trimmed_parts.append(tools_section)
        
        recent_messages = message_parts[-6:] if len(message_parts) > 6 else message_parts
        trimmed_parts.append("[Earlier conversation history omitted for brevity]")
        trimmed_parts.extend(recent_messages)
        trimmed_parts.append("Assistant:")
        prompt = "\n\n".join(trimmed_parts)
    
    # Final hard truncation if still over the limit
    if len(prompt) > MAX_PROMPT_LENGTH:
        prompt = prompt[:MAX_PROMPT_LENGTH]
    
    return prompt

# ---------------------------------------------------------------------------
# 2. RESPONSE PARSER  (detects tool_call tags in GitLab's text response)
# ---------------------------------------------------------------------------

TOOL_CALL_PATTERN = re.compile(
    r'<tool_call>\s*(\{.*?\})\s*</tool_call>',
    re.DOTALL
)

def parse_response_for_tool_calls(response_text: str) -> dict:
    """
    Parse GitLab's raw text response to detect <tool_call> blocks.
    Returns a dict with:
      - content_blocks: list of Anthropic content blocks (text + tool_use)
      - stop_reason: "tool_use" if tools were called, "end_turn" otherwise
    """
    content_blocks = []
    last_end = 0
    block_index = 0
    has_tool_calls = False
    
    for match in TOOL_CALL_PATTERN.finditer(response_text):
        # Add any text before this tool call
        preceding_text = response_text[last_end:match.start()].strip()
        if preceding_text:
            content_blocks.append({
                "type": "text",
                "text": preceding_text
            })
            block_index += 1
        
        # Parse the tool call JSON
        try:
            tool_json = json.loads(match.group(1))
            tool_id = tool_json.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
            content_blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": tool_json.get("name", "unknown"),
                "input": tool_json.get("input", {})
            })
            has_tool_calls = True
            block_index += 1
        except json.JSONDecodeError:
            # If parsing fails, treat the whole match as text
            content_blocks.append({
                "type": "text",
                "text": match.group(0)
            })
            block_index += 1
        
        last_end = match.end()
    
    # Add any remaining text after the last tool call
    remaining_text = response_text[last_end:].strip()
    if remaining_text:
        content_blocks.append({
            "type": "text",
            "text": remaining_text
        })
    
    # If no blocks were created, use the full response as text
    if not content_blocks:
        content_blocks.append({
            "type": "text",
            "text": response_text
        })
    
    return {
        "content_blocks": content_blocks,
        "stop_reason": "tool_use" if has_tool_calls else "end_turn"
    }

# ---------------------------------------------------------------------------
# 3. GitLab API call helper
# ---------------------------------------------------------------------------

async def call_gitlab_api(prompt: str, api_key: str) -> str:
    """Make the actual HTTP call to GitLab Code Suggestions API."""
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
                return data["choices"][0].get("text", "No response text found.")
            else:
                return "No response received from GitLab Duo"
        except Exception as e:
            error_msg = f"Error from GitLab API: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f"\nResponse: {e.response.text}"
            return error_msg

# ---------------------------------------------------------------------------
# 4. SSE STREAMING  (proper Anthropic SSE with tool_use support)
# ---------------------------------------------------------------------------

async def stream_gitlab_response(prompt: str, api_key: str, model: str):
    """
    Call GitLab, parse the response for tool calls, and emit a proper
    Anthropic SSE stream including text and tool_use content blocks.
    """
    response_text = await call_gitlab_api(prompt, api_key)
    parsed = parse_response_for_tool_calls(response_text)
    content_blocks = parsed["content_blocks"]
    stop_reason = parsed["stop_reason"]
    
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    
    # 1. message_start
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"
    
    # 2. Emit each content block
    for index, block in enumerate(content_blocks):
        if block["type"] == "text":
            # content_block_start
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
            
            # Stream text in chunks
            text = block["text"]
            chunk_size = 20
            for i in range(0, len(text), chunk_size):
                chunk = text[i:i + chunk_size]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"
                await asyncio.sleep(0.005)
            
            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"
            
        elif block["type"] == "tool_use":
            # content_block_start with the full tool_use block
            tool_block = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {}
            }
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': tool_block})}\n\n"
            
            # Stream the input JSON as input_json_delta
            input_json_str = json.dumps(block["input"])
            # Send in chunks to simulate streaming
            chunk_size = 40
            for i in range(0, len(input_json_str), chunk_size):
                chunk = input_json_str[i:i + chunk_size]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': chunk}})}\n\n"
                await asyncio.sleep(0.005)
            
            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"
    
    # 3. message_delta with stop_reason
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': len(response_text.split())}})}\n\n"
    
    # 4. message_stop
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

# ---------------------------------------------------------------------------
# 5. MAIN MESSAGES ENDPOINT
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def messages_proxy(request: Request, api_key: str = Depends(get_api_key)):
    """Proxy Anthropic messages to GitLab Duo with full tool-use support"""
    payload = await request.json()
    is_stream = payload.get("stream", False)
    model = payload.get("model", "claude-3-5-sonnet-20241022")
    
    prompt = build_gitlab_prompt(payload)
    print(f"[DEBUG] Built prompt ({len(prompt)} chars), stream={is_stream}, tools={len(payload.get('tools', []))}")
    
    if is_stream:
        return StreamingResponse(
            stream_gitlab_response(prompt, api_key, model),
            media_type="text/event-stream"
        )
    else:
        # Non-streaming response
        response_text = await call_gitlab_api(prompt, api_key)
        parsed = parse_response_for_tool_calls(response_text)
                
        return JSONResponse({
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "content": parsed["content_blocks"],
            "model": model,
            "stop_reason": parsed["stop_reason"],
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": len(response_text.split())
            }
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
