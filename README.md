# GUI Agent Setup & Run Guide

## Status
You have successfully:
1. Installed requirements (assuming dependencies are fixed).
2. Downloaded models (`OmniParser` and `Qwen2-VL`).
3. Set up the project structure.

## Changes Applied
I have automatically applied the following fixes to your code:
1. **Added Chinese Support**: Enabled `['ch_sim', 'en']` in `src/tools/parser.py` for OCR on Weibo.
2. **Enhanced Actor**: Added keyboard shortcut support (`Ctrl+C`, `Ctrl+V`, `Enter`) to `src/actor.py` so the agent can copy/paste.
3. **Updated Brain**: Updated the VLM system prompt in `src/brain.py` to understand "Key Combination" actions.
4. **Logic Check**: Verified that the `Brain` correctly maps ID `1` to screen coordinates `[x,y]`.

## Next Steps

### 1. Verify Configuration
Ensure your `models` folder structure looks exactly like this:
```
models/
  OmniParser/
    weights/
      icon_detect/model.pt
      icon_caption_florence/model.safetensors
  Qwen2-VL-2B-Int4/
    model.safetensors
    ...
```

### 2. Run the Agent
Make sure your Python environment is active:
```powershell
conda activate agent_env
```

Run the main entry point:
```powershell
python main.py
```

### 3. Expected Workflow
The agent will:
1. Capture your screen (Weibo page).
2. Analyze it using OmniParser (detecting news titles).
3. Qwen2-VL will decide to click a title or select text.
4. It should issue a `key_combo` action to Copy.
5. It should then navigate to Excel and Paste.

*Note: Since this is a probabilistic agent, you might need to refine the prompt in `src/brain.py` if it gets stuck. For example, explicitly telling it to "Open Excel" if it's not open.*

### 4. Troubleshooting
- **OOM (Out of Memory)**: If 8GB VRAM is insufficient, try closing other apps. Qwen2-VL-2B-Int4 is optimized but OmniParser also needs VRAM.
- **OCR Issues**: If it doesn't read Chinese well, ensure `easyocr` model files are downloaded (it will try to download them on first run).

