# TalosAI

Talos is an AI assistant you can use in Python, in your terminal, or in your browser.
Ask questions, search your files, understand code, and add your own tools and skills.

This is an early release. Windows is the tested development platform. Mac and Linux
still need a full first-time setup check.

## Get started

You need Python 3.12. Choose Python, terminal, or browser mode below.
For terminal and browser mode, download this repository and open a terminal in
its folder. Also install and start Docker Desktop with Linux containers and
Docker Compose. You do not need Node.js for normal use.

Create a Python environment:

```sh
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on Mac and Linux:

```sh
source .venv/bin/activate
```

Bring pip up to date:

```sh
python -m pip install --upgrade pip
```

For terminal or browser mode, install from the downloaded folder:

```sh
python -m pip install ".[cli]"
```

For the Python SDK, once the package is published on PyPI, install it directly:

```sh
python -m pip install talos-runtime
```

The SDK does not need this repository, Git, Docker, or a browser. Until the first
PyPI release is published, use `python -m pip install .` from this folder instead.
Keep your Python environment active when running Talos.

The SDK installs only its core dependencies. Add PDF reading with
`python -m pip install "talos-runtime[pdf]"`. Custom branching pipelines use
`"talos-runtime[graph]"`; sending traces to an external service uses
`"talos-runtime[telemetry]"`. Local run history and traces work with the core SDK.
Terminal and browser installation above includes their host-side tools; Docker
installs the server dependencies separately.

### 1. Terminal

Create your local settings once, from the repository folder:

```sh
cp talos.example.yaml talos.yaml
```

`cp` also works in PowerShell. Keep your own `talos.yaml` when updating Talos.

Start the assistant:

```sh
hi talos
```

On first use, Talos asks for an [OpenRouter API key](https://openrouter.ai/settings/keys).
Your typing is hidden and the key is saved in your computer's credential store.
The default model is `openai/gpt-4o-mini`; provider charges may apply.
The first start downloads and builds the local services, so allow a few minutes.

Type a question and press Enter. Use `/help` for commands, `/new` for a fresh chat,
`/why` to inspect the last answer, and `/exit` to leave. Ctrl+C cancels a response.

### 2. Browser

Use the same installation and local settings as terminal mode. From the repository folder, run:

```sh
talos ui
```

Talos asks for a key if needed and opens the browser when it is ready.
If the browser does not open, visit `http://127.0.0.1:3000`.

In Settings, use **Runtime** to add model keys and models, then refresh the model list.
Choose a model beside the microphone in chat. Settings also lets you add tools and
skills, view run traces, and change appearance. **Debug**, after Runtime, runs the
same setup checks as `talos doctor`. New model choices start a new chat.

Closing the browser or terminal keeps your chats and local services.
To stop the services without deleting your data, run `talos stop`.
Run only one local Talos installation at a time; it uses ports 3000 and 8000.

### 3. Python SDK

After `python -m pip install talos-runtime`, create a project in any folder you choose:

```sh
talos init my-agent
cd my-agent
```

Save this as `hello.py`:

```python
import asyncio
from getpass import getpass

from dotenv import dotenv_values
from talos import TalosAgent


async def main():
    settings = {
        key: value for key, value in dotenv_values(".env.example").items() if value is not None
    }
    settings["OPENROUTER_API_KEY"] = getpass("OpenRouter API key: ")
    agent = TalosAgent.from_config("talos.yaml", environment=settings)
    answer = await agent.run("Explain what an AI agent is in two sentences.")
    print(answer.response)


asyncio.run(main())
```

Run it:

```sh
python hello.py
```

For a free, offline setup check, import `FakeModelProvider` from `talos` and add
`provider=FakeModelProvider("Talos is ready.")` to `from_config(...)`. Skip the key prompt.
This returns a fixed sample answer; it does not call an AI model.

## Add tools and skills

In the browser, open Settings and use the tool or skill form.
In the terminal, import your own files:

```sh
talos tools add path/to/tool.yaml
talos skills add path/to/skill.md
talos tools --help
talos skills --help
```

YAML tools support limited text formatting and arithmetic. Skills are Markdown
instructions that guide the assistant. Review files before importing them.
For reviewed Python tools, build their separate sandbox first:

```sh
docker build -f Dockerfile.sandbox -t talos-tool-sandbox:1 .
talos tools add --help
```

Python tools require explicit trust and a matching file hash. They run in a
restricted Docker container. SDK callables registered directly by your app
run with your app's permissions.

## Ask about a repository

In the browser, select **+ → Repository Intel**, paste a GitHub repository URL,
and send your question. Talos remembers that repository for the chat. Ask for an
HLD to get a Mermaid diagram and download it as a `.mmd` file. Source chips show
which files were used; hover or focus a chip to see its full path.

For a private repository, open **Private repository?** and enter a fine-grained
GitHub token with **Contents: read-only** access to that repository. The token is
used once for the import and is not saved. Your chosen model receives source excerpts.

In the terminal, first index a local checkout or GitHub URL:

```sh
talos repo index https://github.com/owner/repository --yes
talos repo status JOB_ID
talos repo ask "Where does a request enter the app?" --path https://github.com/owner/repository
talos repo hld --path https://github.com/owner/repository
```

Wait for the indexing job to finish before asking. Add `--private` to the index
command to enter a GitHub token in a hidden prompt. In `hi talos`, use
`/repo index PATH_OR_URL`, then ask questions normally or run `/repo hld`.
Diagrams and cited explanations are saved under `.talos/artifacts/`. Open the
`.mmd` file in a Mermaid viewer such as [Mermaid Live](https://mermaid.live).
Check what you share before opening a private repository diagram in an online service.

## Your data

Talos runs locally. Your chosen AI provider receives the messages, selected file
excerpts, and tool results needed to answer. Check that provider's privacy terms.
Optional voice input may send audio to your browser's speech service.

Chats and run records stay in local storage. Stopping Talos preserves them;
there is no complete erase-all button. Backups and providers can retain copies.
CLI keys use the computer's credential store; browser-added keys use encrypted
local files. Protect the files and backups. Keep `.env`, `talos.yaml`, `.talos/`,
and imported files private. Use the browser on this computer only; this setup is
for one trusted user and is not ready to be exposed to the Internet.

## When something goes wrong

- Run `talos doctor` or Settings → Debug → Run Talos Doctor to check setup.
  Run `talos status` to check services.
- Start Docker Desktop if Talos cannot reach Docker.
- Run `talos logs --help` for service logs. Remove private information before sharing logs.
- For failed or slow answers, check Settings → Traces, your model key, provider availability,
  and usage limits. Retry in a new chat after fixing the cause.
- If PowerShell blocks environment activation, use `.\.venv\Scripts\python.exe`
  and `.\.venv\Scripts\talos.exe` directly instead of changing system policy.
- If a key leaks, revoke it at the provider and replace it in Talos.

## License

TalosAI is licensed under [Apache 2.0](LICENSE). See [NOTICE](NOTICE).
