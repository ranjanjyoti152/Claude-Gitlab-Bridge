# Claude GitLab Bridge

A Python-based FastAPI proxy that bridges Anthropic SDK clients like `claude-code` with GitLab Duo's AI infrastructure. Use Anthropic-compatible AI models seamlessly via your GitLab account.

## Features

- **Structured Prompt Preservation** — Multi-turn conversation history, system prompts, and tool results are preserved in a structured format instead of being flattened into a single string.
- **Proper Anthropic SSE Streaming** — Responses are streamed back using the full Anthropic Server-Sent Events protocol (`message_start` → `content_block_delta` → `message_delta` → `message_stop`).
- **Tool Use / Function Calling** — The bridge injects tool definitions into the prompt and parses `<tool_call>` tags from the model's response, reconstructing proper Anthropic `tool_use` content blocks for Claude Code.
- **Smart Prompt Truncation** — Automatically manages prompt size limits by truncating system prompts, compressing tool schemas, and trimming older messages when the payload exceeds GitLab's API limits.
- **Secure Token Management** — Your real GitLab PAT is stored locally in a `.env` file and never exposed to the client. A custom proxy API key authenticates Claude Code with the bridge.

## How It Works

```
┌──────────────┐       ┌─────────────────────┐       ┌──────────────┐
│  Claude Code │──────▶│  Claude GitLab Bridge│──────▶│  GitLab Duo  │
│  (Anthropic  │◀──────│  (localhost:8001)    │◀──────│  Code API    │
│   SDK)       │  SSE  │                     │  JSON │              │
└──────────────┘       └─────────────────────┘       └──────────────┘
```

1. Claude Code sends standard Anthropic API requests to the local bridge.
2. The bridge translates the structured messages, system prompt, and tool definitions into a single prompt for GitLab's Code Suggestions API.
3. GitLab Duo generates a response.
4. The bridge parses the response for tool calls (`<tool_call>` tags) and reconstructs proper Anthropic SSE events.
5. Claude Code receives a fully compatible response and continues working normally.

## Prerequisites

- Python 3.8+
- A GitLab Personal Access Token (PAT) with the `api` scope
- Access to GitLab Duo features (GitLab Ultimate/Premium) on your GitLab account

## Installation & Setup

1. **Clone the repository and set up a virtual environment:**
   ```bash
   git clone https://github.com/ranjanjyoti152/Claude-Gitlab-Bridge.git
   cd Claude-Gitlab-Bridge
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Create your environment configuration file:**
   ```bash
   cp .env.example .env
   ```

3. **Configure your tokens in the `.env` file:**
   ```env
   # Your real GitLab Personal Access Token (PAT)
   GITLAB_PAT="glpat-YOUR_GITLAB_PAT"

   # A custom secret key to authenticate Claude Code with this bridge
   PROXY_API_KEY="my-super-secret-proxy-key"
   ```

4. **Start the bridge server:**
   ```bash
   python main.py
   ```
   The server will run on `http://0.0.0.0:8001`.

## Running Claude Code

Open a **new** terminal window and set the environment variables to point Claude Code to the local bridge:

```bash
export ANTHROPIC_BASE_URL="http://localhost:8001"
export ANTHROPIC_API_KEY="my-super-secret-proxy-key"   # Must match PROXY_API_KEY in .env

# Start Claude Code
claude
```

You can also specify a model:
```bash
claude --model claude-opus-4-8
```

> **Note:** The model string is passed through for compatibility, but the actual AI model used is determined by GitLab Duo's internal routing.

## Tool Use Support

The bridge implements tool calling through a **prompt injection** strategy:

1. **Outgoing:** When Claude Code sends tool definitions, the bridge converts them into structured text instructions injected into the prompt. The model is instructed to wrap tool calls in `<tool_call>` XML tags.
2. **Incoming:** When the model responds with `<tool_call>` tags, the bridge parses them and reconstructs proper Anthropic `tool_use` content blocks with the correct `id`, `name`, and `input` fields.
3. **Results:** When Claude Code sends back `tool_result` blocks, these are preserved in `<tool_result>` tags in the conversation history.

This allows Claude Code's agentic workflows (file reading, command execution, etc.) to work through the bridge.

## Advanced Authentication (OAuth 2.0 Browser Login)

If you prefer not to use a static PAT:

1. Go to your GitLab **User Settings → Applications**.
2. Create an application with the `api` scope and **Redirect URI**: `http://localhost:8001/callback`.
3. Add `GITLAB_CLIENT_ID` and `GITLAB_CLIENT_SECRET` to your `.env` file.
4. Visit `http://localhost:8001/login` in your browser to authenticate.
5. Start Claude Code using your `PROXY_API_KEY` as usual.

## Known Limitations

- **Simulated Streaming:** GitLab's Code Suggestions API returns complete responses. The bridge simulates SSE streaming by chunking the response text into delta events. This means you won't see true token-by-token generation.
- **Tool Reliability:** Tool calling relies on the model correctly outputting `<tool_call>` XML tags. The underlying GitLab model may not always follow this format perfectly, which can cause tool calls to fail.
- **Prompt Size Limits:** Very long conversations with many tool results may be truncated. The bridge keeps the most recent 6 message turns when the prompt exceeds 100K characters.
- **No Native Multi-Modal:** Image inputs are noted but not forwarded to GitLab's API.

## License

MIT
