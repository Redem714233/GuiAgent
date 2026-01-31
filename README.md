# GUIAgent Local Backend

## What this is
A local FastAPI service that keeps OmniParser loaded in memory, runs Playwright for screenshots, and uses GPT-4o to pick a target element. It exposes `/parse`, `/plan`, and `/step`.

## Setup
Create/activate your Python env, then install deps:

```powershell
pip install -r requirements.txt
python -m playwright install
```

If you need extra DLL paths (CUDA/cuDNN), set:

```powershell
$env:OMNIPARSER_EXTRA_PATHS="C:\path\to\env\bin;C:\path\to\env\Library\bin"
```

Ensure your OpenAI key is set:

```powershell
$env:OPENAI_API_KEY="your_key_here"
```

Optional: run Playwright on Edge

```powershell
$env:PLAYWRIGHT_CHANNEL="msedge"
# or use explicit path
$env:PLAYWRIGHT_EXECUTABLE="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

## Run

```powershell
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
```

## API

- `POST /parse` with JSON body:
  - `image_path` or `image_base64` (PNG/JPG)
  - returns `elements[]` and `annotated_image_base64`
- `POST /plan` with `{ task, elements[] }`
- `POST /step` with `{ task }` or `{ task, override_point: [x,y] }`
