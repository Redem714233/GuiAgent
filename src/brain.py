import os
import json
import torch
import gc
import traceback
from http import HTTPStatus


class Brain:
    # src/brain.py
    def __init__(self):
        self.brain_type = os.getenv("BRAIN_TYPE", "local").lower()
        print(f"[Brain] Initializing Brain in '{self.brain_type}' mode.")

        if self.brain_type == "dashscope":
            try:
                import dashscope
                self.api_key = os.getenv("DASHSCOPE_API_KEY")
                if not self.api_key:
                    raise ValueError("DASHSCOPE_API_KEY not found in environment variables.")
                dashscope.api_key = self.api_key
                self.model_name = os.getenv("DASHSCOPE_MODEL", "qwen-vl-max")
                print(f"[Brain] DashScope configured with model: {self.model_name}")
            except ImportError:
                print("[Brain] Error: 'dashscope' package not installed. Run 'pip install dashscope'.")
                self.brain_type = "error"
            except Exception as e:
                print(f"[Brain] DashScope initialization error: {e}")
                self.brain_type = "error"

        else:
            # Local Mode Initialization
            from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
            # ...existing code...
            try:
                model_path = os.path.abspath(os.getenv("BRAIN_MODEL_PATH", "models/Qwen2-VL-2B-Int4"))
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
                print(f"[Brain] Loading Qwen2-VL model from {model_path} on {self.device}...")

                # 检查必要依赖
                if "Int4" in model_path and self.device == "cuda":
                    try:
                        import auto_gptq
                        from optimum.gptq import GPTQQuantizer
                    except ImportError as e:
                        print(f"[Brain] 警告: 加载 Int4 模型需要 `auto-gptq` and `optimum` installed. Error: {e}")
                        print(f"[Brain] 建议: 您的显卡有8GB显存，足以运行非量化版本的 Qwen2-VL-2B-Instruct (约4GB显存)。")
                        print(f"[Brain] 如果 Int4 模型持续报错，请去 HuggingFace 下载 'Qwen/Qwen2-VL-2B-Instruct' 并替换路径。")

                if self.device == "cuda":
                    print(f"[Brain] Loading model with device_map='auto'...")
                    # 尝试加载
                    try:
                        self.model = Qwen2VLForConditionalGeneration.from_pretrained(
                            model_path,
                            device_map="auto",
                            torch_dtype=torch.float16,
                            trust_remote_code=True,
                            local_files_only=True
                        )
                    except Exception as load_err:
                        print(f"[Brain] Standard load failed: {load_err}")
                        # 如果是 Int4 加载失败，提示更明确
                        if "Int4" in model_path:
                             raise RuntimeError("GPTQ Int4 模型加载失败。如果是Windows环境，auto-gptq 兼容性较差。建议切换到非量化版本 Qwen2-VL-2B-Instruct。")
                        else:
                            raise load_err
                else:
                    self.model = Qwen2VLForConditionalGeneration.from_pretrained(
                        model_path,
                        device_map="cpu",
                        trust_remote_code=True,
                        local_files_only=True
                    )

                self.processor = AutoProcessor.from_pretrained(model_path, local_files_only=True, trust_remote_code=True)
                print("[Brain] Model loaded successfully!")

            except Exception as e:
                print(f"[Brain] Critical Error: {e}")
                traceback.print_exc()
                self.model = None
                self.processor = None

    def think(self, task, screenshot_path, ui_elements, history):
        if self.brain_type == "error":
             return {"action": "wait", "explanation": "Brain initialization failed", "params": {}}

        system_prompt = """You are an intelligent GUI automation agent.
You are given a screenshot where UI elements are marked with numeric IDs.
User Task: {task}
History: {history}

Analyze the screenshot and history, then select the ONE best next step.
Output ONLY a single valid JSON object with the following fields:
- "action": One of "click", "type", "scroll", "finish", "wait".
- "element_id": The numeric ID of the UI element to interact with (required for "click").
- "text": The text to type (required for "type").
- "amount": The scroll amount (required for "scroll", positive/negative).
- "explanation": A brief reasoning for this action.

Example: {{"action": "click", "element_id": 12, "explanation": "Clicking the 'News' button."}}
"""

        prompt_text = system_prompt.format(task=task, history=str(history)[-500:])

        if self.brain_type == "dashscope":
            return self._think_dashscope(prompt_text, screenshot_path, ui_elements)
        else:
            return self._think_local(prompt_text, screenshot_path, ui_elements)

    def _think_dashscope(self, prompt_text, screenshot_path, ui_elements):
        import dashscope

        # Ensure path is absolute and has file:// prefix
        abs_path = os.path.abspath(screenshot_path)
        image_url = f"file://{abs_path}"

        print(f"[Brain] Calling DashScope with image: {image_url}")

        messages = [
            {
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": prompt_text}
                ]
            }
        ]

        try:
            response = dashscope.MultiModalConversation.call(
                model=self.model_name,
                messages=messages
            )

            if response.status_code == HTTPStatus.OK:
                content = response.output.choices[0].message.content
                # print(f"[Brain] Raw Output: {content}")
                if type(content) == list: # Sometimes content is a list
                    text_content = ""
                    for item in content:
                        if isinstance(item, dict) and 'text' in item:
                            text_content += item['text']
                    content = text_content

                print(f"[Brain] Raw Output: {content}")
                return self._parse_output(content, ui_elements)
            else:
                print(f"[Brain] DashScope Error: {response.code} - {response.message}")
                return {"action": "wait", "explanation": f"API Error: {response.message}", "params": {}}

        except Exception as e:
            print(f"[Brain] Exception during DashScope call: {e}")
            traceback.print_exc()
            return {"action": "wait", "explanation": f"Exception: {e}", "params": {}}

    def _think_local(self, prompt_text, screenshot_path, ui_elements):
        if not self.model:
            return {"action": "wait", "explanation": "Model not loaded", "params": {}}

        from qwen_vl_utils import process_vision_info

        # 清理显存碎片，为推理腾空间
        if torch.cuda.is_available():
            gc.collect()
            torch.cuda.empty_cache()

        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": screenshot_path},
                {"type": "text", "text": prompt_text}
            ]
        }]

        # 准备推理数据
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        vision_outputs = process_vision_info(messages)
        image_inputs = vision_outputs[0]
        video_inputs = vision_outputs[1]
        inputs = self.processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt"
        ).to(self.model.device)

        # 执行推理
        with torch.no_grad():
            generated_ids = self.model.generate(**inputs, max_new_tokens=256)

        generated_ids_trimmed = [
            out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        output_text = self.processor.batch_decode(
            generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
        )[0]

        print(f"[Brain] Raw Output: {output_text}")
        return self._parse_output(output_text, ui_elements)

    def _parse_output(self, output_text, ui_elements):
        # 逻辑同你之前的，保持不变
        try:
            json_str = output_text
            if "```json" in output_text:
                json_str = output_text.split("```json")[1].split("```")[0].strip()
            plan = json.loads(json_str)
            if 'element_id' in plan:
                eid = plan['element_id']
                target = next((e for e in ui_elements if str(e['id']) == str(eid)), None)
                if target:
                    plan['coordinates'] = target['center']
            return plan
        except:
            return {"action": "wait", "explanation": "Parse Error", "raw": output_text}